from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any
import copy

import requests
from googleapiclient.discovery import build

from ..config import settings
from ..utils.meta_graph import extract_graph_error
from .account_service import AccountService
from .discord_service import DiscordService
from .elevenlabs_service import ElevenLabsService
from .gemini_service import GeminiService
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
    def _check_gemini_api(cls) -> dict[str, Any]:
        return GeminiService.check_api_health()

    @classmethod
    def _check_elevenlabs_api(cls) -> dict[str, Any]:
        return ElevenLabsService.check_api_health()

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
    def _meta_endpoint_failure_detail(cls, endpoint: str, response: requests.Response) -> str:
        return (
            f"Meta endpoint `{endpoint}` failed: {extract_graph_error(response)}. "
            "Action: use a PAGE access token with pages_show_list, pages_read_engagement and pages_manage_posts."
        )

    @classmethod
    def _check_meta_page_endpoints(
        cls,
        *,
        base: str,
        page_id: str,
        page_access_token: str,
        mode: str | None = None,
    ) -> dict[str, Any] | None:
        checks = (
            (f"/{page_id}", {"fields": "id,name"}),
            (f"/{page_id}/videos", {"limit": 1}),
            (f"/{page_id}/video_reels", {"limit": 1}),
            (f"/{page_id}/posts", {"limit": 1}),
        )
        for endpoint, params in checks:
            payload = {"access_token": page_access_token, **params}
            resp = requests.get(f"{base}{endpoint}", params=payload, timeout=30)
            if resp.status_code >= 400:
                detail = cls._meta_endpoint_failure_detail(endpoint, resp)
                result: dict[str, Any] = {"status": "error", "detail": detail}
                if mode:
                    result["mode"] = mode
                return result
        return None

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
            endpoints_error = cls._check_meta_page_endpoints(
                base=base,
                page_id=creds.page_id,
                page_access_token=creds.facebook_page_access_token,
                mode=creds.mode,
            )
            if endpoints_error:
                return endpoints_error

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
                    "detail": (
                        "Instagram account check failed: "
                        f"{extract_graph_error(ig_resp)}"
                    ),
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
    def _check_account_youtube(cls, account_id: str) -> dict[str, Any]:
        """Check YouTube access for a specific account."""
        try:
            creds = AccountService.get_youtube_credentials(account_id)
            from google.auth.transport.requests import Request as _Request
            creds.refresh(_Request())
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            channels: list[dict[str, Any]] = []
            request = youtube.channels().list(part="id,snippet", mine=True, maxResults=50)
            while request is not None:
                response = request.execute()
                batch = response.get("items", [])
                if isinstance(batch, list):
                    channels.extend(item for item in batch if isinstance(item, dict))
                request = youtube.channels().list_next(request, response)
            if not channels:
                return {"status": "error", "detail": "No YouTube channel found for account credentials"}
            account = AccountService.get_account(account_id)
            channel_id = account.youtube.channel_id if account and account.youtube else None
            if channel_id:
                if channel_id not in {str(c.get("id", "")) for c in channels}:
                    return {"status": "error", "detail": f"Channel {channel_id} not accessible"}
                return {"status": "ok", "detail": f"Channel {channel_id} accessible"}
            return {"status": "ok", "detail": f"{len(channels)} channel(s) accessible"}
        except ValueError as exc:
            return {"status": "skipped", "detail": str(exc)}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @classmethod
    def _check_account_meta(cls, account_id: str) -> dict[str, Any]:
        """Check Meta (Facebook + Instagram) access for a specific account."""
        try:
            meta_creds = AccountService.get_meta_credentials(account_id)
        except ValueError as exc:
            return {"status": "skipped", "detail": str(exc)}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

        if not meta_creds.page_id or not meta_creds.facebook_page_access_token:
            return {"status": "error", "detail": "Missing page_id or page_access_token"}

        base = f"https://graph.facebook.com/{settings.meta_graph_api_version}"
        try:
            endpoints_error = cls._check_meta_page_endpoints(
                base=base,
                page_id=meta_creds.page_id,
                page_access_token=meta_creds.facebook_page_access_token,
            )
            if endpoints_error:
                return endpoints_error

            if meta_creds.instagram_business_account_id and meta_creds.instagram_access_token:
                ig_resp = requests.get(
                    f"{base}/{meta_creds.instagram_business_account_id}",
                    params={"fields": "id,username", "access_token": meta_creds.instagram_access_token},
                    timeout=30,
                )
                if ig_resp.status_code >= 400:
                    return {
                        "status": "error",
                        "detail": f"Instagram check failed: {extract_graph_error(ig_resp)}",
                    }

            return {"status": "ok", "detail": "Meta credentials valid"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    @classmethod
    def _compute_status(cls, checks: dict[str, dict[str, Any]], account_checks: dict[str, dict[str, Any]] | None = None) -> str:
        all_statuses = [entry.get("status") for entry in checks.values()]
        if account_checks:
            for acc_checks in account_checks.values():
                if isinstance(acc_checks, dict):
                    for check in acc_checks.values():
                        if isinstance(check, dict):
                            all_statuses.append(check.get("status"))
        if any(status == "error" for status in all_statuses):
            return "degraded"
        if any(status == "skipped" for status in all_statuses):
            return "partial"
        return "ok"

    @classmethod
    def run_startup_health_check(cls) -> dict[str, Any]:
        """Run checks exactly once per server process and cache the result."""
        with cls._lock:
            if cls._cached_result is not None:
                return copy.deepcopy(cls._cached_result)

            global_checks = {
                "discord": cls._check_discord(),
                "google_drive": cls._check_google_drive(),
                "gemini_api": cls._check_gemini_api(),
                "elevenlabs_api": cls._check_elevenlabs_api(),
            }

            # Per-account checks
            accounts = AccountService.list_accounts()
            account_checks: dict[str, dict[str, Any]] = {}
            if accounts:
                for acc in accounts:
                    acc_id = acc["id"]
                    acc_result: dict[str, Any] = {}
                    account_cfg = AccountService.get_account(acc_id)
                    if account_cfg and account_cfg.youtube:
                        acc_result["youtube"] = cls._check_account_youtube(acc_id)
                    if account_cfg and account_cfg.meta:
                        acc_result["meta"] = cls._check_account_meta(acc_id)
                    if acc_result:
                        account_checks[acc_id] = acc_result
            else:
                # No accounts configured: fall back to global YouTube + Meta checks
                global_checks["youtube"] = cls._check_youtube()
                global_checks["meta"] = cls._check_meta()

            result: dict[str, Any] = {
                "status": cls._compute_status(global_checks, account_checks),
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "run_mode": "startup_once",
                "global": global_checks,
            }
            if account_checks:
                result["accounts"] = account_checks

            # Keep "checks" key for backwards compat when no accounts
            if not accounts:
                result["checks"] = global_checks

            cls._cached_result = result
            return copy.deepcopy(result)

    @classmethod
    def get_cached_health(cls) -> dict[str, Any] | None:
        with cls._lock:
            if cls._cached_result is None:
                return None
            return copy.deepcopy(cls._cached_result)
