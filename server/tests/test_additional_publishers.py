from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.internal import router
from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services import additional_publishers as publishers
from app.services.job_store import JobStore
from app.services.reaction_listener import ReactionListener
from app.services.reminder_scheduler import dispatch_due_actions, wait_for_inflight

AUTH = {"Authorization": "Bearer internal_secret"}


@pytest.fixture
def env(tmp_path, example_yaml, example_env, tmp_server_dir):
    settings = Settings.load(config_path=example_yaml, avatars_dir=tmp_server_dir / "avatars")
    store = JobStore(tmp_path / "jobs.json")
    discord = AsyncMock()
    discord.post_message.side_effect = [
        "dm_reminder",
        "snap_reminder",
        "bili_reminder",
        "new_reminder",
    ]
    app = FastAPI()
    app.include_router(router)
    app.state.settings, app.state.job_store, app.state.discord = settings, store, discord
    app.state.cep_launch_store = SimpleNamespace(delete=AsyncMock())
    return SimpleNamespace(settings=settings, store=store, discord=discord, app=app)


def job(*, mode="manual", due=None):
    due = due or datetime.now(UTC) + timedelta(minutes=1)
    return Job(
        project_id="p1",
        job_id="j1",
        account_id="anime_fr",
        device_id="",
        anime_title="Video",
        description="Caption",
        drive_video_url="https://drive.google.com/video",
        slot_time=due,
        platforms_requested=list(publishers.PLATFORMS),
        platform_statuses={p: PlatformStatus(status="pending") for p in publishers.PLATFORMS},
        discord_message_id="embed",
        reminder_message_id=None,
        platform_scheduled_at={p: due for p in publishers.PLATFORMS},
        publisher_payloads={
            p: {
                "mode": mode,
                "credentials": {"client_id": "app", "profile_id": "profile"},
                "description": "Caption",
                "video_url": "https://drive.google.com/video",
            }
            for p in publishers.PLATFORMS
        },
    )


async def request(env, method, route, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test"
    ) as client:
        return await client.request(method, f"/api/internal/jobs/p1{route}", headers=AUTH, **kwargs)


async def test_manual_reminders_restart_and_ack_are_per_platform(env):
    await env.store.create(job())
    assert (
        await dispatch_due_actions(store=env.store, settings=env.settings, discord=env.discord) == 3
    )
    await wait_for_inflight()
    stored = await env.store.get("p1")
    assert len(stored.publisher_reminders) == 3
    reloaded = JobStore(env.store._path)
    assert (
        await dispatch_due_actions(store=reloaded, settings=env.settings, discord=env.discord) == 0
    )
    listener = ReactionListener(
        bot_token="token", store=reloaded, settings=env.settings, rest_discord_client=env.discord
    )
    await listener._handle_reaction(
        SimpleNamespace(emoji="✅", user_id=123, message_id=stored.publisher_reminders["snapchat"])
    )
    updated = await reloaded.get("p1")
    assert updated.platform_statuses["snapchat"].status == "uploaded"
    assert updated.platform_statuses["bilibili"].status == "pending"
    assert updated.platform_statuses["dailymotion"].status == "pending"
    assert "snapchat" not in updated.publisher_reminders


async def test_reschedule_rearms_one_reminder_and_cancel_stops_dispatch(env):
    original = job()
    await env.store.create(original)
    await dispatch_due_actions(store=env.store, settings=env.settings, discord=env.discord)
    await wait_for_inflight()
    later = datetime.now(UTC) + timedelta(days=1)
    assert (
        await request(
            env, "PATCH", "/slot", json={"platform_scheduled_at": {"snapchat": later.isoformat()}}
        )
    ).status_code == 200
    updated = await env.store.get("p1")
    assert "snapchat" not in updated.publisher_reminders
    assert "bilibili" in updated.publisher_reminders
    assert updated.platform_scheduled_at["bilibili"] == original.slot_time
    for _ in range(2):
        assert (await request(env, "POST", "/platforms/snapchat/cancel")).status_code == 200
    assert (
        await dispatch_due_actions(
            store=env.store, settings=env.settings, discord=env.discord, now=later
        )
        == 0
    )
    assert (await env.store.get("p1")).platform_statuses["snapchat"].status == "skipped"


