from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api.routes.anime import _validate_batch_folders_sync
from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService, SourceVideoScan
from app.services.storage_box_repository import StorageBoxRepository


def _write_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")


def _scan_result(
    *,
    readable: list[Path] | tuple[Path, ...] = (),
    invalid: list[Path] | tuple[Path, ...] = (),
) -> SourceVideoScan:
    return SourceVideoScan(
        readable_files=tuple(readable),
        invalid_files=tuple(invalid),
    )


def test_validate_batch_folders_treats_remote_series_as_exact_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Demo"

    _write_video(source_dir / "ep1.mkv")
    _write_video(source_dir / "ep2.mkv")

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: _scan_result(
                readable=[source_dir / "ep1.mkv", source_dir / "ep2.mkv"],
            )
        ),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_catalog_entry_by_name",
        classmethod(lambda cls, library_type, display_name: _async_result({"series_id": "series-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_current_release",
        classmethod(lambda cls, library_type, series_id: _async_result({"release_id": "release-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_series_manifest",
        classmethod(
            lambda cls, library_type, series_id, release_id=None: _async_result(
                {
                    "series_id": "series-1",
                    "release_id": "release-1",
                    "episode_count": 2,
                    "torrent_count": 0,
                    "episodes": [
                        {"episode_key": "ep1"},
                        {"episode_key": "ep2"},
                    ],
                }
            )
        ),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["has_videos"] is True
    assert results[0]["resolution"] == "exact_match"
    assert results[0]["series_id"] == "series-1"
    assert results[0]["storage_release_id"] == "release-1"
    assert results[0]["conflict_details"] is None


def test_validate_batch_folders_reports_remote_update_required_by_stem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Demo"

    _write_video(source_dir / "ep1.mkv")
    _write_video(source_dir / "ep3.mkv")

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: _scan_result(
                readable=[source_dir / "ep1.mkv", source_dir / "ep3.mkv"],
            )
        ),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_catalog_entry_by_name",
        classmethod(lambda cls, library_type, display_name: _async_result({"series_id": "series-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_current_release",
        classmethod(lambda cls, library_type, series_id: _async_result({"release_id": "release-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_series_manifest",
        classmethod(
            lambda cls, library_type, series_id, release_id=None: _async_result(
                {
                    "series_id": "series-1",
                    "release_id": "release-1",
                    "episode_count": 2,
                    "torrent_count": 2,
                    "episodes": [
                        {"episode_key": "ep1"},
                        {"episode_key": "ep2"},
                    ],
                }
            )
        ),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["resolution"] == "update_required"
    assert results[0]["conflict_details"] == {
        "new_episodes": ["ep3"],
        "removed_episodes": ["ep2"],
        "existing_episode_count": 2,
        "existing_torrent_count": 2,
    }


def test_validate_batch_folders_preserves_suggested_path_when_no_direct_videos(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Nested"
    nested_video_dir = source_dir / "Season 1"

    _write_video(nested_video_dir / "ep1.mkv")

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(lambda cls, folder: _scan_result()),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["has_videos"] is False
    assert results[0]["suggested_path"] == str(nested_video_dir)
    assert results[0]["resolution"] == "needs_fix"


def test_validate_batch_folders_blocks_orphan_local_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Demo"
    orphan_dir = library_path / "Demo"

    _write_video(source_dir / "ep1.mkv")
    _write_video(orphan_dir / "ep1.mp4")

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: _scan_result(readable=[source_dir / "ep1.mkv"])
        ),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_catalog_entry_by_name",
        classmethod(lambda cls, library_type, display_name: _async_result({"series_id": "series-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_current_release",
        classmethod(lambda cls, library_type, series_id: _async_result({"release_id": "release-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_series_manifest",
        classmethod(
            lambda cls, library_type, series_id, release_id=None: _async_result(
                {
                    "series_id": "series-1",
                    "release_id": "release-1",
                    "episode_count": 1,
                    "torrent_count": 0,
                    "episodes": [{"episode_key": "ep1"}],
                }
            )
        ),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["resolution"] == "blocked_orphan"
    assert "orphelin" in str(results[0]["orphan_reason"]).lower()


def test_validate_batch_folders_ignores_invalid_duplicates_when_readable_files_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Demo"

    readable_episode = source_dir / "ep1.mkv"
    invalid_duplicate = source_dir / "ep1-clean.mkv"
    _write_video(readable_episode)
    _write_video(invalid_duplicate)

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: _scan_result(
                readable=[readable_episode],
                invalid=[invalid_duplicate],
            )
        ),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "find_catalog_entry_by_name",
        classmethod(lambda cls, library_type, display_name: _async_result({"series_id": "series-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_current_release",
        classmethod(lambda cls, library_type, series_id: _async_result({"release_id": "release-1"})),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "get_series_manifest",
        classmethod(
            lambda cls, library_type, series_id, release_id=None: _async_result(
                {
                    "series_id": "series-1",
                    "release_id": "release-1",
                    "episode_count": 1,
                    "torrent_count": 0,
                    "episodes": [{"episode_key": "ep1"}],
                }
            )
        ),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["resolution"] == "exact_match"
    assert results[0]["invalid_video_files"] == ["ep1-clean.mkv"]


def test_validate_batch_folders_reports_invalid_direct_videos_when_all_are_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Broken"

    invalid_a = source_dir / "ep1.mkv"
    invalid_b = source_dir / "ep2.mp4"
    _write_video(invalid_a)
    _write_video(invalid_b)

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: _scan_result(
                invalid=[invalid_a, invalid_b],
            )
        ),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["has_videos"] is True
    assert results[0]["resolution"] == "needs_fix"
    assert results[0]["invalid_video_files"] == ["ep1.mkv", "ep2.mp4"]


async def _async_result(value):
    return value
