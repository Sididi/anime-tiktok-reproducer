"""Different platforms progress independently without losing per-platform state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.internal import router
from app.models.job import Job, PlatformStatus
from app.services import additional_publishers as publishers
from app.services.job_store import JobStore


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.json")
    settings = SimpleNamespace(
        internal_api_token="test-token",
        data_dir=tmp_path,
        discord=SimpleNamespace(reminder_channel_id="reminders"),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.job_store = store
    app.state.settings = settings
    app.state.discord = AsyncMock()
    files = []

    async def download(*args, **kwargs):
        path = tmp_path / f"video-{len(files)}.mp4"
        files.append(path)
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(publishers, "_download_video", download)
    return SimpleNamespace(
        store=store,
        settings=settings,
        discord=app.state.discord,
        app=app,
        files=files,
    )


def make_job(mode="api"):
    due = datetime.now(UTC) - timedelta(seconds=10)
    return Job(
        project_id="project",
        job_id="job",
        account_id="account",
        device_id="",
        anime_title="Video",
        description="Description",
        drive_video_url="https://drive.invalid/",
        slot_time=due,
        platforms_requested=list(publishers.PLATFORMS),
        platform_statuses={p: PlatformStatus(status="pending") for p in publishers.PLATFORMS},
        discord_message_id=None,
        reminder_message_id=None,
        platform_scheduled_at=dict.fromkeys(publishers.PLATFORMS, due),
        publisher_payloads={p: {"mode": mode, "credentials": {}} for p in publishers.PLATFORMS},
    )


async def request(context, method, route, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(context.app), base_url="https://local.test"
    ) as client:
        return await client.request(
            method,
            f"/api/internal/jobs/project{route}",
            headers={"Authorization": "Bearer test-token"},
            **kwargs,
        )


def block_dailymotion(monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    class Publisher:
        async def publish(self, *args, on_state, **kwargs):
            await on_state({"stage": "uploading"})
            started.set()
            await finish.wait()
            await on_state({"stage": "submitted", "resource_id": "dm-video"})
            return SimpleNamespace(
                success=True, url="https://dailymotion.com/video/dm", detail=None
            )

    monkeypatch.setattr(publishers, "publisher_for", lambda platform, credentials: Publisher())
    return started, finish


async def dispatch(context, job, platform):
    await publishers.dispatch(job, platform, context.store, context.settings, context.discord)


async def test_other_platform_can_cancel_and_reschedule_during_long_upload(context, monkeypatch):
    job = make_job()
    await context.store.create(job)
    started, finish = block_dailymotion(monkeypatch)
    task = asyncio.create_task(dispatch(context, job, "dailymotion"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not context.store.publisher_lock(job.project_id).locked()
        response = await request(context, "POST", "/platforms/snapchat/cancel")
        assert response.status_code == 200
        new_time = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        response = await request(
            context,
            "PATCH",
            "/slot",
            json={
                "platform_scheduled_at": {"bilibili": new_time},
            },
        )
        assert response.status_code == 200
        assert (await request(context, "POST", "/platforms/dailymotion/cancel")).status_code == 409
        assert (
            await request(
                context,
                "PATCH",
                "/slot",
                json={
                    "platform_scheduled_at": {"dailymotion": new_time},
                },
            )
        ).status_code == 409
        assert (await request(context, "DELETE", "")).status_code == 409
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(context.app), base_url="https://local.test"
        ) as client:
            response = await client.post(
                "/api/internal/jobs",
                json={
                    "project_id": "project",
                    "account_id": "account",
                    "anime_title": "Video",
                    "description": "Description",
                    "drive_video_url": "https://drive.invalid/",
                    "slot_time": new_time,
                    "platforms_requested": list(publishers.PLATFORMS),
                },
                headers={"Authorization": "Bearer test-token"},
            )
        assert response.status_code == 409
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
    saved = await context.store.get(job.project_id)
    assert saved.platform_statuses["dailymotion"].status == "uploaded"
    assert saved.platform_statuses["snapchat"].status == "skipped"
    assert saved.platform_scheduled_at["bilibili"].isoformat() == new_time


async def test_parallel_uploads_preserve_every_checkpoint_and_result(context, monkeypatch):
    job = make_job()
    await context.store.create(job)
    barrier = asyncio.Barrier(3)

    class Publisher:
        def __init__(self, platform):
            self.platform = platform

        async def publish(self, *args, on_state, **kwargs):
            await on_state({"stage": "uploading", "platform": self.platform})
            await barrier.wait()
            await on_state({"stage": "submitted", "resource_id": self.platform})
            await barrier.wait()
            return SimpleNamespace(
                success=True, url=f"https://provider/{self.platform}", detail=None
            )

    monkeypatch.setattr(
        publishers, "publisher_for", lambda platform, credentials: Publisher(platform)
    )
    await asyncio.wait_for(
        asyncio.gather(*(dispatch(context, job, platform) for platform in publishers.PLATFORMS)), 3
    )
    saved = await context.store.get(job.project_id)
    assert set(saved.publisher_states) == set(publishers.PLATFORMS)
    for platform in publishers.PLATFORMS:
        assert saved.publisher_states[platform]["resource_id"] == platform
        assert saved.platform_statuses[platform].status == "uploaded"
        assert saved.platform_statuses[platform].attempts == 1
    assert all(not path.exists() for path in context.files)


async def test_parallel_manual_reminders_keep_all_message_ids(context):
    job = make_job(mode="manual")
    await context.store.create(job)
    barrier = asyncio.Barrier(3)

    async def post_message(channel, *, content):
        await barrier.wait()
        return next(p for p in publishers.PLATFORMS if p.lower() in content.lower())

    context.discord.post_message.side_effect = post_message
    await asyncio.wait_for(
        asyncio.gather(*(dispatch(context, job, platform) for platform in publishers.PLATFORMS)), 2
    )
    saved = await context.store.get(job.project_id)
    assert saved.publisher_reminders == {p: p for p in publishers.PLATFORMS}


async def test_reaction_for_manual_platform_does_not_wait_for_other_upload(context, monkeypatch):
    job = make_job()
    job.publisher_payloads["snapchat"]["mode"] = "manual"
    job.publisher_reminders["snapchat"] = "reminder"
    await context.store.create(job)
    started, finish = block_dailymotion(monkeypatch)
    task = asyncio.create_task(dispatch(context, job, "dailymotion"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(
            publishers.acknowledge(
                job,
                "snapchat",
                context.store,
                context.settings,
                context.discord,
            ),
            2,
        )
        saved = await context.store.get(job.project_id)
        assert saved.platform_statuses["snapchat"].status == "uploaded"
        assert "snapchat" not in saved.publisher_reminders
        assert saved.platform_statuses["dailymotion"].status == "uploading"
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)


async def test_retry_and_reconcile_other_platform_while_uploading(context, monkeypatch):
    job = make_job()
    job.platform_statuses["snapchat"] = PlatformStatus(status="failed")
    job.platform_statuses["bilibili"] = PlatformStatus(status="failed")
    job.publisher_states["bilibili"] = {"submission_started": True, "upload_token": "token"}
    await context.store.create(job)
    started, finish = block_dailymotion(monkeypatch)
    task = asyncio.create_task(dispatch(context, job, "dailymotion"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (
            await request(context, "POST", "/platforms/snapchat/retry", json={})
        ).status_code == 200
        response = await request(
            context,
            "POST",
            "/platforms/bilibili/reconcile",
            json={
                "confirm": True,
                "outcome": "not_published",
            },
        )
        assert response.status_code == 200
        saved = await context.store.get(job.project_id)
        assert saved.publisher_states["dailymotion"]["stage"] == "uploading"
        assert saved.publisher_states["bilibili"]["submission_started"] is False
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)


async def test_reminder_created_concurrently_does_not_resurrect_cancelled_reminder(context):
    job = make_job(mode="manual")
    job.publisher_reminders["snapchat"] = "snap-reminder"
    await context.store.create(job)
    started, finish = asyncio.Event(), asyncio.Event()

    async def post_message(*args, **kwargs):
        started.set()
        await finish.wait()
        return "bili-reminder"

    context.discord.post_message.side_effect = post_message
    task = asyncio.create_task(dispatch(context, job, "bilibili"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert (await request(context, "POST", "/platforms/snapchat/cancel")).status_code == 200
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
    assert (await context.store.get(job.project_id)).publisher_reminders == {
        "bilibili": "bili-reminder",
    }


async def test_parallel_partial_reschedules_preserve_both_slots(context):
    job = make_job(mode="manual")
    job.publisher_reminders = {"snapchat": "snap-reminder", "bilibili": "bili-reminder"}
    await context.store.create(job)
    barrier = asyncio.Barrier(2)

    async def delete_message(*args, **kwargs):
        await barrier.wait()

    context.discord.delete_message.side_effect = delete_message
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    targets = {"snapchat": tomorrow, "bilibili": tomorrow + timedelta(hours=1)}
    responses = await asyncio.wait_for(
        asyncio.gather(
            *(
                request(
                    context,
                    "PATCH",
                    "/slot",
                    json={
                        "platform_scheduled_at": {platform: target.isoformat()},
                    },
                )
                for platform, target in targets.items()
            )
        ),
        2,
    )
    assert all(response.status_code == 200 for response in responses)
    saved = await context.store.get(job.project_id)
    assert not saved.publisher_reminders
    for platform, target in targets.items():
        assert saved.platform_scheduled_at[platform] == target


async def test_duplicate_dispatch_for_same_platform_does_not_publish_twice(context, monkeypatch):
    job = make_job()
    await context.store.create(job)
    started, finish = block_dailymotion(monkeypatch)
    first = asyncio.create_task(dispatch(context, job, "dailymotion"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(dispatch(context, job, "dailymotion"), 2)
        assert len(context.files) == 1
    finally:
        finish.set()
        await asyncio.wait_for(first, 2)
