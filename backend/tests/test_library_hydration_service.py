from __future__ import annotations

import json
import sys
from pathlib import Path

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
    _write_json(shard_dir / "metadata.json", {})
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
