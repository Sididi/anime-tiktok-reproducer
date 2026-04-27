"""Tests for app.services.reminder_service."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx

from app.config import AccountConfig
from app.models.job import PlatformStatus, TikTokJob
from app.services.reminder_service import build_reminder_embed, post_reminder


def _account() -> AccountConfig:
    return AccountConfig(
        id="anime_fr",
        name="Anime FR",
        language="fr",
        device="iphone_13_pro",
        avatar="anime_fr.jpg",
    )


def _job(*, discord_message_id: str | None = "m_embed") -> TikTokJob:
    now = datetime(2026, 4, 27, 21, 0, tzinfo=timezone.utc)
    return TikTokJob(
        project_id="p1",
        job_id="j_x",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece 1063",
        description="Posted today",
        drive_video_url="https://drive/x",
        slot_time=now,
        platforms_requested=["youtube", "tiktok"],
        status="pending",
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=discord_message_id,
        reminder_message_id=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )


def test_build_reminder_embed_renders_paris_time_and_account():
    embed = build_reminder_embed(_job(), _account(), "https://tiktok.sididi.tv")
    assert embed["author"]["name"] == "Anime FR"
    assert embed["author"]["icon_url"].endswith("/api/avatars/anime_fr.jpg")
    assert embed["title"] == "One Piece 1063"
    # Paris time at 21:00 UTC on 2026-04-27 → 23:00 CEST
    assert "23:00" in embed["description"]
    field_names = {f["name"] for f in embed["fields"]}
    assert "📱 Device" in field_names
    assert "📺 Compte" in field_names
    assert "Description TikTok" in field_names


def test_build_reminder_embed_escapes_triple_backticks():
    job = _job()
    job.description = "Inject ```evil``` here"
    embed = build_reminder_embed(job, _account(), "https://tiktok.sididi.tv")
    desc_field = next(f for f in embed["fields"] if f["name"] == "Description TikTok")
    assert "```evil```" not in desc_field["value"]
    assert "ʼʼʼ" in desc_field["value"]


async def test_post_reminder_posts_rich_then_forward():
    discord = AsyncMock()
    discord.post_message.side_effect = ["m_rich", "m_forward"]

    rich_id, forward_id = await post_reminder(
        discord,
        job=_job(),
        account=_account(),
        public_base_url="https://tiktok.sididi.tv",
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        role_id="r_99",
        guild_id="g_1",
    )

    assert rich_id == "m_rich"
    assert forward_id == "m_forward"
    assert discord.post_message.call_count == 2

    # First call is the rich message: role ping + embed, no message_reference.
    first = discord.post_message.call_args_list[0]
    assert first.args == ("c_reminder",)
    assert "<@&r_99>" in first.kwargs["content"]
    assert first.kwargs["embed"]["title"] == "One Piece 1063"
    assert first.kwargs.get("message_reference") is None

    # Second call is the forward: message_reference, no embed.
    second = discord.post_message.call_args_list[1]
    assert second.kwargs["message_reference"] == {
        "type": 1,
        "channel_id": "c_upload",
        "message_id": "m_embed",
    }


async def test_post_reminder_falls_back_to_url_when_forward_fails():
    async def side_effect(*args, **kwargs):
        if kwargs.get("message_reference"):
            raise httpx.HTTPStatusError(
                "forbidden",
                request=httpx.Request("POST", "u"),
                response=httpx.Response(403),
            )
        return "m_rich" if "embed" in kwargs else "m_forward_url"

    discord = AsyncMock()
    discord.post_message.side_effect = side_effect

    rich_id, forward_id = await post_reminder(
        discord,
        job=_job(),
        account=_account(),
        public_base_url="https://tiktok.sididi.tv",
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        role_id="r_99",
        guild_id="g_1",
    )

    assert rich_id == "m_rich"
    assert forward_id == "m_forward_url"
    # Three calls: rich, forward (fails), URL fallback
    assert discord.post_message.call_count == 3
    last = discord.post_message.call_args_list[-1]
    assert "https://discord.com/channels/g_1/c_upload/m_embed" in last.kwargs["content"]


async def test_post_reminder_skips_forward_when_no_embed_id():
    discord = AsyncMock()
    discord.post_message.return_value = "m_rich"

    rich_id, forward_id = await post_reminder(
        discord,
        job=_job(discord_message_id=None),
        account=_account(),
        public_base_url="https://tiktok.sididi.tv",
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        role_id="r_99",
        guild_id="g_1",
    )

    assert rich_id == "m_rich"
    assert forward_id is None
    assert discord.post_message.call_count == 1
