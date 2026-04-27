"""Shared pytest fixtures for the VPS server test suite."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_server_dir(tmp_path: Path) -> Path:
    """A temporary server-root with empty avatars/ and data/."""
    (tmp_path / "avatars").mkdir()
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture
def example_avatar(tmp_server_dir: Path) -> Path:
    """A 1x1 PNG file under tmp_server_dir/avatars/anime_fr.jpg."""
    # 1x1 PNG (smallest valid)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63f8cf00000000050001a5f645450000000049454e"
        "44ae426082"
    )
    p = tmp_server_dir / "avatars" / "anime_fr.jpg"
    p.write_bytes(png_bytes)
    return p


@pytest.fixture
def example_yaml(tmp_server_dir: Path, example_avatar: Path) -> Path:
    """A minimal but valid config YAML."""
    yaml_text = """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_13_pro"
    avatar: "anime_fr.jpg"
"""
    p = tmp_server_dir / "config.yaml"
    p.write_text(yaml_text)
    return p


@pytest.fixture
def example_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Sets the env vars referenced by the example_yaml fixture."""
    monkeypatch.setenv("ATR_TIKTOK_SERVER_INTERNAL_TOKEN", "internal_secret")
    monkeypatch.setenv("ATR_DISCORD_BOT_TOKEN", "bot_secret")
    monkeypatch.setenv("ATR_DISCORD_GUILD_ID", "111")
    monkeypatch.setenv("ATR_DISCORD_UPLOAD_CHANNEL_ID", "222")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_CHANNEL_ID", "333")
    monkeypatch.setenv("ATR_DISCORD_REMINDER_ROLE_ID", "444")
    monkeypatch.setenv("ATR_PUBLIC_BASE_URL", "https://tiktok.sididi.tv")
    yield
