from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.anime_library import AnimeLibraryService
from app.services.indexation_queue import IndexationQueueService
from app.services.library_hydration_service import (
    HYDRATION_STATUS_FULLY_LOCAL,
    LibraryHydrationService,
)
from app.services.library_state_db import LibraryStateDb
from app.services.pending_publish_store import (
    PendingPublishRecord,
    PendingPublishStore,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "anime_library_path", tmp_path / "library")
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    LibraryStateDb.initialize()
    return tmp_path


def _make_finalized_series(
    tmp_path: Path,
    *,
    publish_id: str = "pub-1",
    series_id: str = "series-1",
    display_name: str = "Astro Note",
    shard_key: str = "astro-note",
    write_episode: bool = True,
) -> PendingPublishRecord:
    """Build the on-disk state prepare_series_release leaves behind."""
    library_path = AnimeLibraryService.get_library_path("anime")
    series_dir = library_path / display_name
    series_dir.mkdir(parents=True, exist_ok=True)
    episode_name = f"{display_name} - 01.mkv"
    if write_episode:
        (series_dir / episode_name).write_bytes(b"video")

    index_dir = library_path / AnimeLibraryService.INDEX_DIR_NAME
    shard_dir = index_dir / "series" / shard_key
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "faiss.index").write_bytes(b"index")
    _write_json(shard_dir / "metadata.json", {})
    _write_json(
        index_dir / AnimeLibraryService.MANIFEST_FILE,
        {
            "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
            "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
            "series": {display_name: {"key": shard_key}},
        },
    )
    _write_json(index_dir / AnimeLibraryService.STATE_FILE, {"files": {}})

    staged_dir = PendingPublishStore.staging_dir_root() / publish_id
    staged_dir.mkdir(parents=True, exist_ok=True)
    (staged_dir / "manifest.fragment.json").write_text("{}", encoding="utf-8")

    release_id = f"release-{publish_id}"
    return PendingPublishRecord(
        publish_id=publish_id,
        library_type="anime",
        series_id=series_id,
        release_id=release_id,
        display_name=display_name,
        series_dir=str(series_dir),
        staged_index_dir=str(staged_dir),
        is_brand_new_series=True,
        manifest={
            "series_id": series_id,
            "release_id": release_id,
            "display_name": display_name,
            "episode_count": 1,
            "total_size_bytes": 5,
            "fps": 23.976,
            "torrent_count": 0,
            "episodes": [
                {
                    "episode_key": f"{display_name} - 01",
                    "media": {
                        "relative_path": f"payload/library/{display_name}/{episode_name}",
                        "local_relative_path": f"{display_name}/{episode_name}",
                        "size_bytes": 5,
                        "sha256": "abc",
                    },
                    "sidecars": [],
                }
            ],
            "artifacts": [],
        },
    )


@pytest.mark.asyncio
async def test_apply_local_publish_state_makes_series_matchable(env: Path) -> None:
    """The heart of parallel upload: after finalize (no remote upload at
    all), the purely-local matching gate must pass — and re-applying the
    state (startup resume healing a crash) must be idempotent."""
    record = _make_finalized_series(env)
    PendingPublishStore.save(record)

    await LibraryHydrationService.apply_local_publish_state(record)

    assert await LibraryHydrationService.ensure_index_ready(
        library_type="anime",
        series_id=record.series_id,
    )
    state = LibraryStateDb.get_series_state("anime", record.series_id)
    assert state is not None
    assert state.release_id == record.release_id
    assert state.hydration_status == HYDRATION_STATUS_FULLY_LOCAL
    assert state.local_episode_count == 1

    # Idempotent re-run (what resume_pending_uploads does after a crash).
    await LibraryHydrationService.apply_local_publish_state(record)
    assert await LibraryHydrationService.ensure_index_ready(
        library_type="anime",
        series_id=record.series_id,
    )


@pytest.mark.asyncio
async def test_resume_pending_uploads_heals_state_and_enqueues(
    monkeypatch: pytest.MonkeyPatch,
    env: Path,
) -> None:
    record = _make_finalized_series(env, publish_id="pub-resume")
    PendingPublishStore.save(record)

    applied: list[str] = []
    enqueued: list[str] = []

    async def fake_apply(record_arg: PendingPublishRecord) -> None:
        applied.append(record_arg.publish_id)

    monkeypatch.setattr(
        "app.services.indexation_queue.LibraryHydrationService.apply_local_publish_state",
        fake_apply,
    )
    monkeypatch.setattr(
        "app.services.indexation_queue.StorageBoxRepository.is_enabled",
        lambda: True,
    )

    service = IndexationQueueService()
    monkeypatch.setattr(
        service,
        "enqueue_upload",
        lambda rec: enqueued.append(rec.publish_id) or "job-id",
    )

    await service.resume_pending_uploads()

    assert applied == ["pub-resume"]
    assert enqueued == ["pub-resume"]


