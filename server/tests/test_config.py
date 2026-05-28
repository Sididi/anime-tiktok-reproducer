"""Tests for app.config.Settings.load()."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, Settings


def test_load_minimal_valid_config(example_yaml: Path, example_env, tmp_server_dir: Path):
    s = Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")
    assert s.internal_api_token == "internal_secret"
    assert s.discord.bot_token == "bot_secret"
    assert s.discord.upload_channel_id == "222"
    assert "anime_fr" in s.accounts
    assert s.accounts["anime_fr"].device == "iphone_13_pro"
    assert s.accounts["anime_fr"].avatar == "anime_fr.jpg"
    assert s.data_dir == example_yaml.parent / "data"


def test_account_avatar_must_exist_on_disk(tmp_server_dir: Path, example_env):
    bad = tmp_server_dir / "bad.yaml"
    bad.write_text(
        """\
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
