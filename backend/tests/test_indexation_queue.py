from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.library_types import LibraryType
from app.models.torrent import IndexationJob
from app.services.anime_library import IndexProgress
from app.services.indexation_queue import IndexationQueueService


@pytest.mark.asyncio
async def test_update_job_publishes_as_merged_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "updates"
    source_dir.mkdir()
    for episode in range(12, 23):
        (source_dir / f"Sakamoto Days - {episode:02d}.mkv").write_bytes(b"video")

    remote_manifest = {
        "release_id": "release-1",
        "episodes": [
            {"episode_key": f"Sakamoto Days - {episode:02d}"}
            for episode in range(1, 12)
        ],
    }
    publish_calls: list[dict[str, Any]] = []

    async def fake_ensure_series_index_hydrated(**kwargs: Any) -> dict[str, Any]:
        return remote_manifest

    async def fake_update_anime(**kwargs: Any):
        prepared = [
            str(tmp_path / "library" / "Sakamoto Days" / f"Sakamoto Days - {episode:02d}.mkv")
            for episode in range(12, 23)
        ]
        yield IndexProgress(
            status="complete",
            progress=1.0,
            anime_name="Sakamoto Days",
            prepared_library_paths=prepared,
        )

    async def fake_publish_series_release(**kwargs: Any) -> dict[str, Any]:
        publish_calls.append(kwargs)
        return {"series_id": "series-1", "release_id": "release-2"}

    async def fake_link_torrents(
        self: IndexationQueueService,
        job: IndexationJob,
    ) -> None:
        return None

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.ensure_series_index_hydrated",
        fake_ensure_series_index_hydrated,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.AnimeLibraryService.update_anime",
        fake_update_anime,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.publish_series_release",
        fake_publish_series_release,
    )
    monkeypatch.setattr(
        IndexationQueueService,
        "_link_torrents",
        fake_link_torrents,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.AnimeMatcherService.mark_series_updated",
        lambda *args, **kwargs: None,
    )

    service = IndexationQueueService()
    job = IndexationJob(
        job_type="update",
        source_name="Sakamoto Days",
        library_type=LibraryType.ANIME,
        source_path=str(source_dir),
        series_id="series-1",
    )

    await service._run_job(job)

    assert job.status == "complete"
    assert len(publish_calls) == 1
    assert publish_calls[0]["merge_existing_release"] is True
    assert publish_calls[0]["expected_min_episodes"] == 22


@pytest.mark.asyncio
async def test_gpu_semaphore_caps_concurrent_heavy_tasks() -> None:
    """The shared GPU budget bounds indexation + /matches to MAX_CONCURRENT
    heavy tasks (8 GB VRAM worst case: 2x SSCD embedder). A third acquirer
    waits until a slot frees (GOAL v5.3 W5)."""
    import asyncio

    service = IndexationQueueService()
    sem = service.gpu_semaphore()
    assert sem is service.gpu_semaphore()  # stable shared object
    assert service.MAX_CONCURRENT == 2

    # Two heavy tasks (e.g. one index job + one match run) hold both slots.
    await sem.acquire()
    await sem.acquire()
    assert sem.locked()  # fully subscribed

    # A third heavy task (a second /matches) must wait for a slot.
    third = asyncio.ensure_future(sem.acquire())
    await asyncio.sleep(0.05)
    assert not third.done(), "third heavy task should block while 2 are in flight"

    # Free one slot -> the waiter proceeds.
    sem.release()
    await asyncio.wait_for(third, timeout=1.0)
    assert third.done()

    sem.release()
    sem.release()


@pytest.mark.asyncio
async def test_multi_slot_heavy_job_reserves_full_budget() -> None:
    """A fast-mode matching run (slots=MAX_CONCURRENT) holds the whole GPU
    budget: an indexation job must wait until it exits, and both slots come
    back afterwards (2026-07-19 OOM: matching ~5.3 GiB cannot share the card
    with a GPU-decode indexation)."""
    import asyncio

    service = IndexationQueueService()

    async with service.heavy_slot("matching", slots=service.MAX_CONCURRENT):
        assert service.available_heavy_slots() == 0
        waiter = asyncio.ensure_future(service.acquire_heavy_slot("indexation"))
        await asyncio.sleep(0.05)
        assert not waiter.done(), "indexation must wait while matching holds the budget"
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    assert service.available_heavy_slots() == service.MAX_CONCURRENT


@pytest.mark.asyncio
async def test_cancelled_multi_slot_acquire_releases_partial() -> None:
    """Cancelling a multi-slot acquire mid-wait (SSE client disconnects while
    the match waits for its second slot) must return the partially-acquired
    slot instead of leaking it."""
    import asyncio

    service = IndexationQueueService()
    await service.acquire_heavy_slot("indexation")  # hold 1 of 2

    pending = asyncio.ensure_future(
        service.acquire_heavy_slot("matching", slots=service.MAX_CONCURRENT)
    )
    await asyncio.sleep(0.05)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # The slot matching had already grabbed must be back in the budget.
    assert service.available_heavy_slots() == service.MAX_CONCURRENT - 1
    service.release_heavy_slot("indexation")
    assert service.available_heavy_slots() == service.MAX_CONCURRENT
