from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Literal

from contextlib import nullcontext

from ..config import settings
from ..utils.subprocess_runner import CommandTimeoutError, run_command
from .storage_box_lftp import StorageBoxLftpService
from .storage_box_progress import TransferSession
from .storage_box_sftp_client import StorageBoxSftpClient


logger = logging.getLogger("uvicorn.error")


TransferMode = Literal["auto", "sftp", "rsync", "lftp"]
SelectedMode = Literal["sftp", "rsync", "lftp"]


class StorageBoxTransferService:
    """Bulk file transfer helper with optional rsync fast path over SSH."""

    _preflight_lock = asyncio.Lock()
    _preflight_cache: dict[tuple[object, ...], tuple[bool, str]] = {}

    @classmethod
    def _configured_mode(cls) -> TransferMode:
        return settings.storage_box_transfer_mode  # validated in config

    @classmethod
    def _rsync_min_size_bytes(cls) -> int:
        return settings.storage_box_rsync_min_file_size_mb * 1024 * 1024

    @classmethod
    def _lftp_min_size_bytes(cls) -> int:
        return settings.storage_box_lftp_min_file_size_mb * 1024 * 1024

    @classmethod
    def _ssh_binary(cls) -> str | None:
        return shutil.which("ssh")

    @classmethod
    def _rsync_binary(cls) -> str | None:
        return shutil.which("rsync")

    @classmethod
    def _ssh_key_path(cls) -> Path | None:
        if not settings.storage_box_ssh_key_path:
            return None
        return settings.storage_box_ssh_key_path.expanduser()

    @classmethod
    def _preflight_key(cls) -> tuple[object, ...]:
        ssh_key_path = cls._ssh_key_path()
        known_hosts = settings.storage_box_known_hosts_path.expanduser() if settings.storage_box_known_hosts_path else None
        return (
            os.name,
            settings.storage_box_host,
            settings.storage_box_port,
            settings.storage_box_username,
            str(ssh_key_path) if ssh_key_path else None,
            str(known_hosts) if known_hosts else None,
            settings.storage_box_password or None,
            cls._ssh_binary(),
            cls._rsync_binary(),
        )

    @classmethod
    def _rsync_capability(cls) -> tuple[bool, str]:
        if os.name != "posix":
            return False, "rsync fast path requires a POSIX runtime"
        if settings.storage_box_port != 23:
            return False, "rsync fast path requires ATR_STORAGE_BOX_PORT=23"
        if settings.storage_box_password:
            return False, "rsync fast path requires SSH key auth without ATR_STORAGE_BOX_PASSWORD"
        ssh_key_path = cls._ssh_key_path()
        if ssh_key_path is None:
            return False, "rsync fast path requires ATR_STORAGE_BOX_SSH_KEY_PATH"
        if not ssh_key_path.is_file():
            return False, f"SSH key not found: {ssh_key_path}"
        if cls._ssh_binary() is None:
            return False, "ssh binary not found in PATH"
        if cls._rsync_binary() is None:
            return False, "rsync binary not found in PATH"
        if not StorageBoxSftpClient.is_configured():
            return False, "Storage Box is not configured"
        return True, "ready"

    @classmethod
    def _build_ssh_command(cls) -> list[str]:
        ssh_binary = cls._ssh_binary()
        ssh_key_path = cls._ssh_key_path()
        if ssh_binary is None or ssh_key_path is None:
            raise RuntimeError("Storage Box rsync SSH command requested without required binaries or SSH key")

        command = [
            ssh_binary,
            "-p",
            str(settings.storage_box_port),
            "-i",
            str(ssh_key_path),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if settings.storage_box_known_hosts_path:
            command.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={settings.storage_box_known_hosts_path.expanduser()}",
                ]
            )
        return command

    @classmethod
    def _build_remote_spec(cls, remote_path: PurePosixPath) -> str:
        return f"{settings.storage_box_username}@{settings.storage_box_host}:{PurePosixPath(remote_path).as_posix()}"

    @classmethod
    def _build_rsync_command(cls, src: str, dst: str, *, resume: bool) -> list[str]:
        rsync_binary = cls._rsync_binary()
        if rsync_binary is None:
            raise RuntimeError("Storage Box rsync command requested without rsync installed")
        command = [
            rsync_binary,
            "--mkpath",
            "--partial",
            "--protect-args",
            "--info=stats2",
            "--compress-level=0",  # Skip compression (video files already compressed)
            "--block-size=131072",  # 128KB blocks for large files (vs 700 byte default)
            "-e",
            shlex.join(cls._build_ssh_command()),
        ]
        command.append("--append-verify" if resume else "--whole-file")
        command.extend([src, dst])
        return command

    @classmethod
    def _build_rsync_list_command(cls, remote_path: PurePosixPath) -> list[str]:
        rsync_binary = cls._rsync_binary()
        if rsync_binary is None:
            raise RuntimeError("Storage Box rsync command requested without rsync installed")
        return [
            rsync_binary,
            "--list-only",
            "-e",
            shlex.join(cls._build_ssh_command()),
            cls._build_remote_spec(remote_path),
        ]

    @classmethod
    def _build_upload_rsync_command(
        cls,
        local_path: Path,
        remote_path: PurePosixPath,
        *,
        resume: bool,
    ) -> list[str]:
        return cls._build_rsync_command(
            str(local_path),
            cls._build_remote_spec(remote_path),
            resume=resume,
        )

    @classmethod
    def _build_download_rsync_command(
        cls,
        remote_path: PurePosixPath,
        local_path: Path,
        *,
        resume: bool,
    ) -> list[str]:
        return cls._build_rsync_command(
            cls._build_remote_spec(remote_path),
            str(local_path),
            resume=resume,
        )

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

            available, reason = cls._rsync_capability()
            if not available:
                cls._preflight_cache[key] = (False, reason)
                return {"available": False, "reason": reason, "cached": False}

            cmd = cls._build_rsync_list_command(PurePosixPath("."))
            try:
                result = await run_command(
                    cmd,
                    timeout_seconds=min(30, settings.storage_box_rsync_timeout_seconds),
                )
            except CommandTimeoutError:
                cls._preflight_cache[key] = (False, "rsync preflight timed out")
                return {"available": False, "reason": "rsync preflight timed out", "cached": False}
            except Exception as exc:
                cls._preflight_cache[key] = (False, f"rsync preflight failed: {exc}")
                return {"available": False, "reason": f"rsync preflight failed: {exc}", "cached": False}

            if result.returncode != 0:
                reason = cls._summarize_command_error(result.stderr)
                cls._preflight_cache[key] = (False, f"rsync preflight failed: {reason}")
                return {"available": False, "reason": f"rsync preflight failed: {reason}", "cached": False}

            cls._preflight_cache[key] = (True, "ready")
            return {"available": True, "reason": "ready", "cached": False}

    @staticmethod
    def _summarize_command_error(stderr: bytes) -> str:
        text = stderr.decode("utf-8", "replace").strip()
        if not text:
            return "command failed without stderr"
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return "command failed without stderr"
        summary = " | ".join(lines[-5:])
        if len(summary) > 2000:
            return f"...{summary[-1997:]}"
        return summary

    @staticmethod
    def _local_size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @classmethod
    async def _remote_size(cls, remote_path: PurePosixPath) -> int | None:
        try:
            stat_result = await StorageBoxSftpClient.stat(remote_path)
        except Exception:
            return None
        return int(getattr(stat_result, "size", 0))

    @classmethod
    async def _remote_exists(cls, remote_path: PurePosixPath) -> bool:
        try:
            return await StorageBoxSftpClient.exists(remote_path)
        except Exception:
            return False

    @classmethod
    async def _select_upload_mode(
        cls,
        local_path: Path,
    ) -> tuple[SelectedMode, str, int | None]:
        configured_mode = cls._configured_mode()
        size_bytes = cls._local_size(local_path)

        if configured_mode == "sftp":
            return "sftp", "configured_sftp_mode", size_bytes

        if not local_path.is_file():
            reason = f"bulk transfer requires a regular file: {local_path}"
            if configured_mode in ("rsync", "lftp"):
                raise RuntimeError(reason)
            return "sftp", reason, size_bytes

        # Explicit lftp mode remains available for opt-in use.
        if configured_mode == "lftp":
            if StorageBoxLftpService.is_available():
                lftp_preflight = await StorageBoxLftpService.preflight()
                if bool(lftp_preflight["available"]):
                    return "lftp", "lftp_ready", size_bytes
                elif configured_mode == "lftp":
                    raise RuntimeError(
                        f"Storage Box lftp mode is unavailable: {lftp_preflight['reason']}"
                    )
            elif configured_mode == "lftp":
                raise RuntimeError("Storage Box lftp mode is unavailable: lftp not installed")

        # Try rsync for medium files
        if configured_mode == "auto" and (size_bytes is None or size_bytes < cls._rsync_min_size_bytes()):
            return "sftp", "below_rsync_size_threshold", size_bytes

        capability_ok, reason = cls._rsync_capability()
        if not capability_ok:
            if configured_mode == "rsync":
                raise RuntimeError(f"Storage Box rsync mode is unavailable: {reason}")
            return "sftp", reason, size_bytes

        preflight = await cls.preflight()
        if not bool(preflight["available"]):
            reason = str(preflight["reason"])
            if configured_mode == "rsync":
                raise RuntimeError(f"Storage Box rsync mode is unavailable: {reason}")
            return "sftp", reason, size_bytes

        return "rsync", "rsync_ready", size_bytes

    @classmethod
    async def _select_download_mode(
        cls,
        remote_path: PurePosixPath,
    ) -> tuple[SelectedMode, str, int | None]:
        configured_mode = cls._configured_mode()
        if configured_mode == "sftp":
            return "sftp", "configured_sftp_mode", await cls._remote_size(remote_path)

        size_bytes = await cls._remote_size(remote_path)

        # Explicit lftp mode remains available for opt-in use.
        if configured_mode == "lftp":
            if StorageBoxLftpService.is_available():
                lftp_preflight = await StorageBoxLftpService.preflight()
                if bool(lftp_preflight["available"]):
                    return "lftp", "lftp_ready", size_bytes
                elif configured_mode == "lftp":
                    raise RuntimeError(
                        f"Storage Box lftp mode is unavailable: {lftp_preflight['reason']}"
                    )
            elif configured_mode == "lftp":
                raise RuntimeError("Storage Box lftp mode is unavailable: lftp not installed")

        # Try rsync for medium files
        if configured_mode == "auto":
            if size_bytes is None:
                return "sftp", "remote_size_unavailable", None
            if size_bytes < cls._rsync_min_size_bytes():
                return "sftp", "below_rsync_size_threshold", size_bytes

        capability_ok, reason = cls._rsync_capability()
        if not capability_ok:
            if configured_mode == "rsync":
                raise RuntimeError(f"Storage Box rsync mode is unavailable: {reason}")
            return "sftp", reason, size_bytes

        preflight = await cls.preflight()
        if not bool(preflight["available"]):
            reason = str(preflight["reason"])
            if configured_mode == "rsync":
                raise RuntimeError(f"Storage Box rsync mode is unavailable: {reason}")
            return "sftp", reason, size_bytes

        return "rsync", "rsync_ready", size_bytes

    @classmethod
    def _avg_mib_per_sec(cls, size_bytes: int | None, elapsed_seconds: float) -> float | None:
        if size_bytes is None or elapsed_seconds <= 0:
            return None
        return round((size_bytes / 1024 / 1024) / elapsed_seconds, 2)

    @classmethod
    def _log_selection(
        cls,
        *,
        direction: str,
        mode: SelectedMode,
        local_path: Path,
        remote_path: PurePosixPath,
        reason: str,
        size_bytes: int | None,
    ) -> None:
        logger.info(
            "Storage Box %s transfer mode=%s local=%s remote=%s size_bytes=%s reason=%s",
            direction,
            mode,
            local_path,
            PurePosixPath(remote_path).as_posix(),
            size_bytes if size_bytes is not None else "unknown",
            reason,
        )

    @classmethod
    def _log_success(
        cls,
        *,
        direction: str,
        mode: SelectedMode,
        local_path: Path,
        remote_path: PurePosixPath,
        size_bytes: int | None,
        elapsed_seconds: float,
    ) -> None:
        logger.info(
            "Storage Box %s transfer complete mode=%s local=%s remote=%s size_bytes=%s elapsed_seconds=%.3f avg_mib_per_sec=%s",
            direction,
            mode,
            local_path,
            PurePosixPath(remote_path).as_posix(),
            size_bytes if size_bytes is not None else "unknown",
            elapsed_seconds,
            cls._avg_mib_per_sec(size_bytes, elapsed_seconds),
        )

    @classmethod
    async def upload_file(
        cls,
        local_path: Path,
        remote_path: str | PurePosixPath,
        *,
        session: TransferSession | None = None,
    ) -> None:
        configured_mode = cls._configured_mode()
        remote = StorageBoxSftpClient.normalize_remote_path(remote_path)
        mode, reason, size_bytes = await cls._select_upload_mode(local_path)
        cls._log_selection(
            direction="upload",
            mode=mode,
            local_path=local_path,
            remote_path=remote,
            reason=reason,
            size_bytes=size_bytes,
        )

        track_cm = (
            session.track(
                local_path=local_path,
                remote_path=remote,
                target_size=size_bytes if size_bytes is not None else 0,
            )
            if session is not None
            else nullcontext()
        )

        started = time.perf_counter()
        async with track_cm:
            if mode == "sftp":
                await StorageBoxSftpClient.upload_file(local_path, remote)
            elif mode == "lftp":
                await StorageBoxLftpService.upload_file(local_path, remote)
            else:
                resume = await cls._remote_exists(remote)
                try:
                    result = await run_command(
                        cls._build_upload_rsync_command(local_path, remote, resume=resume),
                        timeout_seconds=settings.storage_box_rsync_timeout_seconds,
                    )
                except Exception:
                    if configured_mode == "rsync":
                        raise
                    logger.warning(
                        "Storage Box rsync upload command failed for %s -> %s; falling back to sftp",
                        local_path,
                        remote.as_posix(),
                        exc_info=True,
                    )
                    await StorageBoxSftpClient.upload_file(local_path, remote)
                    mode = "sftp"
                else:
                    if result.returncode != 0:
                        detail = cls._summarize_command_error(result.stderr)
                        if configured_mode == "rsync":
                            raise RuntimeError(
                                f"Storage Box rsync upload failed for {local_path}: {detail}"
                            )
                        logger.warning(
                            "Storage Box rsync upload failed for %s -> %s; "
                            "falling back to sftp: %s",
                            local_path,
                            remote.as_posix(),
                            detail,
                        )
                        await StorageBoxSftpClient.upload_file(local_path, remote)
                        mode = "sftp"

        elapsed = time.perf_counter() - started
        cls._log_success(
            direction="upload",
            mode=mode,
            local_path=local_path,
            remote_path=remote,
            size_bytes=size_bytes if size_bytes is not None else cls._local_size(local_path),
            elapsed_seconds=elapsed,
        )

    @classmethod
    async def download_file(
        cls,
        remote_path: str | PurePosixPath,
        local_path: Path,
        *,
        session: TransferSession | None = None,
    ) -> None:
        configured_mode = cls._configured_mode()
        remote = StorageBoxSftpClient.normalize_remote_path(remote_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        mode, reason, size_bytes = await cls._select_download_mode(remote)
        cls._log_selection(
            direction="download",
            mode=mode,
            local_path=local_path,
            remote_path=remote,
            reason=reason,
            size_bytes=size_bytes,
        )

        track_cm = (
            session.track(
                local_path=local_path,
                remote_path=remote,
                target_size=size_bytes if size_bytes is not None else 0,
            )
            if session is not None
            else nullcontext()
        )

        started = time.perf_counter()
        async with track_cm:
            if mode == "sftp":
                await StorageBoxSftpClient.download_file(remote, local_path)
            elif mode == "lftp":
                await StorageBoxLftpService.download_file(remote, local_path)
            else:
                resume = bool(local_path.exists() and (cls._local_size(local_path) or 0) > 0)
                try:
                    result = await run_command(
                        cls._build_download_rsync_command(remote, local_path, resume=resume),
                        timeout_seconds=settings.storage_box_rsync_timeout_seconds,
                    )
                except Exception:
                    if configured_mode == "rsync":
                        raise
                    logger.warning(
                        "Storage Box rsync download command failed for %s -> %s; falling back to sftp",
                        remote.as_posix(),
                        local_path,
                        exc_info=True,
                    )
                    await StorageBoxSftpClient.download_file(remote, local_path)
                    mode = "sftp"
                else:
                    if result.returncode != 0:
                        detail = cls._summarize_command_error(result.stderr)
                        if configured_mode == "rsync":
                            raise RuntimeError(
                                f"Storage Box rsync download failed for {remote.as_posix()}: {detail}"
                            )
                        logger.warning(
                            "Storage Box rsync download failed for %s -> %s; "
                            "falling back to sftp: %s",
                            remote.as_posix(),
                            local_path,
                            detail,
                        )
                        await StorageBoxSftpClient.download_file(remote, local_path)
                        mode = "sftp"

        elapsed = time.perf_counter() - started
        cls._log_success(
            direction="download",
            mode=mode,
            local_path=local_path,
            remote_path=remote,
            size_bytes=cls._local_size(local_path) or size_bytes,
            elapsed_seconds=elapsed,
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
        """Upload entire directory using lftp mirror (preferred) or batched transfers.

        Args:
            local_dir: Local directory to upload
            remote_dir: Remote directory path
            parallel_files: Number of parallel file transfers (default: from config)
            delete_remote: If True, delete remote files not in local directory
        """
        remote = StorageBoxSftpClient.normalize_remote_path(remote_dir)

        if parallel_files is None:
            parallel_files = settings.storage_box_upload_max_parallel

        # Prefer lftp mirror for directory uploads (most efficient)
        if StorageBoxLftpService.is_available():
            lftp_preflight = await StorageBoxLftpService.preflight()
            if bool(lftp_preflight["available"]):
                logger.info(
                    "Storage Box directory upload using lftp mirror local=%s remote=%s parallel=%d",
                    local_dir,
                    remote.as_posix(),
                    parallel_files,
                )
                await StorageBoxLftpService.upload_directory(
                    local_dir,
                    remote,
                    parallel_files=parallel_files,
                    delete_remote=delete_remote,
                )
                return

        # Fallback: collect files and upload with bounded parallelism
        logger.info(
            "Storage Box directory upload using batched transfers local=%s remote=%s parallel=%d",
            local_dir,
            remote.as_posix(),
            parallel_files,
        )

        files_to_upload: list[tuple[Path, PurePosixPath]] = []
        for local_file in local_dir.rglob("*"):
            if local_file.is_file():
                relative = local_file.relative_to(local_dir)
                remote_file = remote / PurePosixPath(*relative.parts)
                files_to_upload.append((local_file, remote_file))

        if not files_to_upload:
            logger.info("Storage Box directory upload: no files to upload")
            return

        import asyncio

        semaphore = asyncio.Semaphore(max(1, parallel_files))

        async def _upload_one(item: tuple[Path, PurePosixPath]) -> None:
            async with semaphore:
                local_file, remote_file = item
                await cls.upload_file(local_file, remote_file)

        async with asyncio.TaskGroup() as tg:
            for item in files_to_upload:
                tg.create_task(_upload_one(item))

        logger.info(
            "Storage Box directory upload complete local=%s remote=%s files=%d",
            local_dir,
            remote.as_posix(),
            len(files_to_upload),
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
        """Download entire directory using lftp mirror (preferred) or batched transfers.

        Args:
            remote_dir: Remote directory path
            local_dir: Local directory to download to
            parallel_files: Number of parallel file transfers (default: from config)
            delete_local: If True, delete local files not in remote directory
        """
        remote = StorageBoxSftpClient.normalize_remote_path(remote_dir)

        if parallel_files is None:
            parallel_files = settings.storage_box_download_max_parallel

        # Prefer lftp mirror for directory downloads (most efficient)
        if StorageBoxLftpService.is_available():
            lftp_preflight = await StorageBoxLftpService.preflight()
            if bool(lftp_preflight["available"]):
                logger.info(
                    "Storage Box directory download using lftp mirror remote=%s local=%s parallel=%d",
                    remote.as_posix(),
                    local_dir,
                    parallel_files,
                )
                await StorageBoxLftpService.download_directory(
                    remote,
                    local_dir,
                    parallel_files=parallel_files,
                    delete_local=delete_local,
                )
                return

        # Fallback: list remote files and download with bounded parallelism
        logger.info(
            "Storage Box directory download using batched transfers remote=%s local=%s parallel=%d",
            remote.as_posix(),
            local_dir,
            parallel_files,
        )

        local_dir.mkdir(parents=True, exist_ok=True)

        remote_entries = await StorageBoxSftpClient.scandir(remote)
        files_to_download: list[tuple[PurePosixPath, Path]] = []

        for entry in remote_entries:
            filename = getattr(entry, "filename", None)
            if filename is None:
                continue
            attrs = getattr(entry, "attrs", None)
            is_dir = attrs is not None and getattr(attrs, "type", None) == 2  # SFTP directory type

            if is_dir:
                # Recursively handle subdirectories
                sub_remote = remote / filename
                sub_local = local_dir / filename
                await cls.download_directory(
                    sub_remote,
                    sub_local,
                    parallel_files=parallel_files,
                    delete_local=delete_local,
                )
            else:
                remote_file = remote / filename
                local_file = local_dir / filename
                files_to_download.append((remote_file, local_file))

        if not files_to_download:
            return

        import asyncio

        semaphore = asyncio.Semaphore(max(1, parallel_files))

        async def _download_one(item: tuple[PurePosixPath, Path]) -> None:
            async with semaphore:
                remote_file, local_file = item
                await cls.download_file(remote_file, local_file)

        async with asyncio.TaskGroup() as tg:
            for item in files_to_download:
                tg.create_task(_download_one(item))

        logger.info(
            "Storage Box directory download complete remote=%s local=%s files=%d",
            remote.as_posix(),
            local_dir,
            len(files_to_download),
        )
