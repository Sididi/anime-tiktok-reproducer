from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.source_hash_service import SourceHashService
from app.services.storage_box_repository import (
    HASH_CACHE_FILENAME,
    LOCAL_STORAGE_BOX_METADATA,
)


@pytest.fixture(autouse=True)
def _cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    yield


def _write_series_cache(series_dir: Path, rel: str, entry: dict) -> None:
    (series_dir / HASH_CACHE_FILENAME).write_text(
        json.dumps({"version": 1, "files": {rel: entry}}), encoding="utf-8"
    )


def test_series_cache_hit_skips_hashing(tmp_path: Path) -> None:
    series_dir = tmp_path / "library" / "Some Series"
    series_dir.mkdir(parents=True)
    episode = series_dir / "ep01.mkv"
    episode.write_bytes(b"episode-bytes")
    stat = episode.stat()
    fake_sha = "a" * 64
    _write_series_cache(
        series_dir,
        "ep01.mkv",
        {"sha256": fake_sha, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )

    # The fake sha differs from the real hash: getting it back proves the
    # cache short-circuited hashing.
    assert SourceHashService.sha256_for(episode) == fake_sha


def test_stale_series_cache_falls_through_to_hashing(tmp_path: Path) -> None:
    series_dir = tmp_path / "library" / "Some Series"
    series_dir.mkdir(parents=True)
    episode = series_dir / "ep01.mkv"
    episode.write_bytes(b"episode-bytes")
    stat = episode.stat()
    _write_series_cache(
        series_dir,
        "ep01.mkv",
        {"sha256": "a" * 64, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns - 1},
    )

    expected = hashlib.sha256(b"episode-bytes").hexdigest()
    assert SourceHashService.sha256_for(episode) == expected


def test_series_dir_without_cache_hashes(tmp_path: Path) -> None:
    series_dir = tmp_path / "library" / "Marked Series"
    series_dir.mkdir(parents=True)
    (series_dir / LOCAL_STORAGE_BOX_METADATA).write_text("{}", encoding="utf-8")
    episode = series_dir / "ep01.mkv"
    episode.write_bytes(b"other-bytes")

    expected = hashlib.sha256(b"other-bytes").hexdigest()
    assert SourceHashService.sha256_for(episode) == expected


def test_central_cache_write_through_and_inode_reuse(tmp_path: Path) -> None:
    project_a = tmp_path / "projects" / "aaa"
    project_a.mkdir(parents=True)
    video = project_a / "tiktok_clean.mp4"
    video.write_bytes(b"pure-bytes")
    expected = hashlib.sha256(b"pure-bytes").hexdigest()
    assert SourceHashService.sha256_for(video) == expected

    cache_path = settings.cache_dir / "drive_shared_sources" / "hash_cache.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert str(video) in payload["by_path"]
    assert len(payload["by_inode"]) == 1

    # Poison the cached sha, then look the same inode up through a hardlink
    # in another project dir: getting the poisoned value back proves the
    # inode-keyed cache answered (no re-hash for pure-mode duplicates).
    poisoned = "f" * 64
    inode_key = next(iter(payload["by_inode"]))
    payload["by_inode"][inode_key]["sha256"] = poisoned
    payload["by_path"][str(video)]["sha256"] = poisoned
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    project_b = tmp_path / "projects" / "bbb"
    project_b.mkdir(parents=True)
    linked = project_b / "tiktok_clean.mp4"
    os.link(video, linked)
    assert SourceHashService.sha256_for(linked) == poisoned
