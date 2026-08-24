"""Tests for app.services.reminder_service (manual TikTok reminder, 2026-08)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services.job_store import JobStore
from app.services.reminder_service import (
    build_reminder_content,
    build_reminder_embed,
    cleanup_reminder,
    post_reminder,
)


def _settings_for(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


def _job(
    *,
    discord_message_id: str | None = "m_embed",
    reminder_message_id: str | None = None,
    reminder_forward_message_id: str | None = None,
    device_id: str = "iphone_13_pro",
) -> Job:
    now = datetime(2026, 4, 27, 21, 0, tzinfo=UTC)
    return Job(
        project_id="p1",
        job_id="j_x",
        account_id="anime_fr",
        device_id=device_id,
        anime_title="One Piece 1063",
        description="Posted `today`",
        drive_video_url="https://drive/x",
        slot_time=now,
        platform_scheduled_at={"tiktok": datetime(2026, 4, 27, 21, 3, tzinfo=UTC)},
        platforms_requested=["youtube", "tiktok"],
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=discord_message_id,
        reminder_message_id=reminder_message_id,
        reminder_forward_message_id=reminder_forward_message_id,
        tiktok_manual=True,
        created_at=now,
        updated_at=now,
    )


def test_build_reminder_embed_uses_tiktok_time_and_plain_description(
    example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    account = settings.accounts["anime_fr"]
    embed = build_reminder_embed(_job(), account, "https://tiktok.sididi.tv")
    assert embed["author"]["name"] == "Anime FR"
    assert embed["author"]["icon_url"].endswith("/api/avatars/anime_fr.jpg")
    assert embed["title"] == "One Piece 1063"
    # 21:03 UTC on 2026-04-27 → 23:03 Paris (the TikTok per-platform time,
    # not slot_time 21:00 → 23:00).
    assert "23:03" in embed["description"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["📺 Compte"] == "Anime FR"
    assert fields["📱 Device"] == "iphone_13_pro"
    # Plain text: no backtick escaping (mobile copy/paste into TikTok).
    assert fields["Description TikTok"] == "Posted `today`"
    assert fields["Lien vidéo"] == "https://drive/x"


def test_build_reminder_embed_omits_device_when_empty(
    example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    embed = build_reminder_embed(
        _job(device_id=""), settings.accounts["anime_fr"], "https://x"
    )
    assert "📱 Device" not in {f["name"] for f in embed["fields"]}


def test_build_reminder_content_pings_role_and_links_original(
    example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    content = build_reminder_content(settings, _job(), settings.accounts["anime_fr"])
    assert content.startswith("<@&444>")
    assert "**One Piece 1063**" in content
    assert "**Anime FR**" in content
    assert "https://discord.com/channels/111/222/m_embed" in content
    assert "✅" in content


def test_build_reminder_content_without_original_message_has_no_link(
    example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    content = build_reminder_content(
        settings, _job(discord_message_id=None), settings.accounts["anime_fr"]
    )
    assert "discord.com/channels" not in content
    assert content.startswith("<@&444>")


async def test_post_reminder_posts_exactly_one_message_in_reminder_channel(
    example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    discord = AsyncMock()
    discord.post_message.return_value = "m_rem"

    message_id = await post_reminder(
        discord, job=_job(), account=settings.accounts["anime_fr"], settings=settings
    )

    assert message_id == "m_rem"
    discord.post_message.assert_awaited_once()
    call = discord.post_message.call_args
    assert call.args == ("333",)  # reminder channel
    assert "<@&444>" in call.kwargs["content"]
    assert call.kwargs["embed"]["title"] == "One Piece 1063"
    assert call.kwargs.get("message_reference") is None  # no forward message


async def test_post_reminder_returns_none_on_failure(
    example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    discord = AsyncMock()
    discord.post_message.side_effect = httpx.HTTPStatusError(
        "forbidden",
        request=httpx.Request("POST", "u"),
        response=httpx.Response(403),
    )
    message_id = await post_reminder(
        discord, job=_job(), account=settings.accounts["anime_fr"], settings=settings
    )
    assert message_id is None


async def test_cleanup_reminder_deletes_messages_and_clears_ids(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _job(reminder_message_id="m_rem", reminder_forward_message_id="m_fwd")
    await store.create(job)
    discord = AsyncMock()

    deleted = await cleanup_reminder(discord, store, settings, job)

    assert deleted is True
    deleted_ids = {c.args[1] for c in discord.delete_message.call_args_list}
    assert deleted_ids == {"m_rem", "m_fwd"}
    assert all(c.args[0] == "333" for c in discord.delete_message.call_args_list)
    saved = await store.get("p1")
    assert saved is not None
    assert saved.reminder_message_id is None
    assert saved.reminder_forward_message_id is None


async def test_cleanup_reminder_is_noop_without_reminder(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _job()
    await store.create(job)
    discord = AsyncMock()

    deleted = await cleanup_reminder(discord, store, settings, job)

    assert deleted is False
    discord.delete_message.assert_not_called()


async def test_cleanup_reminder_tolerates_delete_failure(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _job(reminder_message_id="m_rem")
    await store.create(job)
    discord = AsyncMock()
    discord.delete_message.side_effect = RuntimeError("gone")

    deleted = await cleanup_reminder(discord, store, settings, job)

    assert deleted is False
    saved = await store.get("p1")
    assert saved is not None
    assert saved.reminder_message_id is None  # id cleared regardless
