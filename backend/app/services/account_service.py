from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from google.oauth2.credentials import Credentials

from ..config import settings
from .meta_token_service import MetaUploadCredentials

logger = logging.getLogger("uvicorn.error")


@dataclass
class AccountSlots:
    hours: list[str]  # e.g. ["14:00", "18:00"]


@dataclass
class AccountYouTubeConfig:
    refresh_token: str
    channel_id: str | None = None
    category_id: str | None = None


@dataclass
class AccountMetaConfig:
    token_mode: str = "system_user"
    facebook_page_id: str | None = None
    facebook_page_access_token: str | None = None
    instagram_business_account_id: str | None = None
    instagram_access_token: str | None = None


@dataclass
class AccountConfig:
    id: str
    name: str
    language: str
    avatar: str | None = None
    slots: list[str] = field(default_factory=list)
    youtube: AccountYouTubeConfig | None = None
    meta: AccountMetaConfig | None = None


class AccountService:
    """Loads and caches account configuration from YAML."""

    _lock = Lock()
    _accounts: dict[str, AccountConfig] | None = None

    @classmethod
    def _config_path(cls) -> Path:
        return settings.accounts_config_path

    @classmethod
    def _avatars_dir(cls) -> Path:
        return cls._config_path().parent / "avatars"

    @classmethod
    def _parse_account(cls, account_id: str, raw: dict[str, Any]) -> AccountConfig:
        youtube_raw = raw.get("youtube")
        youtube = None
        if isinstance(youtube_raw, dict) and youtube_raw.get("refresh_token"):
            youtube = AccountYouTubeConfig(
                refresh_token=str(youtube_raw["refresh_token"]),
                channel_id=str(youtube_raw["channel_id"]) if youtube_raw.get("channel_id") else None,
                category_id=str(youtube_raw["category_id"]) if youtube_raw.get("category_id") else None,
            )

        meta_raw = raw.get("meta")
        meta = None
        if isinstance(meta_raw, dict) and (meta_raw.get("facebook_page_id") or meta_raw.get("facebook_page_access_token")):
            meta = AccountMetaConfig(
                token_mode=str(meta_raw.get("token_mode", "system_user")),
                facebook_page_id=str(meta_raw["facebook_page_id"]) if meta_raw.get("facebook_page_id") else None,
                facebook_page_access_token=str(meta_raw["facebook_page_access_token"]) if meta_raw.get("facebook_page_access_token") else None,
                instagram_business_account_id=str(meta_raw["instagram_business_account_id"]) if meta_raw.get("instagram_business_account_id") else None,
                instagram_access_token=str(meta_raw["instagram_access_token"]) if meta_raw.get("instagram_access_token") else None,
            )

        slots_raw = raw.get("slots", [])
        slots = [str(s) for s in slots_raw] if isinstance(slots_raw, list) else []

        return AccountConfig(
            id=account_id,
            name=str(raw.get("name", account_id)),
            language=str(raw.get("language", "")),
            avatar=str(raw.get("avatar")) if raw.get("avatar") else None,
            slots=slots,
            youtube=youtube,
            meta=meta,
        )

    @classmethod
    def _load_from_disk(cls) -> dict[str, AccountConfig]:
        path = cls._config_path()
        if not path.exists():
            logger.info("Account config not found at %s, no accounts loaded", path)
            return {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Failed to parse account config at %s", path)
            return {}
        if not isinstance(raw, dict):
            return {}
        accounts_raw = raw.get("accounts", {})
        if not isinstance(accounts_raw, dict):
            return {}
        result: dict[str, AccountConfig] = {}
        for account_id, account_raw in accounts_raw.items():
            if not isinstance(account_raw, dict):
                continue
            try:
                result[str(account_id)] = cls._parse_account(str(account_id), account_raw)
            except Exception:
                logger.exception("Failed to parse account %s", account_id)
        logger.info("Loaded %d account(s) from %s", len(result), path)
        return result

    @classmethod
    def load(cls) -> None:
        """Load or reload config from disk."""
        with cls._lock:
            cls._accounts = cls._load_from_disk()

    @classmethod
    def _ensure_loaded(cls) -> dict[str, AccountConfig]:
        if cls._accounts is None:
            cls.load()
        assert cls._accounts is not None
        return cls._accounts

    @classmethod
    def list_accounts(cls) -> list[dict[str, Any]]:
        accounts = cls._ensure_loaded()
        return [
            {
                "id": acc.id,
                "name": acc.name,
                "language": acc.language,
                "avatar_url": f"/api/accounts/{acc.id}/avatar",
                "slots": acc.slots,
            }
            for acc in accounts.values()
        ]

    @classmethod
    def get_account(cls, account_id: str) -> AccountConfig | None:
        accounts = cls._ensure_loaded()
        return accounts.get(account_id)

    @classmethod
    def get_avatar_path(cls, account_id: str) -> tuple[Path | None, str]:
        """Find avatar file for account. Returns (path, content_type) or (None, '')."""
        account = cls.get_account(account_id)
        if not account or not account.avatar:
            return None, ""
        avatars_dir = cls._avatars_dir()
        # Try exact filename first
        exact = avatars_dir / account.avatar
        if exact.is_file():
            ct = mimetypes.guess_type(str(exact))[0] or "image/jpeg"
            return exact, ct
        # Scan directory for matching stem
        stem = Path(account.avatar).stem
        if avatars_dir.is_dir():
            for candidate in avatars_dir.iterdir():
                if candidate.is_file() and candidate.stem == stem:
                    ct = mimetypes.guess_type(str(candidate))[0] or "image/jpeg"
                    return candidate, ct
        return None, ""

    @classmethod
    def get_youtube_credentials(cls, account_id: str) -> Credentials:
        """Build Google OAuth Credentials for a specific account's YouTube."""
        account = cls.get_account(account_id)
        if not account or not account.youtube:
            raise ValueError(f"No YouTube config for account {account_id}")
        if not settings.google_client_id or not settings.google_client_secret:
            raise RuntimeError(
                "YouTube requires global ATR_GOOGLE_CLIENT_ID and ATR_GOOGLE_CLIENT_SECRET"
            )
        return Credentials(
            token=None,
            refresh_token=account.youtube.refresh_token,
            token_uri=settings.google_token_uri,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=[
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.force-ssl",
            ],
        )

    @classmethod
    def get_meta_credentials(cls, account_id: str) -> MetaUploadCredentials:
        """Build Meta upload credentials from account config.

        For system_user mode, the configured token may be a system-user token
        rather than a page-scoped token.  We derive the real page token (and
        discover the Instagram business account) the same way the global
        MetaTokenService does.
        """
        account = cls.get_account(account_id)
        if not account or not account.meta:
            raise ValueError(f"No Meta config for account {account_id}")
        meta = account.meta

        page_id = meta.facebook_page_id
        page_token = meta.facebook_page_access_token
        ig_user_id = meta.instagram_business_account_id
        ig_token = meta.instagram_access_token or page_token

        # Derive page-scoped token from system-user token when needed
        if meta.token_mode == "system_user" and page_id and page_token:
            from .meta_token_service import MetaTokenService
            derived_page_token, discovered_ig_id = MetaTokenService._derive_page_credentials_from_token(
                page_id=page_id,
                access_token=page_token,
            )
            if derived_page_token:
                page_token = derived_page_token
                if not meta.instagram_access_token:
                    ig_token = derived_page_token
            if not ig_user_id and discovered_ig_id:
                ig_user_id = discovered_ig_id

        if not ig_user_id and page_id and page_token:
            ig_user_id = MetaTokenService._discover_ig_user_id(
                page_id=page_id,
                page_access_token=page_token,
            )

        return MetaUploadCredentials(
            page_id=page_id,
            facebook_page_access_token=page_token,
            instagram_business_account_id=ig_user_id,
            instagram_access_token=ig_token,
            mode=meta.token_mode,
        )
