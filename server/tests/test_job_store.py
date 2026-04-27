"""Tests for app.services.job_store."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.job import PlatformStatus, Job
from app.services.job_store import JobStore


def _make_job(project_id: str = "proj_1", device_id: str = "iphone_13_pro") -> Job:
    now = datetime(2026, 4, 26, 21, 0, tzinfo=UTC)
    return Job(
        project_id=project_id,
        job_id=f"j_{project_id}",
        account_id="anime_fr",
        device_id=device_id,
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        slot_time=now,
        platforms_requested=["tiktok"],
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id=None,
        reminder_message_id=None,
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
    from app.models.job import PlatformStatus
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job()
    await store.create(job)
    completed = datetime(2026, 4, 26, 21, 5, tzinfo=UTC)
    updated = await store.update(
        job.project_id,
        platform_statuses={"tiktok": PlatformStatus(status="uploaded", completed_at=completed)},
    )
    assert updated.platform_statuses["tiktok"].status == "uploaded"
    assert updated.platform_statuses["tiktok"].completed_at == completed


async def test_update_missing_raises(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(KeyError):
        await store.update("missing", anime_title="new title")


async def test_delete(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job())
    await store.delete("proj_1")
    assert await store.get("proj_1") is None


async def test_delete_missing_is_noop(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.delete("never_existed")  # must not raise


async def test_list_all_returns_all_jobs(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    await store.create(_make_job(project_id="a"))
    await store.create(_make_job(project_id="b"))
    await store.create(_make_job(project_id="c"))

    every = await store.list_all()
    assert {j.project_id for j in every} == {"a", "b", "c"}


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


async def test_merge_platform_status_preserves_other_keys(tmp_path: Path):
    """Concurrency safety: merging instagram status does not clobber tiktok."""
    store = JobStore(tmp_path / "jobs.json")
    job = _make_job()
    job.platform_statuses = {
        "tiktok": PlatformStatus(status="pending"),
        "instagram": PlatformStatus(status="pending"),
    }
    await store.create(job)

    # Simulate two writers updating different platforms.
    await store.merge_platform_status(
        job.project_id, "instagram", PlatformStatus(status="uploading", attempts=1)
    )
    await store.merge_platform_status(
        job.project_id, "tiktok", PlatformStatus(status="uploaded")
    )
    # Even if a third caller had a stale snapshot of platform_statuses, the
    # merge would preserve both prior writes:
    await store.merge_platform_status(
        job.project_id, "instagram", PlatformStatus(status="uploaded", url="https://x")
    )

    fresh = await store.get(job.project_id)
    assert fresh.platform_statuses["tiktok"].status == "uploaded"
    assert fresh.platform_statuses["instagram"].status == "uploaded"
    assert fresh.platform_statuses["instagram"].url == "https://x"


async def test_merge_platform_status_missing_job_raises(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.json")
    with pytest.raises(KeyError):
        await store.merge_platform_status(
            "missing", "instagram", PlatformStatus(status="pending")
        )
