from __future__ import annotations

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