@pytest.mark.asyncio
async def test_startup_cleanup_reconciles_pending_publishes(
    monkeypatch: pytest.MonkeyPatch,
    env: Path,
) -> None:
    # Record with everything in place → untouched.
    keep = _make_finalized_series(env, publish_id="pub-keep", series_id="series-keep")
    PendingPublishStore.save(keep)

    # Record whose local series dir vanished → dropped entirely.
    gone = _make_finalized_series(
        env,
        publish_id="pub-gone",
        series_id="series-gone",
        display_name="Deleted Series",
        shard_key="deleted-series",
    )
    PendingPublishStore.save(gone)
    import shutil

    shutil.rmtree(gone.series_dir)

    # Record whose staged dir vanished (cache wiped) → kept, flagged.
    wiped = _make_finalized_series(
        env,
        publish_id="pub-wiped",
        series_id="series-wiped",
        display_name="Wiped Cache",
        shard_key="wiped-cache",
    )
    PendingPublishStore.save(wiped)
    shutil.rmtree(wiped.staged_index_dir)

    # Staged dir without any record → swept.
    orphan_staged = PendingPublishStore.staging_dir_root() / "pub-orphan"
    orphan_staged.mkdir(parents=True, exist_ok=True)
    (orphan_staged / "junk.bin").write_bytes(b"junk")

    await LibraryHydrationService.startup_cleanup()

    remaining = {record.publish_id for record in PendingPublishStore.list_all()}
    assert remaining == {"pub-keep", "pub-wiped"}
    assert Path(keep.staged_index_dir).exists()
    assert not orphan_staged.exists()
    wiped_after = PendingPublishStore.load("pub-wiped")
    assert wiped_after is not None
    assert "Re-index" in (wiped_after.last_error or "")


@pytest.mark.asyncio
async def test_list_source_details_overlays_pending_series(
    monkeypatch: pytest.MonkeyPatch,
    env: Path,
) -> None:
    """A finalized-but-not-uploaded series is listed from its pending record,
    even when the Storage Box catalog is unreachable."""
    record = _make_finalized_series(env, publish_id="pub-overlay")
    PendingPublishStore.save(record)
    await LibraryHydrationService.apply_local_publish_state(record)

    async def failing_list_catalog(*args: Any, **kwargs: Any) -> list[dict]:
        raise RuntimeError("storage box unreachable")

    monkeypatch.setattr(
        "app.services.library_hydration_service.StorageBoxRepository.list_catalog",
        failing_list_catalog,
    )

    details = await LibraryHydrationService.list_source_details(library_type="anime")

    assert len(details) == 1
    row = details[0]
    assert row["name"] == "Astro Note"
    assert row["series_id"] == record.series_id
    assert row["storage_release_id"] == record.release_id
    assert row["pending_upload"] is True
    assert row["episode_count"] == 1
    assert row["is_fully_local"] is True
    assert row["fps"] == pytest.approx(23.976)


@pytest.mark.asyncio
async def test_list_source_details_marks_catalog_series_with_pending_update(
    monkeypatch: pytest.MonkeyPatch,
    env: Path,
) -> None:
    """Mid-update: the catalog still advertises the old release while the new
    one is pending — the row must surface the pending release and NOT be
    re-synced against the stale catalog release."""
    record = _make_finalized_series(env, publish_id="pub-midupdate")
    PendingPublishStore.save(record)
    await LibraryHydrationService.apply_local_publish_state(record)

    async def fake_list_catalog(*args: Any, **kwargs: Any) -> list[dict]:
        return [
            {
                "series_id": record.series_id,
                "name": record.display_name,
                "storage_release_id": "release-old",
                "episode_count": 1,
                "total_size_bytes": 3,
                "fps": 23.976,
                "torrent_count": 0,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    sync_calls: list[Any] = []

    async def tracking_sync(*args: Any, **kwargs: Any):
        sync_calls.append(kwargs)
        raise AssertionError("sync_local_series_state must not run for pending series")

    monkeypatch.setattr(
        "app.services.library_hydration_service.StorageBoxRepository.list_catalog",
        fake_list_catalog,
    )
    monkeypatch.setattr(
        LibraryHydrationService,
        "sync_local_series_state",
        tracking_sync,
    )

    details = await LibraryHydrationService.list_source_details(library_type="anime")

    assert len(details) == 1
    row = details[0]
    assert row["pending_upload"] is True
    assert row["storage_release_id"] == record.release_id
    assert sync_calls == []
