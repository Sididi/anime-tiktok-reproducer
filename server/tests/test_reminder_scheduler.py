"""Tests for app.services.reminder_scheduler."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.models.job import PlatformStatus, TikTokJob
from app.services.job_store import JobStore
from app.services.reminder_scheduler import (
    dispatch_due_reminders,
    run_scheduler_loop,
)


def _settings_for(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


def _make_job(
    *,
    project_id: str = "p1",
    slot_time: datetime,
    reminder_message_id: str | None = None,
    status: str = "pending",
    discord_message_id: str | None = "embed_id",
) -> TikTokJob:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    return TikTokJob(
        project_id=project_id,
        job_id=f"j_{project_id}",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece",
        description="Posted today",
        drive_video_url="https://drive/x",
        slot_time=slot_time,
        platforms_requested=["tiktok"],
        status=status,  # type: ignore[arg-type]
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=discord_message_id,
        reminder_message_id=reminder_message_id,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )


async def test_dispatch_skips_jobs_not_yet_due(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    await store.create(_make_job(slot_time=future))
    discord = AsyncMock()

    posted = await dispatch_due_reminders(store=store, settings=settings, discord=discord)

    assert posted == 0
    discord.post_message.assert_not_called()


async def test_dispatch_fires_due_jobs_and_marks_them(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    await store.create(_make_job(slot_time=past))

    discord = AsyncMock()
    discord.post_message.side_effect = ["m_rich", "m_forward"]

    posted = await dispatch_due_reminders(store=store, settings=settings, discord=discord)

    assert posted == 1
    refreshed = await store.get("p1")
    assert refreshed is not None
    assert refreshed.reminder_message_id == "m_rich"
    assert refreshed.reminder_forward_message_id == "m_forward"


async def test_dispatch_skips_already_reminded_jobs(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    await store.create(_make_job(slot_time=past, reminder_message_id="already_sent"))

    discord = AsyncMock()
    posted = await dispatch_due_reminders(store=store, settings=settings, discord=discord)

    assert posted == 0
    discord.post_message.assert_not_called()


async def test_dispatch_skips_acked_jobs(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """An acked job is filtered out by status='pending' on list_for_device."""
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    await store.create(_make_job(slot_time=past, status="acked"))

    discord = AsyncMock()
    posted = await dispatch_due_reminders(store=store, settings=settings, discord=discord)

    assert posted == 0


async def test_dispatch_retries_on_next_tick_when_post_fails(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """If the rich message post returns None, reminder_message_id stays None
    and the next tick re-attempts."""
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    await store.create(_make_job(slot_time=past))

    discord = AsyncMock()
    # First tick: rich-post raises, reminder_service swallows -> rich_id is None
    # so the scheduler doesn't update the store.
    discord.post_message.side_effect = Exception("Discord 5xx")

    posted = await dispatch_due_reminders(store=store, settings=settings, discord=discord)
    assert posted == 0
    assert (await store.get("p1")).reminder_message_id is None

    # Second tick: success.
    discord.post_message.side_effect = ["m_rich", "m_forward"]
    posted2 = await dispatch_due_reminders(store=store, settings=settings, discord=discord)
    assert posted2 == 1
    assert (await store.get("p1")).reminder_message_id == "m_rich"


async def test_run_scheduler_loop_stops_on_event(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_scheduler_loop(
            store=store,
            settings=settings,
            discord=discord,
            interval_seconds=0.01,
            stop_event=stop,
        )
    )
    await asyncio.sleep(0.05)  # let it tick a few times
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    # Loop exited cleanly via stop_event.
    assert task.done()
