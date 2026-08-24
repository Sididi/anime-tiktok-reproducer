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
        post_for_me_platform="tiktok_business",
        privacy_status="public",
        allow_comment=True,
        allow_duet=False,
        allow_stitch=True,
    ))
    payload = UploadPhaseService._build_tiktok_payload(account, "my description")
    assert payload == {
        "social_account_id": "spc_123",
        "post_for_me_platform": "tiktok_business",
        "caption": "my description",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": False,
        "allow_stitch": True,
    }


def test_build_tiktok_payload_includes_thumbnail_timestamp():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_123"))
    payload = UploadPhaseService._build_tiktok_payload(
        account, "desc", thumbnail_timestamp_ms=2350
    )
    assert payload["thumbnail_timestamp_ms"] == 2350


def test_build_tiktok_payload_omits_thumbnail_when_none():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_123"))
    payload = UploadPhaseService._build_tiktok_payload(account, "desc")
    assert "thumbnail_timestamp_ms" not in payload


def test_build_tiktok_payload_none_without_pfm_id():
    assert UploadPhaseService._build_tiktok_payload(
        _account(AccountTikTokConfig()), "d"
    ) is None
    assert UploadPhaseService._build_tiktok_payload(_account(None), "d") is None
    assert UploadPhaseService._build_tiktok_payload(None, "d") is None


def test_upfront_tiktok_without_pfm_id_is_manual_pending():
    """Manual TikTok mode (2026-08): no Post for Me id → the row is seeded
    "pending" (the VPS posts the T-5 reminder; ✅ flips it to uploaded),
    never "skipped"."""
    for tiktok in (AccountTikTokConfig(), None):
        skips = UploadPhaseService._compute_upfront_skips(("tiktok",), _account(tiktok))
        assert skips["tiktok"].status == "pending"
        assert skips["tiktok"].detail == UploadPhaseService._TIKTOK_MANUAL_DETAIL


def test_tiktok_manual_result_shape():
    result = UploadPhaseService._tiktok_manual_result()
    assert result.platform == "tiktok"
    assert result.status == "pending"
    assert result.url is None
    assert "✅" in result.detail


def test_no_upfront_skip_with_pfm_id():
    skips = UploadPhaseService._compute_upfront_skips(
        ("tiktok",), _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    )
    assert "tiktok" not in skips


def test_vps_platforms_includes_tiktok_row_for_embed():
    """TikTok stays in the VPS job's platform list so the Discord embed
    shows its row; the backend publishes it (no payload is sent, so the
    server never dispatches it)."""
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    payload = {"social_account_id": "spc_1", "caption": "c"}
    platforms = UploadPhaseService._vps_platforms(
        ("youtube", "facebook", "instagram"), account, payload
    )
    assert platforms == ["youtube", "facebook", "instagram", "tiktok"]
    assert platforms.count("tiktok") == 1


def test_vps_platforms_no_duplicate_tiktok():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    payload = {"social_account_id": "spc_1", "caption": "c"}
    platforms = UploadPhaseService._vps_platforms(("tiktok",), account, payload)
    assert platforms == ["tiktok"]


def test_vps_platforms_no_tiktok_without_block():
    platforms = UploadPhaseService._vps_platforms(
        ("youtube", "facebook", "instagram"), _account(None), None
    )
    assert "tiktok" not in platforms


def test_tiktok_enrolled_with_payload():
    account = _account(AccountTikTokConfig(post_for_me_account_id="spc_1"))
    payload = {"social_account_id": "spc_1", "caption": "c"}
    assert UploadPhaseService._tiktok_enrolled(account, payload) is True


def test_tiktok_enrolled_with_tiktok_slots_and_no_payload():
    account = _account(AccountTikTokConfig(slots=["18:00"]))
    assert UploadPhaseService._tiktok_enrolled(account, None) is True


def test_tiktok_enrolled_with_only_top_level_slots_as_manual():
    """Top-level `slots:` alone (no `tiktok:` block) enrolls the account in
    MANUAL TikTok mode (owner decision 2026-08-24): the slot is reserved by
    _platforms_to_reserve with the same rule, so the upload must carry the
    TikTok row (reminder + ✅ ack) rather than drop it."""
    account = AccountConfig(
        id="anime_fr", name="Anime FR", language="fr", device="",
        slots=["06:00"], tiktok=None,
    )
    assert account.tiktok_mode() == "manual"
    assert UploadPhaseService._tiktok_enrolled(account, None) is True
    platforms = UploadPhaseService._vps_platforms(("youtube",), account, None)
    assert platforms == ["youtube", "tiktok"]


def test_tiktok_pfm_account_is_not_manual():
    account = AccountConfig(
        id="anime_fr", name="Anime FR", language="fr", device="",
        slots=["06:00"], tiktok=AccountTikTokConfig(post_for_me_account_id="spc_1"),
    )
    assert account.tiktok_mode() == "pfm"
    assert UploadPhaseService._tiktok_enrolled(account, None) is True


def test_tiktok_not_enrolled_without_block():
    assert UploadPhaseService._tiktok_enrolled(_account(None), None) is False


def test_attach_tiktok_cover_business_only():
    business = {"post_for_me_platform": "tiktok_business", "thumbnail_timestamp_ms": 500}
    UploadPhaseService._attach_tiktok_cover(business, "https://drive/x.jpg")
    assert business["thumbnail_url"] == "https://drive/x.jpg"
    assert business["thumbnail_timestamp_ms"] == 500

    personal = {"post_for_me_platform": "tiktok", "thumbnail_timestamp_ms": 500}
    UploadPhaseService._attach_tiktok_cover(personal, "https://drive/x.jpg")
    assert "thumbnail_url" not in personal


def test_attach_tiktok_cover_noop_on_none():
    payload = {"post_for_me_platform": "tiktok_business"}
    UploadPhaseService._attach_tiktok_cover(payload, None)
    assert "thumbnail_url" not in payload
    UploadPhaseService._attach_tiktok_cover(None, "https://drive/x.jpg")  # no raise
