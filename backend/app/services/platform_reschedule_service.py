from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from googleapiclient.discovery import build

from ..models import Project
from .account_service import AccountService

logger = logging.getLogger("uvicorn.error")


NotificationStatus = Literal["ok", "pending_retry", "skipped"]


@dataclass
class NotificationResult:
    status: NotificationStatus
    error: str | None = None


class PlatformRescheduleService:
    """Propagates slot changes to YouTube/Facebook/Instagram-server.

    TikTok is manual and never notified.
    """

    @classmethod
    def _platform_video_url(cls, project: Project, platform: str) -> str | None:
        result = project.upload_last_result or {}
        platforms = result.get("platforms") if isinstance(result, dict) else None
        if not isinstance(platforms, dict):
            return None
        entry = platforms.get(platform)
        if not isinstance(entry, dict):
            return None
        url = entry.get("url")
        return url if isinstance(url, str) else None

    @classmethod
    def _youtube_video_id(cls, url: str) -> str | None:
        # Accepts youtu.be/<id>, youtube.com/watch?v=<id>, youtube.com/shorts/<id>
        patterns = (
            r"youtu\.be/([A-Za-z0-9_\-]{6,})",
            r"[?&]v=([A-Za-z0-9_\-]{6,})",
            r"shorts/([A-Za-z0-9_\-]{6,})",
        )
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return None

    @classmethod
    def _facebook_video_id(cls, url: str) -> str | None:
        m = re.search(r"/videos?/(\d+)", url) or re.search(r"v=(\d+)", url)
        return m.group(1) if m else None

    @classmethod
    def notify(
        cls, project: Project, platform: str, new_scheduled_at: datetime
    ) -> NotificationResult:
        if platform == "tiktok":
            return NotificationResult(status="skipped")

        url = cls._platform_video_url(project, platform)
        if not url:
            return NotificationResult(status="skipped")

        try:
            if platform == "youtube":
                return cls._notify_youtube(project, url, new_scheduled_at)
            if platform == "facebook":
                return cls._notify_facebook(project, url, new_scheduled_at)
            if platform == "instagram":
                return cls._notify_instagram(project, new_scheduled_at)
        except Exception as exc:
            logger.warning(
                "platform reschedule failed: project=%s platform=%s error=%s",
                project.id, platform, exc,
            )
            return NotificationResult(status="pending_retry", error=str(exc))
        return NotificationResult(status="skipped")

    @classmethod
    def cancel(cls, project: Project, platform: str) -> NotificationResult:
        if platform == "tiktok":
            return NotificationResult(status="skipped")

        url = cls._platform_video_url(project, platform)
        if not url and platform != "instagram":
            return NotificationResult(status="skipped")

        try:
            if platform == "youtube":
                return cls._cancel_youtube(project, url)
            if platform == "facebook":
                return cls._cancel_facebook(project, url)
            if platform == "instagram":
                return cls._cancel_instagram(project)
        except Exception as exc:
            logger.warning(
                "platform cancel failed: project=%s platform=%s error=%s",
                project.id, platform, exc,
            )
            return NotificationResult(status="pending_retry", error=str(exc))
        return NotificationResult(status="skipped")

    # Implementations live in tasks 10-12.
    @classmethod
    def _notify_youtube(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        video_id = cls._youtube_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        creds = AccountService.get_youtube_credentials(project.scheduled_account_id)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": new_scheduled_at.isoformat(),
            },
        }
        youtube.videos().update(part="status", body=body).execute()
        return NotificationResult(status="ok")

    @classmethod
    def _notify_facebook(cls, project: Project, url: str, new_scheduled_at: datetime) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _notify_instagram(cls, project: Project, new_scheduled_at: datetime) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _cancel_youtube(cls, project: Project, url: str) -> NotificationResult:
        video_id = cls._youtube_video_id(url)
        if not video_id:
            return NotificationResult(status="skipped")
        creds = AccountService.get_youtube_credentials(project.scheduled_account_id)
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "id": video_id,
            "status": {"privacyStatus": "private"},
        }
        youtube.videos().update(part="status", body=body).execute()
        return NotificationResult(status="ok")

    @classmethod
    def _cancel_facebook(cls, project: Project, url: str) -> NotificationResult:
        raise NotImplementedError

    @classmethod
    def _cancel_instagram(cls, project: Project) -> NotificationResult:
        raise NotImplementedError
