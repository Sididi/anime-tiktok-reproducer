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
      post_for_me_platform: tiktok_business
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
    assert account.tiktok.post_for_me_platform == "tiktok_business"
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
    assert account.tiktok.post_for_me_platform == "tiktok"
    assert account.tiktok.privacy_status == "public"
    assert account.tiktok.allow_comment is True


def test_tiktok_config_rejects_unknown_post_for_me_platform():
    with pytest.raises(ValueError, match="post_for_me_platform"):
        AccountService._parse_account(
            "anime_fr",
            {
                "name": "Anime FR",
                "language": "fr",
                "tiktok": {
                    "post_for_me_account_id": "spc_123",
                    "post_for_me_platform": "tiktok_enterprise",
                },
            },
        )


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


def test_independent_reel_limits_and_conservative_fallback(tmp_path: Path, monkeypatch):
    cfg = _write_config(
        tmp_path,
        """\
accounts:
  verified:
    name: Verified
    language: fr
    facebook:
      max_reel_duration_seconds: 14400
    instagram:
      max_reel_duration_seconds: 1200
  fallback:
    name: Fallback
    language: fr
    facebook:
      max_reel_duration_seconds: invalid
""",
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", cfg
    )
    AccountService.invalidate()
    assert AccountService.get_account("verified").max_reel_duration_for("facebook") == 14400
    assert AccountService.get_account("verified").max_reel_duration_for("instagram") == 1200
    assert AccountService.get_account("fallback").max_reel_duration_for("facebook") == 90
    assert AccountService.get_account("fallback").max_reel_duration_for("instagram") == 90
    rows = {row["id"]: row for row in AccountService.list_accounts_as_dicts()}
    assert rows["verified"]["max_reel_duration_seconds_by_platform"] == {
        "facebook": 14400,
        "instagram": 1200,
    }


# ---------------------------------------------------------------------------
# Template system: unit tests for the merge/resolution helpers


def test_deep_merge_nested_dicts_and_list_replacement():
    from app.services.account_service import _deep_merge

    base = {
        "slots": ["06:00", "12:00"],
        "youtube": {"refresh_token": "tok", "slots": ["10:00"]},
        "name": "Base",
    }
    override = {
        "slots": ["08:00"],
        "youtube": {"slots": ["11:00"]},
    }
    merged = _deep_merge(base, override)
    # Lists replace wholesale, dicts merge key-by-key.
    assert merged["slots"] == ["08:00"]
    assert merged["youtube"] == {"refresh_token": "tok", "slots": ["11:00"]}
    assert merged["name"] == "Base"
    # Inputs are not mutated.
    assert base["slots"] == ["06:00", "12:00"]
    assert base["youtube"]["slots"] == ["10:00"]
    assert override["youtube"] == {"slots": ["11:00"]}


def test_resolve_templates_order_and_strip():
    from app.services.account_service import _resolve_templates

    templates = {
        "a": {"language": "fr", "slots": ["06:00"]},
        "b": {"slots": ["09:00"], "device": "iphone_16"},
    }
    resolved = _resolve_templates(
        "acc",
        {"template": ["a", "b"], "name": "Acc", "device": "poco_x7_pro"},
        templates,
    )
    # Later template wins over earlier; account keys win over all.
    assert resolved == {
        "language": "fr",
        "slots": ["09:00"],
        "device": "poco_x7_pro",
        "name": "Acc",
    }
    assert "template" not in resolved


def test_resolve_templates_string_form_and_no_template():
    from app.services.account_service import _resolve_templates

    templates = {"a": {"language": "fr"}}
    resolved = _resolve_templates("acc", {"template": "a", "name": "Acc"}, templates)
    assert resolved == {"language": "fr", "name": "Acc"}
    # Without a template key the dict passes through unchanged.
    raw = {"name": "Acc"}
    assert _resolve_templates("acc", raw, templates) == {"name": "Acc"}


def test_resolve_templates_errors():
    from app.services.account_service import _resolve_templates

    with pytest.raises(ValueError, match="nope"):
        _resolve_templates("acc", {"template": "nope"}, {})
    with pytest.raises(ValueError, match="template"):
        _resolve_templates("acc", {"template": 42}, {})
    with pytest.raises(ValueError, match="template"):
        _resolve_templates("acc", {"template": ["a", 42]}, {"a": {}})


def test_resolve_templates_ignores_nested_template_key():
    from app.services.account_service import _resolve_templates

    templates = {"a": {"template": "b", "language": "fr"}, "b": {"language": "en"}}
    resolved = _resolve_templates("acc", {"template": "a", "name": "Acc"}, templates)
    # Nested reference is NOT resolved: language comes from "a", not "b".
    assert resolved == {"language": "fr", "name": "Acc"}
