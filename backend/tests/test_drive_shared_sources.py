from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services import drive_shared_sources as dss_module
from app.services.drive_shared_sources import (
    LOCAL_MANIFEST_FILENAME,
    DriveSharedSources,
    SharedFileRecord,
)
from app.services.export_service import ManifestEntry
from app.services.google_drive_rclone import GoogleDriveRclone
from app.services.google_drive_service import GoogleDriveService


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(settings, "projects_dir", projects_dir)
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(settings, "drive_shared_sources_min_bytes", 10)
    monkeypatch.setattr(dss_module, "_shared_folder_id_cache", None)
    monkeypatch.setattr(
        GoogleDriveService,
        "ensure_child_folder_id",
        classmethod(lambda cls, name, parent_id=None, drive=None: "shared-1"),
    )
    yield


def _record(name: str, sha: str = "ab" * 32, size: int = 100) -> SharedFileRecord:
    return SharedFileRecord(
        path_in_folder=f"sources/{name}",
        size=size,
        sha256=sha,
        md5=None,
        drive_file_id=f"id-{name}",
        shared_name=DriveSharedSources.shared_name(sha, name),
    )


def _write_manifest(project_id: str, records: list[SharedFileRecord]) -> None:
    DriveSharedSources.persist_local_manifest(
        project_id, status="uploaded", records=records, drive_folder_id="f-1"
    )


# --------------------------------------------------------------------------- #
# Partitioning + naming                                                       #
# --------------------------------------------------------------------------- #


def test_partition_externalizes_only_large_sources(tmp_path: Path) -> None:
    big = tmp_path / "Episode 01.mkv"
    big.write_bytes(b"x" * 50)
    small = tmp_path / "title_overlay.png"
    small.write_bytes(b"x" * 5)
    big_root = tmp_path / "tts_edited.wav"
    big_root.write_bytes(b"x" * 50)

    entries = [
        ManifestEntry(
            relative_path="SPM_f/sources/Episode 01.mkv", source_path=big
        ),
        ManifestEntry(
            relative_path="SPM_f/sources/title_overlay.png", source_path=small
        ),
        ManifestEntry(relative_path="SPM_f/tts_edited.wav", source_path=big_root),
        ManifestEntry(relative_path="SPM_f/README.txt", inline_content=b"hi"),
    ]

    kept, externalized = DriveSharedSources.partition_entries(entries)

    assert [e.relative_path for e in externalized] == [
        "SPM_f/sources/Episode 01.mkv"
    ]
    assert len(kept) == 3
    assert DriveSharedSources.path_in_folder(externalized[0]) == (
        "sources/Episode 01.mkv"
    )


def test_shared_name_is_deterministic() -> None:
    sha = "0123456789abcdef" + "0" * 48
    name = DriveSharedSources.shared_name(sha, "Ep 01.mkv")
    assert name == "0123456789abcdef__Ep 01.mkv"
    assert dss_module.SHARED_NAME_RE.match(name)


# --------------------------------------------------------------------------- #
# Local manifest persistence                                                  #
# --------------------------------------------------------------------------- #


def test_local_manifest_roundtrip() -> None:
    record = _record("Ep.mkv")
    DriveSharedSources.persist_local_manifest(
        "abc123def456", status="pending", records=[record]
    )
    loaded = DriveSharedSources.load_local_manifest("abc123def456")
    assert loaded is not None
    assert loaded["status"] == "pending"
    assert loaded["shared_files"][0]["shared_name"] == record.shared_name

    DriveSharedSources.persist_local_manifest(
        "abc123def456", status="uploaded", records=[record], drive_folder_id="f-9"
    )
    loaded = DriveSharedSources.load_local_manifest("abc123def456")
    assert loaded["status"] == "uploaded"
    assert loaded["drive_folder_id"] == "f-9"


# --------------------------------------------------------------------------- #
# GC                                                                          #
# --------------------------------------------------------------------------- #


def _mock_drive_children(
    monkeypatch: pytest.MonkeyPatch, children: list[dict[str, Any]]
) -> list[str]:
    deleted: list[str] = []
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(lambda cls, folder_id, drive=None: list(children)),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "delete_file",
        classmethod(lambda cls, file_id, drive=None: deleted.append(file_id)),
    )
    return deleted


