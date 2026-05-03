"""Tests for app.services.embed_builder.build_embed."""
from __future__ import annotations

from datetime import UTC, datetime

from app.config import AccountConfig
from app.models.job import Job, PlatformStatus
from app.services.embed_builder import build_embed


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
    assert "🎯" in plats and "TikTok" in plats and "Pending" in plats


def test_embed_description_field_uses_code_block():
    embed = build_embed(
        _job_fixture(), _accounts(), "https://tiktok.sididi.tv"
    )
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Description TikTok"].startswith("```")
    assert "Posted today!" in fields["Description TikTok"]
    assert fields["Description TikTok"].endswith("```")


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


def test_embed_description_escapes_triple_backticks():
    job = _job_fixture()
    job.description = "Look at this code: ```python\nprint('hi')\n```"
    embed = build_embed(job, _accounts(), "https://tiktok.sididi.tv")
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    desc_field = fields["Description TikTok"]
    # The outer fence intact:
    assert desc_field.startswith("```\n")
    assert desc_field.endswith("\n```")
    # Inner triple-backticks replaced:
    inner = desc_field[4:-4]  # strip outer fences
    assert "```" not in inner
    assert "ʼʼʼ" in inner
