"""Single-transport Storage Box bulk transfers via one rclone batch process.

Replaces the old sftp/rsync/lftp selector: publishes upload their whole
``to_upload`` batch and hydration downloads its whole episode/index batch
with a single ``rclone copy`` invocation over SFTP. rclone owns transfer
parallelism, retries and low-level resume; the shared
:mod:`rclone_runner` streams per-second JSON stats which are mapped onto
the existing :class:`ProgressSnapshot` contract so the progress chain is
byte-accurate instead of stat-polling paths.
"""

from __future__ import annotations

import inspect
import logging
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from ..config import settings
from .rclone_runner import (
    RcloneError,
    RcloneStats,
    find_binary,
    obscure_password,
    run_rclone,
)
from .storage_box_progress import ProgressCallback, ProgressSnapshot
from .storage_box_sftp_client import StorageBoxSftpClient

logger = logging.getLogger("uvicorn.error")

_INSTALL_HINT = (
    "rclone is required for Storage Box transfers but was not found on PATH. "
    "Install it first (Arch: sudo pacman -S rclone)."
)


class StorageBoxRcloneError(RcloneError):
    """Raised when the rclone subprocess fails or rclone is unavailable."""


class StorageBoxRclone:
    """Batch uploader/downloader for the Storage Box using one rclone process."""

    @classmethod
    def binary(cls) -> str | None:
        return find_binary()

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

            binary = cls.binary() or ""
            cmd = [
                binary,
                "copy",
                str(tree_dir),
                f":sftp:{remote_root.as_posix()}",
                "--copy-links",
                *cls._sftp_flags(checkers=4),
            ]
            await cls._run_transfer(
                cmd,
                binary=binary,
                total_bytes=total_bytes,
                progress_callback=progress_callback,
            )
        finally:
            shutil.rmtree(tree_dir, ignore_errors=True)

    @classmethod
    async def download_batch(
        cls,
        items: list[PurePosixPath],
        *,
        remote_base: str | PurePosixPath,
        dest_root: Path,
        total_bytes: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """Download ``items`` (remote paths relative to ``remote_base``).

        One ``rclone copy`` with ``--files-from``; each item lands at
        ``dest_root/<item>`` (rclone preserves source-relative layout —
        callers map results to their final targets). Raises
        :class:`StorageBoxRcloneError` on failure.
        """
        if not items:
            return
        cls.ensure_available()

        remote_root = StorageBoxSftpClient.normalize_remote_path(remote_base)
        for item in items:
            if item.is_absolute():
                raise StorageBoxRcloneError(
                    f"download_batch expects remote paths relative to the batch "
                    f"root, got absolute path: {item}"
                )

        dest_root.mkdir(parents=True, exist_ok=True)
        list_file = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="atr-rclone-files-",
            suffix=".txt",
            dir=str(settings.cache_dir),
            delete=False,
        )
        try:
            with list_file:
                for item in items:
                    list_file.write(f"{item.as_posix()}\n")

            binary = cls.binary() or ""
            cmd = [
                binary,
                "copy",
                f":sftp:{remote_root.as_posix()}",
                str(dest_root),
                "--files-from",
                list_file.name,
                "--no-traverse",
                *cls._sftp_flags(checkers=8),
            ]
            await cls._run_transfer(
                cmd,
                binary=binary,
                total_bytes=total_bytes,
                progress_callback=progress_callback,
            )
        finally:
            _unlink_quietly(Path(list_file.name))

    @classmethod
    def _sftp_flags(cls, *, checkers: int) -> list[str]:
        flags = [
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
            str(checkers),
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
            flags += ["--sftp-key-file", str(settings.storage_box_ssh_key_path)]
        if settings.storage_box_known_hosts_path:
            flags += [
                "--sftp-known-hosts-file",
                str(settings.storage_box_known_hosts_path),
            ]
        return flags

    @classmethod
    async def _run_transfer(
        cls,
        cmd: list[str],
        *,
        binary: str,
        total_bytes: int | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        env = dict(os.environ)
        if not settings.storage_box_ssh_key_path and settings.storage_box_password:
            try:
                env["RCLONE_SFTP_PASS"] = await obscure_password(
                    binary, settings.storage_box_password
                )
            except RcloneError as exc:
                raise StorageBoxRcloneError(str(exc)) from exc

        async def _on_stats(stats: RcloneStats) -> None:
            await cls._emit(
                progress_callback, cls._snapshot_from_stats(stats, total_bytes)
            )

        try:
            last_stats = await run_rclone(
                cmd, env=env, stats_callback=_on_stats
            )
        except RcloneError as exc:
            raise StorageBoxRcloneError(str(exc)) from exc

        # Terminal snapshot so the UI lands exactly on 100%.
        final_total = (
            total_bytes
            if total_bytes is not None
            else (last_stats.bytes_total if last_stats else 0)
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

    @staticmethod
    def _snapshot_from_stats(
        stats: RcloneStats, total_bytes: int | None
    ) -> ProgressSnapshot:
        # Prefer our exact precomputed total: rclone's totalBytes grows while
        # it is still scanning the source tree.
        total = total_bytes if total_bytes is not None else stats.bytes_total
        transferred = stats.bytes_transferred
        if total > 0:
            transferred = min(transferred, total)
        return ProgressSnapshot(
            bytes_transferred=max(0, transferred),
            bytes_total=max(0, total),
            mib_per_sec=round(stats.speed_bytes_per_sec / (1024 * 1024), 2),
            eta_seconds=stats.eta_seconds,
            active_transfers=len(stats.transferring_names),
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


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
