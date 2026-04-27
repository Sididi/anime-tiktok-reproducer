from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from zoneinfo import ZoneInfo
import logging
import shutil
import tempfile
import os
import time

import requests

from ..config import settings
from ..library_types import coerce_library_type
from ..models import Project
from .account_service import AccountConfig, AccountService
from .discord_service import DiscordService
from .export_service import ExportService
from .google_drive_service import GoogleDriveService
from .metadata import MetadataService
from .meta_token_service import MetaTokenService
from .music_config_service import MusicConfigService
from .project_service import ProjectService
from .scheduling_service import SchedulingService
from .social_upload_service import PlatformUploadResult, SocialUploadService

logger = logging.getLogger("uvicorn.error")


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


def _uploaded_fields(project: "Project") -> dict[str, Any]:
    """Return uploaded + uploaded_status based on scheduled_at vs now."""
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
        return {"uploaded": is_live, "uploaded_status": status}
    # No scheduling: rely on discord message presence (immediate publish)
    return {
        "uploaded": has_discord,
        "uploaded_status": "green" if has_discord else "red",
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
    _FRENCH_TZ = ZoneInfo("Europe/Paris")
    _drive_video_cache: dict[str, dict[str, Any]] = {}
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
    def list_manager_rows(cls) -> list[dict[str, Any]]:
        projects = ProjectService.list_all()
        folder_candidates_by_name: dict[str, dict[str, Any]] = {}
        drive_root_videos: dict[str, list[dict[str, Any]]] = {}
        drive_batch_lookup_failed = False
        if GoogleDriveService.is_configured():
            for attempt in range(1, cls._DRIVE_BATCH_LOOKUP_MAX_ATTEMPTS + 1):
                try:
                    drive = GoogleDriveService.client()
                    folder_candidates_by_name = GoogleDriveService.list_project_folders_under_parent(drive=drive)
                    folder_ids: list[str] = []
                    for project in projects:
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
                    drive_batch_lookup_failed = False
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
            project_dir = ProjectService.get_project_dir(project.id)
            metadata_exists = ProjectService.get_metadata_file(project.id).exists()
            folder_id, folder_url = cls._resolve_drive_folder(
                project,
                folder_candidates_by_name=folder_candidates_by_name if folder_candidates_by_name else None,
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
                metadata_exists=metadata_exists,
                folder_id=folder_id,
                folder_url=folder_url,
                video_files=video_files,
                video_lookup_failed=drive_batch_lookup_failed,
            )
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
                "platform_schedules": {
                    platform: {
                        "slot": ps.slot.isoformat(),
                        "scheduled_at": ps.scheduled_at.isoformat(),
                    }
                    for platform, ps in (project.platform_schedules or {}).items()
                },
            }

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
        instagram_enabled: bool,
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
                if not instagram_enabled:
                    reason = "Instagram upload disabled: ATR_N8N_WEBHOOK_URL is not configured"
                elif account is not None and account.meta is None:
                    reason = default_detail
            if reason is not None:
                skips[platform] = PlatformUploadResult(
                    platform=platform,
                    status="skipped",
                    detail=reason,
                )
        return skips

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

    @classmethod
    def execute_upload(
        cls,
        project_id: str,
        account_id: str | None = None,
        platforms: list[str] | None = None,
        facebook_strategy: str | None = None,
        youtube_strategy: str | None = None,
        copyright_audio_path: str | None = None,
        reserved_slots: dict[str, tuple[datetime, datetime]] | None = None,
        progress_callback: Callable[[float, str, str], None] | None = None,
        platform_result_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
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

        readiness = cls.compute_readiness(project)
        if readiness.status != "green" or not readiness.drive_video_id:
            raise ValueError(f"Project is not ready for upload: {', '.join(readiness.reasons)}")

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
                if not account.slots_for(_platform):
                    continue
                _pre = (reserved_slots or {}).get(_platform)
                if _pre is not None:
                    _, _sched = _pre
                else:
                    _, _sched = SchedulingService.find_next_slot_for_platform(account_id, _platform)
                platform_scheduled_at[_platform] = _sched

        # Public share the drive video before upload phase.
        emit_progress(0.15, "prepare", "Preparing Drive upload assets...")
        GoogleDriveService.set_public_read(readiness.drive_video_id)
        drive_video_url = readiness.drive_video_web_url or GoogleDriveService.get_web_view_url(readiness.drive_video_id)
        direct_drive_download = GoogleDriveService.get_direct_download_url(readiness.drive_video_id)

        instagram_enabled = bool((settings.n8n_webhook_url or "").strip())

        results_by_platform: dict[str, PlatformUploadResult] = dict(
            cls._compute_upfront_skips(requested_platforms, account, instagram_enabled)
        )
        discord_message_id: str | None = None

        def emit_platform_result(result: PlatformUploadResult) -> None:
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
            emit_platform_result(skip_result)

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

        discord_message_id = None
        try:
            job_response = DiscordService.create_job(
                project_id=project_id,
                # Use the live account_id arg (validated above), not
                # project.scheduled_account_id which is only persisted at the
                # END of execute_upload — None on first upload.
                account_id=account_id or project.scheduled_account_id or "",
                slot_time=project.scheduled_at or datetime.now(timezone.utc),
                anime_title=project.anime_name or "Unknown",
                description=metadata.tiktok.description,
                drive_video_url=direct_drive_download or drive_video_url,
                platforms_requested=list(requested_platforms),
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

        with tempfile.TemporaryDirectory(prefix=f"atr-upload-{project_id}-") as tmp_dir:
            local_video_path = Path(tmp_dir) / (readiness.drive_video_name or "final_video.mp4")
            emit_progress(0.30, "download", "Downloading final video from Drive...")
            GoogleDriveService.download_file(readiness.drive_video_id, local_video_path)

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

            jobs: dict[str, Any] = {}

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
                )
            elif "youtube" in requested_platforms and not account:
                # Global (backwards compat)
                _yt_strategy_global = youtube_strategy
                _yt_prep_dir_global = cls._youtube_prep_dir(project_id)
                jobs["youtube"] = lambda: SocialUploadService.upload_youtube(
                    video_path=local_video_path,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                    target_language=project.output_language,
                    metadata=metadata,
                    youtube_strategy=_yt_strategy_global,
                    youtube_prep_dir=_yt_prep_dir_global,
                )

            # Facebook + Instagram jobs
            if account and account.meta and account_id and (
                "facebook" in requested_platforms or "instagram" in requested_platforms
            ):
                meta_creds = AccountService.get_meta_credentials(account_id)

                if "facebook" in requested_platforms:
                    _fb_strategy = facebook_strategy  # capture for lambda
                    _fb_prep_dir = cls._facebook_prep_dir(project_id)
                    _fb_video_url = None if force_local_upload else direct_drive_download
                    _fb_scheduled_at = platform_scheduled_at.get("facebook")
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
                    )

                # Instagram: disabled when n8n webhook is not configured.
                if "instagram" in requested_platforms and instagram_enabled:
                    _ig_scheduled_at = platform_scheduled_at.get("instagram")
                    if _ig_scheduled_at:
                        ig_deferred = cls._send_n8n_instagram_webhook(
                            project_id=project_id,
                            scheduled_at=_ig_scheduled_at,
                            drive_video_id=readiness.drive_video_id,
                            metadata=metadata,
                            ig_user_id=meta_creds.instagram_business_account_id,
                            ig_access_token=meta_creds.instagram_access_token,
                        )
                        jobs["instagram"] = lambda: ig_deferred
                    else:
                        jobs["instagram"] = lambda: SocialUploadService.upload_instagram(
                            video_path=local_video_path,
                            metadata=metadata,
                            ig_user_id=meta_creds.instagram_business_account_id,
                            ig_access_token=meta_creds.instagram_access_token,
                        )
            elif not account:
                # Global (backwards compat)
                if "facebook" in requested_platforms:
                    _fb_strategy_global = facebook_strategy
                    _fb_prep_dir_global = cls._facebook_prep_dir(project_id)
                    _fb_video_url_global = None if force_local_upload else direct_drive_download
                    jobs["facebook"] = lambda: SocialUploadService.upload_facebook(
                        video_path=local_video_path,
                        subtitle_path=subtitle_path,
                        subtitle_locale=subtitle_locale,
                        metadata=metadata,
                        video_url=_fb_video_url_global,
                        facebook_strategy=_fb_strategy_global,
                        facebook_prep_dir=_fb_prep_dir_global,
                    )
                if "instagram" in requested_platforms and instagram_enabled:
                    jobs["instagram"] = lambda: SocialUploadService.upload_instagram(
                        video_path=local_video_path,
                        metadata=metadata,
                    )

            selected_jobs = {platform: jobs[platform] for platform in requested_platforms if platform in jobs}

            emit_progress(0.55, "platform_upload", "Uploading to social platforms...")
            max_parallel = max(1, min(settings.social_upload_max_parallel, len(selected_jobs))) if selected_jobs else 1
            executor = ThreadPoolExecutor(max_workers=max_parallel)
            timed_out_platforms = False
            try:
                future_to_platform = {
                    executor.submit(job): platform
                    for platform, job in selected_jobs.items()
                }
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
            finally:
                executor.shutdown(
                    wait=not timed_out_platforms,
                    cancel_futures=timed_out_platforms,
                )

            # Keep deterministic ordering in reports/messages.
            platform_results = [
                results_by_platform[platform]
                for platform in requested_platforms
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
            "discord_message_id": project.final_upload_discord_message_id,
            "platform_scheduled_at": {
                platform: dt.isoformat() for platform, dt in platform_scheduled_at.items()
            },
            "scheduled_at": project.scheduled_at.isoformat() if project.scheduled_at else None,
        }

    @classmethod
    def _send_n8n_payload(
        cls,
        *,
        platform: str,
        payload: dict[str, Any],
        success_detail: str,
    ) -> PlatformUploadResult:
        webhook_url = settings.n8n_webhook_url
        if not webhook_url:
            return PlatformUploadResult(
                platform=platform,
                status="skipped",
                detail="n8n webhook URL not configured",
            )
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            if resp.status_code >= 400:
                return PlatformUploadResult(
                    platform=platform,
                    status="failed",
                    detail=f"n8n webhook returned {resp.status_code}: {resp.text[:200]}",
                )
            return PlatformUploadResult(
                platform=platform,
                status="uploaded",
                detail=success_detail,
            )
        except Exception as exc:
            return PlatformUploadResult(
                platform=platform,
                status="failed",
                detail=f"n8n webhook call failed: {exc}",
            )

    @classmethod
    def _send_n8n_instagram_webhook(
        cls,
        *,
        project_id: str,
        scheduled_at: datetime,
        drive_video_id: str,
        metadata: "VideoMetadataPayload",
        ig_user_id: str,
        ig_access_token: str,
    ) -> PlatformUploadResult:
        """Send self-contained webhook to n8n for deferred Instagram publish."""
        payload = {
            "project_id": project_id,
            "scheduled_at": scheduled_at.isoformat(),
            "drive_video_id": drive_video_id,
            "graph_api_version": settings.meta_graph_api_version,
            "instagram": {
                "ig_user_id": ig_user_id,
                "ig_access_token": ig_access_token,
                "caption": metadata.instagram.caption,
            },
            "discord_webhook_url": settings.discord_webhook_url,
        }
        return cls._send_n8n_payload(
            platform="instagram",
            payload=payload,
            success_detail=(
                f"Deferred to n8n; scheduled for "
                f"{cls._format_french_datetime(scheduled_at)}"
            ),
        )

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
    def _youtube_upload_enabled(cls, account_id: str | None) -> bool:
        if account_id:
            account = AccountService.get_account(account_id)
            if not account:
                raise ValueError(f"Account '{account_id}' not found")
            return bool(account.youtube)
        return SocialUploadService.is_youtube_configured()

    @classmethod
    def _check_platform_duration(
        cls,
        project_id: str,
        account_id: str | None,
        *,
        platform_label: str,
        prep_dir: Path,
        cleanup_stale: Callable[[], None],
        is_enabled: Callable[[str | None], bool],
        probe_media: Callable[..., Any],
        transcode_to_limit: Callable[..., Any],
        max_duration: float,
        max_speed: float,
    ) -> dict[str, Any]:
        cleanup_stale()

        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        if not is_enabled(account_id):
            return cls._neutral_duration_check_result()

        readiness = cls.compute_readiness(project)
        if readiness.status != "green" or not readiness.drive_video_id:
            raise ValueError(f"Project is not ready for upload: {', '.join(readiness.reasons)}")

        prep_dir.mkdir(parents=True, exist_ok=True)

        video_name = readiness.drive_video_name or "final_video.mp4"
        original_path = prep_dir / video_name
        if not original_path.exists():
            GoogleDriveService.download_file(readiness.drive_video_id, original_path)

        probe, probe_error = probe_media(video_path=original_path)
        if probe_error or probe is None or probe.duration_seconds is None:
            raise ValueError(f"Unable to probe video duration: {probe_error or 'unknown'}")

        duration_seconds = probe.duration_seconds
        if duration_seconds <= max_duration + 0.01:
            return {
                "needed": False,
                "duration_seconds": round(duration_seconds, 2),
                "speed_factor": 1.0,
                "sped_up_available": False,
            }

        speed_factor = duration_seconds / max_duration
        sped_up_available = speed_factor <= max_speed + 1e-6

        # Note: We no longer transcode the sped-up preview here.
        # The frontend uses HTML5 playbackRate for instant preview.
        # Actual transcoding happens at upload time if user chooses "sped_up" strategy.

        return {
            "needed": True,
            "duration_seconds": round(duration_seconds, 2),
            "speed_factor": round(speed_factor, 4),
            "sped_up_available": sped_up_available,
        }

    @classmethod
    def check_facebook_duration(
        cls,
        project_id: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return cls._check_platform_duration(
            project_id,
            account_id,
            platform_label="Facebook",
            prep_dir=cls._facebook_prep_dir(project_id),
            cleanup_stale=cls.cleanup_stale_facebook_prep,
            is_enabled=cls._facebook_upload_enabled,
            probe_media=SocialUploadService._probe_facebook_media,
            transcode_to_limit=SocialUploadService._transcode_facebook_video_to_limit,
            max_duration=SocialUploadService._FACEBOOK_MAX_DURATION_SECONDS,
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
            platform_label="YouTube",
            prep_dir=cls._youtube_prep_dir(project_id),
            cleanup_stale=cls.cleanup_stale_youtube_prep,
            is_enabled=cls._youtube_upload_enabled,
            probe_media=SocialUploadService._probe_youtube_media,
            transcode_to_limit=SocialUploadService._transcode_youtube_video_to_limit,
            max_duration=SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS,
            max_speed=SocialUploadService._YOUTUBE_MAX_SPEED_FACTOR,
        )

    @classmethod
    def managed_delete(cls, project_id: str) -> dict[str, Any]:
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        cleanup_warnings: list[str] = []
        try:
            if project.final_upload_discord_message_id:
                # delete_job cascades: removes both the embed and the reminder
                # message on the VPS in one call.
                DiscordService.delete_job(project_id)
            elif project.generation_discord_message_id:
                DiscordService.delete_message(project.generation_discord_message_id)
        except Exception as exc:
            cleanup_warnings.append(f"discord cleanup failed: {exc}")

        drive_deleted = False
        try:
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

            if drive_folder_id and GoogleDriveService.is_configured():
                GoogleDriveService.delete_folder(drive_folder_id)
                drive_deleted = True
        except Exception as exc:
            cleanup_warnings.append(f"drive cleanup failed: {exc}")
            drive_deleted = False

        local_deleted = ProjectService.delete(project.id)
        result = {
            "status": "deleted" if local_deleted else "not_found",
            "local_deleted": local_deleted,
            "drive_deleted": drive_deleted,
        }
        if cleanup_warnings:
            result["cleanup_warnings"] = cleanup_warnings
        return result

    # ── Copyright music replacement ──────────────────────────────────────

    @classmethod
    def check_copyright(cls, project_id: str, account_id: str | None = None) -> dict[str, Any]:
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        if not project.music_key:
            return {"copyrighted": False}

        try:
            music = MusicConfigService.get_music(project.music_key)
        except ValueError:
            return {"copyrighted": False}

        if not music.copyright:
            return {"copyrighted": False}

        # Look for output_no_music.wav in GDrive folder
        readiness = cls.compute_readiness(project)
        no_music_file_id = None
        no_music_available = False

        if readiness.drive_folder_id:
            try:
                children = GoogleDriveService.list_children(readiness.drive_folder_id)
                for child in children:
                    if child.get("name") == "output_no_music.wav":
                        no_music_file_id = child["id"]
                        no_music_available = True
                        break
            except Exception:
                pass

        available = MusicConfigService.list_non_copyrighted()
        available_musics = [{"key": m.key, "display_name": m.display_name} for m in available]

        return {
            "copyrighted": True,
            "music_key": project.music_key,
            "music_display_name": music.display_name,
            "no_music_file_id": no_music_file_id,
            "no_music_available": no_music_available,
            "available_musics": available_musics,
            "drive_video_id": readiness.drive_video_id,
        }

    @classmethod
    def build_copyright_audio(cls, project_id: str, music_key: str | None, no_music_file_id: str) -> Path:
        from pydub import AudioSegment

        prep_dir = cls._copyright_audio_dir(project_id)

        # Download output_no_music.wav from GDrive (cached)
        no_music_path = prep_dir / "output_no_music.wav"
        if not no_music_path.exists():
            GoogleDriveService.download_file(no_music_file_id, no_music_path)

        if music_key is None:
            # No music - use output_no_music.wav as-is
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
