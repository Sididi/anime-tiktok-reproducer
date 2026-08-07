from __future__ import annotations

import hashlib
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService
from app.services.library_hydration_service import LibraryHydrationService
from app.services.library_state_db import LibraryStateDb
from app.services.storage_box_progress import ProgressSnapshot
from app.services.storage_box_rclone import StorageBoxRclone
from app.services.storage_box_repository import StorageBoxRepository

SERIES_ID = "series-1"
DISPLAY_NAME = "Series Name"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


EPISODE_CONTENT = {
    "Episode 01": b"episode-one-bytes",
    "Episode 02": b"episode-two-bytes!",
}


def _manifest() -> dict[str, Any]:
    episodes = []
    for key, content in EPISODE_CONTENT.items():
        episodes.append(
            {
                "episode_key": key,
                "media": {
                    "relative_path": f"payload/library/{DISPLAY_NAME}/{key}.mkv",
                    "local_relative_path": f"{DISPLAY_NAME}/{key}.mkv",
                    "size_bytes": len(content),
                    "sha256": _sha(content),
                },
                "sidecars": [],
            }
        )
    return {
        "schema_version": 1,
        "series_id": SERIES_ID,
        "release_id": "release-1",
        "display_name": DISPLAY_NAME,
        "episode_count": len(episodes),
        "episodes": episodes,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    library_root = tmp_path / "library"
    library_root.mkdir()
    db_path = tmp_path / "library_state.db"
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(settings, "library_state_db_path", db_path)
    LibraryStateDb.initialize()
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )

    async def _noop_ensure_episode_manifest(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        AnimeLibraryService,
        "ensure_episode_manifest",
        _noop_ensure_episode_manifest,
    )

    manifest = _manifest()

    async def _fake_load_manifest(cls, library_type, series_id):
        return manifest

    async def _fake_describe(cls, library_type, series_id):
        return {"series_id": series_id}

    monkeypatch.setattr(
        LibraryHydrationService,
        "_load_or_fetch_manifest",
        classmethod(_fake_load_manifest),
    )
    monkeypatch.setattr(
        LibraryHydrationService, "describe_series", classmethod(_fake_describe)
    )
    yield {"library_root": library_root, "manifest": manifest}


