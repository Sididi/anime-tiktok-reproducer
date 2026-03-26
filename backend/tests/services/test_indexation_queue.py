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
