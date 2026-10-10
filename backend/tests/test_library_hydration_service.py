from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.anime_library import AnimeLibraryService
from app.services.library_hydration_service import (
    HYDRATION_STATUS_HYDRATING_EPISODES,
    LibraryHydrationService,
)
from app.services.library_state_db import LibraryStateDb
from app.services.storage_box_repository import StorageBoxRepository


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_hydrating_episodes_state_keeps_matcher_index_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    series_id = "series-1"
    release_id = "release-1"
    display_name = "Astro Note"

    monkeypatch.setattr(settings, "anime_library_path", tmp_path / "library")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    LibraryStateDb.initialize()

    manifest = {
        "series_id": series_id,
        "release_id": release_id,
        "display_name": display_name,
        "episode_count": 12,
        "episodes": [],
    }
    await LibraryHydrationService._cache_manifest("anime", manifest)

    library_path = AnimeLibraryService.get_library_path("anime")
    series_dir = library_path / display_name
    series_dir.mkdir(parents=True, exist_ok=True)
    StorageBoxRepository.write_local_series_metadata(
        series_dir=series_dir,
        series_id=series_id,
        display_name=display_name,
        release_id=release_id,
    )

    index_dir = library_path / AnimeLibraryService.INDEX_DIR_NAME
    shard_dir = index_dir / "series" / "astro-note"
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "faiss.index").write_bytes(b"index")
    _write_json(shard_dir / "metadata.json", {"frames": [{"episode": "Episode 01"}]})
    _write_json(
        index_dir / AnimeLibraryService.MANIFEST_FILE,
        {
            "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
            "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
            "series": {display_name: {"key": "astro-note"}},
        },
    )
    _write_json(index_dir / AnimeLibraryService.STATE_FILE, {"files": {}})

    LibraryStateDb.upsert_series_state(
        library_type="anime",
        series_id=series_id,
        release_id=release_id,
        hydration_status=HYDRATION_STATUS_HYDRATING_EPISODES,
        local_episode_count=0,
        expected_episode_count=12,
    )

    assert await LibraryHydrationService.ensure_index_ready(
        library_type="anime",
        series_id=series_id,
    )


