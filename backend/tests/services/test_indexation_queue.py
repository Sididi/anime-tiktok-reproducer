from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService, IndexProgress
from app.services.anime_matcher import AnimeMatcherService
from app.services.indexation_queue import IndexationQueueService
from app.services.library_hydration_service import LibraryHydrationService
from app.services.storage_box_repository import StorageBoxRepository


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _patch_publish_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    service: IndexationQueueService,
) -> None:
    monkeypatch.setattr(AnimeMatcherService, "mark_series_updated", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_link_torrents", lambda job: asyncio.sleep(0))
    monkeypatch.setattr(
        StorageBoxRepository,
        "publish_series",
        classmethod(
            lambda cls, library_type, display_name, series_id=None: _async_value(
                {
                    "series_id": series_id or f"series-{display_name.lower()}",
                    "release_id": f"release-{display_name.lower()}",
                }
            )
        ),
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "sync_local_series_state",
        classmethod(lambda cls, **kwargs: _async_value(None)),
    )


@pytest.mark.asyncio
async def test_enqueue_reuses_live_job_and_serializes_distinct_series_indexing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()
    started: dict[str, asyncio.Event] = {}
    release: dict[str, asyncio.Event] = {}

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        assert anime_name is not None
        started.setdefault(anime_name, asyncio.Event()).set()
        gate = release.setdefault(anime_name, asyncio.Event())
        await gate.wait()
        yield IndexProgress(status="complete", progress=1.0, message=f"done {anime_name}")

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    first_job_id = await service.enqueue("/tmp/demo-a", LibraryType.ANIME, "Demo", 2.0)
    duplicate_job_id = await service.enqueue("/tmp/demo-b", LibraryType.ANIME, " demo ", 2.0)
    other_job_id = await service.enqueue("/tmp/other", LibraryType.ANIME, "Other", 2.0)
    third_job_id = await service.enqueue("/tmp/third", LibraryType.ANIME, "Third", 2.0)

    assert duplicate_job_id == first_job_id
    assert {job.source_name for job in service.list_jobs()} == {"Demo", "Other", "Third"}

    await _wait_for(lambda: "Demo" in started)
    await asyncio.wait_for(started["Demo"].wait(), timeout=1.0)
    await _wait_for(lambda: "Other" in started)
    await asyncio.wait_for(started["Other"].wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert "Third" not in started

    release["Demo"].set()
    release["Other"].set()
    await _wait_for(lambda: "Third" in started)
    await asyncio.wait_for(started.setdefault("Third", asyncio.Event()).wait(), timeout=1.0)
    release["Third"].set()

    await _wait_for(
        lambda: {
            job.id: job.status
            for job in service.list_jobs()
        } == {
            first_job_id: "complete",
            other_job_id: "complete",
            third_job_id: "complete",
        }
    )


@pytest.mark.asyncio
async def test_enqueue_blocks_concurrent_index_and_update_for_same_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()
    gate = asyncio.Event()

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        await gate.wait()
        yield IndexProgress(status="complete", progress=1.0, message=f"done {anime_name}")

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    first_job_id = await service.enqueue(
        "/tmp/demo-a",
        LibraryType.ANIME,
        "Demo",
        2.0,
        job_type="index",
    )
    duplicate_job_id = await service.enqueue(
        "/tmp/demo-update",
        LibraryType.ANIME,
        "demo",
        2.0,
        job_type="update",
        series_id="series-demo",
    )

    assert duplicate_job_id == first_job_id

    gate.set()
    await _wait_for(lambda: service.list_jobs()[0].status == "complete")


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_queue_preserves_warnings_from_index_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        yield IndexProgress(
            status="copying",
            progress=0.1,
            message="skipping broken file",
            warnings=["Ignored unreadable source file: broken.mkv"],
        )
        yield IndexProgress(status="complete", progress=1.0, message=f"done {anime_name}")

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    job_id = await service.enqueue("/tmp/demo", LibraryType.ANIME, "Demo", 2.0)
    await _wait_for(lambda: service.list_jobs()[0].status == "complete")

    job = next(job for job in service.list_jobs() if job.id == job_id)
    assert job.warnings == ["Ignored unreadable source file: broken.mkv"]


@pytest.mark.asyncio
async def test_queue_preserves_current_file_progress_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()
    gate = asyncio.Event()

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        yield IndexProgress(
            status="indexing",
            progress=0.52,
            message="Processing Demo/ep1.mp4 (batch 3, frames 48)",
            current_file="Demo/ep1.mp4",
            total_files=4,
            completed_files=1,
            current_file_progress=0.4,
            current_file_frames_processed=48,
            current_file_total_frames=120,
            current_file_batches_processed=3,
        )
        await gate.wait()
        yield IndexProgress(status="complete", progress=1.0, message=f"done {anime_name}")

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    job_id = await service.enqueue("/tmp/demo", LibraryType.ANIME, "Demo", 2.0)
    await _wait_for(lambda: service.list_jobs()[0].current_file == "Demo/ep1.mp4")

    job = next(job for job in service.list_jobs() if job.id == job_id)
    assert job.current_file == "Demo/ep1.mp4"
    assert job.total_files == 4
    assert job.completed_files == 1
    assert job.current_file_progress == pytest.approx(0.4)
    assert job.current_file_frames_processed == 48
    assert job.current_file_total_frames == 120
    assert job.current_file_batches_processed == 3

    gate.set()
    await _wait_for(lambda: service.list_jobs()[0].status == "complete")


@pytest.mark.asyncio
async def test_failed_job_releases_slot_while_parallel_job_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()
    started: dict[str, asyncio.Event] = {}
    release: dict[str, asyncio.Event] = {}
    crash_now = asyncio.Event()

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        assert anime_name is not None
        started.setdefault(anime_name, asyncio.Event()).set()
        if anime_name == "Crash":
            await crash_now.wait()
            yield IndexProgress(status="error", error="boom")
            return

        gate = release.setdefault(anime_name, asyncio.Event())
        await gate.wait()
        yield IndexProgress(status="complete", progress=1.0, message=f"done {anime_name}")

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    crash_job_id = await service.enqueue("/tmp/crash", LibraryType.ANIME, "Crash", 2.0)
    steady_job_id = await service.enqueue("/tmp/steady", LibraryType.ANIME, "Steady", 2.0)
    queued_job_id = await service.enqueue("/tmp/queued", LibraryType.ANIME, "Queued", 2.0)

    await _wait_for(lambda: "Crash" in started and "Steady" in started)
    await asyncio.wait_for(started["Crash"].wait(), timeout=1.0)
    await asyncio.wait_for(started["Steady"].wait(), timeout=1.0)
    assert "Queued" not in started

    crash_now.set()
    await _wait_for(
        lambda: any(job.id == crash_job_id and job.status == "error" for job in service.list_jobs())
    )
    await _wait_for(lambda: "Queued" in started)
    await asyncio.wait_for(started["Queued"].wait(), timeout=1.0)

    release["Steady"].set()
    release["Queued"].set()
    await _wait_for(
        lambda: {
            job.id: job.status
            for job in service.list_jobs()
        } == {
            crash_job_id: "error",
            steady_job_id: "complete",
            queued_job_id: "complete",
        }
    )


@pytest.mark.asyncio
async def test_queue_preserves_service_level_cuda_retry_metadata_without_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()
    attempts = 0

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        nonlocal attempts
        attempts += 1
        yield IndexProgress(
            status="indexing",
            progress=0.55,
            message="CUDA OOM detected under the current parallel load; retrying with reduced batch size (64 -> 32) while keeping fp32.",
            requested_batch_size=64,
            effective_batch_size=32,
            effective_decode_backend="ffmpeg_cuda",
            retry_reason=AnimeLibraryService.SEARCHER_CUDA_OOM_RETRY_REASON,
            warnings=[
                "CUDA OOM detected under the current parallel load; retrying with reduced batch size (64 -> 32) while keeping fp32."
            ],
        )
        yield IndexProgress(
            status="complete",
            progress=1.0,
            message=f"done {anime_name}",
            requested_batch_size=64,
            effective_batch_size=32,
            effective_decode_backend="ffmpeg_cuda",
            retry_reason=AnimeLibraryService.SEARCHER_CUDA_OOM_RETRY_REASON,
            warnings=[
                "CUDA OOM detected under the current parallel load; retrying with reduced batch size (64 -> 32) while keeping fp32."
            ],
        )

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    job_id = await service.enqueue("/tmp/retry", LibraryType.ANIME, "Retry", 2.0)

    await _wait_for(
        lambda: any(
            job.id == job_id and job.status == "complete"
            for job in service.list_jobs()
        )
    )
    job = next(job for job in service.list_jobs() if job.id == job_id)
    assert attempts == 1
    assert job.requested_batch_size == 64
    assert job.effective_batch_size == 32
    assert job.effective_decode_backend == "ffmpeg_cuda"
    assert job.retry_reason == AnimeLibraryService.SEARCHER_CUDA_OOM_RETRY_REASON
    assert any(
        "reduced batch size (64 -> 32)" in warning
        for warning in job.warnings
    )


@pytest.mark.asyncio
async def test_cuda_oom_job_becomes_terminal_without_queue_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = IndexationQueueService()
    attempts = 0

    async def fake_index_anime(
        *,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        **_: object,
    ):
        nonlocal attempts
        attempts += 1
        yield IndexProgress(
            status="error",
            error="CUDA out of memory. Tried to allocate 1.48 GiB.",
        )

    monkeypatch.setattr(
        AnimeLibraryService,
        "index_anime",
        classmethod(lambda cls, **kwargs: fake_index_anime(**kwargs)),
    )
    _patch_publish_dependencies(monkeypatch, service)

    job_id = await service.enqueue("/tmp/retry", LibraryType.ANIME, "Retry", 2.0)

    await _wait_for(
        lambda: any(
            job.id == job_id and job.status == "error"
            for job in service.list_jobs()
        )
    )
    job = next(job for job in service.list_jobs() if job.id == job_id)
    assert attempts == 1
    assert job.phase == "error"
    assert "exceeded available VRAM under the current parallel load" in str(job.error)
