from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService
from app.services.pending_publish_store import PendingPublishStore
from app.services.storage_box_repository import (
    StorageBoxRepository,
    _json_dumps,
    _sha256_file,
)


DISPLAY_NAME = "Series Name"


@pytest.fixture
def library_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "anime_library_path", tmp_path / "library")
    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))

    library_path = AnimeLibraryService.get_library_path(LibraryType.ANIME)
    series_dir = library_path / DISPLAY_NAME
    series_dir.mkdir(parents=True)
    (series_dir / "Episode 01.mkv").write_bytes(b"episode-one-bytes")
    (series_dir / "Episode 02.mkv").write_bytes(b"episode-two-bytes!")
    (series_dir / ".atr_torrents.json").write_text(
        json.dumps({"torrents": [{"id": "t1"}]}), encoding="utf-8"
    )

    index_dir = library_path / AnimeLibraryService.INDEX_DIR_NAME
    shard_dir = index_dir / "series" / "series-name"
    shard_dir.mkdir(parents=True)
    (shard_dir / "faiss.index").write_bytes(b"faiss")
    (shard_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (index_dir / AnimeLibraryService.MANIFEST_FILE).write_text(
        json.dumps(
            {
                "version": AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION,
                "engine_profile": AnimeLibraryService.SEARCHER_ENGINE_PROFILE,
                "config": {},
                "series": {DISPLAY_NAME: {"key": "series-name", "fps": 23.976}},
            }
        ),
        encoding="utf-8",
    )
    (index_dir / AnimeLibraryService.STATE_FILE).write_text(
        json.dumps({"files": {f"{DISPLAY_NAME}/Episode 01.mkv": {}}}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_prepare_freezes_upload_plan_in_durable_staging(
    monkeypatch: pytest.MonkeyPatch,
    library_env: Path,
) -> None:
    """prepare_series_release must: partition unchanged artifacts into
    hardlinks against the previous release, relocate the staged index shard
    into the restart-surviving pending_publish dir, and produce a manifest
    whose serialized form survives a store round-trip byte-identically
    (the current.json checksum is recomputed from it at upload time)."""
    tmp_path = library_env
    series_dir = tmp_path / "library" / "anime" / DISPLAY_NAME
    episode_one_sha = _sha256_file(series_dir / "Episode 01.mkv")

    async def fake_get_current_release(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"release_id": "release-prev"}

    async def fake_get_series_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "release_id": "release-prev",
            "episodes": [],
            "artifacts": [
                {
                    "relative_path": f"payload/library/{DISPLAY_NAME}/Episode 01.mkv",
                    "sha256": episode_one_sha,
                    "size_bytes": 17,
                }
            ],
        }

    monkeypatch.setattr(
        StorageBoxRepository, "get_current_release", fake_get_current_release
    )
    monkeypatch.setattr(
        StorageBoxRepository, "get_series_manifest", fake_get_series_manifest
    )

    record = await StorageBoxRepository.prepare_series_release(
        library_type=LibraryType.ANIME,
        display_name=DISPLAY_NAME,
        series_id="series-1",
    )

    assert record.series_id == "series-1"
    assert record.is_brand_new_series is False
    assert record.previous_release_id == "release-prev"

    # Episode 01 is byte-identical to the previous release → hardlink.
    hardlink_paths = {a.remote_relative_path for a in record.hardlinks}
    assert hardlink_paths == {f"payload/library/{DISPLAY_NAME}/Episode 01.mkv"}
    assert record.hardlinks[0].previous_remote_path == (
        f"v1/anime/series/series-1/releases/release-prev/"
        f"payload/library/{DISPLAY_NAME}/Episode 01.mkv"
    )
    # Hardlink entries keep a local path for the no-hardlink-support fallback.
    assert Path(record.hardlinks[0].local_path or "").is_file()

    upload_paths = {a.remote_relative_path for a in record.uploads}
    assert f"payload/library/{DISPLAY_NAME}/Episode 02.mkv" in upload_paths
    assert f"payload/library/{DISPLAY_NAME}/.atr_torrents.json" in upload_paths
    index_uploads = [a for a in record.uploads if a.artifact_type == "index"]
    assert index_uploads, "the staged index shard must be part of the plan"

    # The staged index tree moved into the durable pending_publish area and
    # the recorded local paths point inside it.
    staged_dir = Path(record.staged_index_dir)
    assert staged_dir == (
        PendingPublishStore.staging_dir_root() / record.publish_id
    )
    for artifact in index_uploads:
        local_path = Path(artifact.local_path or "")
        assert local_path.is_file()
        assert staged_dir in local_path.parents
    # Nothing left behind in the crash-swept release tempdir area.
    assert list((tmp_path / "cache").glob("storage_box_release_*")) == []

    # Manifest serialization is stable across a store round-trip: the
    # upload-side checksum recomputation must match what prepare built.
    manifest_text = _json_dumps(record.manifest)
    PendingPublishStore.save(record)
    loaded = PendingPublishStore.load(record.publish_id)
    assert loaded is not None
    assert _json_dumps(loaded.manifest) == manifest_text
    assert loaded.manifest["episode_count"] == 2
    assert loaded.manifest["torrent_count"] == 1
    assert loaded.manifest["fps"] == pytest.approx(23.976)