def test_gc_deletes_only_unreferenced(monkeypatch: pytest.MonkeyPatch) -> None:
    shared_rec = _record("Shared.mkv", sha="aa" * 32)
    only_rec = _record("Only.mkv", sha="bb" * 32)
    _write_manifest("aaaaaaaaaaaa", [shared_rec, only_rec])  # torn down
    _write_manifest("bbbbbbbbbbbb", [shared_rec])  # live sibling keeps Shared

    deleted = _mock_drive_children(
        monkeypatch,
        [
            {"id": "d-shared", "name": shared_rec.shared_name, "size": "100"},
            {"id": "d-only", "name": only_rec.shared_name, "size": "100"},
        ],
    )

    released = DriveSharedSources.load_local_manifest("aaaaaaaaaaaa")
    result = DriveSharedSources.collect_garbage(
        released, exclude_project_id="aaaaaaaaaaaa"
    )

    assert result["deleted"] == [only_rec.shared_name]
    assert result["kept"] == [shared_rec.shared_name]
    assert deleted == ["d-only"]


def test_gc_aborts_on_unreadable_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _record("Ep.mkv")
    _write_manifest("aaaaaaaaaaaa", [rec])
    broken_dir = settings.projects_dir / "cccccccccccc"
    broken_dir.mkdir()
    (broken_dir / LOCAL_MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")

    deleted = _mock_drive_children(
        monkeypatch, [{"id": "d-1", "name": rec.shared_name, "size": "100"}]
    )

    released = DriveSharedSources.load_local_manifest("aaaaaaaaaaaa")
    result = DriveSharedSources.collect_garbage(
        released, exclude_project_id="aaaaaaaaaaaa"
    )

    assert result["deleted"] == []
    assert result["kept"] == [rec.shared_name]
    assert result["warnings"]
    assert deleted == []


def test_gc_refuses_unexpected_names(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted = _mock_drive_children(
        monkeypatch, [{"id": "d-evil", "name": "evil.txt", "size": "1"}]
    )
    released = {
        "schema_version": 1,
        "shared_files": [{"shared_name": "evil.txt"}],
    }
    result = DriveSharedSources.collect_garbage(
        released, exclude_project_id="aaaaaaaaaaaa"
    )
    assert result["deleted"] == []
    assert result["warnings"]
    assert deleted == []


# --------------------------------------------------------------------------- #
# Audit                                                                       #
# --------------------------------------------------------------------------- #


def test_audit_reports_orphans_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    referenced = _record("Ref.mkv", sha="aa" * 32)
    missing = _record("Missing.mkv", sha="bb" * 32)
    _write_manifest("aaaaaaaaaaaa", [referenced, missing])
    orphan_name = DriveSharedSources.shared_name("cc" * 32, "Orphan.mkv")

    deleted = _mock_drive_children(
        monkeypatch,
        [
            {"id": "d-ref", "name": referenced.shared_name, "size": "100"},
            {"id": "d-orphan", "name": orphan_name, "size": "100"},
            {"id": "d-human", "name": "notes.txt", "size": "1"},
        ],
    )

    report = DriveSharedSources.audit()
    assert report["orphans"] == [orphan_name]
    assert [item["shared_name"] for item in report["missing"]] == [
        missing.shared_name
    ]
    assert deleted == []

    report = DriveSharedSources.audit(apply=True)
    assert report["deleted"] == [orphan_name]
    assert deleted == ["d-orphan"]


# --------------------------------------------------------------------------- #
# ensure_uploaded                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ensure_uploaded_reuses_uploads_and_verifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    present_path = tmp_path / "Present.mkv"
    present_path.write_bytes(b"p" * 40)
    missing_path = tmp_path / "New.mkv"
    missing_path.write_bytes(b"n" * 60)
    present_entry = ManifestEntry(
        relative_path="SPM_f/sources/Present.mkv", source_path=present_path
    )
    missing_entry = ManifestEntry(
        relative_path="SPM_f/sources/New.mkv", source_path=missing_path
    )
    present_name = DriveSharedSources.shared_name("aa" * 32, "Present.mkv")
    missing_name = DriveSharedSources.shared_name("bb" * 32, "New.mkv")

    drive_children: list[dict[str, Any]] = [
        {"id": "d-present", "name": present_name, "size": "40", "md5Checksum": "m1"}
    ]
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(lambda cls, folder_id, drive=None: list(drive_children)),
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        GoogleDriveService,
        "delete_file",
        classmethod(lambda cls, file_id, drive=None: deleted.append(file_id)),
    )

    staged_batches: list[list[str]] = []

    async def _fake_copy(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        names = sorted(p.name for p in stage_dir.iterdir())
        staged_batches.append(names)
        for name in names:
            drive_children.append(
                {
                    "id": f"d-{name[:4]}",
                    "name": name,
                    "size": str((stage_dir / name).stat().st_size),
                    "md5Checksum": "m2",
                }
            )

    monkeypatch.setattr(GoogleDriveRclone, "copy_tree", classmethod(_fake_copy))

    records = await DriveSharedSources.ensure_uploaded(
        [(present_entry, "aa" * 32), (missing_entry, "bb" * 32)],
        shared_folder_id="shared-1",
    )

    # Only the missing file was staged, named by its shared name.
    assert staged_batches == [[missing_name]]
    assert deleted == []
    by_name = {record.shared_name: record for record in records}
    assert by_name[present_name].drive_file_id == "d-present"
    assert by_name[present_name].md5 == "m1"
    assert by_name[missing_name].size == 60


@pytest.mark.asyncio
async def test_ensure_uploaded_replaces_size_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "Ep.mkv"
    source.write_bytes(b"x" * 50)
    entry = ManifestEntry(relative_path="SPM_f/sources/Ep.mkv", source_path=source)
    name = DriveSharedSources.shared_name("aa" * 32, "Ep.mkv")

    drive_children: list[dict[str, Any]] = [
        {"id": "d-torn", "name": name, "size": "10", "md5Checksum": None}
    ]
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(lambda cls, folder_id, drive=None: list(drive_children)),
    )
    deleted: list[str] = []

    def _delete(cls, file_id, drive=None):
        deleted.append(file_id)
        drive_children[:] = [c for c in drive_children if c["id"] != file_id]

    monkeypatch.setattr(GoogleDriveService, "delete_file", classmethod(_delete))

    async def _fake_copy(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        for p in stage_dir.iterdir():
            drive_children.append(
                {"id": "d-new", "name": p.name, "size": str(p.stat().st_size)}
            )

    monkeypatch.setattr(GoogleDriveRclone, "copy_tree", classmethod(_fake_copy))

    records = await DriveSharedSources.ensure_uploaded(
        [(entry, "aa" * 32)], shared_folder_id="shared-1"
    )
    assert deleted == ["d-torn"]
    assert records[0].drive_file_id == "d-new"
    assert records[0].size == 50


@pytest.mark.asyncio
async def test_ensure_uploaded_fails_when_verify_finds_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "Ep.mkv"
    source.write_bytes(b"x" * 50)
    entry = ManifestEntry(relative_path="SPM_f/sources/Ep.mkv", source_path=source)

    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(lambda cls, folder_id, drive=None: []),
    )

    async def _noop_copy(cls, stage_dir: Path, *, folder_id, stats_callback=None):
        return None

    monkeypatch.setattr(GoogleDriveRclone, "copy_tree", classmethod(_noop_copy))

    with pytest.raises(RuntimeError, match="verification failed"):
        await DriveSharedSources.ensure_uploaded(
            [(entry, "aa" * 32)], shared_folder_id="shared-1"
        )


@pytest.mark.asyncio
async def test_ensure_uploaded_reports_wait_and_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    source = tmp_path / "Ep.mkv"
    source.write_bytes(b"x" * 50)
    entry = ManifestEntry(relative_path="SPM_f/sources/Ep.mkv", source_path=source)
    name = DriveSharedSources.shared_name("aa" * 32, "Ep.mkv")
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(
            lambda cls, folder_id, drive=None: [
                {"id": "d-1", "name": name, "size": "50", "md5Checksum": "m"}
            ]
        ),
    )

    # Simulate another job (export or pre-warm) uploading this very file.
    lock = dss_module._name_lock(name)
    await lock.acquire()
    events: list[str] = []

    async def _release_soon() -> None:
        await asyncio.sleep(0.05)
        lock.release()

    releaser = asyncio.create_task(_release_soon())
    records = await DriveSharedSources.ensure_uploaded(
        [(entry, "aa" * 32)],
        shared_folder_id="shared-1",
        on_wait=lambda: events.append("wait"),
        on_plan=lambda reused, missing: events.append(f"plan:{reused}/{missing}"),
    )
    await releaser

    assert events == ["wait", "plan:1/0"]
    assert records[0].drive_file_id == "d-1"
