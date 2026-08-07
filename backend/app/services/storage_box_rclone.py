"""Single-transport Storage Box uploads via one rclone batch process.

Replaces the old sftp/rsync/lftp selector for the publish path: every
publish uploads its whole ``to_upload`` batch with a single ``rclone copy``
invocation over SFTP. rclone owns transfer parallelism, retries and
low-level resume; we read its per-second JSON stats from stderr and map
them onto the existing :class:`ProgressSnapshot` contract so the SSE
progress chain is byte-accurate instead of stat-polling remote paths.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import tempfile
from collections import deque
from pathlib import Path, PurePosixPath

from ..config import settings
from ..utils.subprocess_runner import terminate_process
from .storage_box_progress import ProgressCallback, ProgressSnapshot
from .storage_box_sftp_client import StorageBoxSftpClient

logger = logging.getLogger("uvicorn.error")

_INSTALL_HINT = (
    "rclone is required for Storage Box uploads but was not found on PATH. "
    "Install it first (Arch: sudo pacman -S rclone)."
)

# stderr lines are JSON logs; stats lines stay small but keep headroom for
# long "transferring" arrays.
_STDERR_LINE_LIMIT = 1024 * 1024
_MAX_ERROR_LINES = 5


class StorageBoxRcloneError(RuntimeError):
    """Raised when the rclone subprocess fails or rclone is unavailable."""


class StorageBoxRclone:
    """Batch uploader for the Storage Box using one rclone process."""

    @classmethod
    def binary(cls) -> str | None:
        return shutil.which("rclone")

    @classmethod
    def ensure_available(cls) -> None:
        if cls.binary() is None:
            raise StorageBoxRcloneError(_INSTALL_HINT)

    @classmethod
    async def upload_batch(
        cls,
        items: list[tuple[Path, PurePosixPath]],
        *,
        remote_base: str | PurePosixPath,
        total_bytes: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Upload ``items`` (local file → remote path relative to ``remote_base``).

        The whole batch runs as one ``rclone copy`` over a temporary symlink
        tree mirroring the remote-relative layout (symlinks, not hardlinks:
        sources may span filesystems). Raises :class:`StorageBoxRcloneError`
        on failure; partial uploads are retried internally by rclone.
        """
        if not items:
            return
        cls.ensure_available()

        remote_root = StorageBoxSftpClient.normalize_remote_path(remote_base)
        tree_dir = Path(
            tempfile.mkdtemp(prefix="atr-rclone-", dir=str(settings.cache_dir))
        )
        try:
            for local_path, remote_relative in items:
                if remote_relative.is_absolute():
                    raise StorageBoxRcloneError(
                        f"upload_batch expects remote paths relative to the batch "
                        f"root, got absolute path: {remote_relative}"
                    )
                link_path = tree_dir / Path(*remote_relative.parts)
                link_path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(Path(local_path).resolve(), link_path)

            await cls._run_copy(
                tree_dir,
                remote_root,
                total_bytes=total_bytes,
                progress_callback=progress_callback,
            )
        finally:
            shutil.rmtree(tree_dir, ignore_errors=True)

    @classmethod
    async def _run_copy(
        cls,
        tree_dir: Path,
        remote_root: PurePosixPath,
        *,
        total_bytes: int | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        binary = cls.binary()
        if binary is None:  # pragma: no cover - ensure_available already ran
            raise StorageBoxRcloneError(_INSTALL_HINT)

        cmd = [
            binary,
            "copy",
            str(tree_dir),
            f":sftp:{remote_root.as_posix()}",
            "--copy-links",
            "--config",
            "/dev/null",
            "--sftp-host",
            str(settings.storage_box_host or ""),
            "--sftp-user",
            str(settings.storage_box_username or ""),
            "--sftp-port",
            str(settings.storage_box_port),
            "--transfers",
            str(max(1, settings.storage_box_rclone_transfers)),
            "--checkers",
            "4",
            "--sftp-concurrency",
            "64",
            "--sftp-chunk-size",
            "255Ki",
            "--buffer-size",
            "16Mi",
            "--retries",
            "3",
            "--retries-sleep",
            "5s",
            "--low-level-retries",
            "10",
            "--timeout",
            "5m",
            "--contimeout",
            "30s",
            "--stats",
            "1s",
            "--stats-log-level",
            "NOTICE",
            "--use-json-log",
            "--log-level",
            "NOTICE",
        ]
        if settings.storage_box_ssh_key_path:
            cmd += ["--sftp-key-file", str(settings.storage_box_ssh_key_path)]
        if settings.storage_box_known_hosts_path:
            cmd += [
                "--sftp-known-hosts-file",
                str(settings.storage_box_known_hosts_path),
            ]

        env = dict(os.environ)
        if not settings.storage_box_ssh_key_path and settings.storage_box_password:
            env["RCLONE_SFTP_PASS"] = await cls._obscure_password(
                binary, settings.storage_box_password
            )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STDERR_LINE_LIMIT,
        )
        error_lines: deque[str] = deque(maxlen=_MAX_ERROR_LINES)
        last_snapshot: ProgressSnapshot | None = None
        try:
            assert process.stderr is not None
            while True:
                try:
                    line = await process.stderr.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    continue
                if not line:
                    break
                snapshot = cls._handle_stderr_line(
                    line, total_bytes=total_bytes, error_lines=error_lines
                )
                if snapshot is not None:
                    last_snapshot = snapshot
                    await cls._emit(progress_callback, snapshot)
            returncode = await process.wait()
        except asyncio.CancelledError:
            await terminate_process(process)
            raise

        if returncode != 0:
            details = "; ".join(error_lines) or "no error output captured"
            raise StorageBoxRcloneError(
                f"rclone exited with code {returncode}: {details}"
            )

        # Terminal snapshot so the UI lands exactly on 100%.
        final_total = (
            total_bytes
            if total_bytes is not None
            else (last_snapshot.bytes_total if last_snapshot else 0)
        )
        await cls._emit(
            progress_callback,
            ProgressSnapshot(
                bytes_transferred=final_total,
                bytes_total=final_total,
                mib_per_sec=None,
                eta_seconds=0.0,
                active_transfers=0,
            ),
        )

    @classmethod
    def _handle_stderr_line(
        cls,
        raw_line: bytes,
        *,
        total_bytes: int | None,
        error_lines: deque[str],
    ) -> ProgressSnapshot | None:
        try:
            payload = json.loads(raw_line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        if payload.get("level") == "error":
            message = str(payload.get("msg") or "").strip()
            if message:
                error_lines.append(message)

        stats = payload.get("stats")
        if not isinstance(stats, dict):
            return None
        try:
            transferred = int(stats.get("bytes") or 0)
            # Prefer our exact precomputed total: rclone's totalBytes grows
            # while it is still scanning the source tree.
            reported_total = int(stats.get("totalBytes") or 0)
            total = total_bytes if total_bytes is not None else reported_total
            speed = float(stats.get("speed") or 0.0)
            eta = stats.get("eta")
            transferring = stats.get("transferring")
        except (TypeError, ValueError):
            return None

        if total > 0:
            transferred = min(transferred, total)
        return ProgressSnapshot(
            bytes_transferred=max(0, transferred),
            bytes_total=max(0, total),
            mib_per_sec=round(speed / (1024 * 1024), 2),
            eta_seconds=float(eta) if isinstance(eta, (int, float)) else None,
            active_transfers=(
                len(transferring) if isinstance(transferring, list) else 0
            ),
        )

    @staticmethod
    async def _emit(
        progress_callback: ProgressCallback | None, snapshot: ProgressSnapshot
    ) -> None:
        if progress_callback is None:
            return
        try:
            result = progress_callback(snapshot)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "rclone progress callback raised; continuing", exc_info=True
            )

    @staticmethod
    async def _obscure_password(binary: str, password: str) -> str:
        # Password goes through stdin (never argv) and reaches rclone as an
        # obscured env value, matching rclone's expectations for RCLONE_SFTP_PASS.
        process = await asyncio.create_subprocess_exec(
            binary,
            "obscure",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(password.encode("utf-8"))
        if process.returncode != 0:
            raise StorageBoxRcloneError(
                f"rclone obscure failed: {stderr.decode(errors='replace').strip()}"
            )
        return stdout.decode("utf-8").strip()