async def test_api_checkpoints_and_rotated_tokens(env, monkeypatch, tmp_path):
    original = job(mode="api", due=datetime.now(UTC) - timedelta(seconds=1))
    original.platforms_requested = ["snapchat"]
    await env.store.create(original)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(publishers, "_download_video", AsyncMock(return_value=video))
    seen = []

    class Adapter:
        async def publish(self, path, description, *, state, on_state, on_credentials):
            await on_credentials({"refresh_token": "rotated", "title": "not cached"})
            await on_state({"stage": "submitting"})
            seen.append((await env.store.get("p1")).publisher_states["snapchat"])
            await on_state({"stage": "submitted", "resource_id": "spotlight"})
            return SimpleNamespace(
                success=True, url="https://www.snapchat.com/spotlight/id", detail="Submitted"
            )

    monkeypatch.setattr(publishers, "publisher_for", lambda p, c: Adapter())
    assert (
        await dispatch_due_actions(store=env.store, settings=env.settings, discord=env.discord) == 1
    )
    await wait_for_inflight()
    assert seen == [{"stage": "submitting"}]
    assert (await env.store.get("p1")).platform_statuses["snapchat"].status == "uploaded"
    cached = next((env.settings.data_dir / "publisher_credentials").glob("*.json"))
    assert cached.stat().st_mode & 0o777 == 0o600
    assert "not cached" not in cached.read_text()
    assert not video.exists()
    assert (
        await dispatch_due_actions(store=env.store, settings=env.settings, discord=env.discord) == 0
    )
    assert (await request(env, "POST", "/platforms/snapchat/cancel")).status_code == 409


async def test_cancel_reschedule_delete_cannot_race_submission(env, monkeypatch, tmp_path):
    original = job(mode="api", due=datetime.now(UTC) - timedelta(seconds=1))
    original.platforms_requested = ["bilibili"]
    await env.store.create(original)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(publishers, "_download_video", AsyncMock(return_value=video))
    started, finish = asyncio.Event(), asyncio.Event()

    class Adapter:
        async def publish(self, *args, on_state, **kwargs):
            await on_state({"submission_started": True})
            started.set()
            await finish.wait()
            return SimpleNamespace(success=False, url=None, detail="Submission outcome unknown")

    monkeypatch.setattr(publishers, "publisher_for", lambda p, c: Adapter())
    await dispatch_due_actions(store=env.store, settings=env.settings, discord=env.discord)
    await started.wait()
    assert (await request(env, "POST", "/platforms/bilibili/cancel")).status_code == 409
    assert (
        await request(
            env,
            "PATCH",
            "/slot",
            json={"platform_scheduled_at": {"bilibili": datetime.now(UTC).isoformat()}},
        )
    ).status_code == 409
    assert (await request(env, "DELETE", "")).status_code == 409
    finish.set()
    await wait_for_inflight()
    assert (await request(env, "DELETE", "")).status_code == 409
    assert (await request(env, "POST", "/platforms/bilibili/cancel")).status_code == 409


async def test_cancel_staged_private_video_and_retry_identity_guard(env, monkeypatch):
    original = job(mode="api")
    original.publisher_states["dailymotion"] = {"stage": "private", "resource_id": "dm_id"}
    original.platform_statuses["dailymotion"] = PlatformStatus(status="failed")
    await env.store.create(original)
    adapter = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr(publishers, "publisher_for", lambda p, c: adapter)
    assert (
        await request(
            env,
            "POST",
            "/platforms/dailymotion/retry",
            json={"credentials": {"client_id": "other", "profile_id": "profile"}},
        )
    ).status_code == 409
    assert (await request(env, "POST", "/platforms/dailymotion/cancel")).status_code == 200
    adapter.delete.assert_awaited_once_with("dm_id")
    assert "dailymotion" not in (await env.store.get("p1")).publisher_states


async def test_projection_never_contains_credentials(env):
    original = job(mode="api")
    original.publisher_payloads["snapchat"]["credentials"]["client_secret"] = "must-not-leak"
    await env.store.create(original)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/internal/jobs", headers=AUTH)
    assert response.status_code == 200
    assert "must-not-leak" not in response.text
    assert "publisher_payloads" not in response.text
