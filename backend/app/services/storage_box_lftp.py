"""lftp-based transfer service for segmented parallel file transfers."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath

from ..config import settings
from ..utils.subprocess_runner import CommandResult, CommandTimeoutError, run_command

logger = logging.getLogger("uvicorn.error")


class StorageBoxLftpService:
    """Segmented parallel file transfers using lftp over SFTP."""

    _preflight_lock = asyncio.Lock()
    _preflight_cache: dict[tuple[object, ...], tuple[bool, str]] = {}

    @classmethod
    def _lftp_binary(cls) -> str | None:
        return shutil.which("lftp")

    @classmethod
    def _ssh_key_path(cls) -> Path | None:
        if not settings.storage_box_ssh_key_path:
            return None
        return settings.storage_box_ssh_key_path.expanduser()

    @classmethod
    def _segments(cls) -> int:
        return max(1, min(16, settings.storage_box_lftp_segments))

    @classmethod
    def _min_size_bytes(cls) -> int:
        return settings.storage_box_lftp_min_file_size_mb * 1024 * 1024

    @classmethod
    def _preflight_key(cls) -> tuple[object, ...]:
        ssh_key_path = cls._ssh_key_path()
        known_hosts = (
            settings.storage_box_known_hosts_path.expanduser()
            if settings.storage_box_known_hosts_path
            else None
        )
        return (
            os.name,
            settings.storage_box_host,
            settings.storage_box_port,
            settings.storage_box_username,
            str(ssh_key_path) if ssh_key_path else None,
            str(known_hosts) if known_hosts else None,
            settings.storage_box_password or None,
            cls._lftp_binary(),
        )

    @classmethod
    def _capability(cls) -> tuple[bool, str]:
        if os.name != "posix":
            return False, "lftp requires a POSIX runtime"
        if cls._lftp_binary() is None:
            return False, "lftp binary not found in PATH"
        ssh_key_path = cls._ssh_key_path()
        if ssh_key_path is None:
            return False, "lftp requires ATR_STORAGE_BOX_SSH_KEY_PATH"
        if not ssh_key_path.is_file():
            return False, f"SSH key not found: {ssh_key_path}"
        if not settings.storage_box_host or not settings.storage_box_username:
            return False, "Storage Box host/username not configured"
        return True, "ready"

    @classmethod
    def is_available(cls) -> bool:
        ok, _ = cls._capability()
        return ok

    @classmethod
    def _build_ssh_options(cls) -> str:
        ssh_key_path = cls._ssh_key_path()
        if ssh_key_path is None:
            raise RuntimeError("SSH key path required for lftp")

        command = [
            "ssh",
            "-p",
            str(settings.storage_box_port),
            "-i",
            str(ssh_key_path),
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
        ]
        if settings.storage_box_known_hosts_path:
            known_hosts = settings.storage_box_known_hosts_path.expanduser()
            command.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={known_hosts}",
                ]
            )

        return shlex.join(command)

    @staticmethod
    def _quote(value: str | Path | PurePosixPath) -> str:
        return shlex.quote(str(value))

    @classmethod
    def _build_lftp_script(
        cls,
        commands: list[str],
        *,
        segments: int | None = None,
        connection_limit: int | None = None,
    ) -> str:
        if segments is None:
            segments = cls._segments()
        if connection_limit is None:
            connection_limit = settings.storage_box_max_connections

        lines = [
            f'set sftp:connect-program "{cls._build_ssh_options()}"',
            "set cmd:fail-exit yes",
            "set net:timeout 30",
            "set net:max-retries 3",
            "set net:reconnect-interval-base 5",
            f"set net:connection-limit {connection_limit}",
            f"set pget:default-n {segments}",
            f"set mirror:parallel-transfer-count {min(segments, 4)}",
            "set xfer:clobber on",
            f"open {cls._quote(cls._build_connection_url())}",
        ]
        lines.extend(commands)
        lines.append("quit")
        return "\n".join(lines)

    @classmethod
    def _build_connection_url(cls) -> str:
        user = settings.storage_box_username
        host = settings.storage_box_host
        return f"sftp://{user}@{host}"

    @classmethod
    def _normalize_remote_path(cls, remote_path: str | PurePosixPath) -> PurePosixPath:
        path = PurePosixPath(str(remote_path).strip() or ".")
        if str(path) == ".":
            return path
        if path.is_absolute():
            return path
        root = PurePosixPath(str(settings.storage_box_root or "").strip() or ".")
        return root / path

    @classmethod
    async def _run_lftp_script(
        cls,
        script: str,
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        lftp_binary = cls._lftp_binary()
        if lftp_binary is None:
            raise RuntimeError("lftp binary not found")

        if timeout_seconds is None:
            timeout_seconds = settings.storage_box_rsync_timeout_seconds

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lftp",
            delete=False,
        ) as script_file:
            script_file.write(script)
            script_path = script_file.name

        try:
            cmd = [
                lftp_binary,
                "-f",
                script_path,
            ]
            return await run_command(cmd, timeout_seconds=timeout_seconds)
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    @classmethod
    async def preflight(cls) -> dict[str, object]:
        key = cls._preflight_key()
        cached = cls._preflight_cache.get(key)
        if cached is not None:
            available, reason = cached
            return {"available": available, "reason": reason, "cached": True}

        async with cls._preflight_lock:
            cached = cls._preflight_cache.get(key)
            if cached is not None:
                available, reason = cached
                return {"available": available, "reason": reason, "cached": True}

            available, reason = cls._capability()
            if not available:
                cls._preflight_cache[key] = (False, reason)
                return {"available": False, "reason": reason, "cached": False}

            script = cls._build_lftp_script(["pwd"])
            try:
                result = await cls._run_lftp_script(script, timeout_seconds=30)
            except CommandTimeoutError:
                cls._preflight_cache[key] = (False, "lftp preflight timed out")
                return {"available": False, "reason": "lftp preflight timed out", "cached": False}
            except Exception as exc:
                cls._preflight_cache[key] = (False, f"lftp preflight failed: {exc}")
                return {"available": False, "reason": f"lftp preflight failed: {exc}", "cached": False}

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", "replace").strip()
                reason = stderr.splitlines()[-1] if stderr else "lftp command failed"
                cls._preflight_cache[key] = (False, f"lftp preflight failed: {reason}")
                return {"available": False, "reason": f"lftp preflight failed: {reason}", "cached": False}

            cls._preflight_cache[key] = (True, "ready")
            return {"available": True, "reason": "ready", "cached": False}

    @staticmethod
    def _local_size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _avg_mib_per_sec(size_bytes: int | None, elapsed_seconds: float) -> float | None:
        if size_bytes is None or elapsed_seconds <= 0:
            return None
        return round((size_bytes / 1024 / 1024) / elapsed_seconds, 2)

    @classmethod
    async def upload_file(
        cls,
        local_path: Path,
        remote_path: str | PurePosixPath,
        *,
        segments: int | None = None,
    ) -> None:
        """Upload a single file using lftp with parallel segments."""
        remote = cls._normalize_remote_path(remote_path)
        size_bytes = cls._local_size(local_path)

        if segments is None:
            segments = cls._segments()

        logger.info(
            "Storage Box lftp upload starting local=%s remote=%s segments=%d size_bytes=%s",
            local_path,
            remote.as_posix(),
            segments,
            size_bytes if size_bytes is not None else "unknown",
        )

        remote_dir = str(remote.parent) if str(remote.parent) != "." else ""
        commands = []
        if remote_dir:
            commands.append(f"mkdir -p {cls._quote(remote_dir)}")
        commands.append(
            f"put -c -O {cls._quote(remote_dir or '.')} {cls._quote(local_path)}"
        )

        script = cls._build_lftp_script(commands, segments=segments)
        started = time.perf_counter()

        result = await cls._run_lftp_script(script)

        elapsed = time.perf_counter() - started

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"lftp upload failed for {local_path}: {stderr}")

        logger.info(
            "Storage Box lftp upload complete local=%s remote=%s size_bytes=%s elapsed_seconds=%.3f avg_mib_per_sec=%s",
            local_path,
            remote.as_posix(),
            size_bytes if size_bytes is not None else "unknown",
            elapsed,
            cls._avg_mib_per_sec(size_bytes, elapsed),
        )

    @classmethod
    async def download_file(
        cls,
        remote_path: str | PurePosixPath,
        local_path: Path,
        *,
        segments: int | None = None,
    ) -> None:
        """Download a single file using lftp with parallel segments (pget)."""
        remote = cls._normalize_remote_path(remote_path)

        if segments is None:
            segments = cls._segments()

        logger.info(
            "Storage Box lftp download starting remote=%s local=%s segments=%d",
            remote.as_posix(),
            local_path,
            segments,
        )

        local_path.parent.mkdir(parents=True, exist_ok=True)
        commands = [
            f"pget -c -n {segments} -O {cls._quote(local_path.parent)} "
            f"{cls._quote(remote.as_posix())}",
        ]

        if local_path.name != remote.name:
            commands.append(
                f"!mv {cls._quote(local_path.parent / remote.name)} "
                f"{cls._quote(local_path)}"
            )

        script = cls._build_lftp_script(commands, segments=segments)
        started = time.perf_counter()

        result = await cls._run_lftp_script(script)

        elapsed = time.perf_counter() - started
        size_bytes = cls._local_size(local_path)

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"lftp download failed for {remote.as_posix()}: {stderr}")

        logger.info(
            "Storage Box lftp download complete remote=%s local=%s size_bytes=%s elapsed_seconds=%.3f avg_mib_per_sec=%s",
            remote.as_posix(),
            local_path,
            size_bytes if size_bytes is not None else "unknown",
            elapsed,
            cls._avg_mib_per_sec(size_bytes, elapsed),
        )

    @classmethod
    async def upload_directory(
        cls,
        local_dir: Path,
        remote_dir: str | PurePosixPath,
        *,
        parallel_files: int | None = None,
        delete_remote: bool = False,
    ) -> None:
        """Upload entire directory using lftp mirror with parallel transfers."""
        remote = cls._normalize_remote_path(remote_dir)

        if parallel_files is None:
            parallel_files = min(4, cls._segments())

        logger.info(
            "Storage Box lftp mirror upload starting local=%s remote=%s parallel=%d",
            local_dir,
            remote.as_posix(),
            parallel_files,
        )

        mirror_opts = ["--reverse", "--continue", f"--parallel={parallel_files}"]
        if delete_remote:
            mirror_opts.append("--delete")

        commands = [
            f"mirror {' '.join(mirror_opts)} "
            f"{cls._quote(local_dir)} {cls._quote(remote.as_posix())}",
        ]

        script = cls._build_lftp_script(commands)
        started = time.perf_counter()

        result = await cls._run_lftp_script(script)

        elapsed = time.perf_counter() - started

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"lftp mirror upload failed for {local_dir}: {stderr}")

        logger.info(
            "Storage Box lftp mirror upload complete local=%s remote=%s elapsed_seconds=%.3f",
            local_dir,
            remote.as_posix(),
            elapsed,
        )

    @classmethod
    async def download_directory(
        cls,
        remote_dir: str | PurePosixPath,
        local_dir: Path,
        *,
        parallel_files: int | None = None,
        delete_local: bool = False,
    ) -> None:
        """Download entire directory using lftp mirror with parallel transfers."""
        remote = cls._normalize_remote_path(remote_dir)

        if parallel_files is None:
            parallel_files = min(4, cls._segments())

        logger.info(
            "Storage Box lftp mirror download starting remote=%s local=%s parallel=%d",
            remote.as_posix(),
            local_dir,
            parallel_files,
        )

        local_dir.mkdir(parents=True, exist_ok=True)

        mirror_opts = ["--continue", f"--parallel={parallel_files}"]
        if delete_local:
            mirror_opts.append("--delete")

        commands = [
            f"mirror {' '.join(mirror_opts)} "
            f"{cls._quote(remote.as_posix())} {cls._quote(local_dir)}",
        ]

        script = cls._build_lftp_script(commands)
        started = time.perf_counter()

        result = await cls._run_lftp_script(script)

        elapsed = time.perf_counter() - started

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"lftp mirror download failed for {remote.as_posix()}: {stderr}")

        logger.info(
            "Storage Box lftp mirror download complete remote=%s local=%s elapsed_seconds=%.3f",
            remote.as_posix(),
            local_dir,
            elapsed,
        )
