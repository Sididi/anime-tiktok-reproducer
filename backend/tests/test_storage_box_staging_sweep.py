"""Staging sweep must not delete another machine's upload.

Two backends on two PCs share one Storage Box, and ``_resolve_or_create_series_id``
deliberately converges both on the same ``series_id`` for the same show. The
sweep therefore sees staging dirs it did not create, whose pending-publish
records live only on the other machine.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.pending_publish_store import PendingPublishRecord
from app.services.storage_box_repository import (
    STAGING_OWNER_FILENAME,
    StorageBoxRepository,
)
from app.services.storage_box_sftp_client import StorageBoxSftpClient


def _install_staging_box(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entries: list[str],
    owners: dict[str, str],
    ages_hours: dict[str, float],
) -> list[str]:
    """Fake one series' ``staging/`` dir. Returns the list removals land in."""
    removed: list[str] = []

    async def listdir(remote_path: str | PurePosixPath) -> list[str]:
        assert PurePosixPath(remote_path).as_posix().endswith("/staging")
        return entries

    async def read_text(remote_path: str | PurePosixPath) -> str:
        path = PurePosixPath(remote_path)
        assert path.name == STAGING_OWNER_FILENAME
        publish_id = path.parent.name
        if publish_id not in owners:
            raise FileNotFoundError("No such file")
        return '{"machine_id": "%s"}' % owners[publish_id]

    async def stat(remote_path: str | PurePosixPath):
        publish_id = PurePosixPath(remote_path).name
        age = ages_hours.get(publish_id, 0.0)
        return SimpleNamespace(mtime=time.time() - age * 3600.0)

    async def remove_tree(remote_path: str | PurePosixPath) -> None:
        removed.append(PurePosixPath(remote_path).name)

    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxRepository, "_machine_id", classmethod(lambda cls: "this-pc"))
    monkeypatch.setattr(StorageBoxSftpClient, "listdir", listdir)
    monkeypatch.setattr(StorageBoxSftpClient, "read_text", read_text)
    monkeypatch.setattr(StorageBoxSftpClient, "stat", stat)
    monkeypatch.setattr(StorageBoxSftpClient, "remove_tree", remove_tree)
    return removed


@pytest.mark.asyncio
async def test_sweep_preserves_staging_owned_by_another_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _install_staging_box(
        monkeypatch,
        entries=["mine-old", "theirs-inflight"],
        owners={"mine-old": "this-pc", "theirs-inflight": "other-pc"},
        ages_hours={"mine-old": 100.0, "theirs-inflight": 0.1},
    )

    await StorageBoxRepository.sweep_series_staging(
        "anime", "series-1", keep_publish_ids={"current"}
    )

    assert removed == ["mine-old"]


@pytest.mark.asyncio
async def test_sweep_preserves_another_machines_stale_looking_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Age must not override ownership: the other machine may be resuming a
    publish it parked days ago, and only it holds the record to resume from."""
    removed = _install_staging_box(
        monkeypatch,
        entries=["theirs-parked"],
        owners={"theirs-parked": "other-pc"},
        ages_hours={"theirs-parked": 24 * 30.0},
    )

    await StorageBoxRepository.sweep_series_staging(
        "anime", "series-1", keep_publish_ids=set()
    )

    assert removed == []


@pytest.mark.asyncio
async def test_sweep_keeps_the_publish_ids_it_was_told_to_keep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _install_staging_box(
        monkeypatch,
        entries=["keep-me", "mine-old"],
        owners={"keep-me": "this-pc", "mine-old": "this-pc"},
        ages_hours={"keep-me": 0.0, "mine-old": 100.0},
    )

    await StorageBoxRepository.sweep_series_staging(
        "anime", "series-1", keep_publish_ids={"keep-me"}
    )

    assert removed == ["mine-old"]


@pytest.mark.asyncio
async def test_sweep_defers_unowned_staging_until_the_grace_period_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dirs predating the owner marker have no attributable machine. A recent
    one may be the other PC mid-upload on an older build, so let it age out."""
    removed = _install_staging_box(
        monkeypatch,
        entries=["legacy-fresh", "legacy-old"],
        owners={},
        ages_hours={"legacy-fresh": 1.0, "legacy-old": 24 * 7.0},
    )

    await StorageBoxRepository.sweep_series_staging(
        "anime", "series-1", keep_publish_ids=set()
    )

    assert removed == ["legacy-old"]


