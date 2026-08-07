from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.library_types import LibraryType
from app.models.torrent import IndexationJob
from app.services.anime_library import IndexProgress
from app.services.indexation_queue import IndexationQueueService
from app.services.pending_publish_store import (
    PendingPublishRecord,
    PendingPublishStore,
)


def _make_record(
    tmp_path: Path,
    *,
    publish_id: str = "pub-1",
    series_id: str = "series-1",
    display_name: str = "Sakamoto Days",
) -> PendingPublishRecord:
    return PendingPublishRecord(
        publish_id=publish_id,
        library_type="anime",
        series_id=series_id,
        release_id=f"release-{publish_id}",
        display_name=display_name,
        series_dir=str(tmp_path / "library" / "anime" / display_name),
        staged_index_dir=str(
            tmp_path / "cache" / "storage_box" / "pending_publish" / publish_id
        ),
        is_brand_new_series=True,
        manifest={
            "series_id": series_id,
            "release_id": f"release-{publish_id}",
            "display_name": display_name,
            "episode_count": 1,
            "episodes": [],
        },
    )


async def _drain_job_tasks(
    service: IndexationQueueService, timeout: float = 5.0
) -> None:
    import asyncio

    async def _wait() -> None:
        while service._job_tasks:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait(), timeout=timeout)


@pytest.fixture
def isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    return tmp_path


