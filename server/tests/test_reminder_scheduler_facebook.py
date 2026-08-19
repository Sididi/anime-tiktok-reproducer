"""Long-range Facebook hold dispatch (2026-08 upload flows redesign)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.models.job import FacebookPublishState, Job, PlatformStatus
from app.services.facebook_publisher import FacebookHoldResult
from app.services.job_store import JobStore
from app.services.reminder_scheduler import (
    FACEBOOK_CONVERT_LEAD_DAYS,
    _platform_due_time,
    dispatch_due_actions,
    wait_for_inflight,
)


def _settings_for(example_yaml: Path, avatars_dir: Path) -> Settings:
    return Settings.load(config_path=example_yaml, avatars_dir=avatars_dir)


def _fb_job(
    *,
    project_id: str = "p1",
    target: datetime,
    payload: dict | None = None,
    status: str = "pending",
) -> Job:
    now = datetime.now(tz=UTC)
    return Job(
        project_id=project_id,
        job_id=f"j_{project_id}",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece",
        description="desc",
        drive_video_url="https://drive/x",
        slot_time=target,
        platform_scheduled_at={"facebook": target},
        platforms_requested=["facebook"],
        platform_statuses={"facebook": PlatformStatus(status=status)},
        discord_message_id=None,
        reminder_message_id=None,
        facebook_payload=payload,
        created_at=now,
        updated_at=now,
    )


_CREATE_PAYLOAD = {
    "page_id": "page_1",
    "page_access_token": "tok",
    "title": "T",
    "description": "D",
    "prepared_video_url": "https://drive/fb.mp4",
    "graph_api_version": "v25.0",
}


def test_due_time_is_target_minus_convert_lead():
    target = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    job = _fb_job(target=target, payload=_CREATE_PAYLOAD)
    assert _platform_due_time(job, "facebook") == target - timedelta(
        days=FACEBOOK_CONVERT_LEAD_DAYS
    )


async def test_facebook_without_payload_is_never_dispatched(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """Native FB posts (backend-scheduled, no payload) stay server no-ops."""
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_fb_job(target=datetime.now(tz=UTC) - timedelta(minutes=1)))
    started = await dispatch_due_actions(
        store=store, settings=settings, discord=AsyncMock()
    )
    await wait_for_inflight()
    assert started == 0


async def test_create_hold_converts_when_due(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    target = datetime.now(tz=UTC) + timedelta(days=FACEBOOK_CONVERT_LEAD_DAYS - 1)
    await store.create(_fb_job(target=target, payload=_CREATE_PAYLOAD))

    calls: list[dict] = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return FacebookHoldResult(
            success=True,
            video_id="fbv_1",
            url="https://www.facebook.com/page_1/videos/fbv_1",
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.create_facebook_scheduled_post", fake_create
    )

    started = await dispatch_due_actions(
        store=store, settings=settings, discord=AsyncMock()
    )
    await wait_for_inflight()
    assert started == 1
    assert calls and calls[0]["scheduled_at"] == target
    job = await store.get("p1")
    assert job.platform_statuses["facebook"].status == "uploaded"
    assert job.facebook_publish_state.video_id == "fbv_1"
    assert job.facebook_publish_state.stage == "created"


async def test_retime_hold_patches_existing_post(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    target = datetime.now(tz=UTC) + timedelta(days=5)
    payload = {**_CREATE_PAYLOAD, "video_id": "fbv_existing"}
    await store.create(_fb_job(target=target, payload=payload))

    calls: list[dict] = []

    async def fake_retime(**kwargs):
        calls.append(kwargs)
        return FacebookHoldResult(
            success=True,
            video_id=kwargs["video_id"],
            url="https://www.facebook.com/page_1/videos/fbv_existing",
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.retime_facebook_scheduled_post", fake_retime
    )

    started = await dispatch_due_actions(
        store=store, settings=settings, discord=AsyncMock()
    )
    await wait_for_inflight()
    assert started == 1
    assert calls and calls[0]["video_id"] == "fbv_existing"
    job = await store.get("p1")
    assert job.platform_statuses["facebook"].status == "uploaded"
    assert job.facebook_publish_state.stage == "retimed"


async def test_hold_not_due_yet_is_skipped(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    target = datetime.now(tz=UTC) + timedelta(days=FACEBOOK_CONVERT_LEAD_DAYS + 10)
    await store.create(_fb_job(target=target, payload=_CREATE_PAYLOAD))
    started = await dispatch_due_actions(
        store=store, settings=settings, discord=AsyncMock()
    )
    await wait_for_inflight()
    assert started == 0


async def test_hold_failure_pings_and_becomes_terminal_after_max_attempts(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path, monkeypatch
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    target = datetime.now(tz=UTC) + timedelta(days=3)
    job = _fb_job(target=target, payload=_CREATE_PAYLOAD)
    job.facebook_publish_state = FacebookPublishState(attempts=4)
    await store.create(job)

    async def fake_create(**kwargs):
        return FacebookHoldResult(success=False, detail="start: HTTP 400")

    monkeypatch.setattr(
        "app.services.reminder_scheduler.create_facebook_scheduled_post", fake_create
    )
    discord = AsyncMock()
    started = await dispatch_due_actions(store=store, settings=settings, discord=discord)
    await wait_for_inflight()
    assert started == 1
    stored = await store.get("p1")
    assert stored.platform_statuses["facebook"].status == "failed"
    assert stored.facebook_publish_state.attempts == 5
    discord.post_message.assert_called()  # terminal failure ping


async def test_terminal_status_is_not_redispatched(
    tmp_path: Path, example_yaml: Path, example_env, tmp_server_dir: Path
):
    settings = _settings_for(example_yaml, tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    target = datetime.now(tz=UTC) + timedelta(days=3)
    await store.create(
        _fb_job(target=target, payload=_CREATE_PAYLOAD, status="uploaded")
    )
    started = await dispatch_due_actions(
        store=store, settings=settings, discord=AsyncMock()
    )
    await wait_for_inflight()
    assert started == 0
