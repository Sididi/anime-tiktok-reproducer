"""Tests for AccountService device field handling."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.account_service import AccountService


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "anime_fr.jpg").write_bytes(b"\x89PNG")
    return p


def test_device_field_required(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    avatar: "anime_fr.jpg"
    slots: ["14:00"]
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    with pytest.raises(ValueError, match="anime_fr"):
        AccountService.list_accounts()


def test_device_field_loaded(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    avatar: "anime_fr.jpg"
    device: "iphone_13_pro"
    slots: ["14:00"]
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    accounts = AccountService.list_accounts()
    assert accounts[0].id == "anime_fr"
    assert accounts[0].device == "iphone_13_pro"