@pytest.fixture
def partial_index(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anime_library_path", tmp_path / "library")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(settings, "library_state_db_path", tmp_path / "library_state.db")
    LibraryStateDb.initialize()
    manifest = {
        "series_id": "series-1", "release_id": "release-1", "display_name": "Series",
        "episode_count": 3,
        "episodes": [{"episode_key": f"Episode {number:02}"} for number in (4, 6, 8)],
    }
    library = AnimeLibraryService.get_library_path("anime")
    series_dir = library / "Series"
    series_dir.mkdir(parents=True)
    StorageBoxRepository.write_local_series_metadata(
        series_dir=series_dir, series_id="series-1", display_name="Series", release_id="release-1",
    )
    index = library / ".index"
    shard = index / "series" / "shard"
    shard.mkdir(parents=True)
    (shard / "faiss.index").write_bytes(b"local-index")
    _write_json(shard / "metadata.json", {"frames": [{"episode": "Episode 04"}]})
    _write_json(index / "manifest.json", {
        "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
        "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
        "series": {"Series": {"key": "shard"}},
    })
    _write_json(index / "state.json", {"files": {}})
    LibraryStateDb.upsert_series_state(
        library_type="anime", series_id="series-1", release_id="release-1",
        hydration_status="index_ready", local_episode_count=1, expected_episode_count=3,
    )
    return manifest, shard


@pytest.mark.asyncio
async def test_partial_index_triggers_hydration_before_matching(monkeypatch, partial_index):
    manifest, shard = partial_index
    await LibraryHydrationService._cache_manifest("anime", manifest)
    assert not await LibraryHydrationService.ensure_index_ready(library_type="anime", series_id="series-1")
    monkeypatch.setattr(LibraryHydrationService, "_resolve_release_manifest", AsyncMock(return_value=(manifest, None)))

    async def download(*args, **kwargs):
        # No episode videos are needed to make their index searchable.
        _write_json(shard / "metadata.json", {"frames": [
            {"episode": f"Series/Episode {number:02}.mp4"} for number in (4, 6, 8)
        ]})

    hydrate = AsyncMock(side_effect=download)
    monkeypatch.setattr(LibraryHydrationService, "_hydrate_index_artifacts", hydrate)
    await LibraryHydrationService.ensure_series_index_hydrated(library_type="anime", series_id="series-1")
    hydrate.assert_awaited_once()
    assert await LibraryHydrationService.ensure_index_ready(library_type="anime", series_id="series-1")
    # Complete index coverage reuses the cache, even without local videos.
    await LibraryHydrationService.ensure_series_index_hydrated(library_type="anime", series_id="series-1")
    hydrate.assert_awaited_once()


@pytest.mark.parametrize("payload", [{}, {"frames": []}, {"frames": [{"episode": "Episode 04"}]}])
def test_missing_index_coverage_is_not_ready(partial_index, payload):
    manifest, shard = partial_index
    _write_json(shard / "metadata.json", payload)
    assert not LibraryHydrationService._local_index_ready_for_manifest_sync("anime", manifest)


@pytest.mark.parametrize("invalid", ["coverage", "version"])
def test_invalid_remote_index_does_not_replace_local_index(partial_index, tmp_path, invalid):
    manifest, shard = partial_index
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    fragment = tmp_path / "download" / "series-1"
    _write_json(fragment / "manifest.fragment.json", {
        "version": 0 if invalid == "version" else AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
        "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
        "series": {"Series": {"key": "shard"}},
    })
    _write_json(fragment / "state.fragment.json", {"files": {}})
    _write_json(fragment / "series/shard/metadata.json", {"frames": [{"episode": "Episode 04"}]})
    (fragment / "series/shard/faiss.index").write_bytes(b"remote-index")
    with pytest.raises(RuntimeError, match="Re-index the series"):
        LibraryHydrationService._materialize_local_matcher_cache_sync("anime", manifest, fragment.parent)
    assert {p.name: p.read_bytes() for p in shard.iterdir()} == before


def test_complete_remote_index_replaces_partial_local_shard(partial_index, tmp_path):
    manifest, shard = partial_index
    fragment = tmp_path / "download" / "series-1"
    _write_json(fragment / "manifest.fragment.json", {
        "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
        "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
        "series": {"Series": {"key": "shard"}},
    })
    _write_json(fragment / "state.fragment.json", {"files": {}})
    _write_json(fragment / "series/shard/metadata.json", {"frames": [
        {"episode": f"Episode {number:02}"} for number in (4, 6, 8)
    ]})
    (fragment / "series/shard/faiss.index").write_bytes(b"complete-index")
    LibraryHydrationService._materialize_local_matcher_cache_sync("anime", manifest, fragment.parent)
    assert (shard / "faiss.index").read_bytes() == b"complete-index"
    assert LibraryHydrationService._local_index_ready_for_manifest_sync("anime", manifest)


@pytest.mark.asyncio
async def test_completed_activation_with_incomplete_index_fails_instead_of_polling(monkeypatch):
    monkeypatch.setattr(LibraryHydrationService, "ensure_index_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(LibraryHydrationService, "enqueue_project_activation", AsyncMock(return_value={
        "operation": {"status": "complete"},
    }))
    with pytest.raises(RuntimeError, match="matcher index is incomplete"):
        await LibraryHydrationService.ensure_matcher_ready_for_project(
            project_id="project-1", library_type="anime", series_id="series-1",
        )


@pytest.mark.asyncio
async def test_list_source_details_skips_storage_box_for_pure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure has no series tree: it must never reach for the remote catalog.

    The Storage Box has no ``pure`` root, so a probe costs two failing SFTP
    round-trips (catalog read, then the rebuild fallback) and logs a
    traceback on every Project Setup render in pure mode.
    """

    async def _fail(*args: object, **kwargs: object) -> list[dict]:
        raise AssertionError("pure must not touch the Storage Box catalog")

    monkeypatch.setattr(StorageBoxRepository, "list_catalog", _fail)

    assert await LibraryHydrationService.list_source_details(library_type="pure") == []
