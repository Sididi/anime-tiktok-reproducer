"""Tests for app.services.job_store."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.job import PlatformStatus, TikTokJob
from app.services.job_store import JobStore


def _make_job(project_id: str = "proj_1", device_id: str = "iphone_13_pro") -> TikTokJob:
    now = datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc)
    return TikTokJob(
        project_id=project_id,
        job_id=f"j_{project_id}",
        account_id="anime_fr",
        device_id=device_id,
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        slot_time=now,
        platforms_requested=["tiktok"],
        status="pending",
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=None,
        reminder_message_id=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )


async def test_create_and_get(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job()
    await store.create(job)
    fetched = await store.get(job.project_id)
    assert fetched == job


async def test_get_missing_returns_none(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    assert await store.get("missing") is None


async def test_create_duplicate_is_noop(tmp_path: Path):
    """Idempotency: re-creating same project_id keeps the existing record."""
    store = JobStore(tmp_path / "jobs.json")
    j1 = _make_job()
    await store.create(j1)
    j2 = _make_job()
    j2.anime_title = "Different"
    await store.create(j2)  # should NOT overwrite
    fetched = await store.get(j1.project_id)
    assert fetched is not None
    assert fetched.anime_title == "Title"


async def test_update(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job()
    await store.create(job)
    updated = await store.update(
        job.project_id,
        status="acked",
        acked_at=datetime(2026, 4, 26, 21, 5, tzinfo=timezone.utc),
    )
    assert updated.status == "acked"
    assert updated.acked_at is not None


async def test_update_missing_raises(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(KeyError):
        await store.update("missing", status="acked")


async def test_delete(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job())
    await store.delete("proj_1")
    assert await store.get("proj_1") is None


async def test_delete_missing_is_noop(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.delete("never_existed")  # must not raise


async def test_list_for_device_filters_by_device_and_status(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job(project_id="a", device_id="iphone_13_pro"))
    await store.create(_make_job(project_id="b", device_id="pixel_8"))
    j_acked = _make_job(project_id="c", device_id="iphone_13_pro")
    j_acked.status = "acked"
    await store.create(j_acked)

    pending_iphone = await store.list_for_device("iphone_13_pro", status="pending")
    assert {j.project_id for j in pending_iphone} == {"a"}

    all_iphone = await store.list_for_device("iphone_13_pro")
    assert {j.project_id for j in all_iphone} == {"a", "c"}


async def test_persists_across_instances(tmp_path: Path):
    """JSON file survives store re-instantiation."""
    p = tmp_path / "jobs.json"
    s1 = JobStore(p)
    await s1.create(_make_job())
    s2 = JobStore(p)
    assert (await s2.get("proj_1")) is not None


async def test_concurrent_writes_serialize(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job())

    async def bump(i: int):
        await store.update("proj_1", anime_title=f"v{i}")

    await asyncio.gather(*[bump(i) for i in range(50)])
    final = await store.get("proj_1")
    assert final is not None
    assert final.anime_title.startswith("v")


async def test_update_rejects_unknown_field(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job())
    with pytest.raises(ValueError, match="stattus"):
        await store.update("proj_1", stattus="acked")  # typo for status


async def test_corrupt_file_treated_as_empty(tmp_path: Path):
    p = tmp_path / "jobs.json"
    p.write_text("{not valid json}")
    store = JobStore(p)
    assert await store.get("anything") is None
