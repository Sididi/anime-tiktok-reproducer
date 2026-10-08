"""Recovered remote submissions do not need a still-accessible source video."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.job import Job, PlatformStatus
from app.services import additional_publishers
from app.services.job_store import JobStore


@pytest.mark.parametrize(("platform", "state", "expected_status"), [
    ("bilibili", {"resource_id": "BV1done"}, "uploaded"),
    ("bilibili", {"submission_started": True}, "failed"),
    ("snapchat", {"stage": "submitted", "resource_id": "spotlight"}, "uploaded"),
    ("snapchat", {"stage": "submitting"}, "failed"),
    ("dailymotion", {"stage": "submitted", "resource_id": "video"}, "uploaded"),
    ("dailymotion", {"stage": "creating"}, "failed"),
])
async def test_dispatch_recovers_without_downloading(
    tmp_path, monkeypatch, platform, state, expected_status,
):
    store = JobStore(tmp_path / "jobs.json")
    due = datetime.now(UTC) - timedelta(minutes=1)
    job = Job(
        project_id="recover", job_id="recover-job", account_id="anime_fr", device_id="",
        anime_title="Video", description="description", drive_video_url="https://expired.invalid/",
        slot_time=due, platforms_requested=[platform],
        platform_statuses={platform: PlatformStatus(status="uploading")},
        discord_message_id=None, reminder_message_id=None,
        publisher_payloads={platform: {"mode": "api", "credentials": {}}},
        publisher_states={platform: state},
    )
    await store.create(job)
    download = AsyncMock(side_effect=RuntimeError("expired source"))
    monkeypatch.setattr(additional_publishers, "_download_video", download)
    settings = SimpleNamespace(data_dir=tmp_path)
    await additional_publishers.dispatch(job, platform, store, settings, AsyncMock())
    updated = await store.get(job.project_id)
    assert updated.platform_statuses[platform].status == expected_status
    download.assert_not_called()


@pytest.mark.parametrize(("platform", "state"), [
    ("bilibili", {}), ("bilibili", {"upload_token": "token", "uploaded_parts": 1}),
    ("snapchat", {"stage": "media_ready", "media_id": "id"}),
    ("snapchat", {"stage": "submitted"}), ("dailymotion", {"stage": "pending"}),
])
def test_prepublication_states_still_require_media(platform, state):
    assert additional_publishers.needs_video_download(platform, state)


def test_dailymotion_private_resource_can_reconcile_without_source():
    assert not additional_publishers.needs_video_download(
        "dailymotion", {"resource_id": "video", "stage": "private"}
    )
