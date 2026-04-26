"""Tests for app.config.Settings.load()."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, ConfigError


def test_load_minimal_valid_config(example_yaml: Path, example_env, tmp_server_dir: Path):
    s = Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")
    assert s.internal_api_token == "internal_secret"
    assert s.discord.bot_token == "bot_secret"
    assert s.discord.upload_channel_id == "222"
    assert "anime_fr" in s.accounts
    assert s.accounts["anime_fr"].device == "iphone_13_pro"
    assert s.accounts["anime_fr"].avatar == "anime_fr.jpg"
    assert s.devices["iphone_13_pro"].platform == "ios"


def test_resolve_device_for_token(example_yaml: Path, example_env, tmp_server_dir: Path):
    s = Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")
    assert s.resolve_device_for_token("mobile_secret") == "iphone_13_pro"
    assert s.resolve_device_for_token("wrong") is None


def test_missing_device_token_raises(example_yaml: Path, monkeypatch, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_INTERNAL_TOKEN", "x")
    monkeypatch.setenv("ATR_DISCORD_BOT_TOKEN", "x")
    monkeypatch.setenv("ATR_DISCORD_GUILD_ID", "x")
    monkeypatch.setenv("ATR_DISCORD_UPLOAD_CHANNEL_ID", "x")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_CHANNEL_ID", "x")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_ROLE_ID", "x")
    monkeypatch.setenv("ATR_PUBLIC_BASE_URL", "x")
    monkeypatch.delenv("ATR_MOBILE_TOKEN_IPHONE_13_PRO", raising=False)
    with pytest.raises(ConfigError, match="ATR_MOBILE_TOKEN_IPHONE_13_PRO"):
        Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")


def test_account_device_must_exist_in_devices(tmp_server_dir: Path, example_env):
    bad = tmp_server_dir / "bad.yaml"
    bad.write_text(
        """\
devices: {iphone_13_pro: {platform: ios}}
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "missing_device"
    avatar: "anime_fr.jpg"
"""
    )
    (tmp_server_dir / "avatars" / "anime_fr.jpg").write_bytes(b"\x89PNG")
    with pytest.raises(ConfigError, match="missing_device"):
        Settings.load(config_path=bad, avatars_dir=tmp_server_dir / "avatars")


def test_account_avatar_must_exist_on_disk(tmp_server_dir: Path, example_env):
    bad = tmp_server_dir / "bad.yaml"
    bad.write_text(
        """\
devices: {iphone_13_pro: {platform: ios}}
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_13_pro"
    avatar: "missing.png"
"""
    )
    with pytest.raises(ConfigError, match="missing.png"):
        Settings.load(config_path=bad, avatars_dir=tmp_server_dir / "avatars")


def test_missing_config_file_raises(tmp_server_dir: Path, example_env):
    missing = tmp_server_dir / "does-not-exist.yaml"
    with pytest.raises(ConfigError, match="not found"):
        Settings.load(config_path=missing, avatars_dir=tmp_server_dir / "avatars")
