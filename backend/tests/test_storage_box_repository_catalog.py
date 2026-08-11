from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.library_hydration_service import LibraryHydrationService
from app.services.pending_publish_store import PendingPublishRecord
from app.services.storage_box_repository import StorageBoxRepository
from app.services.storage_box_sftp_client import StorageBoxSftpClient


def _iso_hours_ago(hours: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


@pytest.mark.asyncio
async def test_list_catalog_does_not_rebuild_after_transient_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_read(remote_path: PurePosixPath, *, context: str) -> dict:
        raise ConnectionResetError(104, "Connection reset by peer")

    async def fail_if_rebuilt(library_type: str) -> dict:
        raise AssertionError("transient catalog reads must not trigger rebuild")

    monkeypatch.setattr(StorageBoxRepository, "_catalog_cache", {})
    monkeypatch.setattr(StorageBoxRepository, "_read_remote_json", fail_read)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", fail_if_rebuilt)

    with pytest.raises(ConnectionResetError):
        await StorageBoxRepository.list_catalog("anime")


@pytest.mark.asyncio
async def test_rebuild_catalog_does_not_write_empty_catalog_when_series_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_listdir(remote_path: str | PurePosixPath) -> list[str]:
        raise ConnectionResetError(104, "Connection reset by peer")

    async def fail_if_written(remote_path: PurePosixPath, payload: dict) -> None:
        raise AssertionError("failed remote listings must not write catalog files")

    async def fail_if_replaced(src: str | PurePosixPath, dst: str | PurePosixPath) -> None:
        raise AssertionError("failed remote listings must not replace catalog files")

    monkeypatch.setattr(StorageBoxSftpClient, "listdir", fail_listdir)
    monkeypatch.setattr(StorageBoxRepository, "_write_remote_json", fail_if_written)
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", fail_if_replaced)

    with pytest.raises(RuntimeError, match="Cannot rebuild anime catalog"):
        await StorageBoxRepository.rebuild_catalog("anime")


@pytest.mark.asyncio
async def test_upsert_catalog_entry_updates_only_selected_series_without_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: dict[str, object] = {}

    async def read_catalog(remote_path: PurePosixPath, *, context: str) -> dict:
        assert remote_path.as_posix() == "v1/anime/catalog.json"
        return {
            "schema_version": 1,
            "library_type": "anime",
            "items": [
                {
                    "series_id": "series-1",
                    "name": "Old Name",
                    "storage_release_id": "release-1",
                },
                {
                    "series_id": "series-2",
                    "name": "Untouched",
                    "storage_release_id": "release-9",
                },
            ],
        }

    async def write_catalog(remote_path: PurePosixPath, payload: dict) -> None:
        written["path"] = remote_path
        written["payload"] = payload

    async def replace_catalog(
        src: str | PurePosixPath,
        dst: str | PurePosixPath,
    ) -> None:
        written["replace"] = (
            PurePosixPath(src).as_posix(),
            PurePosixPath(dst).as_posix(),
        )

    async def fail_if_rebuilt(library_type: str) -> dict:
        raise AssertionError("single-series catalog updates must not rebuild the catalog")

    monkeypatch.setattr(StorageBoxRepository, "_catalog_cache", {"anime": (0.0, [])})
    monkeypatch.setattr(StorageBoxRepository, "_read_remote_json", read_catalog)
    monkeypatch.setattr(StorageBoxRepository, "_write_remote_json", write_catalog)
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", replace_catalog)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", fail_if_rebuilt)

    result = await StorageBoxRepository.upsert_catalog_entry(
        "anime",
        current={"published_at": "2026-07-25T00:00:00Z"},
        manifest={
            "series_id": "series-1",
            "release_id": "release-2",
            "display_name": "New Name",
            "episode_count": 12,
            "total_size_bytes": 1234,
            "fps": 2.0,
            "torrent_count": 1,
        },
    )

    entries = {item["series_id"]: item for item in result["items"]}
    assert entries["series-1"] == {
        "schema_version": 1,
        "series_id": "series-1",
        "name": "New Name",
        "storage_release_id": "release-2",
        "episode_count": 12,
        "total_size_bytes": 1234,
        "fps": 2.0,
        "torrent_count": 1,
        "updated_at": "2026-07-25T00:00:00Z",
    }
    assert entries["series-2"]["name"] == "Untouched"
    assert str(written["path"]).startswith("v1/anime/catalog.")
    assert written["replace"][1] == "v1/anime/catalog.json"
    assert "anime" not in StorageBoxRepository._catalog_cache