@pytest.mark.asyncio
async def test_sweep_keeps_unowned_staging_when_its_age_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed = _install_staging_box(
        monkeypatch,
        entries=["legacy-unstatable"],
        owners={},
        ages_hours={},
    )

    async def failing_stat(remote_path: str | PurePosixPath):
        raise OSError("stat failed")

    monkeypatch.setattr(StorageBoxSftpClient, "stat", failing_stat)

    await StorageBoxRepository.sweep_series_staging(
        "anime", "series-1", keep_publish_ids=set()
    )

    assert removed == []


@pytest.mark.asyncio
async def test_upload_prepared_release_stamps_the_staging_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without this stamp the other machine cannot tell our upload from junk."""
    writes: list[tuple[str, str]] = []

    async def exists(remote_path: str | PurePosixPath) -> bool:
        return False  # release not committed yet: the upload phase runs

    async def write_text(remote_path: str | PurePosixPath, text: str) -> None:
        writes.append((PurePosixPath(remote_path).as_posix(), text))

    async def upload_batch(*args, **kwargs) -> None:
        return None

    async def noop(*args, **kwargs) -> None:
        return None

    async def read_text(remote_path: str | PurePosixPath) -> str:
        raise FileNotFoundError("no previous current.json")

    async def upsert(*args, **kwargs) -> dict:
        return {}

    monkeypatch.setattr(StorageBoxRepository, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(StorageBoxRepository, "_machine_id", classmethod(lambda cls: "this-pc"))
    monkeypatch.setattr(StorageBoxRepository, "_verify_remote_artifacts", noop)
    monkeypatch.setattr(StorageBoxRepository, "_write_remote_json", noop)
    monkeypatch.setattr(StorageBoxRepository, "upsert_catalog_entry", classmethod(upsert))
    monkeypatch.setattr(StorageBoxSftpClient, "exists", exists)
    monkeypatch.setattr(StorageBoxSftpClient, "write_text", write_text)
    monkeypatch.setattr(StorageBoxSftpClient, "read_text", read_text)
    monkeypatch.setattr(StorageBoxSftpClient, "rename", noop)
    monkeypatch.setattr(StorageBoxSftpClient, "replace_file", noop)
    monkeypatch.setattr(
        "app.services.storage_box_repository.StorageBoxRclone.upload_batch",
        upload_batch,
    )

    record = PendingPublishRecord(
        publish_id="publish-1",
        library_type="anime",
        series_id="series-1",
        release_id="release-1",
        display_name="Name",
        series_dir=str(tmp_path / "missing"),
        staged_index_dir=str(tmp_path / "missing-index"),
        is_brand_new_series=True,
        manifest={"series_id": "series-1", "release_id": "release-1", "display_name": "Name"},
    )

    await StorageBoxRepository.upload_prepared_release(record)

    owner_writes = [
        text for path, text in writes if path.endswith(STAGING_OWNER_FILENAME)
    ]
    assert len(owner_writes) == 1
    assert '"machine_id": "this-pc"' in owner_writes[0]
    assert '"publish_id": "publish-1"' in owner_writes[0]


def test_machine_id_is_stable_across_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(StorageBoxRepository, "_machine_id_cache", None)
    monkeypatch.setattr("app.services.storage_box_repository.settings.data_dir", tmp_path)

    first = StorageBoxRepository._machine_id()
    monkeypatch.setattr(StorageBoxRepository, "_machine_id_cache", None)
    second = StorageBoxRepository._machine_id()

    assert first and first == second
    assert (tmp_path / "machine_id").read_text(encoding="utf-8").strip() == first
