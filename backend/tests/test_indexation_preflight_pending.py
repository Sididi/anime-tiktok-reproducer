from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService
from app.services.indexation_preflight import (
    RESOLUTION_EXACT_MATCH,
    RESOLUTION_UPDATE_REQUIRED,
    IndexationPreflightService,
)
from app.services.pending_publish_store import (
    PendingPublishRecord,
    PendingPublishStore,
)
from app.services.storage_box_repository import StorageBoxRepository


def _setup_pending_series(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    display_name: str = "Astro Note",
    series_id: str = "series-1",
    episode_keys: list[str] | None = None,
) -> PendingPublishRecord:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")

    library_root = tmp_path / "library"
    local_dir = library_root / display_name
    local_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )
    StorageBoxRepository.write_local_series_metadata(
        series_dir=local_dir,
        series_id=series_id,
        display_name=display_name,
        release_id="release-pending",
    )

    keys = episode_keys or [f"{display_name} - 01"]
    record = PendingPublishRecord(
        publish_id="pub-preflight",
        library_type="anime",
        series_id=series_id,
        release_id="release-pending",
        display_name=display_name,
        series_dir=str(local_dir),
        staged_index_dir=str(tmp_path / "cache" / "staged"),
        is_brand_new_series=True,
        manifest={
            "series_id": series_id,
            "release_id": "release-pending",
            "display_name": display_name,
            "episode_count": len(keys),
            "torrent_count": 0,
            "episodes": [{"episode_key": key} for key in keys],
        },
    )
    PendingPublishStore.save(record)

    # Remote resolution must never run for a pending series: it would either
    # find nothing (blocking as orphan) or, worse, a stale release.
    async def forbidden_remote(*args: Any, **kwargs: Any):
        raise AssertionError("remote resolution must not run for pending series")

    monkeypatch.setattr(
        IndexationPreflightService, "_resolve_remote_series", forbidden_remote
    )
    return record


def _fake_scan(monkeypatch: pytest.MonkeyPatch, files: list[Path]) -> None:
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: type(
                "Scan",
                (),
                {
                    "readable_files": tuple(files),
                    "invalid_files": (),
                    "has_direct_videos": bool(files),
                },
            )()
        ),
    )


@pytest.mark.asyncio
async def test_pending_series_preflights_as_exact_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A finalized-but-not-uploaded series must not be a blocked orphan: the
    pending record resolves it locally, same episodes → exact match."""
    record = _setup_pending_series(monkeypatch, tmp_path)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    episode = source_dir / "Astro Note - 01.mkv"
    episode.write_bytes(b"video")
    _fake_scan(monkeypatch, [episode])

    result = await IndexationPreflightService.preflight_source(
        source_path=source_dir,
        library_type=LibraryType.ANIME,
        anime_name="Astro Note",
    )

    assert result["resolution"] == RESOLUTION_EXACT_MATCH
    assert result["series_id"] == record.series_id
    assert result["storage_release_id"] == record.release_id
    assert result["orphan_reason"] is None


@pytest.mark.asyncio
async def test_pending_series_preflights_as_update_when_source_has_new_episode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _setup_pending_series(monkeypatch, tmp_path)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    files = [
        source_dir / "Astro Note - 01.mkv",
        source_dir / "Astro Note - 02.mkv",
    ]
    for file in files:
        file.write_bytes(b"video")
    _fake_scan(monkeypatch, files)

    result = await IndexationPreflightService.preflight_source(
        source_path=source_dir,
        library_type=LibraryType.ANIME,
        anime_name="Astro Note",
    )

    assert result["resolution"] == RESOLUTION_UPDATE_REQUIRED
    assert result["conflict_details"]["new_episodes"] == ["Astro Note - 02"]
    assert result["conflict_details"]["removed_episodes"] == []
