from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from zoneinfo import ZoneInfo
import logging
import re
import shutil
import tempfile
import threading
import os
import time

import httpx

from ..config import settings
from ..library_types import coerce_library_type
from ..utils.video_color import ensure_bt709_tags
from ..models import Project
from .account_service import AccountConfig, AccountService
from .discord_service import DiscordService
from .drive_prewarm_service import DrivePrewarmService
from .drive_shared_sources import DriveSharedSources
from .export_service import ExportService
from .google_drive_service import (
    DriveVideoMetadataLookupError,
    GoogleDriveService,
)
from .metadata import MetadataService
from .meta_token_service import MetaTokenService
from .music_config_service import MusicConfigService
from .platform_reschedule_service import PlatformRescheduleService
from .post_for_me_client import (
    PostForMeClient,
    PostForMeError,
    build_post_body,
)
from .project_service import ProjectService
from .scheduling_service import SchedulingService
from .social_upload_service import PlatformUploadResult, SocialUploadService
from .thumbnail_service import ThumbnailService

logger = logging.getLogger("uvicorn.error")

# Rendered by the CEP panel next to output.mp4 (only for projects whose music
# is copyrighted) and consumed solely by the copyright-replacement flow below.
NO_MUSIC_WAV_FILENAME = "output_no_music.wav"


class PendingProjectDeletionRequiresConfirmation(ValueError):
    def __init__(self, project_id: str, platforms: list[str]):
        super().__init__("Scheduled project deletion requires explicit confirmation")
        self.project_id = project_id
        self.platforms = platforms


class UploadPreflightUnavailableError(RuntimeError):
    """A transient dependency prevented a safe upload preflight decision."""


@dataclass
class UploadReadiness:
    status: str  # green | orange | red
    metadata_exists: bool
    drive_video_count: int
    drive_video_id: str | None
    drive_video_name: str | None
    drive_video_web_url: str | None
    reasons: list[str]
    drive_folder_id: str | None
    drive_folder_url: str | None


def _vps_publish_errors(project: "Project") -> list[str]:
    """VPS-synced terminal publish failures (IG/TT), as display strings."""
    result = project.upload_last_result
    platforms = result.get("platforms") if isinstance(result, dict) else None
    if not isinstance(platforms, list):
        return []
    errors: list[str] = []
    for entry in platforms:
        if (
            isinstance(entry, dict)
            and entry.get("source") == "vps"
            and entry.get("status") == "failed"
        ):
            detail = entry.get("detail") or "publication échouée"
            errors.append(f"{entry.get('platform')}: {detail}")
    return errors


def _uploaded_fields(project: "Project") -> dict[str, Any]:
    """Return uploaded + uploaded_status based on scheduled_at vs now.

    A VPS-reported publish failure (IG/TT gave up after all retries)
    overrides the clock-based status with "publish_error"."""
    publish_errors = _vps_publish_errors(project)
    has_discord = bool(project.final_upload_discord_message_id)
    scheduled_at = project.scheduled_at
    if scheduled_at is not None:
        now = datetime.now(tz=timezone.utc)
        is_live = scheduled_at <= now
        if is_live:
            status = "green"
        elif has_discord:
            status = "orange"  # scheduled, not yet published
        else:
            status = "red"
        if publish_errors:
            status = "publish_error"
        return {
            "uploaded": is_live,
            "uploaded_status": status,
            "publish_error_detail": "; ".join(publish_errors) or None,
        }
    # No scheduling: rely on discord message presence (immediate publish)
    status = "green" if has_discord else "red"
    if publish_errors:
        status = "publish_error"
    return {
        "uploaded": has_discord,
        "uploaded_status": status,
        "publish_error_detail": "; ".join(publish_errors) or None,
    }


def _upload_locked(project: "Project") -> bool:
    """True when the manager's Upload action is disabled for this project.

    Mirrors the frontend rule (`uploaded_status !== "red"`): the project is
    already posted, or scheduled with a dispatched upload. Drive readiness is
    irrelevant to such rows, so the manager view skips their Drive lookups.
    """
    return _uploaded_fields(project)["uploaded_status"] != "red"


