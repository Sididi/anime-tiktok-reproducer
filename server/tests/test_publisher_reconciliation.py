"""Explicit recovery after an operator checks an ambiguous provider submission."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.internal import router
from app.models.job import Job, PlatformStatus
from app.services.job_store import JobStore

AUTH = {"Authorization": "Bearer internal-test"}


async def make_app(tmp_path, platform, state, *, status="failed", mode="api"):
    app = FastAPI()
    app.include_router(router)
    store = JobStore(tmp_path / "jobs.json")
    app.state.job_store = store
    app.state.settings = SimpleNamespace(internal_api_token="internal-test")
    app.state.discord = AsyncMock()
    await store.create(Job(
        project_id="project", job_id="job", account_id="anime_fr", device_id="",
        anime_title="Video", description="Description", drive_video_url="https://drive.invalid/",
        slot_time=datetime.now(UTC), platforms_requested=[platform],
        platform_statuses={platform: PlatformStatus(status=status, attempts=2)},
        discord_message_id=None, reminder_message_id=None,
        publisher_payloads={platform: {"mode": mode, "credentials": {}}},
        publisher_states={platform: state},
    ))
    return app, store


async def post(app, platform, payload, *, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://local.test"
    ) as client:
        return await client.post(
            f"/api/internal/jobs/project/platforms/{platform}/reconcile",
            json=payload, headers=AUTH if headers is None else headers,
        )


@pytest.mark.parametrize(("platform", "state", "resource_id"), [
    ("bilibili", {"submission_started": True}, "BV1found"),
    ("snapchat", {"stage": "submitting", "media_id": "media"}, "spotlight-found"),
    ("dailymotion", {"stage": "creating"}, "xfound"),
])
async def test_confirmed_publication_records_recoverable_provider_identity(
    tmp_path, platform, state, resource_id,
):
    app, store = await make_app(tmp_path, platform, state)
    response = await post(app, platform, {
        "outcome": "published", "confirm": True, "resource_id": resource_id,
    })
    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
    job = await store.get("project")
    assert job.publisher_states[platform]["resource_id"] == resource_id
    assert job.platform_statuses[platform].attempts == 2
    assert job.platform_statuses[platform].completed_at is not None


@pytest.mark.parametrize(("platform", "state", "expected"), [
    ("bilibili", {"submission_started": True, "upload_token": "token"},
        {"submission_started": False, "upload_token": "token"}),
    ("snapchat", {"stage": "submitting", "media_id": "media"},
        {"stage": "media_ready", "media_id": "media"}),
    ("dailymotion", {"stage": "creating"}, {"stage": "pending"}),
    ("dailymotion", {"stage": "submitting", "resource_id": "private-video"},
        {"stage": "private", "resource_id": "private-video"}),
])
async def test_confirmed_absence_preserves_staging_and_waits_for_explicit_retry(
    tmp_path, platform, state, expected,
):
    app, store = await make_app(tmp_path, platform, state)
    response = await post(app, platform, {"outcome": "not_published", "confirm": True})
    assert response.status_code == 200
    job = await store.get("project")
    assert job.platform_statuses[platform].status == "failed"
    assert job.publisher_states[platform].items() >= expected.items()
    assert job.publisher_states[platform]["reconciled_outcome"] == "not_published"


@pytest.mark.parametrize("payload", [
    {"outcome": "not_published"}, {"outcome": "not_published", "confirm": False},
])
async def test_confirmation_is_required(tmp_path, payload):
    state = {"submission_started": True}
    app, store = await make_app(tmp_path, "bilibili", state)
    response = await post(app, "bilibili", payload)
    assert response.status_code == 422
    assert (await store.get("project")).publisher_states["bilibili"] == state


@pytest.mark.parametrize("status", ["pending", "uploading", "uploaded", "skipped"])
async def test_only_failed_jobs_can_be_reconciled(tmp_path, status):
    app, _store = await make_app(tmp_path, "bilibili", {}, status=status)
    response = await post(app, "bilibili", {"outcome": "not_published", "confirm": True})
    assert response.status_code == 409


async def test_inflight_dispatch_is_not_interrupted_or_waited_on(tmp_path):
    app, store = await make_app(tmp_path, "bilibili", {"submission_started": True})
    async with store.publisher_lock("project"):
        response = await post(app, "bilibili", {"outcome": "not_published", "confirm": True})
    assert response.status_code == 409
    assert "in progress" in response.json()["detail"]


@pytest.mark.parametrize("resource_id", [None, "https://foreign.invalid/video", "../file", "wrong"])
async def test_published_bilibili_requires_a_real_video_identifier(tmp_path, resource_id):
    app, _store = await make_app(tmp_path, "bilibili", {"submission_started": True})
    response = await post(app, "bilibili", {
        "outcome": "published", "confirm": True, "resource_id": resource_id,
    })
    assert response.status_code == 400


async def test_reconciliation_requires_internal_authentication(tmp_path):
    app, _store = await make_app(tmp_path, "bilibili", {})
    response = await post(
        app, "bilibili", {"outcome": "not_published", "confirm": True}, headers={}
    )
    assert response.status_code == 401


async def test_manual_mode_cannot_be_reconciled_through_api_recovery(tmp_path):
    app, _store = await make_app(tmp_path, "bilibili", {}, mode="manual")
    response = await post(app, "bilibili", {"outcome": "not_published", "confirm": True})
    assert response.status_code == 409
