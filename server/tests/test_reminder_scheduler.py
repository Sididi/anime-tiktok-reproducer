"""Tests for app.services.reminder_scheduler."""
from __future__ import annotations

import asyncio
import logging
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
    TIKTOK_SCHEDULE_LEAD_MINUTES,
    _platform_due_time,
    dispatch_due_actions,
    run_scheduler_loop,
    wait_for_inflight,
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
    await wait_for_inflight()

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


def _ok_state(**kw):
    defaults = dict(media_url="https://media.example/abc.mp4", stage="media_uploaded")
    defaults.update(kw)
    return TikTokPublishState(**defaults)


def _patch_phases(monkeypatch, *, stage=None, create=None, poll=None):
    """Patch the three publisher phases in the scheduler namespace.
    Unspecified phases succeed with a sensible state progression."""
    calls: dict[str, list[dict]] = {"stage": [], "create": [], "poll": []}

    async def default_stage(**kwargs):
        calls["stage"].append(kwargs)
        return TikTokPublishResult(success=True, publish_state=_ok_state())

    async def default_create(**kwargs):
        calls["create"].append(kwargs)
        scheduled = kwargs.get("scheduled_at") is not None
        return TikTokPublishResult(
            success=True,
            publish_state=_ok_state(
                post_id="post_1",
                stage="post_scheduled" if scheduled else "post_created",
            ),
        )

    async def default_poll(**kwargs):
        calls["poll"].append(kwargs)
        return TikTokPublishResult(
            success=True,
            url="https://tiktok.com/@a/video/1",
            publish_state=_ok_state(post_id="post_1", stage="published",
                                    url="https://tiktok.com/@a/video/1"),
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.stage_media_for_tiktok", stage or default_stage
    )
    monkeypatch.setattr(
        "app.services.reminder_scheduler.create_tiktok_post", create or default_create
    )
    monkeypatch.setattr(
        "app.services.reminder_scheduler.poll_tiktok_post_result", poll or default_poll
    )
    return calls


async def test_dispatch_tiktok_happy_path(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Past-due job with no state runs all three phases in one dispatch
    (instant publish: slot already passed)."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    await store.create(_tiktok_job())
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    await wait_for_inflight()
    assert actions == 1
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "uploaded"
    assert job.platform_statuses["tiktok"].url == "https://tiktok.com/@a/video/1"
    assert job.tiktok_publish_state.stage == "published"
    assert len(calls["stage"]) == 1
    assert calls["create"][0]["scheduled_at"] is None      # late job → instant
    assert calls["create"][0]["social_account_id"] == "spc_1"
    assert calls["create"][0]["caption"] == "cap"
    assert calls["stage"][0]["download_url"] == job.drive_video_url
    assert len(calls["poll"]) == 1


async def test_dispatch_tiktok_missing_payload_skips(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, caplog
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    await store.create(_tiktok_job(payload=False))
    with caplog.at_level(logging.WARNING):
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )
        await wait_for_inflight()
    assert actions == 0
    job = await store.get("p1")
    assert (
        job.platform_statuses.get("tiktok", PlatformStatus(status="pending")).status
        == "pending"
    )
    assert any("no tiktok_payload" in record.message for record in caplog.records)


async def test_dispatch_tiktok_skipped_status_missing_payload_no_warning(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, caplog
):
    """A job seeded with 'skipped' (no PFM account configured) has no
    tiktok_payload by design, and must not warn on every scheduler tick forever."""
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    await store.create(
        _tiktok_job(
            payload=False,
            platform_statuses={"tiktok": PlatformStatus(status="skipped")},
        )
    )
    with caplog.at_level(logging.WARNING):
        actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )
        await wait_for_inflight()
    assert actions == 0
    assert not any("no tiktok_payload" in record.message for record in caplog.records)


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
    await wait_for_inflight()
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

    async def failing_create(**kwargs):
        return TikTokPublishResult(success=False, detail="create_post: HTTP 400")

    _patch_phases(monkeypatch, create=failing_create)
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="pending", attempts=4)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "failed"
    assert updated.platform_statuses["tiktok"].attempts == 5
    contents = [
        str(kwargs.get("content") or (args[1] if len(args) > 1 else ""))
        for args, kwargs in discord.post_message.call_args_list
    ]
    assert any("TikTok" in c for c in contents)


_QUOTA_DETAIL = "result: Failed to post to TikTok [reached_active_user_cap, HTTP 403]"


