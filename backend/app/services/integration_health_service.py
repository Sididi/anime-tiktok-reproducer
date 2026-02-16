from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any
import copy

import requests
from googleapiclient.discovery import build

from ..config import settings
from ..utils.meta_graph import extract_graph_error
from .discord_service import DiscordService
from .google_drive_service import GoogleDriveService
from .meta_token_service import MetaTokenService
from .social_upload_service import SocialUploadService


class IntegrationHealthService:
    """Runs integration checks once at startup and serves cached result."""

    _lock = Lock()
    _cached_result: dict[str, Any] | None = None

    @classmethod
    def _check_discord(cls) -> dict[str, Any]:
        if not DiscordService.is_configured():
            return {"status": "skipped", "detail": "Discord webhook not configured"}
        return {"status": "ok", "detail": "Discord webhook is configured"}

    @classmethod
    def _check_google_drive(cls) -> dict[str, Any]:
        if not GoogleDriveService.is_configured():
            return {"status": "skipped", "detail": "Google Drive credentials are not fully configured"}

        ok, detail = GoogleDriveService.verify_parent_folder_access()
        if not ok:
            return {"status": "error", "detail": detail}
        return {"status": "ok", "detail": detail}

    @classmethod
    def _check_youtube(cls) -> dict[str, Any]:
        if not SocialUploadService.is_youtube_configured():
            return {"status": "skipped", "detail": "YouTube credentials are not fully configured"}

        try:
            youtube = build(
                "youtube",
                "v3",
                credentials=SocialUploadService.youtube_credentials(),
                cache_discovery=False,
            )
            channels: list[dict[str, Any]] = []
            request = youtube.channels().list(part="id,snippet", mine=True, maxResults=50)
            while request is not None:
                response = request.execute()
                batch = response.get("items", [])
                if isinstance(batch, list):
                    channels.extend(item for item in batch if isinstance(item, dict))
                request = youtube.channels().list_next(request, response)
            if not channels:
                return {
                    "status": "error",
                    "detail": (
                        "Authenticated but no YouTube channel returned for current account. "
                        "Generate the Google refresh token with the account that owns/has access "
                        "to the target YouTube channel."
                    ),
                }
            expected_channel_id = (settings.youtube_channel_id or "").strip()
            if expected_channel_id:
                channel_ids = {str(item.get("id") or "") for item in channels}
                if expected_channel_id not in channel_ids:
                    available = ", ".join(
                        f"{item.get('id')} ({item.get('snippet', {}).get('title', 'unknown')})"
                        for item in channels[:10]
                    ) or "none"
                    return {
                        "status": "error",
                        "detail": (
                            f"Configured ATR_YOUTUBE_CHANNEL_ID={expected_channel_id} is not available "
                            f"for current token. Available channels: {available}"
                        ),
                    }
                return {
                    "status": "ok",
                    "detail": (
                        "YouTube API credentials are valid and expected channel is accessible: "
                        f"{expected_channel_id}"
                    ),
                    "youtube_channel_id": expected_channel_id,
                }
            if len(channels) > 1:
                return {
                    "status": "ok",
                    "detail": (
                        "YouTube API credentials are valid, but multiple channels are accessible. "
                        "Set ATR_YOUTUBE_CHANNEL_ID to enforce the upload target."
                    ),
                    "youtube_channel_ids": [str(item.get("id") or "") for item in channels if item.get("id")],
                }
            return {"status": "ok", "detail": "YouTube API credentials are valid"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @classmethod
    def _check_meta(cls) -> dict[str, Any]:
        mode = (settings.meta_token_mode or "system_user").strip().lower()
        try:
            creds = MetaTokenService.get_upload_credentials()
        except Exception as exc:
            return {"status": "error", "mode": mode, "detail": f"Meta credential resolution failed: {exc}"}

        if not creds.page_id or not creds.facebook_page_access_token:
            return {
                "status": "error",
                "mode": creds.mode,
                "detail": "Missing Facebook page id or page access token",
            }

        base = f"https://graph.facebook.com/{settings.meta_graph_api_version}"
        try:
            page_resp = requests.get(
                f"{base}/{creds.page_id}",
                params={
                    "fields": "id,name",
                    "access_token": creds.facebook_page_access_token,
                },
                timeout=30,
            )
            if page_resp.status_code >= 400:
                return {
                    "status": "error",
                    "mode": creds.mode,
                    "detail": f"Facebook page check failed: {page_resp.text[:300]}",
                }

            # Ensure token is usable for page video operations (not just generic page read).
            videos_resp = requests.get(
                f"{base}/{creds.page_id}/videos",
                params={
                    "limit": 1,
                    "access_token": creds.facebook_page_access_token,
                },
                timeout=30,
            )
            if videos_resp.status_code >= 400:
                return {
                    "status": "error",
                    "mode": creds.mode,
                    "detail": (
                        "Facebook page video endpoint check failed: "
                        f"{extract_graph_error(videos_resp)}"
                    ),
                }

            if not creds.instagram_business_account_id or not creds.instagram_access_token:
                return {
                    "status": "error",
                    "mode": creds.mode,
                    "detail": "Missing Instagram business account id or Instagram token",
                }

            ig_resp = requests.get(
                f"{base}/{creds.instagram_business_account_id}",
                params={
                    "fields": "id,username",
                    "access_token": creds.instagram_access_token,
                },
                timeout=30,
            )
            if ig_resp.status_code >= 400:
                return {
                    "status": "error",
                    "mode": creds.mode,
                    "detail": f"Instagram account check failed: {ig_resp.text[:300]}",
                }
        except Exception as exc:
            return {
                "status": "error",
                "mode": creds.mode,
                "detail": str(exc),
            }

        return {
            "status": "ok",
            "mode": creds.mode,
            "detail": "Meta Facebook/Instagram credentials are valid",
            "facebook_page_id": creds.page_id,
            "instagram_business_account_id": creds.instagram_business_account_id,
        }

    @classmethod
    def _compute_status(cls, checks: dict[str, dict[str, Any]]) -> str:
        statuses = [entry.get("status") for entry in checks.values()]
        if any(status == "error" for status in statuses):
            return "degraded"
        if any(status == "skipped" for status in statuses):
            return "partial"
        return "ok"

    @classmethod
    def run_startup_health_check(cls) -> dict[str, Any]:
        """Run checks exactly once per server process and cache the result."""
        with cls._lock:
            if cls._cached_result is not None:
                return copy.deepcopy(cls._cached_result)

            checks = {
                "discord": cls._check_discord(),
                "google_drive": cls._check_google_drive(),
                "youtube": cls._check_youtube(),
                "meta": cls._check_meta(),
            }
            result = {
                "status": cls._compute_status(checks),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "run_mode": "startup_once",
                "checks": checks,
            }
            cls._cached_result = result
            return copy.deepcopy(result)

    @classmethod
    def get_cached_health(cls) -> dict[str, Any] | None:
        with cls._lock:
            if cls._cached_result is None:
                return None
            return copy.deepcopy(cls._cached_result)