@pytest.mark.asyncio
async def test_update_job_finalizes_as_merged_release_and_enqueues_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_store: Path,
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
    finalize_calls: list[dict[str, Any]] = []
    upload_calls: list[str] = []
    record = _make_record(tmp_path, publish_id="pub-merge")

    async def fake_ensure_index_ready(**kwargs: Any) -> bool:
        return False

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

    async def fake_finalize_series_release(**kwargs: Any) -> PendingPublishRecord:
        finalize_calls.append(kwargs)
        return record

    async def fake_run_pending_upload(
        inner_record: PendingPublishRecord, **kwargs: Any
    ) -> dict[str, Any]:
        upload_calls.append(inner_record.publish_id)
        return {
            "series_id": inner_record.series_id,
            "release_id": inner_record.release_id,
        }

    async def fake_link_torrents(
        self: IndexationQueueService,
        job: IndexationJob,
    ) -> None:
        return None

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.ensure_index_ready",
        fake_ensure_index_ready,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.ensure_series_index_hydrated",
        fake_ensure_series_index_hydrated,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.AnimeLibraryService.update_anime",
        fake_update_anime,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.finalize_series_release",
        fake_finalize_series_release,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.run_pending_upload",
        fake_run_pending_upload,
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
    assert job.publish_id == "pub-merge"
    assert len(finalize_calls) == 1
    assert finalize_calls[0]["merge_existing_release"] is True
    assert finalize_calls[0]["expected_min_episodes"] == 22

    # The upload runs as a separate background job for the same publish.
    await _drain_job_tasks(service)
    assert upload_calls == ["pub-merge"]
    upload_jobs = [j for j in service.list_jobs() if j.job_type == "upload"]
    assert len(upload_jobs) == 1
    assert upload_jobs[0].publish_id == "pub-merge"
    assert upload_jobs[0].status == "complete"


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
async def test_enqueue_prunes_oldest_terminal_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job registry is process-lifetime: terminal jobs must be evicted
    beyond a bounded history or the dict grows for every job ever enqueued."""

    async def fake_run_job(self: IndexationQueueService, job: IndexationJob) -> None:
        return None

    monkeypatch.setattr(IndexationQueueService, "_run_job", fake_run_job)
    service = IndexationQueueService()
    limit = IndexationQueueService.MAX_TERMINAL_JOBS

    for i in range(limit + 5):
        job_id = await service.enqueue(
            source_path=f"/tmp/source-{i}",
            library_type=LibraryType.ANIME,
            anime_name=f"Series {i}",
            fps=2.0,
        )
        service._jobs[job_id].status = "complete"

    active_id = await service.enqueue(
        source_path="/tmp/source-active",
        library_type=LibraryType.ANIME,
        anime_name="Active Series",
        fps=2.0,
    )

    jobs = service.list_jobs()
    terminal = [j for j in jobs if j.status in {"complete", "error"}]
    assert len(terminal) <= limit
    names = {j.source_name for j in jobs}
    # Oldest terminal jobs are evicted, newest are kept.
    assert "Series 0" not in names
    assert f"Series {limit + 4}" in names
    # The still-active job is untouched.
    assert any(j.id == active_id for j in jobs)


@pytest.mark.asyncio
async def test_enqueue_keeps_strong_reference_to_job_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare asyncio.create_task results can be garbage-collected mid-flight;
    the queue must hold a strong reference until the job task finishes."""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_job(self: IndexationQueueService, job: IndexationJob) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(IndexationQueueService, "_run_job", fake_run_job)
    service = IndexationQueueService()
    await service.enqueue(
        source_path="/tmp/source-task",
        library_type=LibraryType.ANIME,
        anime_name="Task Series",
        fps=2.0,
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(service._job_tasks) == 1

    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(service._job_tasks) == 0


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


@pytest.mark.asyncio
async def test_publish_progress_spans_and_phase_flips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_store: Path,
) -> None:
    """Indexing owns 0→0.90 of the index job's bar and hashing 0.90→0.99;
    the index job completes at 1.0 as soon as the release is finalized
    locally. The upload runs as a separate job whose bar is driven purely by
    byte snapshots over 0→0.995, without ever touching the GPU budget."""
    import asyncio

    from app.services.storage_box_progress import ProgressSnapshot

    source_dir = tmp_path / "source"
    source_dir.mkdir()

    service = IndexationQueueService()
    job = IndexationJob(
        job_type="index",
        source_name="Sakamoto Days",
        library_type=LibraryType.ANIME,
        source_path=str(source_dir),
    )
    record = _make_record(tmp_path, publish_id="pub-spans")

    observed: dict[str, Any] = {}
    heavy_kinds: list[str] = []
    original_acquire = IndexationQueueService.acquire_heavy_slot

    async def tracking_acquire(
        self: IndexationQueueService, kind: str, slots: int = 1
    ) -> None:
        heavy_kinds.append(kind)
        await original_acquire(self, kind, slots=slots)

    async def fake_index_anime(**kwargs: Any):
        yield IndexProgress(
            status="indexing",
            progress=0.5,
            anime_name="Sakamoto Days",
        )
        observed["progress_mid_indexing"] = job.progress
        yield IndexProgress(
            status="complete",
            progress=1.0,
            anime_name="Sakamoto Days",
        )

    async def fake_finalize_series_release(**kwargs: Any) -> PendingPublishRecord:
        observed["phase_at_finalize"] = job.phase
        observed["progress_at_finalize"] = job.progress

        hashing = kwargs["hashing_progress_callback"]
        hashing(0, 100)
        hashing(50, 100)
        await asyncio.sleep(0)  # run the call_soon_threadsafe callbacks
        observed["progress_after_hashing"] = job.progress
        observed["phase_after_hashing"] = job.phase
        return record

    async def fake_run_pending_upload(
        inner_record: PendingPublishRecord, **kwargs: Any
    ) -> dict[str, Any]:
        # By the time the upload runs, the index job is already terminal
        # and the series is usable.
        observed["index_job_status_at_upload"] = job.status
        observed["index_job_progress_at_upload"] = job.progress

        upload_job = next(
            j for j in service.list_jobs() if j.job_type == "upload"
        )
        progress_callback = kwargs["progress_callback"]
        await progress_callback(
            ProgressSnapshot(
                bytes_transferred=500,
                bytes_total=1000,
                mib_per_sec=12.5,
                eta_seconds=40.0,
                active_transfers=2,
            )
        )
        observed["upload_progress_mid"] = upload_job.progress
        observed["upload_phase_mid"] = upload_job.phase
        observed["upload_network_mid"] = (
            upload_job.network_bytes_transferred,
            upload_job.network_bytes_total,
            upload_job.network_mib_per_sec,
        )
        return {
            "series_id": inner_record.series_id,
            "release_id": inner_record.release_id,
        }

    async def fake_link_torrents(
        self: IndexationQueueService,
        inner_job: IndexationJob,
    ) -> None:
        return None

    monkeypatch.setattr(
        "app.services.indexation_queue.AnimeLibraryService.index_anime",
        fake_index_anime,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.finalize_series_release",
        fake_finalize_series_release,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.run_pending_upload",
        fake_run_pending_upload,
    )
    monkeypatch.setattr(
        IndexationQueueService, "acquire_heavy_slot", tracking_acquire
    )
    monkeypatch.setattr(IndexationQueueService, "_link_torrents", fake_link_torrents)
    monkeypatch.setattr(
        "app.services.indexation_queue.AnimeMatcherService.mark_series_updated",
        lambda *args, **kwargs: None,
    )

    await service._run_job(job)
    await _drain_job_tasks(service)

    assert job.status == "complete"
    assert observed["progress_mid_indexing"] == pytest.approx(0.45)  # 0.5 * 0.90
    assert observed["phase_at_finalize"] == "package_release"
    assert observed["progress_at_finalize"] == pytest.approx(0.90)
    # Hashing now spans 0.90→0.99 (the upload no longer shares this bar).
    assert observed["progress_after_hashing"] == pytest.approx(0.945)
    assert observed["phase_after_hashing"] == "package_release"
    assert job.progress == 1.0
    assert job.publish_id == "pub-spans"
    assert job.network_bytes_transferred is None  # reset after completion

    # Series became available (index job terminal) BEFORE the upload ran.
    assert observed["index_job_status_at_upload"] == "complete"
    assert observed["index_job_progress_at_upload"] == 1.0

    # The upload job's bar is byte-driven over 0→0.995.
    assert observed["upload_progress_mid"] == pytest.approx(0.4975)
    assert observed["upload_phase_mid"] == "upload_release"
    assert observed["upload_network_mid"] == (500, 1000, 12.5)
    upload_job = next(j for j in service.list_jobs() if j.job_type == "upload")
    assert upload_job.status == "complete"
    assert upload_job.progress == 1.0
    # Uploads never touch the GPU heavy-slot budget.
    assert heavy_kinds == ["indexation"]