def _quota_failing_poll():
    async def failing_poll(**kwargs):
        return TikTokPublishResult(
            success=False,
            detail=_QUOTA_DETAIL,
            publish_state=_ok_state(post_id="post_1", stage="failed"),
        )
    return failing_poll


async def test_dispatch_tiktok_quota_error_delays_retry_past_normal_cap(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """A TikTok-side quota error (reached_active_user_cap) must not fail
    terminally at the normal attempt cap: it stays pending with a spaced-out
    retry_not_before, and the due-time gate blocks immediate redispatch."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    _patch_phases(monkeypatch, poll=_quota_failing_poll())
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="pending", attempts=4)
    )
    before = datetime.now(tz=UTC)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    job = await store.get("p1")
    tt = job.platform_statuses["tiktok"]
    assert tt.status == "pending"          # not terminal despite attempts == 5
    assert tt.attempts == 5
    assert tt.retry_not_before is not None
    assert tt.retry_not_before >= before + timedelta(minutes=4)
    discord.post_message.assert_not_called()  # first-error ping fired at attempt 1
    assert _platform_due_time(job, "tiktok") == tt.retry_not_before
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    await wait_for_inflight()
    assert actions == 0                    # gated until retry_not_before


async def test_dispatch_tiktok_first_failure_pings_with_error_detail(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """The very first failed attempt warns immediately (with the platform
    error code) instead of staying silent until the terminal failure."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    _patch_phases(monkeypatch, poll=_quota_failing_poll())
    await store.create(_tiktok_job())
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    job = await store.get("p1")
    tt = job.platform_statuses["tiktok"]
    assert tt.status == "pending"          # still retrying
    assert tt.attempts == 1
    contents = [
        str(kwargs.get("content") or (args[1] if len(args) > 1 else ""))
        for args, kwargs in discord.post_message.call_args_list
    ]
    assert any("reached_active_user_cap" in c and "retrying" in c for c in contents)


async def test_dispatch_tiktok_quota_error_fails_after_extended_cap(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    _patch_phases(monkeypatch, poll=_quota_failing_poll())
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="pending", attempts=11)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    job = await store.get("p1")
    tt = job.platform_statuses["tiktok"]
    assert tt.status == "failed"
    assert tt.attempts == 12
    contents = [
        str(kwargs.get("content") or (args[1] if len(args) > 1 else ""))
        for args, kwargs in discord.post_message.call_args_list
    ]
    assert any("reached_active_user_cap" in c for c in contents)


async def test_dispatch_tiktok_terminal_statuses_are_not_retried(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    await store.create(_tiktok_job())
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="uploaded")
    )
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    await wait_for_inflight()
    assert actions == 0
    assert calls["stage"] == [] and calls["create"] == []


