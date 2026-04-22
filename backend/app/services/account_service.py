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
from ..library_types import DEFAULT_LIBRARY_TYPE, LibraryType, coerce_library_type
from .meta_token_service import MetaTokenService, MetaUploadCredentials

logger = logging.getLogger("uvicorn.error")


PLATFORM_KEYS: tuple[str, ...] = ("youtube", "facebook", "instagram", "tiktok")


def _normalize_slots(value: Any) -> list[str] | None:
    """Return None when absent, [] when present-but-invalid, list[str] otherwise."""
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


@dataclass
class AccountSlots:
    hours: list[str]  # e.g. ["14:00", "18:00"]


@dataclass
class AccountYouTubeConfig:
    refresh_token: str
    channel_id: str | None = None
    category_id: str | None = None
    slots: list[str] | None = None


@dataclass
class AccountFacebookConfig:
    slots: list[str] | None = None


@dataclass
class AccountInstagramConfig:
    slots: list[str] | None = None


@dataclass
class AccountTikTokConfig:
    slots: list[str] | None = None


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
    supported_types: list[LibraryType] = field(default_factory=lambda: [DEFAULT_LIBRARY_TYPE])
    avatar: str | None = None
    slots: list[str] = field(default_factory=list)
    youtube: AccountYouTubeConfig | None = None
    meta: AccountMetaConfig | None = None
    facebook: AccountFacebookConfig | None = None
    instagram: AccountInstagramConfig | None = None
    tiktok: AccountTikTokConfig | None = None

    def slots_for(self, platform: str) -> list[str]:
        """Return slot strings for a platform with replace-semantics.

        A per-platform `slots` list (even empty) completely replaces the top-level
        `slots:`. A platform block without `slots` (or no platform block at all)
        inherits the top-level list.
        """
        override: list[str] | None = None
        if platform == "youtube" and self.youtube is not None:
            override = self.youtube.slots
        elif platform == "facebook" and self.facebook is not None:
            override = self.facebook.slots
        elif platform == "instagram" and self.instagram is not None:
            override = self.instagram.slots
        elif platform == "tiktok" and self.tiktok is not None:
            override = self.tiktok.slots
        return list(override) if override is not None else list(self.slots)

    def pool_key_for(self, platform: str) -> str | None:
        """Shared-pool identity for (this account, platform), or None if unshared.

        Two accounts share a platform's reservation pool iff both return the same
        non-None key. None means the account is always alone in its pool for that
        platform.
        """
        if platform == "youtube":
            cid = self.youtube.channel_id if self.youtube else None
            return f"youtube:{cid}" if cid else None
        if platform == "facebook":
            pid = self.meta.facebook_page_id if self.meta else None
            return f"facebook:{pid}" if pid else None
        if platform == "instagram":
            igid = self.meta.instagram_business_account_id if self.meta else None
            return f"instagram:{igid}" if igid else None
        if platform == "tiktok":
            return None
        return None


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
        if isinstance(youtube_raw, dict):
            refresh_token = youtube_raw.get("refresh_token")
            yt_slots = _normalize_slots(youtube_raw.get("slots"))
            # Allow a credential-less youtube block when slot overrides are defined.
            if refresh_token or yt_slots is not None:
                youtube = AccountYouTubeConfig(
                    refresh_token=str(refresh_token) if refresh_token else "",
                    channel_id=str(youtube_raw["channel_id"]) if youtube_raw.get("channel_id") else None,
                    category_id=str(youtube_raw["category_id"]) if youtube_raw.get("category_id") else None,
                    slots=yt_slots,
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

        facebook_raw = raw.get("facebook")
        facebook = (
            AccountFacebookConfig(slots=_normalize_slots(facebook_raw.get("slots")))
            if isinstance(facebook_raw, dict)
            else None
        )

        instagram_raw = raw.get("instagram")
        instagram = (
            AccountInstagramConfig(slots=_normalize_slots(instagram_raw.get("slots")))
            if isinstance(instagram_raw, dict)
            else None
        )

        tiktok_raw = raw.get("tiktok")
        tiktok = (
            AccountTikTokConfig(slots=_normalize_slots(tiktok_raw.get("slots")))
            if isinstance(tiktok_raw, dict)
            else None
        )

        slots_raw = raw.get("slots", [])
        slots = [str(s) for s in slots_raw] if isinstance(slots_raw, list) else []
        supported_types_raw = raw.get("supported_types")
        supported_types: list[LibraryType] = []
        if isinstance(supported_types_raw, list):
            for item in supported_types_raw:
                try:
                    supported_types.append(coerce_library_type(item))
                except ValueError:
                    logger.warning(
                        "Ignoring unsupported library type %r for account %s",
                        item,
                        account_id,
                    )
        if not supported_types:
            supported_types = [DEFAULT_LIBRARY_TYPE]

        return AccountConfig(
            id=account_id,
            name=str(raw.get("name", account_id)),
            language=str(raw.get("language", "")),
            supported_types=supported_types,
            avatar=str(raw.get("avatar")) if raw.get("avatar") else None,
            slots=slots,
            youtube=youtube,
            meta=meta,
            facebook=facebook,
            instagram=instagram,
            tiktok=tiktok,
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
                "supported_types": [item.value for item in acc.supported_types],
                "avatar_url": f"/api/accounts/{acc.id}/avatar",
                "slots": acc.slots,
                "slots_by_platform": {p: acc.slots_for(p) for p in PLATFORM_KEYS},
            }
            for acc in accounts.values()
        ]

    @classmethod
    def get_account(cls, account_id: str) -> AccountConfig | None:
        accounts = cls._ensure_loaded()
        return accounts.get(account_id)

    @classmethod
    def all_accounts(cls) -> dict[str, AccountConfig]:
        """Return a shallow copy of the loaded account cache."""
        return dict(cls._ensure_loaded())

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
            derived_page_token, discovered_ig_id = MetaTokenService._derive_page_credentials_from_token(
                page_id=page_id,
                access_token=page_token,
            )
            if not derived_page_token:
                raise RuntimeError(
                    "Page access token required: derivation failed for account "
                    f"'{account_id}' (page_id={page_id}). Provide a real page access token."
                )
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