@pytest.mark.asyncio
async def test_upload_job_yields_to_active_indexation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_store: Path,
) -> None:
    """The idle gate defers uploads while any non-upload job is queued or
    running: uploads are strictly the lowest-priority work."""
    import asyncio

    monkeypatch.setattr(IndexationQueueService, "UPLOAD_IDLE_POLL_SECONDS", 0.01)
    service = IndexationQueueService()

    active_index_job = IndexationJob(
        job_type="index",
        source_name="Busy Series",
        library_type=LibraryType.ANIME,
        source_path=str(tmp_path),
        status="indexing",
    )
    service._jobs[active_index_job.id] = active_index_job

    upload_started = asyncio.Event()

    async def fake_run_pending_upload(
        inner_record: PendingPublishRecord, **kwargs: Any
    ) -> dict[str, Any]:
        upload_started.set()
        return {
            "series_id": inner_record.series_id,
            "release_id": inner_record.release_id,
        }

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.run_pending_upload",
        fake_run_pending_upload,
    )

    record = _make_record(tmp_path, publish_id="pub-gate")
    service.enqueue_upload(record)

    await asyncio.sleep(0.1)
    assert not upload_started.is_set(), "upload must wait while indexation runs"

    active_index_job.status = "complete"
    await asyncio.wait_for(upload_started.wait(), timeout=2.0)
    await _drain_job_tasks(service)


@pytest.mark.asyncio
async def test_upload_job_dedup_and_name_dedup_exclusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_store: Path,
) -> None:
    """enqueue_upload dedups by publish_id; a queued upload never absorbs a
    new index/update enqueue for the same series name."""
    import asyncio

    monkeypatch.setattr(IndexationQueueService, "UPLOAD_IDLE_POLL_SECONDS", 0.01)
    service = IndexationQueueService()

    release = asyncio.Event()

    async def fake_run_pending_upload(
        inner_record: PendingPublishRecord, **kwargs: Any
    ) -> dict[str, Any]:
        await release.wait()
        return {
            "series_id": inner_record.series_id,
            "release_id": inner_record.release_id,
        }

    async def fake_run_job(self: IndexationQueueService, job: IndexationJob) -> None:
        return None

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.run_pending_upload",
        fake_run_pending_upload,
    )
    monkeypatch.setattr(IndexationQueueService, "_run_job", fake_run_job)

    record = _make_record(tmp_path, publish_id="pub-dedup")
    upload_job_id = service.enqueue_upload(record)
    assert service.enqueue_upload(record) == upload_job_id

    index_job_id = await service.enqueue(
        source_path=str(tmp_path),
        library_type=LibraryType.ANIME,
        anime_name="Sakamoto Days",
        fps=2.0,
    )
    assert index_job_id != upload_job_id

    # Unblock the idle gate (the fake index job never runs) and the upload.
    service._jobs[index_job_id].status = "complete"
    release.set()
    await _drain_job_tasks(service)


@pytest.mark.asyncio
async def test_upload_job_retries_with_backoff_then_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_store: Path,
) -> None:
    import asyncio

    monkeypatch.setattr(IndexationQueueService, "UPLOAD_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        IndexationQueueService, "UPLOAD_RETRY_DELAYS", (0.01, 0.01)
    )
    service = IndexationQueueService()

    attempts: list[int] = []

    async def failing_run_pending_upload(
        inner_record: PendingPublishRecord, **kwargs: Any
    ) -> dict[str, Any]:
        attempts.append(1)
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.run_pending_upload",
        failing_run_pending_upload,
    )

    record = _make_record(tmp_path, publish_id="pub-retry")
    service.enqueue_upload(record)
    await _drain_job_tasks(service)

    # Initial attempt + one retry per configured delay.
    assert len(attempts) == 3
    upload_job = next(j for j in service.list_jobs() if j.job_type == "upload")
    assert upload_job.status == "error"
    assert "network down" in (upload_job.error or "")


@pytest.mark.asyncio
async def test_cancel_upload_settles_job_as_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_store: Path,
) -> None:
    import asyncio

    monkeypatch.setattr(IndexationQueueService, "UPLOAD_IDLE_POLL_SECONDS", 0.01)
    service = IndexationQueueService()

    started = asyncio.Event()

    async def hanging_run_pending_upload(
        inner_record: PendingPublishRecord, **kwargs: Any
    ) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.run_pending_upload",
        hanging_run_pending_upload,
    )

    record = _make_record(tmp_path, publish_id="pub-cancel")
    service.enqueue_upload(record)
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await service.cancel_upload("pub-cancel")

    upload_job = next(j for j in service.list_jobs() if j.job_type == "upload")
    assert upload_job.status == "error"
    assert upload_job.phase == "cancelled"
    assert service._upload_tasks == {}