async def test_dispatch_tiktok_resumes_uploading_after_crash(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """'uploading' + live post_id + past slot → re-dispatch goes straight to
    polling; the persisted post_id is the double-post protection."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job()
    job.tiktok_publish_state = TikTokPublishState(post_id="post_7", stage="post_created")
    await store.create(job)
    await store.merge_platform_status(
        "p1", "tiktok", PlatformStatus(status="uploading", attempts=1)
    )
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert calls["create"] == []                        # no second post
    assert calls["poll"][0]["publish_state"].post_id == "post_7"
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "uploaded"
    assert updated.platform_statuses["tiktok"].attempts == 2


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
        await wait_for_inflight()

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
        await wait_for_inflight()

    # The dispatch task started (1); it internally fails (still in progress /
    # poll timeout, not yet at max attempts) and leaves status 'pending'.
    assert actions == 1
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
        await wait_for_inflight()

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
        await wait_for_inflight()
        due_actions = await dispatch_due_actions(
            store=store,
            settings=settings,
            discord=discord,
            now=datetime(2026, 4, 26, 21, 1, tzinfo=UTC),
        )
        await wait_for_inflight()

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
        # the 5th should mark 'failed' and post the ping. Each tick must fully
        # complete (wait_for_inflight) before the next, or the in-flight guard
        # would skip re-dispatch and attempts would never accumulate.
        for _ in range(5):
            await dispatch_due_actions(store=store, settings=settings, discord=discord)
            await wait_for_inflight()

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
        await wait_for_inflight()

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
        await wait_for_inflight()

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
        await wait_for_inflight()

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
async def test_dispatch_instagram_retries_old_recoverable_failures_once(
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
        await wait_for_inflight()
        second_actions = await dispatch_due_actions(
            store=store, settings=settings, discord=discord
        )
        await wait_for_inflight()

    refreshed = await store.get("ig-old-failure")
    ig = refreshed.platform_statuses["instagram"]
    # First dispatch is worthwhile (attempts still at the retryable threshold)
    # and starts a task, which then fails permanently (bumped attempts exceed
    # max). Second dispatch is no longer worthwhile at the bumped attempts, so
    # no task starts.
    assert first_actions == 1
    assert second_actions == 0
    assert ig.status == "failed"
    assert ig.attempts == 6
    assert publish_mock.await_count == 1


# ---------------------------------------------------------------------------
# TikTok due-time tests (phased dispatch)
# ---------------------------------------------------------------------------

def test_tiktok_media_staging_due_on_arrival():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    assert job.tiktok_publish_state is None
    assert _platform_due_time(job, "tiktok") == job.created_at


def test_tiktok_post_creation_due_at_lead():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    job.tiktok_publish_state = _ok_state()             # media staged
    assert TIKTOK_SCHEDULE_LEAD_MINUTES == 10
    assert _platform_due_time(job, "tiktok") == slot - timedelta(minutes=10)


def test_tiktok_poll_due_at_slot_once_post_exists():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    job.tiktok_publish_state = _ok_state(post_id="post_1", stage="post_scheduled")
    assert _platform_due_time(job, "tiktok") == slot


def test_tiktok_failed_post_due_at_lead_for_recreate():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    job.tiktok_publish_state = _ok_state(post_id="post_old", stage="failed")
    assert _platform_due_time(job, "tiktok") == slot - timedelta(minutes=10)


def test_instagram_due_time_has_no_lead():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"instagram": slot}
    assert _platform_due_time(job, "instagram") == slot


def test_tiktok_due_does_not_mutate_stored_time():
    slot = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
    job = _make_job(project_id="p1", slot_time=slot)
    job.platform_scheduled_at = {"tiktok": slot}
    _platform_due_time(job, "tiktok")
    assert job.platform_scheduled_at["tiktok"] == slot


# ---------------------------------------------------------------------------
# TikTok phase-behaviour tests
# ---------------------------------------------------------------------------

async def test_tiktok_media_staged_on_arrival_then_waits(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Job far from its slot: dispatch stages media, then stops (no post)."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    await store.create(_tiktok_job(slot_offset_minutes=120))
    actions = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    await wait_for_inflight()
    assert actions == 1
    assert len(calls["stage"]) == 1
    assert calls["create"] == []
    job = await store.get("p1")
    assert job.tiktok_publish_state.stage == "media_uploaded"
    assert job.platform_statuses["tiktok"].status == "pending"
    assert job.platform_statuses["tiktok"].attempts == 0   # staging is attempt-free


async def test_tiktok_staging_failure_before_window_is_quiet(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def failing_stage(**kwargs):
        prior = kwargs.get("publish_state")
        n = (prior.media_attempts if prior else 0) + 1
        return TikTokPublishResult(
            success=False, detail="upload: boom",
            publish_state=TikTokPublishState(media_attempts=n, last_error="upload: boom"),
        )

    _patch_phases(monkeypatch, stage=failing_stage)
    await store.create(_tiktok_job(slot_offset_minutes=120))
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "pending"
    assert job.platform_statuses["tiktok"].attempts == 0   # quiet: no attempts burned
    assert job.tiktok_publish_state.media_attempts == 2
    discord.post_message.assert_not_called()


async def test_tiktok_staging_failure_inside_window_counts_attempts(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def failing_stage(**kwargs):
        return TikTokPublishResult(success=False, detail="upload: boom")

    _patch_phases(monkeypatch, stage=failing_stage)
    await store.create(_tiktok_job(slot_offset_minutes=5))   # inside sched−10
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    job = await store.get("p1")
    assert job.platform_statuses["tiktok"].status == "pending"
    assert job.platform_statuses["tiktok"].attempts == 1


async def test_tiktok_scheduled_create_inside_window(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Slot 5 min out, media staged → create with scheduled_at=sched, no poll."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job(slot_offset_minutes=5)
    job.tiktok_publish_state = _ok_state()
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert len(calls["create"]) == 1
    sched = calls["create"][0]["scheduled_at"]
    assert sched is not None
    assert sched == job.platform_scheduled_at.get("tiktok") or sched == job.slot_time
    assert calls["poll"] == []                       # slot not reached yet
    updated = await store.get("p1")
    assert updated.tiktok_publish_state.stage == "post_scheduled"
    assert updated.platform_statuses["tiktok"].status == "uploading"


async def test_tiktok_instant_create_when_slot_imminent(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Slot < 60 s away → scheduled_at omitted and poll runs immediately."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    calls = _patch_phases(monkeypatch)
    job = _tiktok_job(slot_offset_minutes=0)         # "now" → < 60 s away
    job.tiktok_publish_state = _ok_state()
    await store.create(job)
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert calls["create"][0]["scheduled_at"] is None
    assert len(calls["poll"]) == 1
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "uploaded"


async def test_tiktok_slow_staging_refreshes_now_for_instant_decision(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Media staging (Phase 1) can take a while; the instant/poll decisions in
    Phases 2+3 must read a fresh clock, not the one captured before staging
    began. sched sits just past the 60s instant-publish cutoff at dispatch
    start (61s away); the fake stage sleeps 2s, which is enough for a
    freshly-read clock to land inside the cutoff by the time Phase 2 runs. A
    stale pre-staging clock would still read ~61s away and miss the cutoff,
    scheduling the post instead of publishing instantly and skipping poll."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def slow_stage(**kwargs):
        await asyncio.sleep(2)
        return TikTokPublishResult(success=True, publish_state=_ok_state())

    calls = _patch_phases(monkeypatch, stage=slow_stage)
    job = _make_job(
        project_id="p1",
        slot_time=datetime.now(tz=UTC) + timedelta(seconds=61),
    )
    job.tiktok_payload = {
        "social_account_id": "spc_1",
        "caption": "cap",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": True,
        "allow_stitch": True,
    }
    await store.create(job)

    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()

    assert calls["create"][0]["scheduled_at"] is None      # instant, not scheduled
    assert len(calls["poll"]) == 1                          # polled in same dispatch
    updated = await store.get("p1")
    assert updated.platform_statuses["tiktok"].status == "uploaded"


# ---------------------------------------------------------------------------
# Concurrent dispatch tests (in-flight registry)
# ---------------------------------------------------------------------------

async def test_two_due_jobs_dispatch_concurrently(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """Two same-slot TikTok jobs must overlap, not serialize."""
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    gate = asyncio.Event()
    concurrent = 0
    peak = 0

    async def blocking_poll(**kwargs):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await gate.wait()
        concurrent -= 1
        return TikTokPublishResult(
            success=True, url="https://t/v",
            publish_state=_ok_state(post_id="post_1", stage="published"),
        )

    _patch_phases(monkeypatch, poll=blocking_poll)
    await store.create(_tiktok_job(project_id="pA"))
    await store.create(_tiktok_job(project_id="pB"))
    started = await dispatch_due_actions(
        store=store, settings=settings, discord=discord
    )
    assert started == 2
    await asyncio.sleep(0.05)          # let both tasks reach the gate
    assert peak == 2                   # overlapping, not serialized
    gate.set()
    await wait_for_inflight()
    for pid in ("pA", "pB"):
        job = await store.get(pid)
        assert job.platform_statuses["tiktok"].status == "uploaded"


async def test_inflight_job_is_not_double_dispatched(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    gate = asyncio.Event()
    poll_calls = 0

    async def blocking_poll(**kwargs):
        nonlocal poll_calls
        poll_calls += 1
        await gate.wait()
        return TikTokPublishResult(
            success=True, url="https://t/v",
            publish_state=_ok_state(post_id="post_1", stage="published"),
        )

    _patch_phases(monkeypatch, poll=blocking_poll)
    await store.create(_tiktok_job())
    first = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await asyncio.sleep(0.05)
    second = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    gate.set()
    await wait_for_inflight()
    assert first == 1
    assert second == 0                 # still in flight → skipped
    assert poll_calls == 1


async def test_dispatch_task_exception_clears_inflight(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    """A crashing dispatch must not wedge the (project, platform) key forever."""
    from app.services import reminder_scheduler

    settings = replace(
        _settings_for(example_yaml, tmp_server_dir / "avatars"), pfm_api_key="key"
    )
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()

    async def exploding_stage(**kwargs):
        raise RuntimeError("boom")

    _patch_phases(monkeypatch, stage=exploding_stage)
    await store.create(_tiktok_job(slot_offset_minutes=120))
    await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert reminder_scheduler._IN_FLIGHT == {}
