"""Server settings loader: YAML structural config + environment secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(RuntimeError):
    """Raised when configuration is missing/invalid at startup."""


@dataclass(frozen=True)
class AccountConfig:
    id: str
    name: str
    language: str
    device: str
    avatar: str


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    guild_id: str
    upload_channel_id: str
    reminder_channel_id: str
    reminder_role_id: str


@dataclass(frozen=True)
class Settings:
    internal_api_token: str
    public_base_url: str
    accounts: dict[str, AccountConfig]
    discord: DiscordConfig
    avatars_dir: Path

    @classmethod
    def load(cls, *, config_path: Path, avatars_dir: Path) -> Settings:
        if not config_path.is_file():
            raise ConfigError(f"Config file not found: {config_path}")

        raw = yaml.safe_load(config_path.read_text()) or {}
        accounts_raw = raw.get("accounts", {}) or {}

        accounts: dict[str, AccountConfig] = {}
        for aid, a in accounts_raw.items():
            account = AccountConfig(
                id=aid,
                name=str(a["name"]),
                language=str(a["language"]),
                device=str(a["device"]),
                avatar=str(a["avatar"]),
            )
            if not (avatars_dir / account.avatar).is_file():
                raise ConfigError(
                    f"Account {aid!r} avatar {account.avatar!r} not found in {avatars_dir}"
                )
            accounts[aid] = account

        def _required_env(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise ConfigError(f"Missing required env var {name}")
            return v

        return cls(
            internal_api_token=_required_env("ATR_TIKTOK_SERVER_INTERNAL_TOKEN"),
            public_base_url=_required_env("ATR_PUBLIC_BASE_URL"),
            accounts=accounts,
            discord=DiscordConfig(
                bot_token=_required_env("ATR_DISCORD_BOT_TOKEN"),
                guild_id=_required_env("ATR_DISCORD_GUILD_ID"),
                upload_channel_id=_required_env("ATR_DISCORD_UPLOAD_CHANNEL_ID"),
                reminder_channel_id=_required_env("ATR_DISCORD_REMINDER_CHANNEL_ID"),
                reminder_role_id=_required_env("ATR_DISCORD_REMINDER_ROLE_ID"),
            ),
            avatars_dir=avatars_dir,
        )
