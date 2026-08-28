"""Google Drive export sync via one rclone process.

Replaces the wipe-and-reupload of the /processing export bundle with a
single ``rclone sync`` of a staged tree into the project's Drive folder.
``--checksum`` compares Drive's server-side MD5s, so unchanged source
episodes are skipped (even ones uploaded by the old googleapiclient path)
and stale remote files are deleted — no separate clear phase.

Auth and folder targeting go through env vars (``RCLONE_DRIVE_*``), never
argv; the folder itself is still created/resolved by
``GoogleDriveService.ensure_project_folder`` (googleapiclient), which owns
the folder id + webViewLink contract.

Throttled sessions: Google pins each resumable upload session to a backend,
and some sessions crawl at <1 MB/s for their whole life (receiver-window
limited — measured 2026-08-28: 3 of 11 fresh sessions, the others 4–12 MB/s
on the same link). rclone never abandons a session on its own, so
:class:`_SlowStreamGuard` watches the trailing throughput and restarts the
process (= a fresh session) when it stays under the floor for a full window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Callable

from ..config import settings
from .google_drive_service import GoogleDriveService
from .rclone_runner import (
    RcloneError,
    RcloneRestartRequested,
    RcloneStats,
    StatsCallback,
    find_binary,
    run_rclone,
)

logger = logging.getLogger("uvicorn.error")

_INSTALL_HINT = (
    "rclone is required for Google Drive exports but was not found on PATH. "
    "Install it first (Arch: sudo pacman -S rclone)."
)
_MIB = 1024 * 1024

RestartCallback = Callable[[str], None]


class GoogleDriveRcloneError(RcloneError):
    """Raised when the Drive rclone subprocess fails or rclone is missing."""


class _SlowStreamGuard:
    """Asks for a restart when a batch's trailing throughput stays under the floor.

    The decision uses the batch's own byte counter over a sliding window of
    ``window_seconds`` (rclone's ``speed`` is a lifetime average and hides a
    session that degraded late). Finishing is always preferred when fewer than
    ``min_remaining_bytes`` are left: a restart throws the in-flight bytes away.
    """

    def __init__(
        self,
        *,
        floor_bytes_per_sec: float,
        window_seconds: float,
        min_remaining_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._floor = float(floor_bytes_per_sec)
        self._window = float(window_seconds)
        self._min_remaining = int(min_remaining_bytes)
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self) -> None:
        self._samples.clear()

    def check(self, stats: RcloneStats) -> str | None:
        now = self._clock()
        self._samples.append((now, stats.bytes_transferred))
        # Keep the newest sample that is at least a full window old as the
        # window's anchor; everything older is dead weight.
        while len(self._samples) > 1 and now - self._samples[1][0] >= self._window:
            self._samples.popleft()
        anchor_at, anchor_bytes = self._samples[0]
        span = now - anchor_at
        if span < self._window:
            return None
        remaining = stats.bytes_total - stats.bytes_transferred
        if stats.bytes_total <= 0 or remaining < self._min_remaining:
            return None
        rate = (stats.bytes_transferred - anchor_bytes) / span
        if rate >= self._floor:
            return None
        return (
            f"upload session throttled to {rate / _MIB:.2f} MB/s over the last "
            f"{int(span)}s with {remaining / _MIB:.0f} MB left"
        )


class GoogleDriveRclone:
    """One-process delta sync of a staged export tree into a Drive folder."""

    @classmethod
    def binary(cls) -> str | None:
        return find_binary()

    @classmethod
    def ensure_available(cls) -> None:
        if cls.binary() is None:
            raise GoogleDriveRcloneError(_INSTALL_HINT)

    @classmethod
    def _build_cmd(cls, verb: str, stage_dir: Path, *, delete: bool) -> list[str]:
        cls.ensure_available()
        binary = cls.binary() or ""

        cmd = [
            binary,
            verb,
            str(stage_dir),
            ":drive:",
            "--copy-links",
            # Drive stores MD5s server-side: checksum comparison skips
            # identical files regardless of modtimes (staged trees get fresh
            # mtimes every run) and works against files uploaded by the old
            # googleapiclient path too.
            "--checksum",
            "--drive-skip-gdocs",
        ]
        if delete:
            cmd.append("--delete-during")
        cmd += [
            "--config",
            "/dev/null",
            "--transfers",
            str(max(1, settings.drive_rclone_transfers)),
            "--checkers",
            "8",
            "--drive-chunk-size",
            f"{max(8, settings.drive_rclone_chunk_mb)}M",
            "--drive-upload-cutoff",
            "64M",
            "--drive-pacer-min-sleep",
            "100ms",
            "--drive-pacer-burst",
            "200",
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
        return cmd

    @classmethod
    async def _build_env(cls, folder_id: str) -> dict[str, str]:
        env = dict(os.environ)
        env["RCLONE_DRIVE_CLIENT_ID"] = str(settings.drive_google_client_id or "")
        env["RCLONE_DRIVE_CLIENT_SECRET"] = str(
            settings.drive_google_client_secret or ""
        )
        # Token refresh is a blocking HTTP call — keep it off the event loop.
        env["RCLONE_DRIVE_TOKEN"] = await asyncio.to_thread(
            GoogleDriveService.rclone_token_json
        )
        env["RCLONE_DRIVE_ROOT_FOLDER_ID"] = folder_id
        if settings.google_drive_team_drive_id:
            env["RCLONE_DRIVE_TEAM_DRIVE"] = settings.google_drive_team_drive_id
        return env

    @classmethod
    def _slow_stream_guard(cls) -> _SlowStreamGuard | None:
        if not settings.drive_slow_stream_restart_enabled:
            return None
        return _SlowStreamGuard(
            floor_bytes_per_sec=float(settings.drive_slow_stream_min_mb_per_sec) * _MIB,
            window_seconds=float(settings.drive_slow_stream_window_seconds),
            min_remaining_bytes=int(settings.drive_slow_stream_min_remaining_mb) * _MIB,
        )

    @classmethod
    async def _run(
        cls,
        cmd: list[str],
        folder_id: str,
        *,
        stats_callback: StatsCallback | None,
        error_prefix: str,
        on_restart: RestartCallback | None = None,
    ) -> None:
        env = await cls._build_env(folder_id)
        guard = cls._slow_stream_guard()
        max_restarts = max(0, int(settings.drive_slow_stream_max_restarts))
        restarts = 0
        while True:
            try:
                await run_rclone(
                    cmd,
                    env=env,
                    stats_callback=stats_callback,
                    error_prefix=error_prefix,
                    should_restart=guard.check if guard is not None else None,
                )
                return
            except RcloneRestartRequested as exc:
                restarts += 1
                logger.warning(
                    "%s: restarting transfer (%d/%d): %s",
                    error_prefix,
                    restarts,
                    max_restarts,
                    exc.reason,
                )
                if on_restart is not None:
                    on_restart(exc.reason)
                if restarts >= max_restarts:
                    # Last attempt runs unguarded: whatever session it gets
                    # is allowed to finish.
                    guard = None
                elif guard is not None:
                    guard.reset()
                # The access token may have aged during a long crawl.
                env = await cls._build_env(folder_id)
            except GoogleDriveRcloneError:
                raise
            except RcloneError as exc:
                raise GoogleDriveRcloneError(str(exc)) from exc

    @classmethod
    async def sync_tree(
        cls,
        stage_dir: Path,
        *,
        folder_id: str,
        stats_callback: StatsCallback | None = None,
        on_restart: RestartCallback | None = None,
    ) -> None:
        cmd = cls._build_cmd("sync", stage_dir, delete=True)
        await cls._run(
            cmd,
            folder_id,
            stats_callback=stats_callback,
            error_prefix="rclone drive sync",
            on_restart=on_restart,
        )

    @classmethod
    async def copy_tree(
        cls,
        stage_dir: Path,
        *,
        folder_id: str,
        stats_callback: StatsCallback | None = None,
        on_restart: RestartCallback | None = None,
    ) -> None:
        """Additive copy into a folder shared with other writers.

        ``copy`` never deletes remote siblings — required for the shared
        sources folder, where a ``sync`` scoped to it would wipe every file
        the staged tree doesn't contain.
        """
        cmd = cls._build_cmd("copy", stage_dir, delete=False)
        await cls._run(
            cmd,
            folder_id,
            stats_callback=stats_callback,
            error_prefix="rclone drive copy",
            on_restart=on_restart,
        )
