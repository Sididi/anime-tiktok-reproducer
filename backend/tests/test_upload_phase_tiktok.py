"""TikTok payload building for the VPS job."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.account_service import (
    AccountConfig,
    AccountTikTokConfig,
)
from app.services.upload_phase import UploadPhaseService


def _account(tiktok: AccountTikTokConfig | None) -> AccountConfig:
    return AccountConfig(
        id="anime_fr", name="Anime FR", language="fr", device="", tiktok=tiktok
    )


def test_build_tiktok_payload_full():
    account = _account(AccountTikTokConfig(
        post_for_me_account_id="spc_123",
        privacy_status="public",
        allow_comment=True,
        allow_duet=False,
        allow_stitch=True,
    ))
    payload = UploadPhaseService._build_tiktok_payload(account, "my description")
    assert payload == {
        "social_account_id": "spc_123",
        "caption": "my description",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": False,
        "allow_stitch": True,
    }


def test_build_tiktok_payload_none_without_pfm_id():
    assert UploadPhaseService._build_tiktok_payload(
        _account(AccountTikTokConfig()), "d"
    ) is None
    assert UploadPhaseService._build_tiktok_payload(_account(None), "d") is None
    assert UploadPhaseService._build_tiktok_payload(None, "d") is None


def test_upfront_skip_tiktok_without_pfm_id():
    skips = UploadPhaseService._compute_upfront_skips(
        ("tiktok",), _account(AccountTikTokConfig())
    )
    assert skips["tiktok"].status == "skipped"
    assert "Post for Me" in skips["tiktok"].detail


def test_no_upfront_skip_with_pfm_id():
    skips = UploadPhaseService._compute_upfront_skips(
        ("tiktok",), _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    )
    assert "tiktok" not in skips


def test_vps_platforms_appends_tiktok_when_payload_exists():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    payload = {"social_account_id": "spc_1", "caption": "c"}
    platforms = UploadPhaseService._vps_platforms(
        ("youtube", "facebook", "instagram"), account, payload
    )
    assert platforms == ["youtube", "facebook", "instagram", "tiktok"]
    assert platforms.count("tiktok") == 1


def test_vps_platforms_appends_tiktok_when_account_has_tiktok_slots():
    account = _account(AccountTikTokConfig(slots=["lundi 18:00"]))
    platforms = UploadPhaseService._vps_platforms(
        ("youtube", "facebook", "instagram"), account, None
    )
    assert "tiktok" in platforms


def test_vps_platforms_no_tiktok_without_block():
    platforms = UploadPhaseService._vps_platforms(
        ("youtube", "facebook", "instagram"), _account(None), None
    )
    assert "tiktok" not in platforms


def test_vps_platforms_no_tiktok_with_only_top_level_slots():
    """Top-level `slots:` alone (no explicit `tiktok:` block) must not enroll the
    account in TikTok VPS dispatch — that requires an explicit tiktok block."""
    account = AccountConfig(
        id="anime_fr", name="Anime FR", language="fr", device="",
        slots=["06:00"], tiktok=None,
    )
    platforms = UploadPhaseService._vps_platforms(
        ("youtube", "facebook", "instagram"), account, None
    )
    assert "tiktok" not in platforms


def test_vps_platforms_no_duplicate_tiktok():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    payload = {"social_account_id": "spc_1", "caption": "c"}
    platforms = UploadPhaseService._vps_platforms(("tiktok",), account, payload)
    assert platforms == ["tiktok"]