# Drive file ids as they appear in webViewLink (/file/d/<id>/view) and in the
# direct-download URL (?id=<id>&export=download).
_DRIVE_FILE_ID_PATTERNS = (
    re.compile(r"/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
)


def _persisted_drive_video(project: "Project") -> dict[str, Any] | None:
    """Final-video Drive info recovered from the persisted upload result.

    Uploads persist `drive_video_id`/`drive_video_name` explicitly; older
    projects only stored the video URLs, so fall back to extracting the file
    id from them.
    """
    result = project.upload_last_result or {}
    video_id = result.get("drive_video_id")
    web_url = result.get("drive_video_url")
    if not video_id:
        for url in (web_url, result.get("direct_drive_download")):
            if not url:
                continue
            for pattern in _DRIVE_FILE_ID_PATTERNS:
                match = pattern.search(str(url))
                if match:
                    video_id = match.group(1)
                    break
            if video_id:
                break
    if not video_id:
        return None
    return {
        "id": video_id,
        "name": result.get("drive_video_name"),
        "webViewLink": web_url if web_url and "/file/d/" in str(web_url) else None,
    }


def _dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for filename in files:
            candidate = Path(root) / filename
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
    return total


class UploadPhaseService:
    """Project manager view, upload execution, and managed delete flow."""
    _SUPPORTED_PLATFORMS = ("youtube", "facebook", "instagram")
    _INSTAGRAM_DRIVE_FILENAME = "output_instagram.mp4"
    _FACEBOOK_DRIVE_FILENAME = "facebook_upload.mp4"
    # Meta's scheduled_publish_time window (~30 days documented; 29 kept as a
    # safety margin). Targets beyond it are deferred to the /server hold.
    _FACEBOOK_NATIVE_HORIZON_DAYS = 29
    _FRENCH_TZ = ZoneInfo("Europe/Paris")
    _TIKTOK_NOT_CONFIGURED_DETAIL = "No Post for Me account configured for this account"
    # Manual TikTok mode (2026-08): account has TikTok slots but no Post for
    # Me id. The VPS job row starts "pending"; the server posts a Discord
    # reminder at T-5 and the ✅ reaction flips it to "uploaded".
    _TIKTOK_MANUAL_DETAIL = "Post manuel — en attente du ✅ Discord"
    _drive_video_cache: dict[str, dict[str, Any]] = {}
    _DRIVE_VIDEO_CACHE_TTL_SECONDS = 300.0
    _DRIVE_BATCH_LOOKUP_MAX_ATTEMPTS = 3

    @classmethod
    def _cache_drive_video(
        cls,
        *,
        project_id: str,
        folder_id: str | None,
        folder_url: str | None,
        video_files: list[dict[str, Any]],
    ) -> None:
        if not folder_id:
            cls._drive_video_cache.pop(project_id, None)
            return
        if len(video_files) != 1:
            cls._drive_video_cache.pop(project_id, None)
            return
        video = video_files[0]
        cls._drive_video_cache[project_id] = {
            "id": video.get("id"),
            "name": video.get("name"),
            "webViewLink": video.get("webViewLink"),
            "folder_id": folder_id,
            "folder_url": folder_url,
            "cached_at": time.monotonic(),
        }

    @classmethod
    def _cached_drive_video(
        cls,
        *,
        project_id: str,
        folder_id: str | None,
    ) -> dict[str, Any] | None:
        cached = cls._drive_video_cache.get(project_id)
        if not cached:
            return None
        cached_at = cached.get("cached_at")
        if not isinstance(cached_at, (int, float)) or (
            time.monotonic() - cached_at > cls._DRIVE_VIDEO_CACHE_TTL_SECONDS
        ):
            cls._drive_video_cache.pop(project_id, None)
            return None
        cached_folder_id = cached.get("folder_id")
        if folder_id and cached_folder_id and cached_folder_id != folder_id:
            return None
        if not cached.get("id"):
            return None
        return {
            "id": cached.get("id"),
            "name": cached.get("name"),
            "webViewLink": cached.get("webViewLink"),
        }

    @classmethod
    def _resolve_drive_folder(
        cls,
        project: Project,
        *,
        folder_candidates_by_name: dict[str, dict[str, Any]] | None = None,
        resolve_remote_url: bool = True,
    ) -> tuple[str | None, str | None]:
        if not GoogleDriveService.is_configured():
            return None, None
        if project.drive_folder_id:
            if project.drive_folder_url:
                return project.drive_folder_id, project.drive_folder_url
            if not resolve_remote_url:
                return project.drive_folder_id, f"https://drive.google.com/drive/folders/{project.drive_folder_id}"
            try:
                url = GoogleDriveService.get_web_view_url(project.drive_folder_id)
                return project.drive_folder_id, url
            except Exception:
                return project.drive_folder_id, f"https://drive.google.com/drive/folders/{project.drive_folder_id}"

        if folder_candidates_by_name is not None:
            found = folder_candidates_by_name.get(ExportService.output_folder_name(project))
            if not found:
                return None, None
            folder_id = found["id"]
            folder_url = found.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"
            return folder_id, folder_url

        found = GoogleDriveService.find_project_folder_by_name(ExportService.output_folder_name(project))
        if not found:
            return None, None
        return found["id"], found.get("webViewLink")

    @classmethod
    def _resolve_drive_folder_offline(
        cls,
        project: Project,
        folder_candidates_by_name: dict[str, dict[str, Any]] | None,
    ) -> tuple[str | None, str | None]:
        """Folder id/url from persisted project data or an already-fetched
        folder listing — never issues a Drive call."""
        if project.drive_folder_id:
            url = project.drive_folder_url or (
                f"https://drive.google.com/drive/folders/{project.drive_folder_id}"
            )
            return project.drive_folder_id, url
        if folder_candidates_by_name:
            found = folder_candidates_by_name.get(ExportService.output_folder_name(project))
            if found:
                folder_id = found["id"]
                folder_url = found.get("webViewLink") or (
                    f"https://drive.google.com/drive/folders/{folder_id}"
                )
                return folder_id, folder_url
        return None, None

    @classmethod
    def _build_readiness(
        cls,
        *,
        metadata_exists: bool,
        folder_id: str | None,
        folder_url: str | None,
        video_files: list[dict[str, Any]],
        video_lookup_failed: bool = False,
    ) -> UploadReadiness:
        reasons: list[str] = []
        if not folder_id:
            reasons.append("no output video found")

        video_count = len(video_files)
        drive_video = video_files[0] if video_count == 1 else None

        if not metadata_exists:
            reasons.append("no metadata found")
        if video_count == 0:
            if video_lookup_failed and folder_id:
                reasons.append("unable to verify output video in Drive")
            else:
                reasons.append("no output video found")
        elif video_count > 1:
            reasons.append("more than one output video found (conflicting)")

        if metadata_exists and video_count == 1:
            status = "green"
        elif metadata_exists or video_count == 1:
            status = "orange"
        else:
            status = "red"

        return UploadReadiness(
            status=status,
            metadata_exists=metadata_exists,
            drive_video_count=video_count,
            drive_video_id=drive_video.get("id") if drive_video else None,
            drive_video_name=drive_video.get("name") if drive_video else None,
            drive_video_web_url=drive_video.get("webViewLink") if drive_video else None,
            reasons=sorted(set(reasons)),
            drive_folder_id=folder_id,
            drive_folder_url=folder_url,
        )

    @classmethod
    def compute_readiness(cls, project: Project) -> UploadReadiness:
        metadata_exists = ProjectService.get_metadata_file(project.id).exists()

        folder_id, folder_url = cls._resolve_drive_folder(project)

        video_files: list[dict[str, Any]] = []
        video_lookup_failed = False
        if folder_id:
            try:
                video_files = ExportService.detect_upload_video_in_drive_root(folder_id)
                cls._cache_drive_video(
                    project_id=project.id,
                    folder_id=folder_id,
                    folder_url=folder_url,
                    video_files=video_files,
                )
            except Exception as exc:
                logger.warning(
                    "Drive video lookup failed during upload readiness: project_id=%s folder_id=%s error=%s",
                    project.id,
                    folder_id,
                    exc,
                )
                video_lookup_failed = True
                cached_video = cls._cached_drive_video(
                    project_id=project.id,
                    folder_id=folder_id,
                )
                if cached_video is not None:
                    video_files = [cached_video]
                else:
                    video_files = []

        return cls._build_readiness(
            metadata_exists=metadata_exists,
            folder_id=folder_id,
            folder_url=folder_url,
            video_files=video_files,
            video_lookup_failed=video_lookup_failed,
        )

    @classmethod
    def _compute_preflight_readiness(cls, project: Project) -> UploadReadiness:
        """Reuse the manager's recent Drive result before issuing another query."""
        metadata_exists = ProjectService.get_metadata_file(project.id).exists()

        folder_id, folder_url = cls._resolve_drive_folder_offline(project, None)
        cached_video = _persisted_drive_video(project) or cls._cached_drive_video(
            project_id=project.id,
            folder_id=folder_id,
        )
        if cached_video is not None:
            return cls._build_readiness(
                metadata_exists=metadata_exists,
                folder_id=folder_id,
                folder_url=folder_url,
                video_files=[cached_video],
            )

        # Direct API calls and expired manager caches still perform one live
        # verification. compute_readiness caches the result for the remaining
        # platform checks in this preflight sequence.
        return cls.compute_readiness(project)

    @classmethod
    def list_manager_rows(cls) -> list[dict[str, Any]]:
        projects = ProjectService.list_all()
        # Upload-locked rows (already posted, or scheduled with a dispatched
        # upload) render with the Upload button disabled, so their Drive
        # readiness is never shown: keep them out of the batch video lookup
        # and serve their video/folder info from persisted data instead.
        upload_locked: dict[str, bool] = {
            project.id: _upload_locked(project) for project in projects
        }
        folder_candidates_by_name: dict[str, dict[str, Any]] = {}
        folder_listing_ok = False
        drive_root_videos: dict[str, list[dict[str, Any]]] = {}
        drive_batch_lookup_failed = False
        if GoogleDriveService.is_configured():
            for attempt in range(1, cls._DRIVE_BATCH_LOOKUP_MAX_ATTEMPTS + 1):
                try:
                    drive = GoogleDriveService.client()
                    folder_candidates_by_name = GoogleDriveService.list_project_folders_under_parent(drive=drive)
                    folder_ids: list[str] = []
                    for project in projects:
                        if upload_locked[project.id]:
                            continue
                        folder_id, _ = cls._resolve_drive_folder(
                            project,
                            folder_candidates_by_name=folder_candidates_by_name,
                            resolve_remote_url=False,
                        )
                        if folder_id:
                            folder_ids.append(folder_id)
                    drive_root_videos = GoogleDriveService.list_root_video_files_by_parent_ids(
                        folder_ids,
                        ExportService.VIDEO_EXTENSIONS,
                        drive=drive,
                    )
                    drive_root_videos = {
                        folder_id: ExportService.filter_upload_video_candidates(files)
                        for folder_id, files in drive_root_videos.items()
                    }
                    drive_batch_lookup_failed = False
                    folder_listing_ok = True
                    break
                except Exception as exc:
                    drive_batch_lookup_failed = True
                    logger.warning(
                        "Project manager Drive batch lookup failed: attempt=%d/%d error=%s",
                        attempt,
                        cls._DRIVE_BATCH_LOOKUP_MAX_ATTEMPTS,
                        exc,
                    )
                    GoogleDriveService.reset_client()
                    if attempt >= cls._DRIVE_BATCH_LOOKUP_MAX_ATTEMPTS:
                        folder_candidates_by_name = {}
                        drive_root_videos = {}
                        break
                    time.sleep(min(0.25 * attempt, 0.75))

        def _build_row(project: Project) -> dict[str, Any]:
            if upload_locked[project.id]:
                readiness = cls._locked_manager_readiness(
                    project, folder_candidates_by_name
                )
            else:
                folder_id, folder_url = cls._resolve_drive_folder(
                    project,
                    folder_candidates_by_name=folder_candidates_by_name if folder_listing_ok else None,
                    resolve_remote_url=False,
                )
                video_files = drive_root_videos.get(folder_id or "", [])
                if drive_batch_lookup_failed and folder_id and not video_files:
                    cached_video = cls._cached_drive_video(
                        project_id=project.id,
                        folder_id=folder_id,
                    )
                    if cached_video is not None:
                        video_files = [cached_video]
                if video_files or not drive_batch_lookup_failed:
                    cls._cache_drive_video(
                        project_id=project.id,
                        folder_id=folder_id,
                        folder_url=folder_url,
                        video_files=video_files,
                    )
                readiness = cls._build_readiness(
                    metadata_exists=ProjectService.get_metadata_file(project.id).exists(),
                    folder_id=folder_id,
                    folder_url=folder_url,
                    video_files=video_files,
                    video_lookup_failed=drive_batch_lookup_failed,
                )
            return cls._manager_row_payload(project, readiness)

        if not projects:
            return []

        max_workers = max(1, min(8, len(projects)))
        rows: list[dict[str, Any] | None] = [None] * len(projects)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(_build_row, project): index
                for index, project in enumerate(projects)
            }
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                rows[idx] = future.result()
        return [row for row in rows if row is not None]

    @classmethod
    def _locked_manager_readiness(
        cls,
        project: Project,
        folder_candidates_by_name: dict[str, dict[str, Any]] | None,
    ) -> UploadReadiness:
        """Readiness for an upload-locked row, resolved without touching Drive.

        Locked rows render with the Upload button disabled, so their Drive
        readiness is never surfaced: answer from persisted data (folder link,
        preview video id) instead of issuing a lookup.
        """
        folder_id, folder_url = cls._resolve_drive_folder_offline(
            project, folder_candidates_by_name
        )
        video = _persisted_drive_video(project) or cls._cached_drive_video(
            project_id=project.id,
            folder_id=folder_id,
        )
        return cls._build_readiness(
            metadata_exists=ProjectService.get_metadata_file(project.id).exists(),
            folder_id=folder_id,
            folder_url=folder_url,
            video_files=[video] if video else [],
            video_lookup_failed=video is None,
        )

    @classmethod
    def get_manager_row(cls, project_id: str) -> dict[str, Any] | None:
        """Build a single manager row without the all-projects Drive sweep.

        The manager refreshes one row after an upload reaches a terminal state.
        By then the project is upload-locked, so its readiness comes from
        persisted data and the call costs no Drive requests at all. A row that
        is still unlocked falls back to the single-project live lookup, which
        is still one project's worth of work instead of the whole list's.
        """
        project = ProjectService.load(project_id)
        if project is None:
            return None
        readiness = (
            cls._locked_manager_readiness(project, None)
            if _upload_locked(project)
            else cls.compute_readiness(project)
        )
        return cls._manager_row_payload(project, readiness)

    @classmethod
    def _manager_row_payload(
        cls, project: Project, readiness: UploadReadiness
    ) -> dict[str, Any]:
        project_dir = ProjectService.get_project_dir(project.id)
        return {
            "project_id": project.id,
            "anime_title": project.anime_name,
            "library_type": project.library_type.value,
            "language": project.output_language,
            "local_size_bytes": _dir_size(project_dir) if project_dir.exists() else 0,
            **_uploaded_fields(project),
            "can_upload_status": readiness.status,
            "can_upload_reasons": readiness.reasons,
            "has_metadata": readiness.metadata_exists,
            "drive_video_count": readiness.drive_video_count,
            "drive_video_name": readiness.drive_video_name,
            "drive_video_web_url": readiness.drive_video_web_url,
            "drive_folder_id": readiness.drive_folder_id,
            "drive_folder_url": readiness.drive_folder_url,
            "drive_video_id": readiness.drive_video_id,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "scheduled_at": project.scheduled_at.isoformat() if project.scheduled_at else None,
            "scheduled_account_id": project.scheduled_account_id,
            "mother_project_id": project.mother_project_id,
            "platform_schedules": {
                platform: {
                    "slot": ps.slot.isoformat(),
                    "scheduled_at": ps.scheduled_at.isoformat(),
                }
                for platform, ps in (project.platform_schedules or {}).items()
            },
            "llm_preset_resolved": project.resolved_llm_preset_key(),
            "llm_preset_is_default": project.llm_preset is None,
            "template_resolved": project.resolved_template_key(),
            "template_is_default": project.template is None,
            "min_playback_speed_resolved": project.resolved_min_playback_speed(),
            "min_playback_speed_is_default": project.min_playback_speed is None,
        }

    _FRENCH_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    _FRENCH_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
    _PLATFORM_DISPLAY = {
        "youtube": "__Youtube__",
        "facebook": "__Facebook__",
        "instagram": "__Instagram__",
    }

    @classmethod
    def _format_french_datetime(cls, dt: datetime) -> str:
        aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        french_dt = aware.astimezone(cls._FRENCH_TZ)
        day_name = cls._FRENCH_DAYS[french_dt.weekday()]
        month_name = cls._FRENCH_MONTHS[french_dt.month - 1]
        return (
            f"{day_name} {french_dt.day} {month_name} {french_dt.year} "
            f"à {french_dt.strftime('%H:%M')}"
        )

    @classmethod
    def _compute_upfront_skips(
        cls,
        requested_platforms: tuple[str, ...],
        account: AccountConfig | None,
    ) -> dict[str, PlatformUploadResult]:
        """Determine which requested platforms are known to be unrunnable upfront.

        Mirrors the configuration checks in ``execute_upload`` that decide whether
        each platform gets a job: if a platform cannot run at all, we seed a
        ``"skipped"`` result now so the early Discord message (posted before the
        parallel upload phase) already reflects it.
        """
        skips: dict[str, PlatformUploadResult] = {}
        default_detail = "Platform is not configured for this upload context"
        for platform in requested_platforms:
            reason: str | None = None
            if platform == "youtube":
                if account is not None and (
                    account.youtube is None or not account.youtube.refresh_token
                ):
                    reason = default_detail
            elif platform == "facebook":
                if account is not None and account.meta is None:
                    reason = default_detail
            elif platform == "instagram":
                if account is not None and account.meta is None:
                    reason = default_detail
            elif platform == "tiktok":
                # Explicitly requested TikTok: a Post for Me id publishes
                # automatically; without one the account is in manual mode
                # (Discord reminder + ✅), never silently skipped.
                if account is not None and not (
                    account.tiktok is not None
                    and account.tiktok.post_for_me_account_id
                ):
                    skips[platform] = cls._tiktok_manual_result()
            if reason is not None:
                skips[platform] = PlatformUploadResult(
                    platform=platform,
                    status="skipped",
                    detail=reason,
                )
        return skips

    @classmethod
    def _tiktok_manual_result(cls) -> PlatformUploadResult:
        """Seed row for a manual-mode account: pending until the Discord ✅."""
        return PlatformUploadResult(
            platform="tiktok", status="pending", detail=cls._TIKTOK_MANUAL_DETAIL
        )

    @classmethod
    def _build_tiktok_payload(
        cls,
        account: AccountConfig | None,
        tiktok_description: str,
        thumbnail_timestamp_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Payload for the VPS server's Post for Me publish (see server TikTokPayload)."""
        if account is None or account.tiktok is None:
            return None
        tiktok = account.tiktok
        if not tiktok.post_for_me_account_id:
            return None
        payload: dict[str, Any] = {
            "social_account_id": tiktok.post_for_me_account_id,
            "post_for_me_platform": tiktok.post_for_me_platform,
            "caption": tiktok_description,
            "privacy_status": tiktok.privacy_status,
            "allow_comment": tiktok.allow_comment,
            "allow_duet": tiktok.allow_duet,
            "allow_stitch": tiktok.allow_stitch,
        }
        if thumbnail_timestamp_ms is not None:
            payload["thumbnail_timestamp_ms"] = int(thumbnail_timestamp_ms)
        return payload

    @classmethod
    def _attach_tiktok_cover(
        cls,
        tiktok_payload: dict[str, Any] | None,
        cover_drive_url: str | None,
    ) -> None:
        """Mutates payload in place: a hosted cover image only makes sense for
        the `tiktok_business` Post for Me connector (personal accounts keep
        the timestamp-only fallback). No-op on a missing payload or URL."""
        if tiktok_payload is None or cover_drive_url is None:
            return
        if tiktok_payload.get("post_for_me_platform") != "tiktok_business":
            return
        tiktok_payload["thumbnail_url"] = cover_drive_url

    @classmethod
    def _instagram_thumb_offset(
        cls,
        thumbnail_timestamp_ms: int,
        speed_factor: str | float | None,
        max_duration_seconds: float,
    ) -> int:
        """Map an original-video timestamp to the prepared IG artifact.

        The IG Drive artifact may be sped up (speed_factor > 1) or cut at
        max_duration_seconds; the Graph API thumb_offset must land inside it.
        """
        try:
            speed = float(speed_factor) if speed_factor is not None else 1.0
        except (TypeError, ValueError):
            speed = 1.0
        if not speed or speed <= 0:
            speed = 1.0
        offset = int(round(thumbnail_timestamp_ms / speed))
        ceiling = max(0, int(max_duration_seconds * 1000) - 500)
        return max(0, min(offset, ceiling))

    @classmethod
    def _vps_platforms(
        cls,
        requested_platforms: tuple[str, ...],
        account: AccountConfig | None,
        tiktok_payload: dict[str, Any] | None,
    ) -> list[str]:
        """Platforms recorded on the VPS job (= the Discord embed's rows).

        Every targeted platform joins the list so the embed shows a row (with
        its post URL pushed via update_job_platform as results land). Since
        the 2026-08 PFM migration the server only DISPATCHES platforms whose
        payload is present on the job: TikTok is backend-published (its
        payload is never sent — see _publish_tiktok_via_pfm), and an
        urgent-immediate Instagram row arrives without a payload too. A
        manual-mode TikTok row (tiktok_manual on the job) is the one server
        action left for TikTok: the T-5 Discord reminder."""
        platforms = list(requested_platforms)
        if "tiktok" in platforms:
            return platforms
        if cls._tiktok_enrolled(account, tiktok_payload):
            platforms.append("tiktok")
        return platforms

    @classmethod
    def _tiktok_enrolled(
        cls,
        account: AccountConfig | None,
        tiktok_payload: dict[str, Any] | None,
    ) -> bool:
        """Whether this upload targets TikTok (PFM publish or manual post).

        Same rule as the slot reservation (project_upload_service
        _platforms_to_reserve): a payload exists, or the account has TikTok
        slots — with or without an explicit `tiktok:` block. Without a Post
        for Me id the account is in manual mode (owner decision 2026-08-24)."""
        if tiktok_payload is not None:
            return True
        return account is not None and account.tiktok_mode() is not None

    @classmethod
    def _publish_tiktok_via_pfm(
        cls,
        *,
        project: Project,
        payload: dict[str, Any],
        video_path: Path,
        scheduled_at: datetime | None,
        wait_for_result: bool = False,
    ) -> PlatformUploadResult:
        """Stage media + create the PFM post (scheduled or instant).

        Mutates and persists `project.tiktok_pfm` at every stage transition so
        a crash never orphans a live PFM post without local state. With
        `wait_for_result` (urgent-immediate), blocks on the publish outcome
        (bounded poll); otherwise the post is left scheduled/processing and
        PfmStatusSyncService picks the result up later.
        """
        from ..models.project import TikTokPfmState

        social_account_id = str(payload["social_account_id"])
        pfm_platform = str(payload.get("post_for_me_platform") or "tiktok")
        caption = str(payload.get("caption") or "")
        state = TikTokPfmState(
            social_account_id=social_account_id,
            post_for_me_platform=pfm_platform,
            scheduled_at=scheduled_at,
            caption=caption,
            privacy_status=str(payload.get("privacy_status") or "public"),
            allow_comment=bool(payload.get("allow_comment", True)),
            allow_duet=bool(payload.get("allow_duet", True)),
            allow_stitch=bool(payload.get("allow_stitch", True)),
        )

        def persist_state() -> None:
            project.tiktok_pfm = state
            try:
                ProjectService.save(project)
            except Exception:
                logger.warning(
                    "Failed to persist tiktok_pfm state for %s", project.id, exc_info=True
                )

        def failed(detail: str) -> PlatformUploadResult:
            state.stage = "failed"
            state.last_error = detail
            persist_state()
            return PlatformUploadResult(
                platform="tiktok", status="failed", detail=detail
            )

        try:
            state.media_url = PostForMeClient.stage_media(video_path)
        except PostForMeError as exc:
            return failed(f"PFM media staging failed: {exc.detail}")
        state.stage = "media_uploaded"
        persist_state()

        body = build_post_body(
            social_account_id=social_account_id,
            media_url=state.media_url,
            caption=caption,
            post_for_me_platform=pfm_platform,
            privacy_status=state.privacy_status,
            allow_comment=state.allow_comment,
            allow_duet=state.allow_duet,
            allow_stitch=state.allow_stitch,
            scheduled_at=scheduled_at,
        )
        try:
            state.post_id = PostForMeClient.create_post(body)
        except PostForMeError as exc:
            return failed(f"PFM post creation failed: {exc.detail}")
        state.stage = "post_scheduled" if scheduled_at is not None else "post_created"
        persist_state()

        if wait_for_result:
            outcome = PostForMeClient.poll_outcome(state.post_id, social_account_id)
            state.last_polled_at = datetime.now(timezone.utc)
            if outcome.success:
                state.stage = "published"
                state.url = outcome.url
                persist_state()
                return PlatformUploadResult(
                    platform="tiktok", status="uploaded", url=outcome.url
                )
            if outcome.detail and "resumable=true" in outcome.detail:
                # Poll timeout: PFM is still processing — not terminal.
                state.last_error = outcome.detail
                persist_state()
                return PlatformUploadResult(
                    platform="tiktok",
                    status="uploaded",
                    detail="Publishing via Post for Me (result pending)",
                )
            return failed(f"TikTok publish failed: {outcome.detail}")

        if scheduled_at is not None:
            detail = (
                f"Scheduled via Post for Me at {scheduled_at.isoformat()} "
                f"(post {state.post_id})"
            )
        else:
            detail = f"Publishing via Post for Me (post {state.post_id})"
        return PlatformUploadResult(platform="tiktok", status="uploaded", detail=detail)

    @classmethod
    def _publish_instagram_immediate(
        cls,
        *,
        payload: dict[str, Any],
        video_path: Path | None,
        video_url: str | None,
    ) -> PlatformUploadResult:
        """Urgent-immediate Instagram publish (backend-side Graph API)."""
        from .instagram_immediate_service import InstagramImmediateService

        result = InstagramImmediateService.publish_now(
            ig_user_id=str(payload.get("ig_user_id") or ""),
            ig_access_token=str(payload.get("ig_access_token") or ""),
            caption=str(payload.get("caption") or ""),
            video_path=video_path,
            video_url=video_url,
            graph_api_version=str(
                payload.get("graph_api_version") or settings.meta_graph_api_version
            ),
        )
        if result.success:
            return PlatformUploadResult(
                platform="instagram", status="uploaded", url=result.permalink
            )
        return PlatformUploadResult(
            platform="instagram",
            status="failed",
            detail=f"Immediate Instagram publish failed: {result.detail}",
        )

    @classmethod
    def _normalize_platforms(cls, platforms: list[str] | None) -> tuple[str, ...]:
        if platforms is None:
            return cls._SUPPORTED_PLATFORMS
        normalized: list[str] = []
        for platform in platforms:
            key = str(platform).strip().lower()
            if not key:
                continue
            if key not in cls._SUPPORTED_PLATFORMS:
                raise ValueError(
                    f"Unsupported platform '{platform}'. "
                    f"Supported values: {', '.join(cls._SUPPORTED_PLATFORMS)}"
                )
            if key not in normalized:
                normalized.append(key)
        if not normalized:
            raise ValueError("At least one platform is required when 'platforms' is provided.")
        return tuple(normalized)

    @staticmethod
    def _platform_status_payload(
        results_by_platform: dict[str, PlatformUploadResult],
    ) -> dict[str, dict[str, Any]]:
        return {
            platform: {
                "status": result.status,
                "url": result.url,
                "detail": result.detail,
            }
            for platform, result in results_by_platform.items()
        }

    @classmethod
    def _prepare_instagram_drive_video(
        cls,
        *,
        project_id: str,
        source_video_path: Path,
        drive_folder_id: str,
        instagram_strategy: str | None,
        max_duration_seconds: float,
        work_dir: Path,
        source_drive_video: dict[str, str] | None = None,
    ) -> tuple[PlatformUploadResult | None, dict[str, str]]:
        """Produce what the VPS Instagram scheduler should ingest.

        ``source_drive_video`` (``file_id``/``direct_url``/``web_url``/
        ``filename`` of the final video on Drive) is passed only when the local
        ``source_video_path`` is that very file. When the preparation turns
        out to be a no-op (no cut/retime, container already streamable) the
        metadata then points straight at the original and NO
        ``output_instagram.mp4`` is uploaded — the dedicated artifact is
        optional; it exists only when Instagram must receive different bytes.
        """
        output_path = work_dir / cls._INSTAGRAM_DRIVE_FILENAME
        prep = SocialUploadService.prepare_instagram_video_for_drive(
            source_video_path=source_video_path,
            output_path=output_path,
            instagram_strategy=instagram_strategy,
            facebook_prep_dir=cls._facebook_prep_dir(project_id),
            max_duration_seconds=max_duration_seconds,
            allow_source_passthrough=bool(
                source_drive_video and source_drive_video.get("file_id")
            ),
        )
        if prep.status == "skip":
            return (
                PlatformUploadResult(
                    platform="instagram",
                    status="skipped",
                    detail=prep.detail,
                ),
                {},
            )
        if prep.status != "ready" or prep.video_path is None:
            return (
                PlatformUploadResult(
                    platform="instagram",
                    status="failed",
                    detail=prep.detail or "Instagram video preparation failed.",
                ),
                {},
            )

        drive = GoogleDriveService.client()
        if prep.passthrough and source_drive_video and source_drive_video.get("file_id"):
            # Nothing to re-host: Instagram gets the final video itself. Drop a
            # stale artifact from an earlier run so nobody mistakes it for the
            # current one (best-effort; the payload below never references it).
            cls._delete_stale_instagram_artifact(drive_folder_id, drive=drive)
            logger.info(
                "Instagram prep passthrough: project=%s reuses Drive final video %s (no %s upload)",
                project_id,
                source_drive_video["file_id"],
                cls._INSTAGRAM_DRIVE_FILENAME,
            )
            return (
                None,
                {
                    "instagram_drive_file_id": source_drive_video["file_id"],
                    "instagram_drive_video_url": source_drive_video["direct_url"],
                    "instagram_drive_web_url": source_drive_video.get("web_url") or "",
                    "instagram_drive_filename": source_drive_video.get("filename") or "",
                    "instagram_drive_source": "original",
                    "instagram_speed_factor": "1.0",
                    "instagram_prepared_local_path": str(prep.video_path),
                },
            )
        uploaded = GoogleDriveService.upsert_local_file(
            parent_id=drive_folder_id,
            filename=cls._INSTAGRAM_DRIVE_FILENAME,
            local_path=prep.video_path,
            chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
            drive=drive,
        )
        file_id = str(uploaded.get("id") or "").strip()
        if not file_id:
            return (
                PlatformUploadResult(
                    platform="instagram",
                    status="failed",
                    detail=f"Drive upload returned no file id for {cls._INSTAGRAM_DRIVE_FILENAME}",
                ),
                {},
            )
        GoogleDriveService.set_public_read(file_id, drive=drive)
        direct_url = GoogleDriveService.get_direct_download_url(file_id)
        web_url = str(uploaded.get("webViewLink") or "") or GoogleDriveService.get_web_view_url(file_id)
        return (
            None,
            {
                "instagram_drive_file_id": file_id,
                "instagram_drive_video_url": direct_url,
                "instagram_drive_web_url": web_url,
                "instagram_drive_filename": cls._INSTAGRAM_DRIVE_FILENAME,
                "instagram_drive_source": "prepared",
                "instagram_speed_factor": (
                    f"{prep.speed_factor}" if prep.transcoded and prep.speed_factor else "1.0"
                ),
                # tmp-dir path consumed (and popped) by the urgent-immediate
                # publish; never persisted.
                "instagram_prepared_local_path": str(prep.video_path),
            },
        )

    @classmethod
    def _delete_stale_instagram_artifact(cls, drive_folder_id: str, *, drive=None) -> None:
        try:
            for entry in GoogleDriveService.list_children_named(
                drive_folder_id, cls._INSTAGRAM_DRIVE_FILENAME, drive=drive
            ):
                file_id = str(entry.get("id") or "")
                if file_id:
                    GoogleDriveService.delete_file(file_id, drive=drive)
        except Exception:
            logger.warning(
                "Could not remove stale %s from Drive folder %s",
                cls._INSTAGRAM_DRIVE_FILENAME,
                drive_folder_id,
                exc_info=True,
            )

    @classmethod
    def _prepare_facebook_drive_video(
        cls,
        *,
        project_id: str,
        source_video_path: Path,
        drive_folder_id: str,
        facebook_strategy: str | None,
        max_duration_seconds: float,
        work_dir: Path,
    ) -> tuple[PlatformUploadResult | None, dict[str, str]]:
        """Prepare + Drive-host the Facebook artifact for a >29d server hold.

        Applies the user's duration strategy (cut / sped_up / auto) exactly
        like the native scheduled path, then uploads the ready-to-publish file
        to Drive so the /server can create the native scheduled post at T-28d.
        Mirrors _prepare_instagram_drive_video."""
        strategy = facebook_strategy or "auto"
        if strategy == "cut":
            cut_output = work_dir / f"{source_video_path.stem}.facebook_cut.mp4"
            cut_error = SocialUploadService._cut_facebook_video(
                input_path=source_video_path,
                output_path=cut_output,
                max_duration_seconds=max_duration_seconds,
            )
            if cut_error:
                return (
                    PlatformUploadResult(
                        platform="facebook",
                        status="failed",
                        detail=f"Facebook cut failed: {cut_error}",
                    ),
                    {},
                )
            prepared_video_path = cut_output
        else:
            cached = None
            if strategy == "sped_up":
                prep_dir = cls._facebook_prep_dir(project_id)
                candidate = prep_dir / "sped_up.mp4"
                cached = candidate if candidate.exists() else None
            if cached is not None:
                prepared_video_path = cached
            else:
                prep = SocialUploadService._prepare_facebook_video_for_upload(
                    source_video_path=source_video_path,
                    work_dir=work_dir,
                    max_duration_seconds=max_duration_seconds,
                )
                if prep.status == "skip":
                    return (
                        PlatformUploadResult(
                            platform="facebook", status="skipped", detail=prep.detail
                        ),
                        {},
                    )
                if prep.status != "ready" or prep.video_path is None:
                    return (
                        PlatformUploadResult(
                            platform="facebook",
                            status="failed",
                            detail=prep.detail or "Facebook video preparation failed.",
                        ),
                        {},
                    )
                prepared_video_path = prep.video_path

        media_validation_error = SocialUploadService._validate_facebook_reel_media(
            video_path=prepared_video_path,
            max_duration_seconds=max_duration_seconds,
        )
        if media_validation_error:
            return (
                PlatformUploadResult(
                    platform="facebook", status="failed", detail=media_validation_error
                ),
                {},
            )

        drive = GoogleDriveService.client()
        uploaded = GoogleDriveService.upsert_local_file(
            parent_id=drive_folder_id,
            filename=cls._FACEBOOK_DRIVE_FILENAME,
            local_path=prepared_video_path,
            chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
            drive=drive,
        )
        file_id = str(uploaded.get("id") or "").strip()
        if not file_id:
            return (
                PlatformUploadResult(
                    platform="facebook",
                    status="failed",
                    detail=(
                        f"Drive upload returned no file id for {cls._FACEBOOK_DRIVE_FILENAME}"
                    ),
                ),
                {},
            )
        GoogleDriveService.set_public_read(file_id, drive=drive)
        return (
            None,
            {
                "facebook_drive_file_id": file_id,
                "facebook_drive_video_url": GoogleDriveService.get_direct_download_url(
                    file_id
                ),
            },
        )

    @classmethod
    def execute_upload(
        cls,
        project_id: str,
        account_id: str | None = None,
        platforms: list[str] | None = None,
        facebook_strategy: str | None = None,
        instagram_strategy: str | None = None,
        youtube_strategy: str | None = None,
        copyright_audio_path: str | None = None,
        thumbnail_timestamp_ms: int | None = None,
        thumbnail_candidate_index: int | None = None,
        reserved_slots: dict[str, tuple[datetime, datetime]] | None = None,
        immediate_platforms: list[str] | None = None,
        progress_callback: Callable[[float, str, str], None] | None = None,
        platform_result_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        # THUMBNAIL FEATURE DISABLED (2026-08-16, owner request): force-clear
        # both request fields so every downstream cover path (frame extraction,
        # Drive hosting, YouTube thumbnails.set, Facebook thumb, Instagram
        # cover_url/thumb_offset, TikTok thumbnail fields) stays inert.
        # To re-enable, delete these two lines.
        thumbnail_timestamp_ms = None
        thumbnail_candidate_index = None

        def emit_progress(progress: float, phase: str, message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress, phase, message)
            except Exception:
                logger.warning(
                    "Upload progress callback failed: project_id=%s phase=%s",
                    project_id,
                    phase,
                    exc_info=True,
                )

        emit_progress(0.05, "prepare", "Preparing upload...")
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")
        requested_platforms = cls._normalize_platforms(platforms)
        # Urgent-immediate mode: these platforms publish right now (no
        # scheduled_at). "tiktok" is a valid member here even though it is
        # not part of _SUPPORTED_PLATFORMS (it is PFM-published, not
        # locally uploaded).
        immediate: set[str] = {
            str(p).strip().lower() for p in (immediate_platforms or []) if str(p).strip()
        }
        configured_accounts = AccountService.list_accounts()

        # Validate account if provided
        account = None
        platform_scheduled_at: dict[str, datetime] = {}
        project_library_type = coerce_library_type(project.library_type)
        if account_id:
            account = AccountService.get_account(account_id)
            if not account:
                raise ValueError(f"Account '{account_id}' not found")
            if project.output_language and account.language != project.output_language:
                raise ValueError(
                    f"Project language '{project.output_language}' does not match "
                    f"account language '{account.language}'"
                )
            if project_library_type not in account.supported_types:
                raise ValueError(
                    f"Project type '{project_library_type.value}' does not match "
                    f"account supported types {[item.value for item in account.supported_types]}"
                )
        elif configured_accounts:
            raise ValueError("account_id is required when accounts are configured")

        facebook_max_duration = float(
            account.max_reel_duration_for("facebook") if account else 90
        )
        instagram_max_duration = float(
            min(account.max_reel_duration_for("instagram"), 180) if account else 90
        )

        readiness = cls.compute_readiness(project)
        if readiness.status != "green" or not readiness.drive_video_id:
            raise ValueError(f"Project is not ready for upload: {', '.join(readiness.reasons)}")

        drive_video_id, drive_video_name = readiness.drive_video_id, readiness.drive_video_name

        if not readiness.drive_folder_id:
            raise ValueError("Drive folder ID is required but not resolved")
        metadata = MetadataService.load(project_id)
        if metadata is None:
            raise ValueError("metadata.json is missing or invalid")

        subtitle_path = ExportService.subtitle_path(project)
        if not subtitle_path.exists():
            raise ValueError("Subtitle file is missing")
        subtitle_locale = ExportService.language_to_locale(project.output_language)

        # Calculate per-platform scheduled times if account has slots for that platform.
        if account and account_id:
            for _platform in ("youtube", "facebook", "instagram", "tiktok"):
                if _platform in immediate:
                    # Immediate platforms publish now: no scheduled_at at all
                    # (YT public, FB direct publish, TT instant PFM post).
                    continue
                if not account.slots_for(_platform):
                    continue
                _pre = (reserved_slots or {}).get(_platform)
                if _pre is not None:
                    _, _sched = _pre
                else:
                    _, _sched = SchedulingService.find_next_slot_for_platform(
                        account_id, _platform, project_id=project_id
                    )
                platform_scheduled_at[_platform] = _sched

        # Facebook long-range routing: Meta's scheduled_publish_time only
        # accepts targets within ~29 days. Beyond that the upload is DEFERRED
        # to the /server, which converts the hold to a native scheduled post
        # at T-28d (server/app/services/facebook_publisher.py).
        _fb_target = platform_scheduled_at.get("facebook")
        fb_deferred = (
            "facebook" in requested_platforms
            and account is not None
            and account.meta is not None
            and account_id is not None
            and _fb_target is not None
            and _fb_target
            > datetime.now(timezone.utc) + timedelta(days=cls._FACEBOOK_NATIVE_HORIZON_DAYS)
        )

        # Duplicated-project restrictions: same account never uploads two
        # linked projects; same-language duplicates must be >= 30 days apart.
        from .project_duplication_service import UploadRestrictionService

        UploadRestrictionService.validate_upload(
            project,
            account_id,
            list(platform_scheduled_at.values()) or [datetime.now(timezone.utc)],
        )

        # Build the TikTok payload for the VPS scheduler (server-side publish
        # via Post for Me at slot_time).
        tiktok_payload = cls._build_tiktok_payload(
            account, metadata.tiktok.description, thumbnail_timestamp_ms
        )

        # Public share the drive video before upload phase.
        emit_progress(0.15, "prepare", "Preparing Drive upload assets...")
        GoogleDriveService.set_public_read(drive_video_id)
        drive_video_url = readiness.drive_video_web_url or GoogleDriveService.get_web_view_url(drive_video_id)
        direct_drive_download = GoogleDriveService.get_direct_download_url(drive_video_id)

        vps_platforms = cls._vps_platforms(requested_platforms, account, tiktok_payload)
        tiktok_enrolled = cls._tiktok_enrolled(account, tiktok_payload)
        results_by_platform: dict[str, PlatformUploadResult] = dict(
            cls._compute_upfront_skips(requested_platforms, account)
        )
        tiktok_manual = tiktok_enrolled and tiktok_payload is None
        if tiktok_manual:
            # Manual mode: the row stays pending on the VPS job; the server
            # posts the reminder and the ✅ reaction marks it uploaded.
            results_by_platform["tiktok"] = cls._tiktok_manual_result()
        discord_message_id: str | None = None
        instagram_drive_metadata: dict[str, str] = {}
        facebook_drive_metadata: dict[str, str] = {}

        def emit_platform_result(
            result: PlatformUploadResult,
            *,
            update_discord: bool = True,
        ) -> None:
            if platform_result_callback is not None:
                try:
                    platform_result_callback(asdict(result))
                except Exception:
                    logger.warning(
                        "Upload platform result callback failed: project_id=%s platform=%s",
                        project_id,
                        result.platform,
                        exc_info=True,
                    )
            if update_discord:
                try:
                    DiscordService.update_job_platform(
                        project_id,
                        result.platform,
                        status=result.status,
                        url=result.url,
                        detail=result.detail,
                    )
                except Exception:
                    logger.warning(
                        "Discord platform update failed for %s/%s",
                        project_id,
                        result.platform,
                        exc_info=True,
                    )

        for skip_result in results_by_platform.values():
            emit_platform_result(skip_result, update_discord=False)

        # Clean up any stale Discord messages from prior runs before posting a fresh
        # "upload in progress" message.  We used to delete these at finalize-time,
        # but since we now post the message early, cleanup has to happen early too.
        if project.generation_discord_message_id:
            try:
                DiscordService.delete_message(project.generation_discord_message_id)
            except Exception:
                logger.warning(
                    "Failed to delete generation Discord message for project %s",
                    project_id,
                    exc_info=True,
                )
            project.generation_discord_message_id = None
        if project.final_upload_discord_message_id:
            try:
                DiscordService.delete_job(project_id)
            except Exception:
                logger.warning(
                    "Failed to delete stale upload job for project %s",
                    project_id,
                    exc_info=True,
                )
            project.final_upload_discord_message_id = None

        # Build Instagram payload for the VPS scheduler (deferred publish at slot_time).
        ig_payload_base: dict | None = None
        if account and account.meta and account.meta.instagram_business_account_id:
            ig_token = (
                account.meta.instagram_access_token
                or account.meta.facebook_page_access_token
            )
            if ig_token:
                ig_payload_base = {
                    "ig_user_id": account.meta.instagram_business_account_id,
                    "ig_access_token": ig_token,
                    "caption": metadata.instagram.caption,
                    "graph_api_version": settings.meta_graph_api_version,
                    "poll_interval_seconds": settings.instagram_publish_poll_interval_seconds,
                    "poll_timeout_seconds": settings.instagram_publish_timeout_seconds,
                    "max_duration_seconds": instagram_max_duration,
                }

        with tempfile.TemporaryDirectory(prefix=f"atr-upload-{project_id}-") as tmp_dir:
            video_name = drive_video_name or "final_video.mp4"
            local_video_path = Path(tmp_dir) / video_name
            if cls.cached_source_video(project_id) is not None:
                emit_progress(0.30, "download", "Copying final video from source cache...")
            else:
                emit_progress(0.30, "download", "Downloading final video from Drive...")
            cls._stage_source_video(project_id, readiness, local_video_path)

            # TikTok deliberately keeps the ORIGINAL copyrighted audio: capture
            # the pre-remux file for the PFM media staging before
            # local_video_path is swapped to the copyright-replaced variant.
            tiktok_source_path = local_video_path

            # When copyright audio replacement is active, re-mux the video with the
            # new audio track.  We keep the *original* direct_drive_download URL for
            # the Discord message (TikTok uses the original copyrighted audio), but
            # disable the GDrive fast-path so Facebook/YouTube get the local file.
            force_local_upload = False
            if copyright_audio_path:
                audio_path = Path(copyright_audio_path)
                if not audio_path.exists():
                    raise ValueError("Copyright replacement audio file not found")
                replaced_video = Path(tmp_dir) / "copyright_replaced.mp4"
                cls._replace_video_audio(local_video_path, audio_path, replaced_video)
                local_video_path = replaced_video
                force_local_upload = True

            # Extract the chosen thumbnail frame once for image-native platforms
            # (YouTube, Facebook). Extracted from the ORIGINAL output video, so
            # platform-side cut/sped_up retiming cannot shift the image.
            thumbnail_image_path: Path | None = None
            if thumbnail_candidate_index is not None:
                thumbnail_image_path = ThumbnailService.cover_image_for(
                    project_id,
                    thumbnail_candidate_index,
                    local_video_path,
                    Path(tmp_dir) / "thumbnail.jpg",
                )
            if thumbnail_image_path is None and thumbnail_timestamp_ms is not None:
                thumbnail_image_path = ThumbnailService.extract_frame_image(
                    local_video_path,
                    thumbnail_timestamp_ms / 1000.0,
                    Path(tmp_dir) / "thumbnail.jpg",
                )
            # A timestamp/candidate was requested but the frame could not be
            # extracted: YouTube/Facebook proceed without a thumbnail. Surface
            # that on their platform results instead of failing silently.
            thumbnail_extraction_failed = (
                (thumbnail_timestamp_ms is not None or thumbnail_candidate_index is not None)
                and thumbnail_image_path is None
            )

            # Host the composed cover on Drive once, for the VPS-published
            # platforms that need a public image URL (Instagram cover_url,
            # TikTok business connector thumbnail_url). Failure degrades to
            # the existing timestamp-only fallback, never fatal.
            cover_drive_url: str | None = None
            wants_hosted_cover = thumbnail_image_path is not None and (
                ig_payload_base is not None
                or (
                    account is not None
                    and account.tiktok is not None
                    and account.tiktok.post_for_me_platform == "tiktok_business"
                )
            )
            if wants_hosted_cover:
                try:
                    uploaded_cover = GoogleDriveService.upsert_local_file(
                        parent_id=readiness.drive_folder_id,
                        filename="thumbnail_cover.jpg",
                        local_path=thumbnail_image_path,
                        chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
                    )
                    cover_id = str(uploaded_cover.get("id") or "").strip()
                    if cover_id:
                        GoogleDriveService.set_public_read(cover_id)
                        cover_drive_url = GoogleDriveService.get_direct_download_url(cover_id)
                except Exception:
                    logger.warning(
                        "Cover Drive hosting failed for %s; falling back to timestamps",
                        project_id, exc_info=True,
                    )

            ig_payload = dict(ig_payload_base) if ig_payload_base is not None else None
            ig_prep_needed = False
            if (
                "instagram" in requested_platforms
                and "instagram" not in results_by_platform
            ):
                if ig_payload is None:
                    results_by_platform["instagram"] = PlatformUploadResult(
                        platform="instagram",
                        status="skipped",
                        detail="No Instagram credentials configured for this account",
                    )
                    emit_platform_result(
                        results_by_platform["instagram"],
                        update_discord=False,
                    )
                else:
                    ig_prep_needed = True

            # Hosted-cover attach must precede the TikTok job closure below
            # (the PFM body is built from the payload inside the worker).
            cls._attach_tiktok_cover(tiktok_payload, cover_drive_url)

            jobs: dict[str, Any] = {}

            # TikTok job — backend-owned PFM post (2026-08 migration off the
            # VPS). Uses the pre-copyright-remux file: TikTok keeps the
            # original audio.
            if tiktok_payload is not None:
                _tt_payload = dict(tiktok_payload)
                _tt_scheduled_at = platform_scheduled_at.get("tiktok")
                _tt_immediate = "tiktok" in immediate
                _tt_source = tiktok_source_path
                jobs["tiktok"] = lambda: cls._publish_tiktok_via_pfm(
                    project=project,
                    payload=_tt_payload,
                    video_path=_tt_source,
                    scheduled_at=None if _tt_immediate else _tt_scheduled_at,
                    wait_for_result=_tt_immediate,
                )

            # YouTube job
            if (
                "youtube" in requested_platforms
                and account and account.youtube and account.youtube.refresh_token
                and account_id
            ):
                yt_creds = AccountService.get_youtube_credentials(account_id)
                yt_config = account.youtube
                _yt_strategy = youtube_strategy
                _yt_prep_dir = cls._youtube_prep_dir(project_id)
                _yt_scheduled_at = platform_scheduled_at.get("youtube")
                _yt_thumbnail = thumbnail_image_path
                jobs["youtube"] = lambda: SocialUploadService.upload_youtube(
                    video_path=local_video_path,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                    target_language=project.output_language,
                    metadata=metadata,
                    credentials=yt_creds,
                    scheduled_at=_yt_scheduled_at,
                    category_id=yt_config.category_id,
                    channel_id=yt_config.channel_id,
                    youtube_strategy=_yt_strategy,
                    youtube_prep_dir=_yt_prep_dir,
                    thumbnail_image_path=_yt_thumbnail,
                )
            elif "youtube" in requested_platforms and not account:
                # Global (backwards compat)
                _yt_strategy_global = youtube_strategy
                _yt_prep_dir_global = cls._youtube_prep_dir(project_id)
                _yt_thumbnail_global = thumbnail_image_path
                jobs["youtube"] = lambda: SocialUploadService.upload_youtube(
                    video_path=local_video_path,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                    target_language=project.output_language,
                    metadata=metadata,
                    youtube_strategy=_yt_strategy_global,
                    youtube_prep_dir=_yt_prep_dir_global,
                    thumbnail_image_path=_yt_thumbnail_global,
                )

            # Facebook job. Instagram is deferred to the VPS scheduler via create_job above.
            # A >29d Facebook target is deferred too (fb_deferred): the /server
            # converts it to a native scheduled post at T-28d.
            if (
                account and account.meta and account_id
                and "facebook" in requested_platforms
                and not fb_deferred
            ):
                meta_creds = AccountService.get_meta_credentials(account_id)

                _fb_strategy = facebook_strategy  # capture for lambda
                _fb_prep_dir = cls._facebook_prep_dir(project_id)
                _fb_video_url = None if force_local_upload else direct_drive_download
                _fb_scheduled_at = platform_scheduled_at.get("facebook")
                _fb_thumbnail = thumbnail_image_path
                jobs["facebook"] = lambda: SocialUploadService.upload_facebook(
                    video_path=local_video_path,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                    metadata=metadata,
                    video_url=_fb_video_url,
                    page_id=meta_creds.page_id,
                    page_access_token=meta_creds.facebook_page_access_token,
                    scheduled_at=_fb_scheduled_at,
                    facebook_strategy=_fb_strategy,
                    facebook_prep_dir=_fb_prep_dir,
                    thumbnail_image_path=_fb_thumbnail,
                    max_duration_seconds=facebook_max_duration,
                )
            elif not account:
                # Global (backwards compat)
                if "facebook" in requested_platforms:
                    _fb_strategy_global = facebook_strategy
                    _fb_prep_dir_global = cls._facebook_prep_dir(project_id)
                    _fb_video_url_global = None if force_local_upload else direct_drive_download
                    _fb_thumbnail_global = thumbnail_image_path
                    jobs["facebook"] = lambda: SocialUploadService.upload_facebook(
                        video_path=local_video_path,
                        subtitle_path=subtitle_path,
                        subtitle_locale=subtitle_locale,
                        metadata=metadata,
                        video_url=_fb_video_url_global,
                        facebook_strategy=_fb_strategy_global,
                        facebook_prep_dir=_fb_prep_dir_global,
                        thumbnail_image_path=_fb_thumbnail_global,
                        max_duration_seconds=facebook_max_duration,
                    )

            selected_jobs = {platform: jobs[platform] for platform in requested_platforms if platform in jobs}
            # TikTok is PFM-published, never part of requested_platforms.
            if "tiktok" in jobs:
                selected_jobs["tiktok"] = jobs["tiktok"]

            emit_progress(0.55, "platform_upload", "Uploading to social platforms...")
            worker_count = len(selected_jobs) + (1 if ig_prep_needed else 0)
            max_parallel = max(1, min(settings.social_upload_max_parallel, worker_count)) if worker_count else 1
            executor = ThreadPoolExecutor(max_workers=max_parallel)
            timed_out_platforms = False
            abort_platform_jobs = False
            future_to_platform: dict[Any, str] = {}
            try:
                # All platform jobs run in parallel — including in the
                # urgent-immediate mode (owner decision 2026-08-20: no
                # TikTok-first gating; publish order across platforms is
                # whatever each platform's processing yields).
                future_to_platform = {
                    executor.submit(job): platform
                    for platform, job in selected_jobs.items()
                }

                # >29d Facebook hold: prepare + Drive-host the artifact the
                # /server will upload as a native scheduled post at T-28d.
                fb_payload: dict[str, Any] | None = None
                fb_prep_future = None
                if fb_deferred:
                    fb_prep_future = executor.submit(
                        cls._prepare_facebook_drive_video,
                        project_id=project_id,
                        source_video_path=local_video_path,
                        drive_folder_id=readiness.drive_folder_id,
                        facebook_strategy=facebook_strategy,
                        max_duration_seconds=facebook_max_duration,
                        work_dir=Path(tmp_dir),
                    )

                # Instagram Drive prep (transcode + Drive re-upload) runs alongside
                # the YouTube/Facebook uploads; only the Discord/VPS job created
                # below needs its prepared URL.
                if ig_prep_needed:
                    # The local source is the Drive final video itself unless
                    # the copyright audio swap re-muxed it. Only then may the
                    # Instagram prep point the VPS at that Drive file instead
                    # of re-uploading identical bytes as output_instagram.mp4.
                    # (The cached local copy may carry BT.709 VUI tags the
                    # Drive original lacks — Facebook's URL ingestion and
                    # TikTok already publish the untagged original.)
                    ig_source_drive_video = (
                        {
                            "file_id": drive_video_id,
                            "direct_url": direct_drive_download,
                            "web_url": drive_video_url,
                            "filename": video_name,
                        }
                        if drive_video_id and not force_local_upload
                        else None
                    )
                    ig_prep_future = executor.submit(
                        cls._prepare_instagram_drive_video,
                        project_id=project_id,
                        source_video_path=local_video_path,
                        drive_folder_id=readiness.drive_folder_id,
                        instagram_strategy=instagram_strategy,
                        max_duration_seconds=instagram_max_duration,
                        work_dir=Path(tmp_dir),
                        source_drive_video=ig_source_drive_video,
                    )
                    try:
                        ig_result, instagram_drive_metadata = ig_prep_future.result()
                    except Exception:
                        abort_platform_jobs = True
                        raise
                    ig_prepared_local_path = instagram_drive_metadata.pop(
                        "instagram_prepared_local_path", None
                    )
                    if ig_result is not None:
                        results_by_platform["instagram"] = ig_result
                        ig_payload = None
                        emit_platform_result(ig_result, update_discord=False)
                    elif "instagram" in immediate:
                        # Urgent-immediate: publish from the backend right now
                        # instead of handing a scheduled job to the VPS.
                        _ig_payload_now = dict(ig_payload)
                        _ig_local = (
                            Path(ig_prepared_local_path)
                            if ig_prepared_local_path
                            else None
                        )
                        _ig_drive_url = instagram_drive_metadata.get(
                            "instagram_drive_video_url"
                        )

                        def _ig_publish_now() -> PlatformUploadResult:
                            return cls._publish_instagram_immediate(
                                payload=_ig_payload_now,
                                video_path=_ig_local,
                                video_url=_ig_drive_url,
                            )

                        future_to_platform[executor.submit(_ig_publish_now)] = (
                            "instagram"
                        )
                        ig_payload = None  # never reaches the VPS job
                    else:
                        ig_payload["prepared_video_url"] = instagram_drive_metadata[
                            "instagram_drive_video_url"
                        ]
                        if thumbnail_timestamp_ms is not None:
                            ig_payload["thumb_offset"] = cls._instagram_thumb_offset(
                                thumbnail_timestamp_ms,
                                instagram_drive_metadata.get("instagram_speed_factor"),
                                instagram_max_duration,
                            )
                        if cover_drive_url is not None:
                            ig_payload["cover_url"] = cover_drive_url

                if fb_prep_future is not None:
                    try:
                        fb_result, facebook_drive_metadata = fb_prep_future.result()
                    except Exception:
                        abort_platform_jobs = True
                        raise
                    if fb_result is not None:
                        results_by_platform["facebook"] = fb_result
                        emit_platform_result(fb_result, update_discord=False)
                    else:
                        meta_creds_fb = AccountService.get_meta_credentials(account_id)
                        fb_payload = {
                            "page_id": meta_creds_fb.page_id,
                            "page_access_token": meta_creds_fb.facebook_page_access_token,
                            "title": metadata.facebook.title,
                            "description": metadata.facebook.description,
                            "prepared_video_url": facebook_drive_metadata[
                                "facebook_drive_video_url"
                            ],
                            "graph_api_version": settings.meta_graph_api_version,
                        }
                        results_by_platform["facebook"] = PlatformUploadResult(
                            platform="facebook",
                            status="skipped",
                            detail=(
                                "Deferred to the server "
                                f"(>{cls._FACEBOOK_NATIVE_HORIZON_DAYS}d): native "
                                "scheduling at T-28d"
                            ),
                        )
                        emit_platform_result(
                            results_by_platform["facebook"], update_discord=False
                        )

                discord_message_id = None
                try:
                    discord_slot_time = (
                        platform_scheduled_at.get("tiktok")
                        or project.scheduled_at
                        or datetime.now(timezone.utc)
                    )
                    # Seed in-flight immediate platforms as "uploading" so the
                    # embed shows their rows right away; the real result (URL /
                    # failure) lands via update_job_platform when each job ends.
                    seeded_statuses = cls._platform_status_payload(results_by_platform)
                    for _p in immediate:
                        seeded_statuses.setdefault(
                            _p,
                            {
                                "status": "uploading",
                                "url": None,
                                "detail": "Publication immédiate en cours",
                            },
                        )
                    job_response = DiscordService.create_job(
                        project_id=project_id,
                        # Use the live account_id arg (validated above), not
                        # project.scheduled_account_id which is only persisted at the
                        # END of execute_upload — None on first upload.
                        account_id=account_id or project.scheduled_account_id or "",
                        slot_time=discord_slot_time,
                        anime_title=project.anime_name or "Unknown",
                        description=metadata.tiktok.description,
                        drive_video_url=direct_drive_download or drive_video_url,
                        platforms_requested=vps_platforms,
                        instagram=ig_payload,
                        # 2026-08 PFM migration: TikTok is published by the
                        # backend (see _publish_tiktok_via_pfm); the VPS job no
                        # longer carries a tiktok payload.
                        # tiktok=tiktok_payload,
                        tiktok=None,
                        # Manual TikTok mode: the server posts the T-5 reminder.
                        tiktok_manual=tiktok_manual,
                        facebook=fb_payload,
                        platform_scheduled_at=platform_scheduled_at,
                        platform_statuses=seeded_statuses,
                    )
                except Exception:
                    logger.warning(
                        "Discord create_job failed for project %s",
                        project_id,
                        exc_info=True,
                    )
                    job_response = None

                if job_response is not None:
                    discord_message_id = job_response.get("discord_message_id")
                    if discord_message_id:
                        project.final_upload_discord_message_id = discord_message_id
                        try:
                            ProjectService.save(project)
                        except Exception:
                            logger.warning(
                                "Failed to persist Discord message id for project %s",
                                project_id,
                                exc_info=True,
                            )

                pending = set(future_to_platform)
                deadline = time.monotonic() + max(
                    float(settings.project_manager_platform_phase_timeout_seconds),
                    0.001,
                )

                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out_platforms = True
                        break

                    done, pending = wait(
                        pending,
                        timeout=remaining,
                        return_when=FIRST_COMPLETED,
                    )
                    if not done:
                        timed_out_platforms = True
                        break

                    for future in done:
                        platform = future_to_platform[future]
                        try:
                            results_by_platform[platform] = future.result()
                        except Exception as exc:
                            results_by_platform[platform] = PlatformUploadResult(
                                platform=platform,
                                status="failed",
                                detail=str(exc),
                            )
                        if thumbnail_extraction_failed:
                            cls._apply_thumbnail_extraction_warning(
                                results_by_platform[platform]
                            )
                        emit_platform_result(results_by_platform[platform])

                if pending:
                    timed_out_platforms = True
                    timeout_seconds = max(
                        int(settings.project_manager_platform_phase_timeout_seconds),
                        1,
                    )
                    for future in list(pending):
                        platform = future_to_platform[future]
                        future.cancel()
                        results_by_platform[platform] = PlatformUploadResult(
                            platform=platform,
                            status="failed",
                            detail=(
                                f"{platform.title()} platform job timed out after "
                                f"{timeout_seconds}s."
                            ),
                        )
                        emit_platform_result(results_by_platform[platform])
            except Exception:
                if abort_platform_jobs:
                    # A prep step failed after the platform jobs were already
                    # submitted. shutdown(cancel_futures=True) only drops the
                    # ones still queued: a RUNNING Facebook/YouTube upload
                    # keeps going in its thread and schedules a post nobody
                    # records (orphan reel, 2026-08-27). Settle them instead:
                    # cancel the queued ones, wait for the running ones and
                    # undo whatever they published.
                    cls._settle_aborted_platform_jobs(
                        future_to_platform,
                        account_id=account_id or project.scheduled_account_id,
                        emit_platform_result=emit_platform_result,
                    )
                raise
            finally:
                abandon_jobs = timed_out_platforms or abort_platform_jobs
                executor.shutdown(
                    wait=not abandon_jobs,
                    cancel_futures=abandon_jobs,
                )

            # Keep deterministic ordering in reports/messages. TikTok (PFM)
            # is appended after the locally-uploaded platforms.
            _ordered_platforms = list(requested_platforms)
            if "tiktok" in results_by_platform and "tiktok" not in _ordered_platforms:
                _ordered_platforms.append("tiktok")
            platform_results = [
                results_by_platform[platform]
                for platform in _ordered_platforms
                if platform in results_by_platform
            ]

        emit_progress(0.85, "finalize", "Finalizing upload state...")

        # YouTube quota fallback: if YouTube hit quota, post a follow-up generic
        # message with retry metadata so the operator can manually upload later.
        youtube_quota_hit = any(
            r.platform == "youtube" and r.status == "failed" and getattr(r, "quota_exceeded", False)
            for r in results_by_platform.values()
        )
        if youtube_quota_hit:
            quota_msg = (
                f"YouTube quota limit reached for **{project.anime_name or project_id}**. "
                "Manual retry metadata:\n```\n"
                f"Title: {metadata.youtube.title}\n\n"
                f"{metadata.youtube.description}\n\n"
                f"Tags: {', '.join(metadata.youtube.tags)}\n```"
            )
            try:
                DiscordService.post_message(quota_msg)
            except Exception:
                logger.warning(
                    "YouTube quota fallback message failed for %s",
                    project_id,
                    exc_info=True,
                )

        project.drive_folder_id = readiness.drive_folder_id
        project.drive_folder_url = readiness.drive_folder_url
        project.upload_completed_at = datetime.now(timezone.utc)
        project.upload_last_result = {
            "platforms": [asdict(item) for item in platform_results],
            "requested_platforms": list(requested_platforms),
            "drive_video_url": drive_video_url,
            "direct_drive_download": direct_drive_download,
            "drive_video_id": drive_video_id,
            "drive_video_name": drive_video_name,
            **instagram_drive_metadata,
            **facebook_drive_metadata,
        }

        # Save scheduling info. Per-platform reservations are already persisted
        # by SchedulingService; only the top-level account attribution matters here.
        if account_id:
            project.scheduled_account_id = account_id

        ProjectService.save(project)

        # Cleanup upload prep caches after upload
        cls.cleanup_facebook_prep(project_id)
        cls.cleanup_youtube_prep(project_id)
        emit_progress(1.0, "complete", "Upload complete.")

        return {
            "platform_results": [asdict(item) for item in platform_results],
            "requested_platforms": list(requested_platforms),
            "drive_video_url": drive_video_url,
            "direct_drive_download": direct_drive_download,
            **instagram_drive_metadata,
            "discord_message_id": project.final_upload_discord_message_id,
            "platform_scheduled_at": {
                platform: dt.isoformat() for platform, dt in platform_scheduled_at.items()
            },
            "scheduled_at": project.scheduled_at.isoformat() if project.scheduled_at else None,
        }

    # ── Platform duration checks (pre-upload) ─────────────────────────────

    _FACEBOOK_PREP_CACHE_DIR = settings.cache_dir / "facebook_prep"
    _FACEBOOK_PREP_MAX_AGE_SECONDS = 7200  # 2 hours
    _LEGACY_FACEBOOK_PREP_CACHE_DIR = (
        settings.data_dir.parent / "backend" / "data" / "cache" / "facebook_prep"
    )
    _YOUTUBE_PREP_CACHE_DIR = settings.cache_dir / "youtube_prep"
    _YOUTUBE_PREP_MAX_AGE_SECONDS = 7200  # 2 hours
    _LEGACY_YOUTUBE_PREP_CACHE_DIR = (
        settings.data_dir.parent / "backend" / "data" / "cache" / "youtube_prep"
    )
    _COPYRIGHT_AUDIO_CACHE_DIR = settings.cache_dir / "copyright_audio"
    _COPYRIGHT_AUDIO_MAX_AGE_SECONDS = 7200

    _SOURCE_CACHE_DIR = settings.cache_dir / "upload_source"
    _SOURCE_CACHE_MAX_AGE_SECONDS = 7200  # 2 hours

    _THUMBNAIL_EXTRACTION_FAILED_NOTE = (
        "Miniature non appliquée: extraction de l'image impossible"
    )

    # Shared final-video preview cache bookkeeping (guarded by _source_download_guard)
    _source_download_guard = threading.Lock()
    _source_downloads_in_flight: set[str] = set()
    _source_download_errors: dict[str, str] = {}
    _source_download_totals: dict[str, int] = {}
    _source_locks: dict[str, threading.Lock] = {}

    @classmethod
    def _normalize_legacy_prep_cache_dir(cls, cache_dir: Path, legacy_cache_dir: Path) -> Path:
        if not legacy_cache_dir.exists() or cache_dir.resolve() == legacy_cache_dir.resolve():
            return cache_dir

        cache_dir.parent.mkdir(parents=True, exist_ok=True)

        if not cache_dir.exists():
            shutil.move(str(legacy_cache_dir), str(cache_dir))
            return cache_dir

        for legacy_entry in legacy_cache_dir.iterdir():
            destination = cache_dir / legacy_entry.name
            if destination.exists():
                continue
            shutil.move(str(legacy_entry), str(destination))

        try:
            legacy_cache_dir.rmdir()
        except OSError:
            pass

        return cache_dir

    # How long an aborted upload waits for platform jobs that were already
    # running before giving up on undoing them (a reel upload is minutes).
    _ABORT_SETTLE_TIMEOUT_SECONDS = 600

    @classmethod
    def _settle_aborted_platform_jobs(
        cls,
        future_to_platform: dict[Any, str],
        *,
        account_id: str | None,
        emit_platform_result: Callable[..., None],
    ) -> list[str]:
        """Leave no stray posts behind when the upload aborts mid-phase.

        Queued jobs are cancelled outright. Jobs already running are waited
        for; every ``uploaded`` result they return is rolled back (the post is
        deleted on the platform) and reported as a failed platform row so the
        operator sees what happened. Returns the posts that could NOT be
        undone, as display strings (also surfaced as failed rows).
        """
        leftovers: list[str] = []
        running: dict[Any, str] = {}
        for future, platform in future_to_platform.items():
            if future.cancel():
                continue  # never started — nothing to undo
            running[future] = platform
        if not running:
            return leftovers

        logger.warning(
            "Upload aborted with %d platform job(s) still running (%s); waiting for them to settle",
            len(running),
            ", ".join(sorted(running.values())),
        )
        done, pending = wait(
            set(running), timeout=cls._ABORT_SETTLE_TIMEOUT_SECONDS
        )
        for future in pending:
            platform = running[future]
            leftovers.append(f"{platform}: job still running after abort")
            logger.error(
                "Aborted upload: %s job still running after %ss — check the platform for a stray post",
                platform,
                cls._ABORT_SETTLE_TIMEOUT_SECONDS,
            )

        for future in done:
            platform = running[future]
            try:
                result = future.result()
            except Exception as exc:
                logger.info("Aborted upload: %s job failed on its own: %s", platform, exc)
                continue
            if not isinstance(result, PlatformUploadResult) or result.status != "uploaded":
                continue
            reference = result.url or result.resource_id or "?"
            undone = cls._rollback_platform_upload(account_id, result)
            if undone:
                detail = f"Upload aborted — {platform} post {reference} was removed again"
                logger.warning("Aborted upload: removed %s post %s", platform, reference)
            else:
                leftovers.append(f"{platform}: {reference}")
                detail = (
                    f"Upload aborted — {platform} post {reference} could NOT be removed "
                    "automatically; delete it manually before retrying"
                )
            try:
                emit_platform_result(
                    PlatformUploadResult(
                        platform=platform,
                        status="failed",
                        url=None if undone else result.url,
                        resource_id=None if undone else result.resource_id,
                        detail=detail,
                    ),
                    update_discord=False,
                )
            except Exception:
                logger.debug("Aborted-upload platform row emit failed", exc_info=True)

        if leftovers:
            logger.error(
                "Aborted upload left remote posts in place: %s", "; ".join(leftovers)
            )
        return leftovers

    @classmethod
    def _rollback_platform_upload(
        cls, account_id: str | None, result: PlatformUploadResult
    ) -> bool:
        """Delete a post created by a platform job whose upload run aborted.

        Facebook reels and YouTube videos can be deleted through their APIs;
        other platforms return False so the caller reports them as leftovers.
        """
        resource_id = result.resource_id
        if not resource_id or not account_id:
            return False
        try:
            if result.platform == "facebook":
                creds = AccountService.get_meta_credentials(account_id)
                resp = httpx.delete(
                    f"https://graph.facebook.com/{settings.meta_graph_api_version}/{resource_id}",
                    params={"access_token": creds.facebook_page_access_token},
                    timeout=30.0,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Facebook rollback of %s returned %s: %s",
                        resource_id,
                        resp.status_code,
                        resp.text[:200],
                    )
                    return False
                try:
                    return bool(resp.json().get("success"))
                except Exception:
                    return False
            if result.platform == "youtube":
                creds = AccountService.get_youtube_credentials(account_id)
                youtube = SocialUploadService._build_youtube_client(
                    credentials=creds, deadline=None
                )
                youtube.videos().delete(id=resource_id).execute()
                return True
        except Exception:
            logger.warning(
                "Rollback of %s post %s failed", result.platform, resource_id, exc_info=True
            )
            return False
        return False

    @classmethod
    def _facebook_prep_dir(cls, project_id: str) -> Path:
        return cls._normalize_legacy_prep_cache_dir(
            cls._FACEBOOK_PREP_CACHE_DIR,
            cls._LEGACY_FACEBOOK_PREP_CACHE_DIR,
        ) / project_id

    @classmethod
    def _youtube_prep_dir(cls, project_id: str) -> Path:
        return cls._normalize_legacy_prep_cache_dir(
            cls._YOUTUBE_PREP_CACHE_DIR,
            cls._LEGACY_YOUTUBE_PREP_CACHE_DIR,
        ) / project_id

    @classmethod
    def _copyright_audio_dir(cls, project_id: str) -> Path:
        d = cls._COPYRIGHT_AUDIO_CACHE_DIR / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def _source_cache_dir(cls, project_id: str) -> Path:
        return cls._SOURCE_CACHE_DIR / project_id

    @classmethod
    def _source_lock(cls, project_id: str) -> threading.Lock:
        with cls._source_download_guard:
            return cls._source_locks.setdefault(project_id, threading.Lock())

    @classmethod
    def _source_download_bytes_done(cls, project_id: str) -> int:
        cache_dir = cls._source_cache_dir(project_id)
        if not cache_dir.exists():
            return 0
        newest: Path | None = None
        newest_mtime = -1.0
        for f in cache_dir.iterdir():
            if not f.is_file() or f.suffix != ".part":
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime = mtime
                newest = f
        if newest is None:
            return 0
        try:
            return newest.stat().st_size
        except OSError:
            return 0

    @classmethod
    def cached_source_video(cls, project_id: str) -> Path | None:
        cache_dir = cls._source_cache_dir(project_id)
        if not cache_dir.exists():
            return None
        for f in sorted(cache_dir.iterdir()):
            if f.is_file() and f.suffix.lower() == ".mp4":
                return f
        return None

    @classmethod
    def _ensure_source_video(
        cls, project_id: str, readiness: UploadReadiness
    ) -> Path:
        """Blocking: return the cached final video, materializing it if needed."""
        with cls._source_lock(project_id):
            cached = cls.cached_source_video(project_id)
            if cached is not None:
                return cached

            video_name = readiness.drive_video_name or "final_video.mp4"
            cache_dir = cls._source_cache_dir(project_id)
            cache_dir.mkdir(parents=True, exist_ok=True)
            destination = cache_dir / video_name
            partial = cache_dir / f"{video_name}.part"

            try:
                if not readiness.drive_video_id:
                    raise ValueError("Final video unavailable: no Drive copy")
                total = GoogleDriveService.get_file_size(readiness.drive_video_id)
                if total is not None:
                    with cls._source_download_guard:
                        cls._source_download_totals[project_id] = total
                GoogleDriveService.download_file(readiness.drive_video_id, partial)
                ensure_bt709_tags(partial)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
            return destination

    @classmethod
    def _stage_source_video(
        cls, project_id: str, readiness: UploadReadiness, destination: Path
    ) -> Path:
        """Put the final video at ``destination`` by way of the shared source cache.

        The upload run used to download straight into its temp dir, so every
        retry re-fetched the whole file from Drive (3 x 174 MB on 2026-08-27).
        Going through ``_ensure_source_video`` downloads once into
        ``cache/upload_source/<project>`` (evicted after
        ``_SOURCE_CACHE_MAX_AGE_SECONDS``) and shares those bytes with the
        preview/thumbnail paths; the run still works on its own private copy.
        """
        source = cls._ensure_source_video(project_id, readiness)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    @classmethod
    def start_source_video_download(
        cls, project_id: str, readiness: UploadReadiness | None = None
    ) -> dict[str, Any]:
        """Warm the shared source-video cache in the background."""
        status = cls.source_video_status(project_id)
        if status["state"] in ("ready", "in_progress"):
            return status

        if readiness is None:
            project = ProjectService.load(project_id)
            if not project:
                raise ValueError("Project not found")
            readiness = cls.compute_readiness(project)

        with cls._source_download_guard:
            if project_id in cls._source_downloads_in_flight:
                return {"state": "in_progress"}
            cls._source_downloads_in_flight.add(project_id)
            cls._source_download_errors.pop(project_id, None)

        def _worker() -> None:
            try:
                cls._ensure_source_video(project_id, readiness)
            except Exception as exc:
                logger.warning(
                    "Source video download failed: project_id=%s error=%s",
                    project_id,
                    exc,
                )
                with cls._source_download_guard:
                    cls._source_download_errors[project_id] = str(exc)
            finally:
                with cls._source_download_guard:
                    cls._source_downloads_in_flight.discard(project_id)
                    cls._source_download_totals.pop(project_id, None)

        threading.Thread(
            target=_worker, name=f"source-video-{project_id}", daemon=True
        ).start()
        return {"state": "in_progress"}

    @classmethod
    def source_video_status(cls, project_id: str) -> dict[str, Any]:
        cached = cls.cached_source_video(project_id)
        if cached is not None:
            try:
                stat = cached.stat()
            except OSError:
                pass
            else:
                return {
                    "state": "ready",
                    "version": f"{stat.st_mtime_ns}-{stat.st_size}",
                }
        with cls._source_download_guard:
            in_progress = project_id in cls._source_downloads_in_flight
            total = cls._source_download_totals.get(project_id)
            error = cls._source_download_errors.get(project_id)
        if in_progress:
            result: dict[str, Any] = {
                "state": "in_progress",
                "bytes_done": cls._source_download_bytes_done(project_id),
            }
            if total is not None:
                result["bytes_total"] = total
            return result
        if error:
            return {"state": "error", "detail": error}
        return {"state": "missing"}

    @classmethod
    def _cleanup_prep_dir(cls, prep_dir: Path) -> None:
        if prep_dir.exists():
            shutil.rmtree(prep_dir, ignore_errors=True)

    @classmethod
    def cleanup_facebook_prep(cls, project_id: str) -> None:
        cls._cleanup_prep_dir(cls._facebook_prep_dir(project_id))

    @classmethod
    def cleanup_youtube_prep(cls, project_id: str) -> None:
        cls._cleanup_prep_dir(cls._youtube_prep_dir(project_id))

    @classmethod
    def _cleanup_stale_prep_cache(cls, cache_dir: Path, max_age_seconds: int) -> None:
        if not cache_dir.exists():
            return

        import time as _time

        now = _time.time()
        for entry in cache_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                age = now - entry.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue

    @classmethod
    def cleanup_stale_facebook_prep(cls) -> None:
        cls._cleanup_stale_prep_cache(
            cls._normalize_legacy_prep_cache_dir(
                cls._FACEBOOK_PREP_CACHE_DIR,
                cls._LEGACY_FACEBOOK_PREP_CACHE_DIR,
            ),
            cls._FACEBOOK_PREP_MAX_AGE_SECONDS,
        )

    @classmethod
    def cleanup_stale_youtube_prep(cls) -> None:
        cls._cleanup_stale_prep_cache(
            cls._normalize_legacy_prep_cache_dir(
                cls._YOUTUBE_PREP_CACHE_DIR,
                cls._LEGACY_YOUTUBE_PREP_CACHE_DIR,
            ),
            cls._YOUTUBE_PREP_MAX_AGE_SECONDS,
        )

    @classmethod
    def cleanup_stale_copyright_audio(cls) -> None:
        cls._cleanup_stale_prep_cache(
            cls._COPYRIGHT_AUDIO_CACHE_DIR,
            cls._COPYRIGHT_AUDIO_MAX_AGE_SECONDS,
        )

    @classmethod
    def cleanup_stale_source_cache(cls) -> None:
        cls._cleanup_stale_prep_cache(
            cls._SOURCE_CACHE_DIR, cls._SOURCE_CACHE_MAX_AGE_SECONDS
        )

    @classmethod
    def cleanup_stale_thumbnail_cache(cls) -> None:
        # Same 2-hour horizon as the source-video cache it rides alongside.
        cls._cleanup_stale_prep_cache(
            ThumbnailService._THUMBS_CACHE_DIR, cls._SOURCE_CACHE_MAX_AGE_SECONDS
        )

    @classmethod
    def _apply_thumbnail_extraction_warning(cls, result: PlatformUploadResult) -> None:
        """Append a French warning note when a requested thumbnail could not
        be extracted, so image-native platforms (YouTube, Facebook) surface
        that instead of silently uploading with no thumbnail applied.

        No-op for any other platform, or for results that are not
        'uploaded' (skipped/failed already carry their own detail).
        """
        if result.platform not in ("youtube", "facebook") or result.status != "uploaded":
            return
        note = cls._THUMBNAIL_EXTRACTION_FAILED_NOTE
        result.detail = f"{result.detail}; {note}" if result.detail else note

    @classmethod
    def _neutral_duration_check_result(cls) -> dict[str, Any]:
        return {
            "needed": False,
            "duration_seconds": 0.0,
            "speed_factor": 1.0,
            "sped_up_available": False,
        }

    @classmethod
    def _facebook_upload_enabled(cls, account_id: str | None) -> bool:
        if account_id:
            account = AccountService.get_account(account_id)
            if not account:
                raise ValueError(f"Account '{account_id}' not found")
            return bool(account.meta)
        try:
            creds = MetaTokenService.get_upload_credentials()
        except Exception:
            return False
        return bool(creds.page_id and creds.facebook_page_access_token)

    @classmethod
    def _instagram_upload_enabled(cls, account_id: str | None) -> bool:
        if account_id:
            account = AccountService.get_account(account_id)
            if not account:
                raise ValueError(f"Account '{account_id}' not found")
            return bool(
                account.meta
                and account.meta.instagram_business_account_id
                and (account.meta.instagram_access_token or account.meta.facebook_page_access_token)
            )
        try:
            creds = MetaTokenService.get_upload_credentials()
        except Exception:
            return False
        return bool(creds.instagram_business_account_id and creds.instagram_access_token)

    @classmethod
    def _youtube_upload_enabled(cls, account_id: str | None) -> bool:
        if account_id:
            account = AccountService.get_account(account_id)
            if not account:
                raise ValueError(f"Account '{account_id}' not found")
            return bool(account.youtube)
        return SocialUploadService.is_youtube_configured()

    @classmethod
    def _resolve_final_video_duration(
        cls,
        readiness: UploadReadiness,
        probe_media: Callable[..., Any],
    ) -> float:
        """Duration of the final video without downloading a Drive-only file."""
        if readiness.drive_video_id:
            try:
                duration = GoogleDriveService.get_video_duration_seconds(
                    readiness.drive_video_id
                )
            except DriveVideoMetadataLookupError as exc:
                raise UploadPreflightUnavailableError(
                    "Google Drive could not be reached while checking the final "
                    "video duration. Nothing was queued; please retry."
                ) from exc
            if duration is not None:
                return duration

            # Drive indexes video metadata asynchronously, so an export that
            # just landed answers without a duration. The container header
            # states it regardless: read it over byte ranges (a few KB), which
            # keeps the no-download guarantee.
            header_duration = GoogleDriveService.probe_video_duration_from_header(
                readiness.drive_video_id
            )
            if header_duration is not None:
                return header_duration

            raise UploadPreflightUnavailableError(
                "Google Drive is still processing the final video's duration "
                "metadata. No video was downloaded and nothing was queued; "
                "please retry shortly."
            )

        raise ValueError("Final video unavailable: no local or Drive video found")

    @classmethod
    def _check_platform_duration(
        cls,
        project_id: str,
        account_id: str | None,
        *,
        cleanup_stale: Callable[[], None],
        is_enabled: Callable[[str | None], bool],
        probe_media: Callable[..., Any],
        max_duration: float,
        max_speed: float,
    ) -> dict[str, Any]:
        cleanup_stale()
        cls.cleanup_stale_source_cache()
        cls.cleanup_stale_thumbnail_cache()

        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        if not is_enabled(account_id):
            return cls._neutral_duration_check_result()

        readiness = cls._compute_preflight_readiness(project)
        if readiness.status != "green" or not readiness.drive_video_id:
            raise ValueError(
                f"Project is not ready for upload: {', '.join(readiness.reasons)}"
            )

        duration_seconds = cls._resolve_final_video_duration(
            readiness, probe_media
        )

        if duration_seconds <= max_duration + 0.01:
            return {
                "needed": False,
                "duration_seconds": round(duration_seconds, 2),
                "speed_factor": 1.0,
                "sped_up_available": False,
                "max_duration_seconds": max_duration,
            }

        speed_factor = duration_seconds / max_duration
        sped_up_available = speed_factor <= max_speed + 1e-6

        # A choice modal will open: warm the shared preview cache now so the
        # previews are ready as soon as possible.  Never blocks the check.
        cls.start_source_video_download(project_id, readiness)

        return {
            "needed": True,
            "duration_seconds": round(duration_seconds, 2),
            "speed_factor": round(speed_factor, 4),
            "sped_up_available": sped_up_available,
            "max_duration_seconds": max_duration,
        }

    @staticmethod
    def _account_reel_limit(account_id: str | None, platform: str) -> float:
        if account_id:
            account = AccountService.get_account(account_id)
            if account is not None:
                configured = float(account.max_reel_duration_for(platform))
                # Instagram's operational ceiling intentionally follows the
                # 3-minute YouTube workflow even if Meta can ingest longer media.
                return min(configured, 180.0) if platform == "instagram" else configured
        return 90.0

    @classmethod
    def check_facebook_duration(
        cls,
        project_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return cls._check_platform_duration(
            project_id,
            account_id,
            cleanup_stale=cls.cleanup_stale_facebook_prep,
            is_enabled=cls._facebook_upload_enabled,
            probe_media=SocialUploadService._probe_facebook_media,
            max_duration=cls._account_reel_limit(account_id, "facebook"),
            max_speed=SocialUploadService._FACEBOOK_MAX_SPEED_FACTOR,
        )

    @classmethod
    def check_instagram_duration(
        cls,
        project_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return cls._check_platform_duration(
            project_id,
            account_id,
            cleanup_stale=lambda: None,
            is_enabled=cls._instagram_upload_enabled,
            probe_media=SocialUploadService._probe_facebook_media,
            max_duration=cls._account_reel_limit(account_id, "instagram"),
            max_speed=SocialUploadService._FACEBOOK_MAX_SPEED_FACTOR,
        )

    @classmethod
    def check_youtube_duration(
        cls,
        project_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return cls._check_platform_duration(
            project_id,
            account_id,
            cleanup_stale=cls.cleanup_stale_youtube_prep,
            is_enabled=cls._youtube_upload_enabled,
            probe_media=SocialUploadService._probe_youtube_media,
            max_duration=SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS,
            max_speed=SocialUploadService._YOUTUBE_MAX_SPEED_FACTOR,
        )

    @classmethod
    def managed_delete(
        cls, project_id: str, *, confirmed: bool = False
    ) -> dict[str, Any]:
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        now = datetime.now(timezone.utc)
        pending_platform_set = {
            platform
            for platform, schedule in (project.platform_schedules or {}).items()
            if (
                schedule.scheduled_at.replace(tzinfo=timezone.utc)
                if schedule.scheduled_at.tzinfo is None
                else schedule.scheduled_at.astimezone(timezone.utc)
            ) > now
        }
        aggregate_scheduled_at = project.scheduled_at
        aggregate_is_future = bool(
            aggregate_scheduled_at
            and (
                aggregate_scheduled_at.replace(tzinfo=timezone.utc)
                if aggregate_scheduled_at.tzinfo is None
                else aggregate_scheduled_at.astimezone(timezone.utc)
            )
            > now
        )
        # Older persisted projects may only have the aggregate scheduled_at.
        # Derive their remote platforms from the saved upload result so they
        # receive the same confirmation and cancellation protection.
        if aggregate_is_future and not pending_platform_set:
            upload_result = project.upload_last_result or {}
            stored_platforms = (
                upload_result.get("platforms")
                if isinstance(upload_result, dict)
                else None
            )
            if isinstance(stored_platforms, list):
                pending_platform_set.update(
                    str(item["platform"])
                    for item in stored_platforms
                    if isinstance(item, dict) and item.get("platform")
                )
            elif isinstance(stored_platforms, dict):
                pending_platform_set.update(str(item) for item in stored_platforms)
            requested = (
                upload_result.get("requested_platforms")
                if isinstance(upload_result, dict)
                else None
            )
            if isinstance(requested, list):
                pending_platform_set.update(str(item) for item in requested)
            if not pending_platform_set:
                pending_platform_set.update(
                    ("youtube", "facebook", "instagram", "tiktok")
                )
        pending_platforms = sorted(pending_platform_set)
        if pending_platforms and not confirmed:
            raise PendingProjectDeletionRequiresConfirmation(
                project.id, pending_platforms
            )

        cleanup_warnings: list[str] = []
        drive_deleted = False
        archive_result: dict[str, Any] | None = None
        drive_folder_id = project.drive_folder_id
        should_resolve_by_name = bool(
            not drive_folder_id
            and (
                project.upload_completed_at
                or project.upload_last_result
                or project.drive_folder_url
            )
        )
        if should_resolve_by_name and GoogleDriveService.is_configured():
            found = GoogleDriveService.find_project_folder_by_name(
                ExportService.output_folder_name(project)
            )
            drive_folder_id = found["id"] if found else None

        # Archive is reserved for projects that actually went live: emergency
        # re-access only matters for published videos. Scheduled-only or
        # never-posted projects are deleted outright, without an archive copy.
        has_upload_activity = bool(
            project.upload_completed_at
            or project.upload_last_result
            or project.final_upload_discord_message_id
        )
        live_platforms = {
            platform
            for platform, schedule in (project.platform_schedules or {}).items()
            if (
                schedule.scheduled_at.replace(tzinfo=timezone.utc)
                if schedule.scheduled_at.tzinfo is None
                else schedule.scheduled_at.astimezone(timezone.utc)
            )
            <= now
        }
        everything_still_pending = bool(pending_platform_set) or aggregate_is_future
        was_posted = has_upload_activity and (
            bool(live_platforms) or not everything_still_pending
        )

        # Archive must finish before any destructive Drive or local operation.
        if was_posted and drive_folder_id and GoogleDriveService.is_configured():
            archive_result = GoogleDriveService.archive_project_folder(drive_folder_id)

        cancellation_status: dict[str, str] = {}
        server_platforms = {"instagram", "tiktok"}.intersection(pending_platforms)
        if server_platforms or (
            pending_platforms and project.final_upload_discord_message_id
        ):
            server_result = PlatformRescheduleService.delete_server_job(project)
            for platform in sorted(server_platforms):
                cancellation_status[platform] = server_result.status
            if server_result.status == "pending_retry":
                raise RuntimeError(
                    "Could not remove the pending Instagram/TikTok server job: "
                    f"{server_result.error or 'unknown error'}"
                )

        for platform in (
            item for item in pending_platforms if item not in server_platforms
        ):
            result = PlatformRescheduleService.cancel(project, platform)
            cancellation_status[platform] = result.status
            if result.status == "pending_retry":
                raise RuntimeError(
                    f"Could not unschedule {platform}: {result.error or 'unknown error'}"
                )

        try:
            # Premiere Link: never let the panel launch a project that is gone.
            from .cep_link_service import CepLinkService  # noqa: PLC0415 - import cycle

            CepLinkService.delete_launch(project_id)
            if project.final_upload_discord_message_id:
                # Removes the VPS job and all associated Discord messages.
                DiscordService.delete_job(project_id)
            elif project.generation_discord_message_id:
                DiscordService.delete_message(project.generation_discord_message_id)
        except Exception as exc:
            cleanup_warnings.append(f"discord cleanup failed: {exc}")

        if drive_folder_id and GoogleDriveService.is_configured():
            GoogleDriveService.delete_folder(drive_folder_id)
            drive_deleted = True

        # Shared-source GC: this project's dir (and its export manifest) is
        # about to disappear, so release its shared Drive files while the
        # manifest is still readable. Never blocks the deletion itself.
        shared_gc: dict[str, Any] | None = None
        # A pre-warm still uploading this project's episodes would keep
        # writing references after we release them: stop it first (its own
        # cancel path re-runs GC once the project dir is gone).
        DrivePrewarmService.request_cancel(project.id)
        released_manifest = DriveSharedSources.load_local_manifest(project.id)
        if released_manifest and GoogleDriveService.is_configured():
            try:
                shared_gc = DriveSharedSources.collect_garbage(
                    released_manifest, exclude_project_id=project.id
                )
            except Exception as exc:
                cleanup_warnings.append(f"shared-source GC failed: {exc}")

        local_deleted = ProjectService.delete(project.id)
        result = {
            "status": "deleted" if local_deleted else "not_found",
            "local_deleted": local_deleted,
            "drive_deleted": drive_deleted,
            "archive": archive_result,
            "unscheduled": cancellation_status,
        }
        if shared_gc is not None:
            result["shared_gc"] = shared_gc
        if cleanup_warnings:
            result["cleanup_warnings"] = cleanup_warnings
        return result

    # ── Copyright music replacement ──────────────────────────────────────

    @classmethod
    def check_copyright(cls, project_id: str, account_id: str | None = None) -> dict[str, Any]:
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        music_key = project.resolved_music_key()
        if not music_key:
            return {"copyrighted": False}

        try:
            music = MusicConfigService.get_music(music_key)
        except ValueError:
            return {"copyrighted": False}

        if not music.copyright:
            return {"copyrighted": False}

        # Look for the no-music wav in the GDrive folder. Drive is the only
        # source: build_copyright_audio downloads by file id, and the modal
        # requires no_music_available and no_music_file_id to agree.
        readiness = cls.compute_readiness(project)
        no_music_file_id = None
        no_music_available = False

        if readiness.drive_folder_id:
            try:
                children = GoogleDriveService.list_children(readiness.drive_folder_id)
                for child in children:
                    if child.get("name") == NO_MUSIC_WAV_FILENAME:
                        no_music_file_id = child["id"]
                        no_music_available = True
                        break
            except Exception:
                pass

        available = MusicConfigService.list_non_copyrighted()
        available_musics = [{"key": m.key, "display_name": m.display_name} for m in available]

        return {
            "copyrighted": True,
            "music_key": music_key,
            "music_display_name": music.display_name,
            "no_music_file_id": no_music_file_id,
            "no_music_available": no_music_available,
            "available_musics": available_musics,
            "drive_video_id": readiness.drive_video_id,
        }

    @classmethod
    def build_copyright_audio(
        cls, project_id: str, music_key: str | None, no_music_file_id: str | None = None
    ) -> Path:
        from pydub import AudioSegment

        prep_dir = cls._copyright_audio_dir(project_id)

        no_music_path = prep_dir / NO_MUSIC_WAV_FILENAME
        if not no_music_path.exists():
            if no_music_file_id:
                GoogleDriveService.download_file(no_music_file_id, no_music_path)
            else:
                raise ValueError(f"{NO_MUSIC_WAV_FILENAME} not found on Drive")

        if music_key is None:
            # No music - use the no-music wav as-is
            output_path = prep_dir / "copyright_replacement_no_music.wav"
            if not output_path.exists():
                shutil.copy2(no_music_path, output_path)
            return output_path

        # Mix with replacement music
        music = MusicConfigService.get_music(music_key)
        music_file = Path(music.file_path)
        if not music_file.exists():
            raise ValueError(f"Music file not found: {music.file_path}")

        output_path = prep_dir / f"copyright_replacement_{music_key}.wav"

        no_music_audio = AudioSegment.from_file(str(no_music_path))
        music_audio = AudioSegment.from_file(str(music_file))
        target_len = len(no_music_audio)

        if len(music_audio) < target_len:
            repeats = (target_len // len(music_audio)) + 1
            music_audio = music_audio * repeats
        music_audio = music_audio[:target_len]
        music_audio = music_audio + music.volume_db
        music_audio = music_audio.fade_out(2000)
        result = no_music_audio.overlay(music_audio)

        result.export(str(output_path), format="wav")
        return output_path

    @staticmethod
    def _replace_video_audio(video_path: Path, audio_path: Path, output_path: Path) -> None:
        import subprocess
        from ..utils.media_binaries import rewrite_media_command, get_media_subprocess_env

        cmd = rewrite_media_command([
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-shortest",
            str(output_path),
        ])
        result = subprocess.run(
            cmd,
            capture_output=True,
            env=get_media_subprocess_env(cmd),
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg audio replacement failed: {result.stderr.decode()}")
