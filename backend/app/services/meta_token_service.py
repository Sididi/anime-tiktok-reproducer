from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
import json

import requests

from ..config import settings
from ..utils.meta_graph import extract_graph_error


@dataclass
class MetaUploadCredentials:
    page_id: str | None
    facebook_page_access_token: str | None
    instagram_business_account_id: str | None
    instagram_access_token: str | None
    mode: str


class MetaTokenService:
    """Resolves Meta credentials for upload flows with lifecycle handling."""

    _state_lock = Lock()

    @classmethod
    def _state_file_path(cls) -> Path:
        return settings.data_dir / "meta_token_state.json"

    @classmethod
    def _load_state(cls) -> dict[str, Any]:
        path = cls._state_file_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text())
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
        return {}

    @classmethod
    def _save_state(cls, payload: dict[str, Any]) -> None:
        path = cls._state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=True))

    @classmethod
    def _parse_datetime(cls, value: str | None) -> datetime | None:
        if not value:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.endswith("Z"):
            candidate = f"{candidate[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _now_utc(cls) -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _graph_base(cls) -> str:
        return f"https://graph.facebook.com/{settings.meta_graph_api_version}"

    @classmethod
    def _needs_refresh(cls, expires_at: datetime | None) -> bool:
        if expires_at is None:
            return True
        lead_seconds = max(settings.meta_user_token_refresh_lead_seconds, 0)
        return cls._now_utc() + timedelta(seconds=lead_seconds) >= expires_at

    @classmethod
    def _exchange_user_token(cls, access_token: str) -> tuple[str, datetime]:
        app_id = settings.meta_app_id
        app_secret = settings.meta_app_secret
        if not app_id or not app_secret:
            raise RuntimeError(
                "Meta long_lived_user mode requires ATR_META_APP_ID and ATR_META_APP_SECRET"
            )

        resp = requests.get(
            "https://graph.facebook.com/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": access_token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Meta user token exchange failed: {extract_graph_error(resp)}"
            )
        payload = resp.json()
        refreshed = payload.get("access_token")
        if not refreshed:
            raise RuntimeError(f"Meta user token exchange returned no access_token: {payload}")
        expires_in = int(payload.get("expires_in", 60 * 24 * 3600))
        return str(refreshed), cls._now_utc() + timedelta(seconds=expires_in)

    @classmethod
    def _resolve_long_lived_user_token(cls) -> str:
        with cls._state_lock:
            state = cls._load_state()
            token = state.get("meta_user_access_token") or settings.meta_user_access_token
            expires_at = cls._parse_datetime(
                state.get("meta_user_access_token_expires_at")
                or settings.meta_user_access_token_expires_at
            )
            if not token:
                raise RuntimeError(
                    "Meta long_lived_user mode requires ATR_META_USER_ACCESS_TOKEN"
                )

            needs_refresh = cls._needs_refresh(expires_at)
            if needs_refresh:
                try:
                    token, expires_at = cls._exchange_user_token(str(token))
                except Exception as exc:
                    if expires_at is None or cls._now_utc() >= expires_at:
                        raise RuntimeError(
                            "Meta user token refresh failed and token is expired. "
                            f"Re-authentication required. Root cause: {exc}"
                        )
                    # Keep using current token if still valid.

            cls._save_state(
                {
                    "meta_user_access_token": str(token),
                    "meta_user_access_token_expires_at": expires_at.isoformat() if expires_at else None,
                    "updated_at": cls._now_utc().isoformat(),
                }
            )
            return str(token)

    @classmethod
    def _resolve_page_token_from_user_token(
        cls,
        user_token: str,
    ) -> tuple[str, str, str | None]:
        configured_page_id = settings.facebook_page_id
        url = f"{cls._graph_base()}/me/accounts"
        params = {
            "fields": "id,name,access_token,instagram_business_account{id}",
            "access_token": user_token,
        }
        pages: list[dict[str, Any]] = []
        while url:
            resp = requests.get(url, params=params, timeout=30)
            params = None
            if resp.status_code >= 400:
                raise RuntimeError(
                    "Failed to resolve page token from user token: "
                    f"{extract_graph_error(resp)}"
                )
            payload = resp.json()
            batch = payload.get("data", [])
            if isinstance(batch, list):
                pages.extend(item for item in batch if isinstance(item, dict))
            paging = payload.get("paging") or {}
            next_url = paging.get("next") if isinstance(paging, dict) else None
            url = str(next_url) if next_url else ""
        if not pages:
            raise RuntimeError("No pages returned by /me/accounts for provided Meta user token")

        selected: dict[str, Any] | None = None
        if configured_page_id:
            for page in pages:
                if str(page.get("id")) == configured_page_id:
                    selected = page
                    break
            if selected is None:
                raise RuntimeError(
                    f"Configured ATR_FACEBOOK_PAGE_ID={configured_page_id} "
                    "is not available in /me/accounts response"
                )
        elif len(pages) == 1:
            selected = pages[0]
        else:
            raise RuntimeError(
                "Multiple pages available; set ATR_FACEBOOK_PAGE_ID to disambiguate"
            )

        page_id = str(selected.get("id") or "")
        page_token = str(selected.get("access_token") or "")
        ig_info = selected.get("instagram_business_account") or {}
        discovered_ig_id = str(ig_info.get("id")) if isinstance(ig_info, dict) and ig_info.get("id") else None

        if not page_id or not page_token:
            raise RuntimeError(f"Could not extract page token from /me/accounts: {selected}")
        return page_id, page_token, discovered_ig_id

    @classmethod
    def _discover_ig_user_id(
        cls,
        *,
        page_id: str,
        page_access_token: str,
    ) -> str | None:
        resp = requests.get(
            f"{cls._graph_base()}/{page_id}",
            params={
                "fields": "instagram_business_account{id}",
                "access_token": page_access_token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            return None
        payload = resp.json()
        ig = payload.get("instagram_business_account")
        if isinstance(ig, dict) and ig.get("id"):
            return str(ig["id"])
        return None

    @classmethod
    def _derive_page_credentials_from_token(
        cls,
        *,
        page_id: str,
        access_token: str,
    ) -> tuple[str | None, str | None]:
        """
        Best effort: derive a page-scoped token (and linked IG id) from a broader token.

        For system user setups, users often provide a system-user token directly.
        Some endpoints (for example /{page-id}/videos) still require a page token.
        """
        resp = requests.get(
            f"{cls._graph_base()}/{page_id}",
            params={
                "fields": "access_token,instagram_business_account{id}",
                "access_token": access_token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            return None, None
        payload = resp.json()
        page_token = payload.get("access_token")
        ig_obj = payload.get("instagram_business_account")
        ig_id = str(ig_obj.get("id")) if isinstance(ig_obj, dict) and ig_obj.get("id") else None
        if page_token:
            return str(page_token), ig_id
        return None, ig_id

    @classmethod
    def _system_user_credentials(cls) -> MetaUploadCredentials:
        page_id = settings.facebook_page_id
        page_token = settings.facebook_page_access_token
        ig_user_id = settings.instagram_business_account_id
        ig_token = settings.instagram_access_token or page_token

        if page_id and page_token:
            derived_page_token, discovered_ig_id = cls._derive_page_credentials_from_token(
                page_id=page_id,
                access_token=page_token,
            )
            if not derived_page_token:
                raise RuntimeError(
                    "Page access token required: derivation failed for configured system-user token. "
                    f"Unable to resolve page-scoped token for page {page_id} via /{page_id}?fields=access_token."
                )
            page_token = derived_page_token
            # Default Instagram token to the derived page token when no explicit token is set.
            if not settings.instagram_access_token:
                ig_token = derived_page_token
            if not ig_user_id and discovered_ig_id:
                ig_user_id = discovered_ig_id

        if not ig_user_id and page_id and page_token:
            ig_user_id = cls._discover_ig_user_id(
                page_id=page_id,
                page_access_token=page_token,
            )

        return MetaUploadCredentials(
            page_id=page_id,
            facebook_page_access_token=page_token,
            instagram_business_account_id=ig_user_id,
            instagram_access_token=ig_token,
            mode="system_user",
        )

    @classmethod
    def _long_lived_user_credentials(cls) -> MetaUploadCredentials:
        user_token = cls._resolve_long_lived_user_token()
        page_id, page_token, discovered_ig_id = cls._resolve_page_token_from_user_token(user_token)
        ig_user_id = settings.instagram_business_account_id or discovered_ig_id
        if not ig_user_id:
            ig_user_id = cls._discover_ig_user_id(
                page_id=page_id,
                page_access_token=page_token,
            )

        return MetaUploadCredentials(
            page_id=page_id,
            facebook_page_access_token=page_token,
            instagram_business_account_id=ig_user_id,
            instagram_access_token=page_token,
            mode="long_lived_user",
        )

    @classmethod
    def get_upload_credentials(cls) -> MetaUploadCredentials:
        mode = (settings.meta_token_mode or "system_user").strip().lower()
        if mode not in {"system_user", "long_lived_user"}:
            raise RuntimeError(
                f"Invalid ATR_META_TOKEN_MODE={settings.meta_token_mode}. "
                "Expected 'system_user' or 'long_lived_user'."
            )
        if mode == "long_lived_user":
            return cls._long_lived_user_credentials()
        return cls._system_user_credentials()