@pytest.mark.asyncio
async def test_upload_prepared_release_updates_catalog_without_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Publishing one series must not rescan every remote series.

    The full rebuild reads current.json + series_manifest.json for every
    series in the library, sequentially over SFTP, and warns for every
    series directory left behind by an abandoned first publish.
    """
    written: dict[str, object] = {}

    async def exists(remote_path: str | PurePosixPath) -> bool:
        # Release already committed by a previous attempt: skips the upload
        # phase and goes straight to the current.json + catalog commit.
        return True

    async def read_json(remote_path: PurePosixPath, *, context: str) -> dict:
        assert remote_path.as_posix() == "v1/anime/catalog.json"
        return {
            "schema_version": 1,
            "library_type": "anime",
            "items": [
                {
                    "series_id": "series-1",
                    "name": "Old Name",
                    "storage_release_id": "release-1",
                },
                {
                    "series_id": "series-2",
                    "name": "Untouched",
                    "storage_release_id": "release-9",
                },
            ],
        }

    async def write_json(remote_path: PurePosixPath, payload: dict) -> None:
        written.setdefault("writes", []).append(  # type: ignore[union-attr]
            (remote_path.as_posix(), payload)
        )

    async def replace_file(src: str | PurePosixPath, dst: str | PurePosixPath) -> None:
        written.setdefault("replaces", []).append(  # type: ignore[union-attr]
            PurePosixPath(dst).as_posix()
        )

    async def read_text(remote_path: str | PurePosixPath) -> str:
        raise FileNotFoundError("no previous current.json")

    async def fail_if_rebuilt(library_type: str) -> dict:
        raise AssertionError("publishing one series must not rebuild the whole catalog")

    async def fail_if_listed(remote_path: str | PurePosixPath) -> list[str]:
        raise AssertionError("publishing one series must not list the remote series root")

    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxRepository, "_catalog_cache", {"anime": (0.0, [])})
    monkeypatch.setattr(StorageBoxRepository, "_read_remote_json", read_json)
    monkeypatch.setattr(StorageBoxRepository, "_write_remote_json", write_json)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", fail_if_rebuilt)
    monkeypatch.setattr(StorageBoxSftpClient, "exists", exists)
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", replace_file)
    monkeypatch.setattr(StorageBoxSftpClient, "read_text", read_text)
    monkeypatch.setattr(StorageBoxSftpClient, "listdir", fail_if_listed)

    record = PendingPublishRecord(
        publish_id="publish-1",
        library_type="anime",
        series_id="series-1",
        release_id="release-2",
        display_name="New Name",
        series_dir=str(tmp_path / "missing-series-dir"),
        staged_index_dir=str(tmp_path / "missing-staged-index"),
        is_brand_new_series=False,
        manifest={
            "schema_version": 1,
            "series_id": "series-1",
            "release_id": "release-2",
            "display_name": "New Name",
            "episode_count": 12,
            "total_size_bytes": 1234,
            "fps": 23.976,
            "torrent_count": 1,
        },
    )

    result = await StorageBoxRepository.upload_prepared_release(record)

    assert result["series_id"] == "series-1"
    assert result["release_id"] == "release-2"

    catalog_payloads = [
        payload
        for path, payload in written["writes"]  # type: ignore[misc]
        if path.startswith("v1/anime/catalog.")
    ]
    assert len(catalog_payloads) == 1
    entries = {item["series_id"]: item for item in catalog_payloads[0]["items"]}
    assert entries["series-1"]["name"] == "New Name"
    assert entries["series-1"]["storage_release_id"] == "release-2"
    assert entries["series-1"]["episode_count"] == 12
    assert entries["series-1"]["fps"] == 23.976
    assert entries["series-2"]["name"] == "Untouched"
    assert "v1/anime/catalog.json" in written["replaces"]  # type: ignore[operator]
    assert "anime" not in StorageBoxRepository._catalog_cache


@pytest.mark.asyncio
async def test_delete_series_moves_remote_tree_and_prunes_catalog_without_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    background_tasks = []

    async def exists(remote_path: str | PurePosixPath) -> bool:
        calls.append(("exists", PurePosixPath(remote_path).as_posix()))
        return True

    async def rename(src: str | PurePosixPath, dst: str | PurePosixPath) -> None:
        calls.append(("rename", f"{PurePosixPath(src).as_posix()} -> {PurePosixPath(dst).as_posix()}"))

    async def remove_catalog_entry(library_type: str, series_id: str) -> None:
        library_value = getattr(library_type, "value", str(library_type))
        calls.append(("remove_catalog_entry", f"{library_value}:{series_id}"))

    async def remove_tree(remote_path: str | PurePosixPath) -> None:
        calls.append(("remove_tree", PurePosixPath(remote_path).as_posix()))

    async def fail_if_rebuilt(library_type: str) -> dict:
        raise AssertionError("delete_series must not rebuild the whole catalog")

    real_create_task = asyncio.create_task

    def capture_task(coroutine, **kwargs):
        task = real_create_task(coroutine, **kwargs)
        background_tasks.append(task)
        return task

    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxSftpClient, "exists", exists)
    monkeypatch.setattr(StorageBoxSftpClient, "rename", rename)
    monkeypatch.setattr(StorageBoxSftpClient, "remove_tree", remove_tree)
    monkeypatch.setattr(StorageBoxRepository, "remove_catalog_entry", remove_catalog_entry)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", fail_if_rebuilt)
    monkeypatch.setattr("app.services.storage_box_repository.asyncio.create_task", capture_task)

    await StorageBoxRepository.delete_series(library_type="anime", series_id="series-1")
    await asyncio.gather(*background_tasks)

    assert calls[0] == ("exists", "v1/anime/series/series-1")
    assert calls[1][0] == "rename"
    assert calls[2] == ("remove_catalog_entry", "anime:series-1")
    assert calls[3][0] == "remove_tree"


@pytest.mark.asyncio
async def test_rebuild_catalog_stamps_reconciled_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a full rebuild derives from the remote tree, so only it may
    claim the catalog has been reconciled against ground truth."""
    written: dict[str, object] = {}

    async def listdir(remote_path: str | PurePosixPath) -> list[str]:
        return ["series-1"]

    async def get_current_release(library_type: str, series_id: str) -> dict:
        return {"release_id": "release-1", "published_at": "2026-08-01T00:00:00Z"}

    async def get_series_manifest(
        library_type: str, series_id: str, release_id: str
    ) -> dict:
        return {
            "series_id": "series-1",
            "display_name": "Name",
            "release_id": "release-1",
            "episode_count": 1,
            "total_size_bytes": 1,
            "fps": 23.976,
            "torrent_count": 0,
        }

    async def write_json(remote_path: PurePosixPath, payload: dict) -> None:
        written["payload"] = payload

    async def replace_file(src: str | PurePosixPath, dst: str | PurePosixPath) -> None:
        return None

    monkeypatch.setattr(StorageBoxSftpClient, "listdir", listdir)
    monkeypatch.setattr(StorageBoxRepository, "get_current_release", get_current_release)
    monkeypatch.setattr(StorageBoxRepository, "get_series_manifest", get_series_manifest)
    monkeypatch.setattr(StorageBoxRepository, "_write_remote_json", write_json)
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", replace_file)

    payload = await StorageBoxRepository.rebuild_catalog("anime")

    assert payload["reconciled_at"]
    assert written["payload"]["reconciled_at"] == payload["reconciled_at"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_upsert_catalog_entry_preserves_reconciled_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-series upsert must not claim credit for a reconcile it did not do,
    nor erase the marker and force a rebuild on every backend start."""
    written: dict[str, object] = {}

    async def read_catalog(remote_path: PurePosixPath, *, context: str) -> dict:
        return {
            "schema_version": 1,
            "library_type": "anime",
            "reconciled_at": "2026-08-01T00:00:00+00:00",
            "items": [],
        }

    async def write_catalog(remote_path: PurePosixPath, payload: dict) -> None:
        written["payload"] = payload

    async def replace_catalog(src: str | PurePosixPath, dst: str | PurePosixPath) -> None:
        return None

    async def fail_if_rebuilt(library_type: str) -> dict:
        raise AssertionError("upsert must not rebuild here")

    monkeypatch.setattr(StorageBoxRepository, "_catalog_cache", {})
    monkeypatch.setattr(StorageBoxRepository, "_read_remote_json", read_catalog)
    monkeypatch.setattr(StorageBoxRepository, "_write_remote_json", write_catalog)
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", replace_catalog)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", fail_if_rebuilt)

    result = await StorageBoxRepository.upsert_catalog_entry(
        "anime",
        current={"published_at": "2026-08-11T00:00:00Z"},
        manifest={
            "series_id": "series-1",
            "release_id": "release-1",
            "display_name": "Name",
        },
    )

    assert result["reconciled_at"] == "2026-08-01T00:00:00+00:00"
    assert written["payload"]["reconciled_at"] == "2026-08-01T00:00:00+00:00"  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog_payload, should_rebuild",
    [
        ({"items": []}, True),
        ({"items": [], "reconciled_at": ""}, True),
        ({"items": [], "reconciled_at": "not-a-timestamp"}, True),
        ({"items": [], "reconciled_at": _iso_hours_ago(999)}, True),
        ({"items": [], "reconciled_at": _iso_hours_ago(0.5)}, False),
    ],
    ids=["missing", "empty", "malformed", "stale", "fresh"],
)
async def test_reconcile_catalog_rebuilds_only_when_marker_is_stale(
    monkeypatch: pytest.MonkeyPatch,
    catalog_payload: dict,
    should_rebuild: bool,
) -> None:
    rebuilt: list[str] = []

    async def read_catalog(remote_path: PurePosixPath, *, context: str) -> dict:
        return catalog_payload

    async def rebuild(library_type: str) -> dict:
        rebuilt.append(str(library_type))
        return {"items": []}

    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxRepository, "_read_remote_json", read_catalog)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", rebuild)

    await LibraryHydrationService.reconcile_catalog("anime")

    assert bool(rebuilt) is should_rebuild


@pytest.mark.asyncio
async def test_reconcile_catalog_rebuilds_when_catalog_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuilt: list[str] = []

    async def fail_read(remote_path: PurePosixPath, *, context: str) -> dict:
        raise FileNotFoundError("No such file")

    async def rebuild(library_type: str) -> dict:
        rebuilt.append(str(library_type))
        return {"items": []}

    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxRepository, "_read_remote_json", fail_read)
    monkeypatch.setattr(StorageBoxRepository, "rebuild_catalog", rebuild)

    await LibraryHydrationService.reconcile_catalog("anime")

    assert rebuilt == ["anime"]
