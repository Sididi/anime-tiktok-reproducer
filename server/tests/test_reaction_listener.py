"""Tests for app.services.reaction_listener."""
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
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=discord_message_id,
        reminder_message_id=reminder_message_id,
        reminder_forward_message_id=reminder_forward_message_id,
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


# ---------------------------------------------------------------------------
# Test 1: _handle_reaction ignores non-✅ emoji
# ---------------------------------------------------------------------------

async def test_ignores_non_ack_emoji(tmp_path, example_yaml, example_env, tmp_server_dir):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job_with_embed(discord_message_id="1234")
    await store.create(job)

    listener, rest = _make_listener(store, settings)
    payload = _payload("1234", emoji_str="👍")
    await listener._handle_reaction(payload)

    # Store should be unchanged
    saved = await store.get("p1")
    assert saved is not None
    assert saved.platform_statuses["tiktok"].status == "pending"
    rest.edit_message.assert_not_called()
    rest.add_reaction.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: _handle_reaction ignores reactions from the bot itself
# ---------------------------------------------------------------------------

async def test_ignores_bot_own_reaction(tmp_path, example_yaml, example_env, tmp_server_dir):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job_with_embed(discord_message_id="1234")
    await store.create(job)

    listener, rest = _make_listener(store, settings)
    # Bot user id is 1000 (set in _make_listener)
    payload = _payload("1234", user_id=1000, emoji_str="✅")
    await listener._handle_reaction(payload)

    saved = await store.get("p1")
    assert saved is not None
    assert saved.platform_statuses["tiktok"].status == "pending"
    rest.edit_message.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: _handle_reaction ignores reactions on unknown messages
# ---------------------------------------------------------------------------

async def test_ignores_unknown_message(tmp_path, example_yaml, example_env, tmp_server_dir):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job_with_embed(discord_message_id="1234")
    await store.create(job)

    listener, rest = _make_listener(store, settings)
    # Use a message_id that doesn't match any job
    payload = _payload("9999", emoji_str="✅")
    await listener._handle_reaction(payload)

    saved = await store.get("p1")
    assert saved is not None
    assert saved.platform_statuses["tiktok"].status == "pending"
    rest.edit_message.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Valid reaction on upload-channel embed (reminder NOT yet fired)
#   - tiktok status → uploaded
#   - reminder_cancelled = True (reminder_message_id was None)
#   - edit_message called
#   - add_reaction called
#   - no reminder cleanup (nothing to delete)
# ---------------------------------------------------------------------------

async def test_valid_reaction_on_embed_no_reminder(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    # discord_message_id must be a numeric string so _payload's int() conversion matches
    job = _make_job_with_embed(
        discord_message_id="1234",
        reminder_message_id=None,
        reminder_forward_message_id=None,
    )
    await store.create(job)

    listener, rest = _make_listener(store, settings)
    payload = _payload("1234", emoji_str="✅")
    await listener._handle_reaction(payload)

    saved = await store.get("p1")
    assert saved is not None
    assert saved.platform_statuses["tiktok"].status == "uploaded"
    assert saved.platform_statuses["tiktok"].completed_at is not None
    assert saved.reminder_cancelled is True

    rest.edit_message.assert_called_once()
    rest.add_reaction.assert_called_once_with(
        settings.discord.upload_channel_id, "1234", "✅"
    )
    rest.delete_message.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: Valid reaction on reminder embed (reminder already fired)
#   - Same status update
#   - reminder_cancelled NOT set (reminder already fired; reminder_message_id present)
#   - embed re-rendered
#   - reminder + forward messages deleted
# ---------------------------------------------------------------------------

async def test_valid_reaction_on_reminder_embed(
    tmp_path, example_yaml, example_env, tmp_server_dir
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job_with_embed(
        discord_message_id="1234",
        reminder_message_id="5678",
        reminder_forward_message_id="9012",
    )
    await store.create(job)

    listener, rest = _make_listener(store, settings)
    # React on the reminder message id
    payload = _payload("5678", emoji_str="✅")
    await listener._handle_reaction(payload)

    saved = await store.get("p1")
    assert saved is not None
    assert saved.platform_statuses["tiktok"].status == "uploaded"
    assert saved.platform_statuses["tiktok"].completed_at is not None
    # reminder_cancelled should NOT be set (reminder already fired)
    assert saved.reminder_cancelled is False

    rest.edit_message.assert_called_once()
    rest.add_reaction.assert_called_once()

    # Both reminder messages should have been deleted
    delete_calls = [call.args for call in rest.delete_message.call_args_list]
    deleted_ids = {args[1] for args in delete_calls}
    assert "5678" in deleted_ids
    assert "9012" in deleted_ids

    # Store should clear the reminder message IDs
    assert saved.reminder_message_id is None
    assert saved.reminder_forward_message_id is None


# ---------------------------------------------------------------------------
# Test 6: Bot's own ✅ reaction doesn't loop
#   When the bot adds its own ✅ (via add_reaction), that reaction event comes
#   back through the gateway with user_id == bot_user.id.
#   The listener must drop it before touching the store.
# ---------------------------------------------------------------------------

async def test_bot_reaction_does_not_loop(tmp_path, example_yaml, example_env, tmp_server_dir):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job_with_embed(discord_message_id="1234")
    await store.create(job)

    listener, rest = _make_listener(store, settings)

    # First, a legitimate user reaction to mark as uploaded
    user_payload = _payload("1234", user_id=999, emoji_str="✅")
    await listener._handle_reaction(user_payload)

    # Confirm it was processed
    saved = await store.get("p1")
    assert saved is not None
    assert saved.platform_statuses["tiktok"].status == "uploaded"

    # Reset the mock call counts
    rest.edit_message.reset_mock()
    rest.add_reaction.reset_mock()

    # Now simulate the bot's own ✅ reaction event coming through the gateway
    bot_payload = _payload("1234", user_id=1000, emoji_str="✅")
    await listener._handle_reaction(bot_payload)

    # No further calls should have been made
    rest.edit_message.assert_not_called()
    rest.add_reaction.assert_not_called()
