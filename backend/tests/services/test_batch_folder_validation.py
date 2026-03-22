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
from app.services.anime_library import AnimeLibraryService


def _write_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")


def test_validate_batch_folders_treats_remuxed_files_as_exact_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Demo"
    indexed_dir = library_path / "Demo"

    _write_video(source_dir / "ep1.mkv")
    _write_video(source_dir / "ep2.mkv")
    _write_video(indexed_dir / "ep1.mp4")
    _write_video(indexed_dir / "ep2.mp4")

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["has_videos"] is True
    assert results[0]["index_status"] == "exact_match"
    assert results[0]["conflict_details"] is None


def test_validate_batch_folders_reports_conflicts_by_stem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    source_dir = tmp_path / "incoming" / "Demo"
    indexed_dir = library_path / "Demo"

    _write_video(source_dir / "ep1.mkv")
    _write_video(source_dir / "ep3.mkv")
    _write_video(indexed_dir / "ep1.mp4")
    _write_video(indexed_dir / "ep2.mp4")
    (indexed_dir / ".atr_torrents.json").write_text(
        json.dumps({"torrents": [{"hash": "a"}, {"hash": "b"}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["index_status"] == "conflict"
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

    results = _validate_batch_folders_sync([str(source_dir)], LibraryType.ANIME)

    assert len(results) == 1
    assert results[0]["has_videos"] is False
    assert results[0]["suggested_path"] == str(nested_video_dir)
    assert results[0]["index_status"] == "new"
