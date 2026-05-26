from __future__ import annotations

import asyncio
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.storage_box_repository import StorageBoxRepository
from app.services.storage_box_sftp_client import StorageBoxSftpClient


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
