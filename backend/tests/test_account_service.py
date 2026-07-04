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


def test_device_field_optional(tmp_path: Path, monkeypatch):
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
    accounts = AccountService.list_accounts()
    assert accounts[0].device == ""


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


def test_tiktok_config_parsed(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_16"
    tiktok:
      slots:
        - "20:00"
      post_for_me_account_id: spc_123
      privacy_status: private
      allow_comment: false
      allow_duet: false
      allow_stitch: false
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    account = AccountService.get_account("anime_fr")
    assert account.tiktok.post_for_me_account_id == "spc_123"
    assert account.tiktok.privacy_status == "private"
    assert account.tiktok.allow_comment is False
    assert account.tiktok.allow_duet is False
    assert account.tiktok.allow_stitch is False
    assert account.slots_for("tiktok") == ["20:00"]


def test_tiktok_config_defaults(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  anime_fr:
    name: "Anime FR"
    language: "fr"
    device: "iphone_16"
    tiktok:
      post_for_me_account_id: spc_123
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    account = AccountService.get_account("anime_fr")
    assert account.tiktok.privacy_status == "public"
    assert account.tiktok.allow_comment is True


def test_tiktok_pool_key(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  a1:
    name: "A1"
    language: "fr"
    tiktok:
      post_for_me_account_id: spc_123
  a2:
    name: "A2"
    language: "fr"
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    assert AccountService.get_account("a1").pool_key_for("tiktok") == "tiktok:spc_123"
    assert AccountService.get_account("a2").pool_key_for("tiktok") is None