class FakeDownloadBatch:
    """Materializes requested files into dest_root like rclone would."""

    def __init__(self, content_by_name: dict[str, bytes]):
        self.content_by_name = content_by_name
        self.calls: list[dict[str, Any]] = []
        self.observed: dict[str, Any] = {}

    async def __call__(
        self,
        items: list[PurePosixPath],
        *,
        remote_base,
        dest_root: Path,
        total_bytes=None,
        progress_callback=None,
    ) -> None:
        self.calls.append(
            {
                "items": list(items),
                "remote_base": remote_base,
                "dest_root": dest_root,
                "total_bytes": total_bytes,
            }
        )
        if progress_callback is not None:
            await progress_callback(
                ProgressSnapshot(
                    bytes_transferred=total_bytes or 0,
                    bytes_total=total_bytes or 0,
                    mib_per_sec=12.5,
                    eta_seconds=3.0,
                    active_transfers=1,
                )
            )
            operation = LibraryStateDb.get_operation(
                LibraryType.ANIME, SERIES_ID, "hydrate"
            )
            self.observed["mid_run_operation"] = operation
        for item in items:
            target = dest_root / Path(*item.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = self.content_by_name.get(Path(item.name).stem, b"")
            target.write_bytes(content)


@pytest.mark.asyncio
async def test_single_batch_excludes_local_episodes_and_moves_files(
    monkeypatch: pytest.MonkeyPatch, _env: dict[str, Any]
) -> None:
    library_root: Path = _env["library_root"]
    # Episode 01 already fully local -> must be excluded from the batch.
    series_dir = library_root / DISPLAY_NAME
    series_dir.mkdir(parents=True)
    (series_dir / "Episode 01.mkv").write_bytes(EPISODE_CONTENT["Episode 01"])

    fake = FakeDownloadBatch(EPISODE_CONTENT)
    monkeypatch.setattr(StorageBoxRclone, "download_batch", fake)

    await LibraryHydrationService.hydrate_series(
        library_type=LibraryType.ANIME,
        series_id=SERIES_ID,
        full_series=True,
    )

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["items"] == [
        PurePosixPath(f"payload/library/{DISPLAY_NAME}/Episode 02.mkv")
    ]
    assert call["total_bytes"] == len(EPISODE_CONTENT["Episode 02"])
    assert (
        series_dir / "Episode 02.mkv"
    ).read_bytes() == EPISODE_CONTENT["Episode 02"]

    # Mid-run the operation row carried byte progress + network detail...
    mid_operation = fake.observed["mid_run_operation"]
    assert mid_operation is not None
    assert mid_operation.status == "running"
    assert mid_operation.detail is not None
    assert mid_operation.detail["network_mib_per_sec"] == 12.5
    assert (
        mid_operation.detail["network_bytes_total"]
        == len(EPISODE_CONTENT["Episode 02"])
    )
    # ...and the terminal row cleared it.
    final_operation = LibraryStateDb.get_operation(
        LibraryType.ANIME, SERIES_ID, "hydrate"
    )
    assert final_operation is not None
    assert final_operation.status == "complete"
    assert final_operation.progress == 1.0
    assert final_operation.detail is None

    # Batch temp contents cleaned up (only empty scaffolding dirs may remain).
    leftover_files = [
        path
        for path in settings.cache_dir.rglob("*")
        if path.is_file() and "episodes-batch" in path.parts
    ]
    assert leftover_files == []


@pytest.mark.asyncio
async def test_checksum_mismatch_fails_only_that_episode(
    monkeypatch: pytest.MonkeyPatch, _env: dict[str, Any]
) -> None:
    library_root: Path = _env["library_root"]
    corrupted = dict(EPISODE_CONTENT)
    corrupted["Episode 02"] = b"corrupted-bytes"
    fake = FakeDownloadBatch(corrupted)
    monkeypatch.setattr(StorageBoxRclone, "download_batch", fake)

    with pytest.raises(RuntimeError, match="Episode 02"):
        await LibraryHydrationService.hydrate_series(
            library_type=LibraryType.ANIME,
            series_id=SERIES_ID,
            full_series=True,
        )

    series_dir = library_root / DISPLAY_NAME
    # The healthy episode still materialized; the corrupted one did not.
    assert (
        series_dir / "Episode 01.mkv"
    ).read_bytes() == EPISODE_CONTENT["Episode 01"]
    assert not (series_dir / "Episode 02.mkv").exists()

    operation = LibraryStateDb.get_operation(LibraryType.ANIME, SERIES_ID, "hydrate")
    assert operation is not None
    assert operation.status == "error"


@pytest.mark.asyncio
async def test_idempotent_rerun_downloads_nothing(
    monkeypatch: pytest.MonkeyPatch, _env: dict[str, Any]
) -> None:
    fake = FakeDownloadBatch(EPISODE_CONTENT)
    monkeypatch.setattr(StorageBoxRclone, "download_batch", fake)

    await LibraryHydrationService.hydrate_series(
        library_type=LibraryType.ANIME, series_id=SERIES_ID, full_series=True
    )
    assert len(fake.calls) == 1

    await LibraryHydrationService.hydrate_series(
        library_type=LibraryType.ANIME, series_id=SERIES_ID, full_series=True
    )
    # All episodes local -> empty plan -> no download call at all.
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_index_artifacts_batch_verifies_before_materialize(
    monkeypatch: pytest.MonkeyPatch, _env: dict[str, Any]
) -> None:
    index_content = b"faiss-bytes"
    manifest = {
        "series_id": SERIES_ID,
        "release_id": "release-1",
        "display_name": DISPLAY_NAME,
        "artifacts": [
            {
                "artifact_type": "index",
                "relative_path": f"payload/index/{SERIES_ID}/series/shard/faiss.index",
                "size_bytes": len(index_content),
                "sha256": _sha(index_content),
            }
        ],
    }
    fake = FakeDownloadBatch({"faiss": index_content})
    monkeypatch.setattr(StorageBoxRclone, "download_batch", fake)
    materialized: list[Path] = []

    async def _fake_materialize(cls, library_type, m, temp_root: Path) -> None:
        materialized.append(temp_root)
        assert (
            temp_root / SERIES_ID / "series" / "shard" / "faiss.index"
        ).read_bytes() == index_content

    monkeypatch.setattr(
        LibraryHydrationService,
        "_materialize_local_matcher_cache",
        classmethod(_fake_materialize),
    )

    await LibraryHydrationService._hydrate_index_artifacts(
        LibraryType.ANIME, manifest
    )
    assert len(materialized) == 1
    assert len(fake.calls) == 1
    assert fake.calls[0]["items"] == [
        PurePosixPath(f"payload/index/{SERIES_ID}/series/shard/faiss.index")
    ]
