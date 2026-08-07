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
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..config import settings
from .google_drive_service import GoogleDriveService
from .rclone_runner import (
    RcloneError,
    StatsCallback,
    find_binary,
    run_rclone,
)

logger = logging.getLogger("uvicorn.error")

_INSTALL_HINT = (
    "rclone is required for Google Drive exports but was not found on PATH. "
    "Install it first (Arch: sudo pacman -S rclone)."
)


class GoogleDriveRcloneError(RcloneError):
    """Raised when the Drive rclone subprocess fails or rclone is missing."""


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
    async def sync_tree(
        cls,
        stage_dir: Path,
        *,
        folder_id: str,
        stats_callback: StatsCallback | None = None,
    ) -> None:
        cls.ensure_available()
        binary = cls.binary() or ""

        cmd = [
            binary,
            "sync",
            str(stage_dir),
            ":drive:",
            "--copy-links",
            # Drive stores MD5s server-side: checksum comparison skips
            # identical files regardless of modtimes (staged trees get fresh
            # mtimes every run) and works against files uploaded by the old
            # googleapiclient path too.
            "--checksum",
            "--drive-skip-gdocs",
            "--delete-during",
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

        try:
            await run_rclone(
                cmd,
                env=env,
                stats_callback=stats_callback,
                error_prefix="rclone drive sync",
            )
        except GoogleDriveRcloneError:
            raise
        except RcloneError as exc:
            raise GoogleDriveRcloneError(str(exc)) from exc
