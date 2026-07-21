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
import threading
import os
import time

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
from .platform_reschedule_service import PlatformRescheduleService
from .project_service import ProjectService
from .scheduling_service import SchedulingService
from .social_upload_service import PlatformUploadResult, SocialUploadService

logger = logging.getLogger("uvicorn.error")


class PendingProjectDeletionRequiresConfirmation(ValueError):
    def __init__(self, project_id: str, platforms: list[str]):
        super().__init__("Scheduled project deletion requires explicit confirmation")
        self.project_id = project_id
        self.platforms = platforms


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
    local_video_path: str | None = None
    local_video_name: str | None = None


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
    _INSTAGRAM_DRIVE_FILENAME = "output_instagram.mp4"
    _FRENCH_TZ = ZoneInfo("Europe/Paris")
    _TIKTOK_NOT_CONFIGURED_DETAIL = "No Post for Me account configured for this account"
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
    def _ensure_drive_video(cls, project, readiness: "UploadReadiness") -> tuple[str | None, str | None]:
        """Drive file id/name of the final video, uploading the local copy if Drive lacks it."""
        if readiness.drive_video_id:
            return readiness.drive_video_id, readiness.drive_video_name
        if not readiness.local_video_path or not GoogleDriveService.is_configured():
            return None, None
        local = Path(readiness.local_video_path)
        if not local.exists():
            return None, None
        folder_id = readiness.drive_folder_id
        if not folder_id:
            folder_id, folder_url = GoogleDriveService.ensure_project_folder(
                ExportService.output_folder_name(project)
            )
            readiness.drive_folder_id = folder_id
            if not readiness.drive_folder_url:
                readiness.drive_folder_url = folder_url
        else:
            readiness.drive_folder_id = folder_id
        uploaded = GoogleDriveService.upsert_local_file(
            parent_id=folder_id,
            filename=local.name,
            local_path=local,
            chunksize=settings.drive_upload_chunk_mb * 1024 * 1024,
        )
        return str(uploaded.get("id") or "") or None, local.name

    @classmethod
    def _build_readiness(
        cls,
        *,
        metadata_exists: bool,
        folder_id: str | None,
        folder_url: str | None,
        video_files: list[dict[str, Any]],
        video_lookup_failed: bool = False,
        local_video: Path | None = None,
    ) -> UploadReadiness:
        reasons: list[str] = []
        if not folder_id and local_video is None:
            reasons.append("no output video found")

        video_count = len(video_files)
        drive_video = video_files[0] if video_count == 1 else None

        if not metadata_exists:
            reasons.append("no metadata found")
        if local_video is None:
            if video_count == 0:
                if video_lookup_failed and folder_id:
                    reasons.append("unable to verify output video in Drive")
                else:
                    reasons.append("no output video found")
            elif video_count > 1:
                reasons.append("more than one output video found (conflicting)")

        if local_video is not None:
            status = "green" if metadata_exists else "orange"
        elif metadata_exists and video_count == 1:
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
            local_video_path=str(local_video) if local_video else None,
            local_video_name=local_video.name if local_video else None,
        )

    @classmethod
    def compute_readiness(cls, project: Project) -> UploadReadiness:
        metadata_exists = ProjectService.get_metadata_file(project.id).exists()

        from .lan_transfer_service import LanTransferService

        local_video = LanTransferService.find_local_upload_video(project.id)
        if local_video is not None:
            # Use whatever folder info is already cached on the project; never
            # query Drive (no folder-by-name search, no video lookup) when a
            # local video already answers the readiness question.
            folder_id = project.drive_folder_id
            folder_url = project.drive_folder_url
            return cls._build_readiness(
                metadata_exists=metadata_exists,
                folder_id=folder_id,
                folder_url=folder_url,
                video_files=[],
                local_video=local_video,
            )

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
        from .lan_transfer_service import LanTransferService

        projects = ProjectService.list_all()
        local_videos: dict[str, Path | None] = {
            project.id: LanTransferService.find_local_upload_video(project.id) for project in projects
        }
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
                        if local_videos[project.id] is not None:
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
            local_video = local_videos.get(project.id)
            if local_video is not None:
                # Local-first: a video already exists on disk (delivered over LAN),
                # so skip any Drive folder/video lookup entirely.
                readiness = cls._build_readiness(
                    metadata_exists=metadata_exists,
                    folder_id=project.drive_folder_id,
                    folder_url=project.drive_folder_url,
                    video_files=[],
                    local_video=local_video,
                )
            else:
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
                "local_video_available": local_video is not None,
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
                if account is not None and (
                    account.tiktok is None
                    or not account.tiktok.post_for_me_account_id
                ):
                    reason = cls._TIKTOK_NOT_CONFIGURED_DETAIL
            if reason is not None:
                skips[platform] = PlatformUploadResult(
                    platform=platform,
                    status="skipped",
                    detail=reason,
                )
        return skips

    @classmethod
    def _build_tiktok_payload(
        cls, account: AccountConfig | None, tiktok_description: str
    ) -> dict[str, Any] | None:
        """Payload for the VPS server's Post for Me publish (see server TikTokPayload)."""
        if account is None or account.tiktok is None:
            return None
        tiktok = account.tiktok
        if not tiktok.post_for_me_account_id:
            return None
        return {
            "social_account_id": tiktok.post_for_me_account_id,
            "caption": tiktok_description,
            "privacy_status": tiktok.privacy_status,
            "allow_comment": tiktok.allow_comment,
            "allow_duet": tiktok.allow_duet,
            "allow_stitch": tiktok.allow_stitch,
        }

    @classmethod
    def _vps_platforms(
        cls,
        requested_platforms: tuple[str, ...],
        account: AccountConfig | None,
        tiktok_payload: dict[str, Any] | None,
    ) -> list[str]:
        """Platforms recorded on the VPS job. TikTok is server-published, so it
        joins the job whenever a payload exists or the account has an explicit
        `tiktok:` block that schedules it (slots) — it is never part of the
        locally-uploaded platforms. Without a payload, top-level `slots:` alone
        (no `tiktok:` block) does not count, since `slots_for` falls back to
        the top-level list for every platform."""
        platforms = list(requested_platforms)
        if "tiktok" in platforms:
            return platforms
        if tiktok_payload is not None or (
            account is not None
            and account.tiktok is not None
            and account.slots_for("tiktok")
        ):
            platforms.append("tiktok")
        return platforms

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
    ) -> tuple[PlatformUploadResult | None, dict[str, str]]:
        output_path = work_dir / cls._INSTAGRAM_DRIVE_FILENAME
        prep = SocialUploadService.prepare_instagram_video_for_drive(
            source_video_path=source_video_path,
            output_path=output_path,
            instagram_strategy=instagram_strategy,
            facebook_prep_dir=cls._facebook_prep_dir(project_id),
            max_duration_seconds=max_duration_seconds,
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

        facebook_max_duration = float(
            account.max_reel_duration_for("facebook") if account else 90
        )
        instagram_max_duration = float(
            min(account.max_reel_duration_for("instagram"), 180) if account else 90
        )

        readiness = cls.compute_readiness(project)
        if readiness.status != "green" or (
            not readiness.drive_video_id and not readiness.local_video_path
        ):
            raise ValueError(f"Project is not ready for upload: {', '.join(readiness.reasons)}")

        drive_video_id, drive_video_name = cls._ensure_drive_video(project, readiness)
        if not drive_video_id:
            raise ValueError(
                "Final video is unavailable: not found on Drive and the local "
                "copy (via LAN transfer) could not be uploaded to Drive either"
            )

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
                    _, _sched = SchedulingService.find_next_slot_for_platform(
                        account_id, _platform, project_id=project_id
                    )
                platform_scheduled_at[_platform] = _sched

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
        tiktok_payload = cls._build_tiktok_payload(account, metadata.tiktok.description)

        # Public share the drive video before upload phase.
        emit_progress(0.15, "prepare", "Preparing Drive upload assets...")
        GoogleDriveService.set_public_read(drive_video_id)
        drive_video_url = readiness.drive_video_web_url or GoogleDriveService.get_web_view_url(drive_video_id)
        direct_drive_download = GoogleDriveService.get_direct_download_url(drive_video_id)

        vps_platforms = cls._vps_platforms(requested_platforms, account, tiktok_payload)
        results_by_platform: dict[str, PlatformUploadResult] = dict(
            cls._compute_upfront_skips(requested_platforms, account)
        )
        if "tiktok" in vps_platforms and tiktok_payload is None:
            results_by_platform.setdefault(
                "tiktok",
                PlatformUploadResult(
                    platform="tiktok",
                    status="skipped",
                    detail=cls._TIKTOK_NOT_CONFIGURED_DETAIL,
                ),
            )
        discord_message_id: str | None = None
        instagram_drive_metadata: dict[str, str] = {}

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
            local_video = Path(readiness.local_video_path) if readiness.local_video_path else None
            video_name = drive_video_name or (local_video.name if local_video else "final_video.mp4")
            local_video_path = Path(tmp_dir) / video_name
            if local_video is not None and local_video.exists():
                emit_progress(0.30, "download", "Copying final video from local output...")
                shutil.copy2(local_video, local_video_path)
            else:
                cached_source = cls.cached_source_video(project_id)
                if cached_source is not None and cached_source.exists():
                    emit_progress(0.30, "download", "Copying final video from preview cache...")
                    shutil.copy2(cached_source, local_video_path)
                else:
                    emit_progress(0.30, "download", "Downloading final video from Drive...")
                    GoogleDriveService.download_file(drive_video_id, local_video_path)

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

            # Facebook job. Instagram is deferred to the VPS scheduler via create_job above.
            if account and account.meta and account_id and "facebook" in requested_platforms:
                meta_creds = AccountService.get_meta_credentials(account_id)

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
                    max_duration_seconds=facebook_max_duration,
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
                        max_duration_seconds=facebook_max_duration,
                    )

            selected_jobs = {platform: jobs[platform] for platform in requested_platforms if platform in jobs}

            emit_progress(0.55, "platform_upload", "Uploading to social platforms...")
            worker_count = len(selected_jobs) + (1 if ig_prep_needed else 0)
            max_parallel = max(1, min(settings.social_upload_max_parallel, worker_count)) if worker_count else 1
            executor = ThreadPoolExecutor(max_workers=max_parallel)
            timed_out_platforms = False
            abort_platform_jobs = False
            try:
                future_to_platform = {
                    executor.submit(job): platform
                    for platform, job in selected_jobs.items()
                }

                # Instagram Drive prep (transcode + Drive re-upload) runs alongside
                # the YouTube/Facebook uploads; only the Discord/VPS job created
                # below needs its prepared URL.
                if ig_prep_needed:
                    ig_prep_future = executor.submit(
                        cls._prepare_instagram_drive_video,
                        project_id=project_id,
                        source_video_path=local_video_path,
                        drive_folder_id=readiness.drive_folder_id,
                        instagram_strategy=instagram_strategy,
                        max_duration_seconds=instagram_max_duration,
                        work_dir=Path(tmp_dir),
                    )
                    try:
                        ig_result, instagram_drive_metadata = ig_prep_future.result()
                    except Exception:
                        abort_platform_jobs = True
                        raise
                    if ig_result is not None:
                        results_by_platform["instagram"] = ig_result
                        ig_payload = None
                        emit_platform_result(ig_result, update_discord=False)
                    else:
                        ig_payload["prepared_video_url"] = instagram_drive_metadata[
                            "instagram_drive_video_url"
                        ]

                discord_message_id = None
                try:
                    discord_slot_time = (
                        platform_scheduled_at.get("tiktok")
                        or project.scheduled_at
                        or datetime.now(timezone.utc)
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
                        tiktok=tiktok_payload,
                        platform_scheduled_at=platform_scheduled_at,
                        platform_statuses=cls._platform_status_payload(results_by_platform),
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
                abandon_jobs = timed_out_platforms or abort_platform_jobs
                executor.shutdown(
                    wait=not abandon_jobs,
                    cancel_futures=abandon_jobs,
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
            **instagram_drive_metadata,
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

    # Shared final-video preview cache bookkeeping (guarded by _source_download_guard)
    _source_download_guard = threading.Lock()
    _source_downloads_in_flight: set[str] = set()
    _source_download_errors: dict[str, str] = {}
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

            video_name = (
                readiness.drive_video_name
                or readiness.local_video_name
                or "final_video.mp4"
            )
            cache_dir = cls._source_cache_dir(project_id)
            cache_dir.mkdir(parents=True, exist_ok=True)
            destination = cache_dir / video_name
            partial = cache_dir / f"{video_name}.part"

            try:
                if readiness.local_video_path and Path(readiness.local_video_path).exists():
                    shutil.copy2(readiness.local_video_path, partial)
                elif readiness.drive_video_id:
                    GoogleDriveService.download_file(readiness.drive_video_id, partial)
                else:
                    raise ValueError(
                        "Final video unavailable: not present locally and no Drive copy"
                    )
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
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

        threading.Thread(
            target=_worker, name=f"source-video-{project_id}", daemon=True
        ).start()
        return {"state": "in_progress"}

    @classmethod
    def source_video_status(cls, project_id: str) -> dict[str, Any]:
        if cls.cached_source_video(project_id) is not None:
            return {"state": "ready"}
        with cls._source_download_guard:
            if project_id in cls._source_downloads_in_flight:
                return {"state": "in_progress"}
            error = cls._source_download_errors.get(project_id)
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
        project_id: str,
        readiness: UploadReadiness,
        probe_media: Callable[..., Any],
    ) -> float:
        """Duration of the final video without downloading it when possible."""
        if readiness.local_video_path:
            local = Path(readiness.local_video_path)
            if local.exists():
                probe, probe_error = probe_media(video_path=local)
                if (
                    not probe_error
                    and probe is not None
                    and probe.duration_seconds is not None
                ):
                    return probe.duration_seconds

        if readiness.drive_video_id:
            duration = GoogleDriveService.get_video_duration_seconds(
                readiness.drive_video_id
            )
            if duration is not None:
                return duration

        # Drive has not exposed video metadata yet: single blocking download
        # into the shared cache (also feeds the preview modals and the upload).
        source_path = cls._ensure_source_video(project_id, readiness)
        probe, probe_error = probe_media(video_path=source_path)
        if probe_error or probe is None or probe.duration_seconds is None:
            raise ValueError(
                f"Unable to probe video duration: {probe_error or 'unknown'}"
            )
        return probe.duration_seconds

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

        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        if not is_enabled(account_id):
            return cls._neutral_duration_check_result()

        readiness = cls.compute_readiness(project)
        if readiness.status != "green" or not (
            readiness.drive_video_id or readiness.local_video_path
        ):
            raise ValueError(
                f"Project is not ready for upload: {', '.join(readiness.reasons)}"
            )

        duration_seconds = cls._resolve_final_video_duration(
            project_id, readiness, probe_media
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

        # Archive must finish before any destructive Drive or local operation.
        if drive_folder_id and GoogleDriveService.is_configured():
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

        local_deleted = ProjectService.delete(project.id)
        result = {
            "status": "deleted" if local_deleted else "not_found",
            "local_deleted": local_deleted,
            "drive_deleted": drive_deleted,
            "archive": archive_result,
            "unscheduled": cancellation_status,
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

        music_key = project.resolved_music_key()
        if not music_key:
            return {"copyrighted": False}

        try:
            music = MusicConfigService.get_music(music_key)
        except ValueError:
            return {"copyrighted": False}

        if not music.copyright:
            return {"copyrighted": False}

        # Look for output_no_music.wav in GDrive folder
        readiness = cls.compute_readiness(project)
        no_music_file_id = None
        no_music_available = False

        local_no_music = ExportService.get_output_dir(project_id) / "output_no_music.wav"
        if local_no_music.exists():
            no_music_available = True

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

        # Prefer the locally produced output_no_music.wav (LAN transfer); fall
        # back to downloading it from Drive by file id.
        no_music_path = prep_dir / "output_no_music.wav"
        if not no_music_path.exists():
            local_no_music = ExportService.get_output_dir(project_id) / "output_no_music.wav"
            if local_no_music.exists():
                shutil.copy2(local_no_music, no_music_path)
            elif no_music_file_id:
                GoogleDriveService.download_file(no_music_file_id, no_music_path)
            else:
                raise ValueError("output_no_music.wav not found locally or on Drive")

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
