from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.storage_box_repository as repo_module
from app.config import settings
from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService
from app.services.storage_box_repository import (
    HASH_CACHE_FILENAME,
    StorageBoxRepository,
    _sha256_file,
)


def _scan(series_dir: Path) -> list[tuple[Path, Path, int, int]]:
    files = []
    for path in sorted(series_dir.rglob("*")):
        if not path.is_file() or path.name.startswith(".atr_hash_cache"):
            continue
        stat = path.stat()
        files.append(
            (path, path.relative_to(series_dir), stat.st_size, stat.st_mtime_ns)
        )
    return files


@pytest.fixture
def series_dir(tmp_path: Path) -> Path:
    series = tmp_path / "Series Name"
    series.mkdir()
    (series / "Episode 01.mkv").write_bytes(b"episode-one-bytes")
    (series / "Episode 02.mkv").write_bytes(b"episode-two-bytes!")
    (series / ".atr_torrents.json").write_text("{}", encoding="utf-8")
    return series


@pytest.fixture
def counting_sha(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    hashed: list[Path] = []

    def _counting(path: Path, *, on_bytes=None) -> str:
        hashed.append(path)
        return _sha256_file(path, on_bytes=on_bytes)

    monkeypatch.setattr(repo_module, "_sha256_file", _counting)
    return hashed


def test_miss_then_hit(series_dir: Path, counting_sha: list[Path]) -> None:
    files = _scan(series_dir)
    first = StorageBoxRepository._resolve_series_hashes(
        series_dir=series_dir,
        series_files=files,
        index_files=[],
        hashing_progress_callback=None,
    )
    assert len(counting_sha) == 3
    assert (series_dir / HASH_CACHE_FILENAME).is_file()

    counting_sha.clear()
    second = StorageBoxRepository._resolve_series_hashes(
        series_dir=series_dir,
        series_files=_scan(series_dir),
        index_files=[],
        hashing_progress_callback=None,
    )
    assert counting_sha == []  # every file served from cache
    assert {p.name: h for p, h in first.items()} == {
        p.name: h for p, h in second.items()
    }


def test_mtime_invalidation_rehashes_only_changed_file(
    series_dir: Path, counting_sha: list[Path]
) -> None:
    StorageBoxRepository._resolve_series_hashes(
        series_dir=series_dir,
        series_files=_scan(series_dir),
        index_files=[],
        hashing_progress_callback=None,
    )
    counting_sha.clear()

    changed = series_dir / "Episode 01.mkv"
    changed.write_bytes(b"replaced-episode-one-content")

    hashes = StorageBoxRepository._resolve_series_hashes(
        series_dir=series_dir,
        series_files=_scan(series_dir),
        index_files=[],
        hashing_progress_callback=None,
    )
    assert [p.name for p in counting_sha] == ["Episode 01.mkv"]
    assert hashes[changed] == _sha256_file(changed)


def test_corrupt_cache_falls_back_to_full_hash(
    series_dir: Path, counting_sha: list[Path]
) -> None:
    (series_dir / HASH_CACHE_FILENAME).write_text("{not json", encoding="utf-8")
    hashes = StorageBoxRepository._resolve_series_hashes(
        series_dir=series_dir,
        series_files=_scan(series_dir),
        index_files=[],
        hashing_progress_callback=None,
    )
    assert len(counting_sha) == 3
    assert all(isinstance(value, str) and len(value) == 64 for value in hashes.values())
    # Cache is rewritten with valid content.
    payload = json.loads((series_dir / HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["files"]) == {
        "Episode 01.mkv",
        "Episode 02.mkv",
        ".atr_torrents.json",
    }


def test_index_files_always_hashed_and_never_cached(
    series_dir: Path, tmp_path: Path, counting_sha: list[Path]
) -> None:
    index_file = tmp_path / "staged" / "faiss.index"
    index_file.parent.mkdir()
    index_file.write_bytes(b"embeddings")

    for _ in range(2):
        counting_sha.clear()
        StorageBoxRepository._resolve_series_hashes(
            series_dir=series_dir,
            series_files=_scan(series_dir),
            index_files=[(index_file, index_file.stat().st_size)],
            hashing_progress_callback=None,
        )
    assert [p.name for p in counting_sha] == ["faiss.index"]
    payload = json.loads((series_dir / HASH_CACHE_FILENAME).read_text(encoding="utf-8"))
    assert "faiss.index" not in payload["files"]


def test_hashing_progress_reports_exact_totals(series_dir: Path) -> None:
    files = _scan(series_dir)
    expected_total = sum(size for _, _, size, _ in files)
    emitted: list[tuple[int, int]] = []

    StorageBoxRepository._resolve_series_hashes(
        series_dir=series_dir,
        series_files=files,
        index_files=[],
        hashing_progress_callback=lambda done, total: emitted.append((done, total)),
    )

    assert emitted[0] == (0, expected_total)
    assert emitted[-1] == (expected_total, expected_total)
    assert all(total == expected_total for _, total in emitted)


def test_collect_series_artifacts_excludes_hash_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, series_dir: Path
) -> None:
    library_root = series_dir.parent
    shard_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME / "series" / "shard-1"
    shard_dir.mkdir(parents=True)
    (shard_dir / "faiss.index").write_bytes(b"vectors")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type: library_root),
    )
    monkeypatch.setattr(
        StorageBoxRepository,
        "_read_local_index_series_payload",
        classmethod(
            lambda cls, *, library_type, display_name: (
                {"series": {display_name: {}}},
                {"files": {}},
                "shard-1",
            )
        ),
    )

    # Pre-existing cache from an earlier publish must not become an artifact.
    (series_dir / HASH_CACHE_FILENAME).write_text(
        json.dumps({"version": 1, "files": {}}), encoding="utf-8"
    )

    artifacts, episodes, temp_root = StorageBoxRepository._collect_series_artifacts(
        library_type=LibraryType.ANIME,
        series_dir=series_dir,
        display_name="Series Name",
        series_id="series-1",
    )
    try:
        names = {artifact.local_path.name for artifact in artifacts}
        assert HASH_CACHE_FILENAME not in names
        assert f"{HASH_CACHE_FILENAME}.tmp" not in names
        assert {"Episode 01.mkv", "Episode 02.mkv", "faiss.index"} <= names
        assert [entry["episode_key"] for entry in episodes] == [
            "Episode 01",
            "Episode 02",
        ]
    finally:
        import shutil

        shutil.rmtree(temp_root, ignore_errors=True)
