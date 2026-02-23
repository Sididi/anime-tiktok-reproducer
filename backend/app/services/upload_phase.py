from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import os

import requests

from ..models import Project
from ..config import settings
from .account_service import AccountService
from .discord_service import DiscordService
from .export_service import ExportService
from .google_drive_service import GoogleDriveService
from .metadata import MetadataService
from .project_service import ProjectService
from .scheduling_service import SchedulingService
from .social_upload_service import PlatformUploadResult, SocialUploadService


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
            found = folder_candidates_by_name.get(project.id)
            if not found:
                return None, None
            folder_id = found["id"]
            folder_url = found.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder_id}"
            return folder_id, folder_url

        found = GoogleDriveService.find_project_folder_by_name(project.id)
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
    ) -> UploadReadiness:
        reasons: list[str] = []
        if not folder_id:
            reasons.append("no output video found")

        video_count = len(video_files)
        drive_video = video_files[0] if video_count == 1 else None

        if not metadata_exists:
            reasons.append("no metadata found")
        if video_count == 0:
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
        if folder_id:
            try:
                video_files = ExportService.detect_upload_video_in_drive_root(folder_id)
            except Exception:
                video_files = []

        return cls._build_readiness(
            metadata_exists=metadata_exists,
            folder_id=folder_id,
            folder_url=folder_url,
            video_files=video_files,
        )

    @classmethod
    def list_manager_rows(cls) -> list[dict[str, Any]]:
        projects = ProjectService.list_all()
        folder_candidates_by_name: dict[str, dict[str, Any]] = {}
        drive_root_videos: dict[str, list[dict[str, Any]]] = {}
        if GoogleDriveService.is_configured():
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

        def _build_row(project: Project) -> dict[str, Any]:
            project_dir = ProjectService.get_project_dir(project.id)
            metadata_exists = ProjectService.get_metadata_file(project.id).exists()
            folder_id, folder_url = cls._resolve_drive_folder(
                project,
                folder_candidates_by_name=folder_candidates_by_name if folder_candidates_by_name else None,
                resolve_remote_url=False,
            )
            readiness = cls._build_readiness(
                metadata_exists=metadata_exists,
                folder_id=folder_id,
                folder_url=folder_url,
                video_files=drive_root_videos.get(folder_id or "", []),
            )
            return {
                "project_id": project.id,
                "anime_title": project.anime_name,
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
        "facebook": "__Facebook:__",
        "instagram": "__*Instagram:*__",
    }

    @classmethod
    def _format_french_datetime(cls, dt: datetime) -> str:
        day_name = cls._FRENCH_DAYS[dt.weekday()]
        month_name = cls._FRENCH_MONTHS[dt.month - 1]
        return f"{day_name} {dt.day} {month_name} {dt.year} à {dt.strftime('%H:%M')}"

    @classmethod
    def _format_upload_discord_message(
        cls,
        *,
        project: Project,
        drive_download_url: str,
        platform_results: list[PlatformUploadResult],
        youtube_title: str,
        youtube_description: str,
        youtube_tags: list[str],
        tiktok_description: str,
        scheduled_at: datetime | None = None,
    ) -> str:
        anime_title = project.anime_name or "Inconnu"
        header = f"**{anime_title}**: Upload terminé pour le projet `{project.id}`"
        if scheduled_at:
            header += f" (programmé le *{cls._format_french_datetime(scheduled_at)}*)"
        lines = [
            header,
            f"__**Lien vidéo:**__ {drive_download_url}",
            "",
            "Plateformes:",
        ]
        for result in platform_results:
            icon = ":white_check_mark:" if result.status == "uploaded" else ":warning:" if result.status == "skipped" else ":x:"
            platform_display = cls._PLATFORM_DISPLAY.get(result.platform, result.platform)
            if result.status == "uploaded":
                url_part = f" - <{result.url}>" if result.url else ""
                lines.append(f"{icon} {platform_display}: Uploaded{url_part}")
            elif result.status == "skipped":
                detail_part = f" ({result.detail})" if result.detail else ""
                lines.append(f"{icon} {platform_display}: Skipped{detail_part}")
            else:
                detail_part = f" ({result.detail})" if result.detail else ""
                lines.append(f"{icon} {platform_display}: Failed{detail_part}")

        youtube_quota_hit = any(
            item.platform == "youtube" and item.status == "failed" and item.quota_exceeded
            for item in platform_results
        )
        if youtube_quota_hit:
            lines.append("")
            lines.append(
                "YouTube quota limit reached (default quota ~10,000/day; upload+captions can consume ~2,000/video, about 5 videos/day)."
            )
            lines.append("YouTube metadata for manual retry:")
            lines.append("```")
            lines.append(f"Title: {youtube_title}")
            lines.append("")
            lines.append(youtube_description)
            lines.append("")
            lines.append(f"Tags: {', '.join(youtube_tags)}")
            lines.append("```")

        lines.append("")
        lines.append("TikTok metadata:")
        lines.append(f"`{tiktok_description}`")
        return "\n".join(lines)

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
    ) -> dict[str, Any]:
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")
        requested_platforms = cls._normalize_platforms(platforms)

        # Validate account if provided
        account = None
        scheduled_at: datetime | None = None
        slot_dt: datetime | None = None
        if account_id:
            account = AccountService.get_account(account_id)
            if not account:
                raise ValueError(f"Account '{account_id}' not found")
            if project.output_language and account.language != project.output_language:
                raise ValueError(
                    f"Project language '{project.output_language}' does not match "
                    f"account language '{account.language}'"
                )

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

        # Calculate scheduled time if account has slots
        if account and account.slots and account_id:
            slot_dt, scheduled_at = SchedulingService.find_next_slot(account_id)

        # Public share the drive video before upload phase.
        GoogleDriveService.set_public_read(readiness.drive_video_id)
        drive_video_url = readiness.drive_video_web_url or GoogleDriveService.get_web_view_url(readiness.drive_video_id)
        direct_drive_download = GoogleDriveService.get_direct_download_url(readiness.drive_video_id)

        with tempfile.TemporaryDirectory(prefix=f"atr-upload-{project_id}-") as tmp_dir:
            local_video_path = Path(tmp_dir) / (readiness.drive_video_name or "final_video.mp4")
            GoogleDriveService.download_file(readiness.drive_video_id, local_video_path)

            jobs: dict[str, Any] = {}
            instagram_enabled = bool((settings.n8n_webhook_url or "").strip())

            # YouTube job
            if "youtube" in requested_platforms and account and account.youtube and account_id:
                yt_creds = AccountService.get_youtube_credentials(account_id)
                yt_config = account.youtube
                jobs["youtube"] = lambda: SocialUploadService.upload_youtube(
                    video_path=local_video_path,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                    target_language=project.output_language,
                    metadata=metadata,
                    credentials=yt_creds,
                    scheduled_at=scheduled_at,
                    category_id=yt_config.category_id,
                    channel_id=yt_config.channel_id,
                )
            elif "youtube" in requested_platforms and not account:
                # Global (backwards compat)
                jobs["youtube"] = lambda: SocialUploadService.upload_youtube(
                    video_path=local_video_path,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                    target_language=project.output_language,
                    metadata=metadata,
                )

            # Facebook + Instagram jobs
            if account and account.meta and account_id and (
                "facebook" in requested_platforms or "instagram" in requested_platforms
            ):
                meta_creds = AccountService.get_meta_credentials(account_id)

                if "facebook" in requested_platforms:
                    jobs["facebook"] = lambda: SocialUploadService.upload_facebook(
                        video_path=local_video_path,
                        subtitle_path=subtitle_path,
                        subtitle_locale=subtitle_locale,
                        metadata=metadata,
                        video_url=direct_drive_download,
                        page_id=meta_creds.page_id,
                        page_access_token=meta_creds.facebook_page_access_token,
                        scheduled_at=scheduled_at,
                    )

                # Instagram: disabled when n8n webhook is not configured.
                if "instagram" in requested_platforms and instagram_enabled:
                    if scheduled_at:
                        ig_deferred = cls._send_n8n_instagram_webhook(
                            project_id=project_id,
                            scheduled_at=scheduled_at,
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
                    jobs["facebook"] = lambda: SocialUploadService.upload_facebook(
                        video_path=local_video_path,
                        subtitle_path=subtitle_path,
                        subtitle_locale=subtitle_locale,
                        metadata=metadata,
                        video_url=direct_drive_download,
                    )
                if "instagram" in requested_platforms and instagram_enabled:
                    jobs["instagram"] = lambda: SocialUploadService.upload_instagram(
                        video_path=local_video_path,
                        metadata=metadata,
                    )

            results_by_platform: dict[str, PlatformUploadResult] = {}
            selected_jobs = {platform: jobs[platform] for platform in requested_platforms if platform in jobs}

            for platform in requested_platforms:
                if platform in jobs:
                    continue
                detail = "Platform is not configured for this upload context"
                if platform == "instagram" and not instagram_enabled:
                    detail = "Instagram upload disabled: ATR_N8N_WEBHOOK_URL is not configured"
                results_by_platform[platform] = PlatformUploadResult(
                    platform=platform,
                    status="skipped",
                    detail=detail,
                )

            max_parallel = max(1, min(settings.social_upload_max_parallel, len(selected_jobs))) if selected_jobs else 1
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                future_to_platform = {
                    executor.submit(job): platform
                    for platform, job in selected_jobs.items()
                }
                for future in as_completed(future_to_platform):
                    platform = future_to_platform[future]
                    try:
                        results_by_platform[platform] = future.result()
                    except Exception as exc:
                        results_by_platform[platform] = PlatformUploadResult(
                            platform=platform,
                            status="failed",
                            detail=str(exc),
                        )

            # Keep deterministic ordering in reports/messages.
            platform_results = [
                results_by_platform[platform]
                for platform in requested_platforms
                if platform in results_by_platform
            ]

        # Remove generation message first (if any), then post final upload message.
        if project.generation_discord_message_id:
            DiscordService.delete_message(project.generation_discord_message_id)
            project.generation_discord_message_id = None
        if project.final_upload_discord_message_id:
            DiscordService.delete_message(project.final_upload_discord_message_id)
            project.final_upload_discord_message_id = None

        final_message = DiscordService.post_message(
            cls._format_upload_discord_message(
                project=project,
                drive_download_url=direct_drive_download,
                platform_results=platform_results,
                youtube_title=metadata.youtube.title,
                youtube_description=metadata.youtube.description,
                youtube_tags=metadata.youtube.tags,
                tiktok_description=metadata.tiktok.description,
                scheduled_at=scheduled_at,
            )
        )

        project.drive_folder_id = readiness.drive_folder_id
        project.drive_folder_url = readiness.drive_folder_url
        project.final_upload_discord_message_id = final_message.id if final_message else project.final_upload_discord_message_id
        project.upload_completed_at = datetime.now(timezone.utc)
        project.upload_last_result = {
            "platforms": [asdict(item) for item in platform_results],
            "requested_platforms": list(requested_platforms),
            "drive_video_url": drive_video_url,
            "direct_drive_download": direct_drive_download,
        }

        # Save scheduling info
        if account_id:
            project.scheduled_account_id = account_id
        if scheduled_at:
            project.scheduled_at = scheduled_at
        if slot_dt:
            project.scheduled_slot = slot_dt.isoformat()

        ProjectService.save(project)

        return {
            "platform_results": [asdict(item) for item in platform_results],
            "requested_platforms": list(requested_platforms),
            "drive_video_url": drive_video_url,
            "direct_drive_download": direct_drive_download,
            "discord_message_id": project.final_upload_discord_message_id,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
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
            success_detail=f"Deferred to n8n; scheduled for {scheduled_at.isoformat()}",
        )

    @classmethod
    def managed_delete(cls, project_id: str) -> dict[str, Any]:
        project = ProjectService.load(project_id)
        if not project:
            raise ValueError("Project not found")

        if project.final_upload_discord_message_id:
            existing = DiscordService.get_message(project.final_upload_discord_message_id)
            if existing:
                content = existing.content
                # Disable video embed by wrapping the direct download URL in angle brackets
                direct_url = (project.upload_last_result or {}).get("direct_drive_download", "")
                if direct_url and direct_url in content:
                    content = content.replace(direct_url, f"<{direct_url}>")
                # Strike through each line of the original content
                struck_lines = [f"~~{line}~~" if line.strip() else "" for line in content.splitlines()]
                DiscordService.edit_message(
                    project.final_upload_discord_message_id,
                    "\n".join(struck_lines),
                )
            else:
                DiscordService.edit_message(
                    project.final_upload_discord_message_id,
                    "~~Upload removed~~",
                )
        elif project.generation_discord_message_id:
            DiscordService.delete_message(project.generation_discord_message_id)

        drive_deleted = False
        drive_folder_id = project.drive_folder_id
        if not drive_folder_id:
            found = GoogleDriveService.find_project_folder_by_name(project.id) if GoogleDriveService.is_configured() else None
            drive_folder_id = found["id"] if found else None
        if drive_folder_id and GoogleDriveService.is_configured():
            try:
                GoogleDriveService.delete_folder(drive_folder_id)
                drive_deleted = True
            except Exception:
                drive_deleted = False

        local_deleted = ProjectService.delete(project.id)
        return {
            "status": "deleted" if local_deleted else "not_found",
            "local_deleted": local_deleted,
            "drive_deleted": drive_deleted,
        }
