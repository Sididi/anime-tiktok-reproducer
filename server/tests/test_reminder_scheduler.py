"""Tests for app.services.reminder_scheduler."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.models.job import (
    InstagramPublishState,
    Job,
    PlatformStatus,
    TikTokPublishState,
)
from app.services.job_store import JobStore
from app.services.post_for_me_publisher import TikTokPublishResult
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
# TikTok publish dispatch tests
# ---------------------------------------------------------------------------

def _tiktok_job(project_id="p1", *, slot_offset_minutes=-1, payload=True, **overrides):
    """Build a due-by-default TikTok job."""
    job = _make_job(
        project_id=project_id,
        slot_time=datetime.now(tz=UTC) + timedelta(minutes=slot_offset_minutes),
    )
    if payload:
        job.tiktok_payload = {
            "social_account_id": "spc_1",
            "caption": "cap",
            "privacy_status": "public",
            "allow_comment": True,
            "allow_duet": True,
            "allow_stitch": True,
        }
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


async def test_dispatch_tiktok_happy_path(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = {}

    async def fake_publish(**kwargs):
        calls.update(kwargs)
        return TikTokPublishResult(
            success=True,
            url="https://tiktok.com/@a/video/1",
            publish_state=TikTokPublishState(post_id="post_1", stage="published"),
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    await store.create(_tiktok_job())
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 1
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "uploaded"
    assert job.platform_statuses["tiktok"].url == "https://tiktok.com/@a/video/1"
    assert job.tiktok_publish_state.stage == "published"
    assert calls["social_account_id"] == "spc_1"
    assert calls["caption"] == "cap"
    assert calls["download_url"] == job.drive_video_url


async def test_dispatch_tiktok_missing_payload_skips(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    await store.create(_tiktok_job(payload=False))
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 0
    job = await store.get("p1")
    assert (
        job.platform_statuses.get("tiktok", PlatformStatus(status="pending")).status
        == "pending"
    )


async def test_dispatch_tiktok_missing_api_key_counts_attempt(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key=None
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    await store.create(_tiktok_job())
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    job = await store.get("p1")
    tt = job.platform_statuses["tiktok"]
    assert tt.status == "pending"          # retried next tick
    assert tt.attempts == 1
    assert "ATR_PFM_API_KEY" in tt.detail


async def test_dispatch_tiktok_fails_after_max_attempts_and_pings(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def fake_publish(**kwargs):
        return TikTokPublishResult(success=False, detail="result: tiktok rejected")

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="pending", attempts=4)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "failed"
    assert updated.platform_statuses["tiktok"].attempts == 5
    # a failure ping mentioning TikTok was posted to the alerts channel
    contents = [
        str(kwargs.get("content") or (args[1] if len(args) > 1 else ""))
        for args, kwargs in discord.post_message.call_args_list
    ]
    assert any("TikTok" in c for c in contents)


async def test_dispatch_tiktok_terminal_statuses_are_not_retried(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    called = False

    async def fake_publish(**kwargs):
        nonlocal called
        called = True
        return TikTokPublishResult(success=True)

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="uploaded")
    )
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert actions == 0
    assert called is False


async def test_dispatch_tiktok_passes_publish_state_for_resume(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    seen = {}

    async def fake_publish(**kwargs):
        seen.update(kwargs)
        return TikTokPublishResult(success=True)

    monkeypatch.setattr(
        "app.services.reminder_scheduler.publish_to_tiktok", fake_publish
    )
    job = _tiktok_job()
    job.tiktok_publish_state = TikTokPublishState(
        post_id="post_7", stage="post_created"
    )
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    assert seen["publish_state"].post_id == "post_7"


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
        "prepared_video_url": "https://drive.usercontent.google.com/download?id=ig_prepared",
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
    assert (
        publish_mock.await_args.kwargs["video_url"]
        == "https://drive.usercontent.google.com/download?id=ig_prepared"
    )
    assert (
        publish_mock.await_args.kwargs["download_url"]
        == "https://drive.usercontent.google.com/download?id=ig_prepared"
    )
    assert publish_mock.await_args.kwargs["poll_interval"] == 7
    assert publish_mock.await_args.kwargs["poll_timeout"] == 600
    assert publish_mock.await_args.kwargs["share_to_feed"] is False
    assert publish_mock.await_args.kwargs["thumb_offset"] == 250
    assert publish_mock.await_args.kwargs["project_id"] == "ig-job"
    assert "prepared_media_dir" not in publish_mock.await_args.kwargs
    assert "public_base_url" not in publish_mock.await_args.kwargs
    assert publish_mock.await_args.kwargs["temp_dir"] == (
        settings.data_dir / "tmp" / "instagram"
    )
    # Embed re-render attempted
    discord.edit_message.assert_called()


async def test_dispatch_instagram_passes_and_persists_publish_state(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    existing_state = InstagramPublishState(
        container_id="container_existing",
        upload_uri="https://rupload.facebook.com/existing",
        stage="uploaded",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=10),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=23),
        upload_completed_at=datetime.now(tz=UTC) - timedelta(minutes=9),
        last_status_code="IN_PROGRESS",
    )
    persisted_state = InstagramPublishState(
        container_id="container_existing",
        upload_uri="https://rupload.facebook.com/existing",
        stage="polling",
        created_at=existing_state.created_at,
        expires_at=existing_state.expires_at,
        upload_completed_at=existing_state.upload_completed_at,
        last_polled_at=datetime.now(tz=UTC),
        last_status_code="IN_PROGRESS",
    )
    job = _make_job(slot_time=past, project_id="ig-resume")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.instagram_publish_state = existing_state
    job.platform_statuses = {
        "instagram": PlatformStatus(status="pending"),
        "tiktok": PlatformStatus(status="pending"),
    }
    await store.create(job)
    discord = AsyncMock()

    async def fake_publish(**kwargs):
        assert kwargs["publish_state"] == existing_state
        await kwargs["progress_callback"](persisted_state)
        return type("R", (), {
            "success": False,
            "permalink": None,
            "detail": "status_poll: poll timeout after 600s; container=container_existing",
            "publish_state": persisted_state,
        })()

    with patch("app.services.reminder_scheduler.publish_to_instagram", new=fake_publish):
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )

    assert actions == 0
    refreshed = await store.get("ig-resume")
    assert refreshed is not None
    assert refreshed.instagram_publish_state == persisted_state
    assert refreshed.platform_statuses["tiktok"].status == "pending"
    assert refreshed.platform_statuses["instagram"].status == "pending"
    assert refreshed.platform_statuses["instagram"].detail is not None
    assert "container=container_existing" in refreshed.platform_statuses["instagram"].detail


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
    assert publish_mock.await_args.kwargs["poll_interval"] == 60.0
    assert publish_mock.await_args.kwargs["poll_timeout"] == 4 * 60 * 60.0
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


@pytest.mark.parametrize(
    "detail",
    [
        "prepare_video: video preparation pass 2 failed: ffmpeg version 7.1.4",
        "download:",
    ],
)
async def test_dispatch_instagram_retries_old_prepare_and_download_failures_once(
    tmp_path: Path,
    example_yaml: Path,
    example_env,
    tmp_server_dir: Path,
    detail: str,
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    job = _make_job(slot_time=past, project_id="ig-old-failure")
    job.platforms_requested = ["instagram"]
    job.instagram_payload = {"ig_user_id": "x", "ig_access_token": "x", "caption": "x"}
    job.platform_statuses = {
        "instagram": PlatformStatus(
            status="failed",
            detail=detail,
            attempts=5,
        )
    }
    await store.create(job)

    discord = AsyncMock()
    publish_mock = AsyncMock(return_value=type("R", (), {
        "success": False,
        "permalink": None,
        "detail": detail,
    })())

    with patch("app.services.reminder_scheduler.publish_to_instagram", new=publish_mock):
        first_actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )
        second_actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )

    refreshed = await store.get("ig-old-failure")
    ig = refreshed.platform_statuses["instagram"]
    assert first_actions == 0
    assert second_actions == 0
    assert ig.status == "failed"
    assert ig.attempts == 6
    assert publish_mock.await_count == 1
