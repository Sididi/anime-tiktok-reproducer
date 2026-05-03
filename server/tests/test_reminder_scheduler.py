"""Tests for app.services.reminder_scheduler."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services.job_store import JobStore
from app.services.reminder_scheduler import (
    dispatch_due_actions,
    run_scheduler_loop,
)


def _settings_for(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


def _make_job(
    *,
    project_id: str = "p1",
    slot_time: datetime,
    reminder_message_id: str | None = None,
    platform_status: str = "pending",
    discord_message_id: str | None = "embed_id",
) -> Job:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    return Job(
        project_id=project_id,
        job_id=f"j_{project_id}",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece",
        description="Posted today",
        drive_video_url="https://drive/x",
        slot_time=slot_time,
        platforms_requested=["tiktok"],
        platform_statuses={"tiktok": PlatformStatus(status=platform_status)},
        discord_message_id=discord_message_id,
        reminder_message_id=reminder_message_id,
        created_at=now,
        updated_at=now,
    )


async def test_dispatch_skips_jobs_not_yet_due(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    future = datetime.now(tz=UTC) + timedelta(hours=1)
    await store.create(_make_job(slot_time=future))
    discord = AsyncMock()

    posted = await dispatch_due_actions(store=store, settings=settings, discord=discord)

    assert posted == 0
    discord.post_message.assert_not_called()


async def test_dispatch_fires_due_jobs_and_marks_them(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    await store.create(_make_job(slot_time=past))

    discord = AsyncMock()
    discord.post_message.side_effect = ["m_rich", "m_forward"]

    posted = await dispatch_due_actions(store=store, settings=settings, discord=discord)

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
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    await store.create(_make_job(slot_time=past, reminder_message_id="already_sent"))

    discord = AsyncMock()
    posted = await dispatch_due_actions(store=store, settings=settings, discord=discord)

    assert posted == 0
    discord.post_message.assert_not_called()


async def test_dispatch_retries_on_next_tick_when_post_fails(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """If the rich message post returns None, reminder_message_id stays None
    and the next tick re-attempts."""
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    await store.create(_make_job(slot_time=past))

    discord = AsyncMock()
    # First tick: rich-post raises, reminder_service swallows -> rich_id is None
    # so the scheduler doesn't update the store.
    discord.post_message.side_effect = Exception("Discord 5xx")

    posted = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    assert posted == 0
    assert (await store.get("p1")).reminder_message_id is None

    # Second tick: success.
    discord.post_message.side_effect = ["m_rich", "m_forward"]
    posted2 = await dispatch_due_actions(store=store, settings=settings, discord=discord)
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


# ---------------------------------------------------------------------------
# Instagram dispatch tests
# ---------------------------------------------------------------------------

async def test_dispatch_instagram_happy_path(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    job = _make_job(slot_time=past, project_id="ig-job")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {
        "ig_user_id": "ig_user_42",
        "ig_access_token": "token",
        "caption": "Hello",
        "graph_api_version": "v25.0",
        "poll_interval_seconds": 7,
        "poll_timeout_seconds": 600,
        "share_to_feed": False,
        "thumb_offset": 250,
    }
    job.platform_statuses = {"instagram": PlatformStatus(status="pending")}
    await store.create(job)

    discord = AsyncMock()

    with patch(
        "app.services.reminder_scheduler.publish_to_instagram",
        new=AsyncMock(return_value=type("R", (), {
            "success": True, "permalink": "https://instagram.com/reel/x", "detail": None,
        })()),
    ) as publish_mock:
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )

    assert actions == 1
    refreshed = await store.get("ig-job")
    assert refreshed is not None
    ig = refreshed.platform_statuses["instagram"]
    assert ig.status == "uploaded"
    assert ig.url == "https://instagram.com/reel/x"
    assert ig.attempts == 1
    assert publish_mock.await_args.kwargs["video_url"].endswith("/api/videos/ig-job")
    assert publish_mock.await_args.kwargs["poll_interval"] == 7
    assert publish_mock.await_args.kwargs["poll_timeout"] == 600
    assert publish_mock.await_args.kwargs["share_to_feed"] is False
    assert publish_mock.await_args.kwargs["thumb_offset"] == 250
    # Embed re-render attempted
    discord.edit_message.assert_called()


async def test_dispatch_instagram_uses_platform_scheduled_time(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    instagram_due = datetime(2026, 4, 26, 6, 1, tzinfo=UTC)
    tiktok_due = datetime(2026, 4, 26, 21, 0, tzinfo=UTC)
    job = _make_job(slot_time=tiktok_due, project_id="ig-platform-time")
    job.platforms_requested = ["instagram", "tiktok"]
    job.platform_scheduled_at = {
        "instagram": instagram_due,
        "tiktok": tiktok_due,
    }
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.platform_statuses = {
        "instagram": PlatformStatus(status="pending"),
        "tiktok": PlatformStatus(status="pending"),
    }
    await store.create(job)

    discord = AsyncMock()
    discord.post_message.side_effect = ["m_rich", "m_forward"]

    with patch(
        "app.services.reminder_scheduler.publish_to_instagram",
        new=AsyncMock(return_value=type("R", (), {
            "success": True,
            "permalink": "https://instagram.com/reel/early",
            "detail": None,
        })()),
    ) as publish_mock:
        actions = await dispatch_due_actions(
            store=store,
            settings=settings,
            discord=discord,
            now=datetime(2026, 4, 26, 6, 2, tzinfo=UTC),
        )

    assert actions == 1
    assert publish_mock.await_count == 1
    refreshed = await store.get("ig-platform-time")
    assert refreshed.platform_statuses["instagram"].status == "uploaded"
    assert refreshed.reminder_message_id is None
    discord.post_message.assert_not_called()


async def test_dispatch_legacy_instagram_uses_slot_time(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    slot_time = datetime(2026, 4, 26, 21, 0, tzinfo=UTC)
    job = _make_job(slot_time=slot_time, project_id="ig-legacy-time")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.platform_statuses = {"instagram": PlatformStatus(status="pending")}
    await store.create(job)

    discord = AsyncMock()

    with patch(
        "app.services.reminder_scheduler.publish_to_instagram",
        new=AsyncMock(return_value=type("R", (), {
            "success": True,
            "permalink": "https://instagram.com/reel/legacy",
            "detail": None,
        })()),
    ) as publish_mock:
        early_actions = await dispatch_due_actions(
            store=store,
            settings=settings,
            discord=discord,
            now=datetime(2026, 4, 26, 6, 2, tzinfo=UTC),
        )
        due_actions = await dispatch_due_actions(
            store=store,
            settings=settings,
            discord=discord,
            now=datetime(2026, 4, 26, 21, 1, tzinfo=UTC),
        )

    assert early_actions == 0
    assert due_actions == 1
    assert publish_mock.await_count == 1


async def test_dispatch_instagram_retries_until_max(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """5 failed attempts -> mark failed + post failure ping."""
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    job = _make_job(slot_time=past, project_id="ig-fail")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {
        "ig_user_id": "x", "ig_access_token": "x", "caption": "x",
    }
    job.platform_statuses = {"instagram": PlatformStatus(status="pending")}
    await store.create(job)

    discord = AsyncMock()

    fail = AsyncMock(return_value=type("R", (), {
        "success": False, "permalink": None, "detail": "status_poll: Meta 503",
    })())

    with patch("app.services.reminder_scheduler.publish_to_instagram", new=fail):
        # Fire 5 ticks. The first 4 should leave status='pending' with bumped attempts;
        # the 5th should mark 'failed' and post the ping.
        for _ in range(5):
            await dispatch_due_actions(store=store, settings=settings, discord=discord)

    refreshed = await store.get("ig-fail")
    ig = refreshed.platform_statuses["instagram"]
    assert ig.status == "failed"
    assert ig.attempts == 5
    assert ig.detail == "status_poll: Meta 503"
    # Failure ping posted
    discord.post_message.assert_called()


async def test_dispatch_instagram_skips_already_uploaded(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    job = _make_job(slot_time=past, project_id="ig-done")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.platform_statuses = {"instagram": PlatformStatus(status="uploaded", url="https://x")}
    await store.create(job)

    discord = AsyncMock()
    fail = AsyncMock()  # would fail loudly if called

    with patch("app.services.reminder_scheduler.publish_to_instagram", new=fail):
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )

    assert actions == 0
    fail.assert_not_called()


async def test_dispatch_instagram_retries_legacy_container_error_once(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    job = _make_job(slot_time=past, project_id="ig-legacy-error")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.platform_statuses = {
        "instagram": PlatformStatus(
            status="failed",
            detail="container status_code = ERROR",
            attempts=5,
        )
    }
    await store.create(job)

    discord = AsyncMock()

    with patch(
        "app.services.reminder_scheduler.publish_to_instagram",
        new=AsyncMock(return_value=type("R", (), {
            "success": True,
            "permalink": "https://instagram.com/reel/recovered",
            "detail": None,
        })()),
    ) as publish_mock:
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )

    refreshed = await store.get("ig-legacy-error")
    ig = refreshed.platform_statuses["instagram"]
    assert actions == 1
    assert ig.status == "uploaded"
    assert ig.attempts == 6
    assert publish_mock.await_count == 1


async def test_dispatch_instagram_retries_resumable_header_error_once(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    job = _make_job(slot_time=past, project_id="ig-header-error")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.platform_statuses = {
        "instagram": PlatformStatus(
            status="failed",
            detail="resumable upload failed: Invalid Header format",
            attempts=6,
        )
    }
    await store.create(job)

    discord = AsyncMock()

    with patch(
        "app.services.reminder_scheduler.publish_to_instagram",
        new=AsyncMock(return_value=type("R", (), {
            "success": True,
            "permalink": "https://instagram.com/reel/recovered",
            "detail": None,
        })()),
    ) as publish_mock:
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )

    refreshed = await store.get("ig-header-error")
    ig = refreshed.platform_statuses["instagram"]
    assert actions == 1
    assert ig.status == "uploaded"
    assert ig.attempts == 7
    assert publish_mock.await_count == 1
