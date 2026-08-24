"""Tests for app.services.reaction_listener (manual TikTok ✅ ack)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services.job_store import JobStore
from app.services.reaction_listener import ReactionListener


def _settings_for(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


def _make_job_with_embed(
    *,
    project_id: str = "p1",
    discord_message_id: str | None = "1234",
    reminder_message_id: str | None = None,
    reminder_forward_message_id: str | None = None,
    tiktok_manual: bool = True,
    tiktok_status: str = "pending",
) -> Job:
    now = datetime(2026, 4, 27, 21, 0, tzinfo=UTC)
    return Job(
        project_id=project_id,
        job_id="j_x",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        slot_time=now,
        platforms_requested=["tiktok"],
        platform_statuses={"tiktok": PlatformStatus(status=tiktok_status)},
        discord_message_id=discord_message_id,
        reminder_message_id=reminder_message_id,
        reminder_forward_message_id=reminder_forward_message_id,
        tiktok_manual=tiktok_manual,
    )


def _payload(message_id: str, user_id: int = 999, emoji_str: str = "✅"):
    """Construct a fake RawReactionActionEvent."""
    p = MagicMock(spec=discord.RawReactionActionEvent)
    p.message_id = int(message_id) if message_id.isdigit() else hash(message_id) & 0xFFFFFFFF
    p.user_id = user_id
    p.emoji = MagicMock()
    p.emoji.__str__ = MagicMock(return_value=emoji_str)
    return p


def _make_listener(store, settings):
    """Build a ReactionListener with a mocked discord.Client (no real gateway)."""
    rest = AsyncMock()
    listener = ReactionListener(
        bot_token="fake-token",
        store=store,
        settings=settings,
        rest_discord_client=rest,
    )
    # Set the bot user id directly on the listener (discord.Client.user is a
    # read-only property, so we cannot assign listener._client.user in tests).
    listener._bot_user_id = 1000
    return listener, rest


async def _setup(tmp_path, example_yaml, tmp_server_dir, job):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    await store.create(job)
    listener, rest = _make_listener(store, settings)
    return settings, store, listener, rest


async def test_ignores_non_ack_emoji(tmp_path, example_yaml, example_env, tmp_server_dir):
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed()
    )
    await listener._handle_reaction(_payload("1234", emoji_str="👍"))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "pending"
    rest.edit_message.assert_not_called()
    rest.add_reaction.assert_not_called()


async def test_ignores_bot_own_reaction(tmp_path, example_yaml, example_env, tmp_server_dir):
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed()
    )
    await listener._handle_reaction(_payload("1234", user_id=1000))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "pending"
    rest.edit_message.assert_not_called()


async def test_ignores_unknown_message(tmp_path, example_yaml, example_env, tmp_server_dir):
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed()
    )
    await listener._handle_reaction(_payload("9999"))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "pending"
    rest.edit_message.assert_not_called()


async def test_ignores_pfm_job(tmp_path, example_yaml, example_env, tmp_server_dir):
    """A ✅ on a Post-for-Me account's embed must never override PFM state."""
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed(tiktok_manual=False)
    )
    await listener._handle_reaction(_payload("1234"))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "pending"
    assert saved.reminder_cancelled is False
    rest.edit_message.assert_not_called()
    rest.add_reaction.assert_not_called()


async def test_valid_reaction_on_embed_no_reminder(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    """✅ before the reminder fired: mark uploaded + cancel the reminder."""
    settings, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed()
    )
    await listener._handle_reaction(_payload("1234"))

    saved = await store.get("p1")
    tt = saved.platform_statuses["tiktok"]
    assert tt.status == "uploaded"
    assert tt.detail == "Posté manuellement"
    assert tt.url is None
    assert tt.completed_at is not None
    assert saved.reminder_cancelled is True

    rest.edit_message.assert_called_once()
    embed = rest.edit_message.call_args.kwargs["embed"]
    platforms = next(f for f in embed["fields"] if f["name"] == "Plateformes")
    assert "✅ TikTok — Posté manuellement" in platforms["value"]
    rest.add_reaction.assert_called_once_with(
        settings.discord.upload_channel_id, "1234", "✅"
    )
    rest.delete_message.assert_not_called()


async def test_valid_reaction_on_reminder_message(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    """✅ on the reminder itself (after it fired): mark uploaded + delete it."""
    settings, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir,
        _make_job_with_embed(reminder_message_id="5678"),
    )
    await listener._handle_reaction(_payload("5678"))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "uploaded"
    assert saved.reminder_cancelled is False  # already fired; nothing to cancel
    assert saved.reminder_message_id is None

    rest.edit_message.assert_called_once()
    rest.add_reaction.assert_called_once_with(
        settings.discord.upload_channel_id, "1234", "✅"
    )
    rest.delete_message.assert_called_once_with(
        settings.discord.reminder_channel_id, "5678"
    )


async def test_valid_reaction_on_embed_after_reminder_fired(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    """✅ on the original embed after the reminder fired: reminder deleted too."""
    settings, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir,
        _make_job_with_embed(reminder_message_id="5678"),
    )
    await listener._handle_reaction(_payload("1234"))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "uploaded"
    assert saved.reminder_message_id is None
    rest.delete_message.assert_called_once_with(
        settings.discord.reminder_channel_id, "5678"
    )


async def test_legacy_forward_message_is_also_deleted(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir,
        _make_job_with_embed(reminder_message_id="5678", reminder_forward_message_id="9012"),
    )
    await listener._handle_reaction(_payload("5678"))

    deleted_ids = {c.args[1] for c in rest.delete_message.call_args_list}
    assert deleted_ids == {"5678", "9012"}
    saved = await store.get("p1")
    assert saved.reminder_forward_message_id is None


async def test_second_reaction_is_idempotent(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed()
    )
    await listener._handle_reaction(_payload("1234"))
    first = (await store.get("p1")).platform_statuses["tiktok"]
    rest.edit_message.reset_mock()
    rest.add_reaction.reset_mock()

    await listener._handle_reaction(_payload("1234", user_id=42))

    second = (await store.get("p1")).platform_statuses["tiktok"]
    assert second == first  # completed_at untouched
    rest.edit_message.assert_not_called()
    rest.add_reaction.assert_not_called()


async def test_bot_reaction_does_not_loop(tmp_path, example_yaml, example_env, tmp_server_dir):
    """The bot's own mirrored ✅ comes back through the gateway and must be dropped."""
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir, _make_job_with_embed()
    )
    await listener._handle_reaction(_payload("1234", user_id=999))
    assert (await store.get("p1")).platform_statuses["tiktok"].status == "uploaded"
    rest.edit_message.reset_mock()
    rest.add_reaction.reset_mock()

    await listener._handle_reaction(_payload("1234", user_id=1000))

    rest.edit_message.assert_not_called()
    rest.add_reaction.assert_not_called()


async def test_embed_rerender_failure_does_not_block_ack(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    _, store, listener, rest = await _setup(
        tmp_path, example_yaml, tmp_server_dir,
        _make_job_with_embed(reminder_message_id="5678"),
    )
    rest.edit_message.side_effect = RuntimeError("discord 5xx")

    await listener._handle_reaction(_payload("1234"))

    saved = await store.get("p1")
    assert saved.platform_statuses["tiktok"].status == "uploaded"
    rest.delete_message.assert_called_once()  # reminder cleanup still ran
