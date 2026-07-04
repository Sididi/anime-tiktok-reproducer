"""Tests for app.services.embed_builder.build_embed."""
from __future__ import annotations

from datetime import UTC, datetime

from app.config import AccountConfig
from app.models.job import Job, PlatformStatus
from app.services.embed_builder import _format_platform_line, build_embed


def _job_fixture() -> Job:
    now = datetime(2026, 4, 26, 21, 0, tzinfo=UTC)
    return Job(
        project_id="2ee46c92a4ce",
        job_id="j_abc",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece Episode 1063 — TikTok 2x3",
        description="Posted today!",
        drive_video_url="https://drive.google.com/uc?id=xyz",
        slot_time=now,
        platforms_requested=["youtube", "facebook", "instagram", "tiktok"],
        platform_statuses={
            "youtube": PlatformStatus(status="uploaded", url="https://youtu.be/abc"),
            "facebook": PlatformStatus(status="skipped", detail="Not configured"),
            "instagram": PlatformStatus(status="uploading"),
            "tiktok": PlatformStatus(status="pending"),
        },
        discord_message_id=None,
        reminder_message_id=None,
        created_at=now,
        updated_at=now,
    )


def _accounts() -> dict[str, AccountConfig]:
    return {
        "anime_fr": AccountConfig(
            id="anime_fr",
            name="Anime FR",
            language="fr",
            device="iphone_13_pro",
            avatar="anime_fr.jpg",
        )
    }



def test_embed_has_author_with_avatar_url():
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    assert embed["author"]["name"] == "Anime FR"
    assert (
        embed["author"]["icon_url"]
        == "https://tiktok.sididi.tv/api/avatars/anime_fr.jpg"
    )


def test_embed_title_is_anime_title():
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    assert embed["title"] == "One Piece Episode 1063 — TikTok 2x3"


def test_embed_inline_fields_include_device_and_project():
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f for f in embed["fields"]}
    assert any("iphone_13_pro" in f["value"] for f in fields.values())
    assert any("2ee46c92a4ce" in f["value"] for f in fields.values())


def test_embed_platforms_field_renders_all_statuses():
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    plats = fields["Plateformes"]
    assert "✅" in plats and "YouTube" in plats and "youtu.be/abc" in plats
    assert "⚠️" in plats and "Facebook" in plats and "Not configured" in plats
    assert "⏳" in plats and "Instagram" in plats
    assert "⏳" in plats and "TikTok" in plats and "Pending" in plats


def test_embed_description_field_is_plain_text():
    """Description must be plain text (no code fences) so Discord mobile copy
    yields just the text — long-pressing inline code or a code block on mobile
    includes the surrounding backticks in the clipboard."""
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    value = fields["Description TikTok"]
    assert value == "Posted today!"
    assert "`" not in value


def test_embed_includes_drive_link():
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "drive.google.com/uc?id=xyz" in fields["Lien vidéo"]


def test_embed_after_ack_marks_tiktok_uploaded():
    job = _job_fixture()
    job.platform_statuses["tiktok"] = PlatformStatus(
        status="uploaded",
        completed_at=datetime(2026, 4, 26, 21, 4, tzinfo=UTC),
    )
    embed = build_embed(job, _accounts(), "https://tiktok.sididi.tv")
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    plats = fields["Plateformes"]
    assert "✅ TikTok" in plats


def test_embed_description_is_passed_through_verbatim():
    """Description must be passed through unchanged — no backticks, no
    backslash escaping. Discord mobile's copy returns raw source, so any
    wrapping or escaping ends up in the user's clipboard."""
    job = _job_fixture()
    job.description = "Check *this* _out_ ~now~ #tag 🔥"
    embed = build_embed(job, _accounts(), "https://tiktok.sididi.tv")
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    value = fields["Description TikTok"]
    assert value == "Check *this* _out_ ~now~ #tag 🔥"
    assert "\\" not in value
    assert "`" not in value


def test_tiktok_line_is_generic_with_url():
    ps = PlatformStatus(status="uploaded", url="https://tiktok.com/@a/video/1")
    line = _format_platform_line("tiktok", ps)
    assert line == "✅ TikTok — https://tiktok.com/@a/video/1"


def test_tiktok_line_pending_is_generic():
    line = _format_platform_line("tiktok", PlatformStatus(status="pending"))
    assert line == "⏳ TikTok — Pending"


def test_embed_omits_device_when_empty():
    job = _job_fixture()
    job.device_id = ""
    embed = build_embed(job, _accounts(), "https://tiktok.sididi.tv")
    names = [f["name"] for f in embed["fields"]]
    assert "📱 Device" not in names
    assert " ·  · " not in embed["footer"]["text"]
