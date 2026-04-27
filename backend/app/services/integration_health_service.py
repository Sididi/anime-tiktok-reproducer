from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any
import copy

import requests
from googleapiclient.discovery import build

from ..config import settings
from ..library_types import LibraryType
from ..utils.meta_graph import extract_graph_error
from .account_service import AccountService
from .discord_service import DiscordService
from .elevenlabs_service import ElevenLabsService
from .llm_service import LLMService
from .google_drive_service import GoogleDriveService
from .meta_token_service import MetaTokenService
from .social_upload_service import SocialUploadService


class IntegrationHealthService:
    """Runs integration checks once at startup and serves cached result."""

    _lock = Lock()
    _cached_result: dict[str, Any] | None = None

    @classmethod
    def _startup_task_payload(
        cls,
        *,
        status: str,
        detail: str,
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "detail": detail,
            "checked_at": checked_at,
        }

    @classmethod
    def _empty_result(cls) -> dict[str, Any]:
        return {
            "status": "pending",
            "checked_at": None,
            "run_mode": "startup_background",
            "summary_status": None,
            "global": {},
            "checks": {},
            "startup": {
                "integration_checks": cls._startup_task_payload(
                    status="pending",
                    detail="Integration checks queued.",
                ),
                "storage_box_catalogs": {
                    "status": "pending",
                    "detail": "Storage Box catalog warmup queued.",
                    "checked_at": None,
                    "libraries": {},
                },
            },
        }

    @classmethod
    def _all_check_statuses(
        cls,
        checks: dict[str, dict[str, Any]],
        account_checks: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        all_statuses = [str(entry.get("status") or "") for entry in checks.values()]
        if account_checks:
            for acc_checks in account_checks.values():
                if not isinstance(acc_checks, dict):
                    continue
                for check in acc_checks.values():
                    if isinstance(check, dict):
                        all_statuses.append(str(check.get("status") or ""))
        return [status for status in all_statuses if status]

    @classmethod
    def _compute_summary_status(
        cls,
        checks: dict[str, dict[str, Any]],
        account_checks: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        all_statuses = cls._all_check_statuses(checks, account_checks)
        if any(status == "error" for status in all_statuses):
            return "degraded"
        if any(status == "skipped" for status in all_statuses):
            return "partial"
        return "ok"

    @classmethod
    def _map_summary_to_task_status(
        cls,
        *,
        summary_status: str,
        checks: dict[str, dict[str, Any]],
        account_checks: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        all_statuses = cls._all_check_statuses(checks, account_checks)
        if not all_statuses or all(status == "skipped" for status in all_statuses):
            return "skipped"
        if summary_status == "degraded":
            return "error"
        return "ok"

    @classmethod
    def _recompute_catalog_status(cls, payload: dict[str, Any]) -> None:
        startup = payload.setdefault("startup", {})
        catalog_state = startup.setdefault(
            "storage_box_catalogs",
            {
                "status": "pending",
                "detail": "Storage Box catalog warmup queued.",
                "checked_at": None,
                "libraries": {},
            },
        )
        libraries = catalog_state.setdefault("libraries", {})
        statuses = [
            str(entry.get("status") or "")
            for entry in libraries.values()
            if isinstance(entry, dict)
        ]
        if not statuses:
            catalog_state["status"] = "skipped"
            return
        if any(status == "pending" for status in statuses):
            catalog_state["status"] = "pending"
            return
        if any(status == "error" for status in statuses):
            catalog_state["status"] = "error"
            return
        if all(status == "skipped" for status in statuses):
            catalog_state["status"] = "skipped"
            return
        catalog_state["status"] = "ok"

    @classmethod
    def _recompute_overall_status(cls, payload: dict[str, Any]) -> None:
        startup = payload.setdefault("startup", {})
        component_statuses = [
            str(startup.get("integration_checks", {}).get("status") or "skipped"),
            str(startup.get("storage_box_catalogs", {}).get("status") or "skipped"),
        ]
        if any(status == "pending" for status in component_statuses):
            payload["status"] = "pending"
            return
        if any(status == "error" for status in component_statuses):
            payload["status"] = "error"
            return
        if all(status == "skipped" for status in component_statuses):
            payload["status"] = "skipped"
            return
        payload["status"] = "ok"

    @classmethod
    def initialize_startup_state(
        cls,
        *,
        integration_enabled: bool,
        storage_box_enabled: bool,
        library_types: list[LibraryType] | None = None,
    ) -> dict[str, Any]:
        with cls._lock:
            payload = cls._empty_result()
            now = datetime.now(timezone.utc).isoformat()

            if integration_enabled:
                payload["startup"]["integration_checks"] = cls._startup_task_payload(
                    status="pending",
                    detail="Integration checks queued.",
                )
            else:
                payload["startup"]["integration_checks"] = cls._startup_task_payload(
                    status="skipped",
                    detail="Integration startup health checks are disabled.",
                    checked_at=now,
                )

            libraries: dict[str, dict[str, Any]] = {}
            if storage_box_enabled:
                for library_type in library_types or []:
                    libraries[library_type.value] = cls._startup_task_payload(
                        status="pending",
                        detail="Catalog warmup queued.",
                    )
                payload["startup"]["storage_box_catalogs"] = {
                    "status": "pending",
                    "detail": "Storage Box catalog warmup queued.",
                    "checked_at": None,
                    "libraries": libraries,
                }
                cls._recompute_catalog_status(payload)
            else:
                payload["startup"]["storage_box_catalogs"] = {
                    "status": "skipped",
                    "detail": "Storage Box is disabled.",
                    "checked_at": now,
                    "libraries": {},
                }

            payload["checked_at"] = now
            cls._recompute_overall_status(payload)
            cls._cached_result = payload
            return copy.deepcopy(payload)

    @classmethod
    def update_catalog_warmup(
        cls,
        *,
        library_type: LibraryType | str,
        status: str,
        detail: str,
    ) -> dict[str, Any]:
        scoped_type = (
            library_type.value
            if isinstance(library_type, LibraryType)
            else str(library_type).strip()
        )
        now = datetime.now(timezone.utc).isoformat()
        with cls._lock:
            payload = copy.deepcopy(cls._cached_result) if cls._cached_result else cls._empty_result()
            catalog_state = payload["startup"]["storage_box_catalogs"]
            libraries = catalog_state.setdefault("libraries", {})
            libraries[scoped_type] = cls._startup_task_payload(
                status=status,
                detail=detail,
                checked_at=now if status != "pending" else None,
            )
            catalog_state["checked_at"] = now
            cls._recompute_catalog_status(payload)
            cls._recompute_overall_status(payload)
            payload["checked_at"] = now
            cls._cached_result = payload
            return copy.deepcopy(payload)

    @classmethod
    def record_integration_check_failure(cls, detail: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with cls._lock:
            payload = copy.deepcopy(cls._cached_result) if cls._cached_result else cls._empty_result()
            payload["startup"]["integration_checks"] = cls._startup_task_payload(
                status="error",
                detail=detail,
                checked_at=now,
            )
            payload["summary_status"] = "degraded"
            payload["checked_at"] = now
            cls._recompute_overall_status(payload)
            cls._cached_result = payload
            return copy.deepcopy(payload)

    @classmethod
    def _check_discord(cls) -> dict[str, Any]:
        if not DiscordService.is_configured():
            return {"status": "skipped", "detail": "TikTok server not configured"}
        # Active probe: ping the VPS /healthz to confirm it's reachable.
        base = settings.tiktok_server_base_url or ""
        try:
            import httpx
            with httpx.Client(timeout=5.0) as c:
                r = c.get(f"{base.rstrip('/')}/healthz")
                r.raise_for_status()
            return {"status": "ok", "detail": f"TikTok server reachable at {base}"}
        except Exception as exc:
            return {
                "status": "degraded",
                "detail": f"TikTok server unreachable at {base}: {exc}",
            }

    @classmethod
    def _check_google_drive(cls) -> dict[str, Any]:
        if not GoogleDriveService.is_configured():
            return {"status": "skipped", "detail": "Google Drive credentials are not fully configured"}

        ok, detail = GoogleDriveService.verify_parent_folder_access()
        if not ok:
            return {"status": "error", "detail": detail}
        return {"status": "ok", "detail": detail}

    @classmethod
    def _check_llm_api(cls) -> dict[str, Any]:
        return LLMService.check_api_health()

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
    def run_startup_health_check(cls) -> dict[str, Any]:
        """Run startup checks and update the cached readiness payload."""
        global_checks = {
            "discord": cls._check_discord(),
            "google_drive": cls._check_google_drive(),
            "llm_api": cls._check_llm_api(),
            "elevenlabs_api": cls._check_elevenlabs_api(),
        }

        accounts = AccountService.list_accounts()
        account_checks: dict[str, dict[str, Any]] = {}
        if accounts:
            for acc in accounts:
                acc_id = acc.id
                acc_result: dict[str, Any] = {}
                account_cfg = AccountService.get_account(acc_id)
                if account_cfg and account_cfg.youtube:
                    acc_result["youtube"] = cls._check_account_youtube(acc_id)
                if account_cfg and account_cfg.meta:
                    acc_result["meta"] = cls._check_account_meta(acc_id)
                if acc_result:
                    account_checks[acc_id] = acc_result
        else:
            global_checks["youtube"] = cls._check_youtube()
            global_checks["meta"] = cls._check_meta()

        summary_status = cls._compute_summary_status(global_checks, account_checks)
        task_status = cls._map_summary_to_task_status(
            summary_status=summary_status,
            checks=global_checks,
            account_checks=account_checks,
        )
        now = datetime.now(timezone.utc).isoformat()

        with cls._lock:
            payload = copy.deepcopy(cls._cached_result) if cls._cached_result else cls._empty_result()
            payload["run_mode"] = "startup_background"
            payload["summary_status"] = summary_status
            payload["global"] = global_checks
            payload["checks"] = global_checks
            if account_checks:
                payload["accounts"] = account_checks
            else:
                payload.pop("accounts", None)
            payload["startup"]["integration_checks"] = cls._startup_task_payload(
                status=task_status,
                detail="Integration checks complete.",
                checked_at=now,
            )
            payload["checked_at"] = now
            cls._recompute_overall_status(payload)
            cls._cached_result = payload
            return copy.deepcopy(payload)

    @classmethod
    def get_cached_health(cls) -> dict[str, Any] | None:
        with cls._lock:
            if cls._cached_result is None:
                return None
            return copy.deepcopy(cls._cached_result)
