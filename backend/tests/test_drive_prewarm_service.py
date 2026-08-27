"""Script-phase pre-warm of the shared Drive folder.

Fakes: Drive listing/copy/delete, the sha256 service and the export's source
collection. Everything else (project files, manifests, per-name locks,
the FIFO slot) is real.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import Project, ProjectPhase
from app.services import drive_shared_sources as dss_module
from app.services.drive_prewarm_service import DrivePrewarmService, PREWARMED_STATUS
from app.services.drive_shared_sources import DriveSharedSources, SharedFileRecord
from app.services.export_service import ManifestEntry
from app.services.google_drive_rclone import GoogleDriveRclone
from app.services.google_drive_service import GoogleDriveService
from app.services.project_service import ProjectService
from app.services.source_hash_service import SourceHashService


class FakeDrive:
    """In-memory shared folder with a gate to hold an upload in flight."""

    def __init__(self) -> None:
        self.children: list[dict[str, Any]] = []
        self.batches: list[list[str]] = []
        self.gate: asyncio.Event | None = None
        # When set, only batches touching one of these names block on the gate.
        self.gate_names: set[str] | None = None
        self.in_flight = asyncio.Event()
        self.deleted: list[str] = []

    def list_children(self, folder_id: str, drive=None) -> list[dict[str, Any]]:
        return [dict(child) for child in self.children]

    async def copy_tree(self, stage_dir: Path, *, folder_id: str, stats_callback=None):
        names = sorted(p.name for p in stage_dir.iterdir())
        self.batches.append(names)
        gated = self.gate is not None and (
            self.gate_names is None or bool(set(names) & self.gate_names)
        )
        if gated:
            self.in_flight.set()
            await self.gate.wait()
        for name in names:
            self.children.append(
                {
                    "id": f"d-{len(self.children)}",
                    "name": name,
                    "size": str((stage_dir / name).stat().st_size),
                    "md5Checksum": "m",
                }
            )


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDrive:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(settings, "projects_dir", projects_dir)
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    monkeypatch.setattr(settings, "drive_shared_sources_enabled", True)
    monkeypatch.setattr(settings, "drive_prewarm_enabled", True)
    monkeypatch.setattr(settings, "drive_shared_sources_min_bytes", 10)
    monkeypatch.setattr(dss_module, "_shared_folder_id_cache", None)
    monkeypatch.setattr(dss_module, "_shared_name_locks", {})
    monkeypatch.setattr(DrivePrewarmService, "_states", {})
    monkeypatch.setattr(DrivePrewarmService, "_tasks", {})
    monkeypatch.setattr(DrivePrewarmService, "_done_signatures", {})
    monkeypatch.setattr(DrivePrewarmService, "_slot", None)

    fake = FakeDrive()
    monkeypatch.setattr(GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(
        GoogleDriveService,
        "ensure_child_folder_id",
        classmethod(lambda cls, name, parent_id=None, drive=None: "shared-1"),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_with_hashes",
        classmethod(lambda cls, folder_id, drive=None: fake.list_children(folder_id)),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "delete_file",
        classmethod(lambda cls, file_id, drive=None: fake.deleted.append(file_id)),
    )
    monkeypatch.setattr(
        GoogleDriveRclone,
        "copy_tree",
        classmethod(lambda cls, stage_dir, *, folder_id, stats_callback=None: fake.copy_tree(
            stage_dir, folder_id=folder_id, stats_callback=stats_callback
        )),
    )
    # sha256 = derived from the file name so identical episodes share a name.
    monkeypatch.setattr(
        SourceHashService,
        "sha256_for_many",
        classmethod(lambda cls, paths: {Path(p): _sha(Path(p).name) for p in paths}),
    )
    return fake


def _sha(basename: str) -> str:
    return (basename.encode("utf-8").hex() * 8)[:64]


def _shared(basename: str) -> str:
    return DriveSharedSources.shared_name(_sha(basename), basename)


class _Matches:
    def __init__(self) -> None:
        self.matches = [object()]


def _make_project(monkeypatch: pytest.MonkeyPatch, sources: dict[str, list[Path]], project_id: str) -> Project:
    project = Project(id=project_id, anime_name="Test Anime", phase=ProjectPhase.SCRIPT_RESTRUCTURE)
    ProjectService.save(project)
    ProjectService.get_matches_file(project.id).write_text('{"matches": []}')
    sources.setdefault(project.id, [])
    return project


@pytest.fixture
def episodes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Route collect_entries through a per-project map of source files."""
    sources: dict[str, list[Path]] = {}

    def _collect(cls, project: Project, matches) -> list[ManifestEntry]:
        return [
            ManifestEntry(relative_path=f"SPM_x/sources/{path.name}", source_path=path)
            for path in sources.get(project.id, [])
        ]

    monkeypatch.setattr(DrivePrewarmService, "collect_entries", classmethod(_collect))
    monkeypatch.setattr(ProjectService, "aload_matches", classmethod(lambda cls, pid: _async(_Matches())))

    lib = tmp_path / "library"
    lib.mkdir()

    def _episode(name: str, size: int = 64) -> Path:
        path = lib / name
        if not path.exists():
            path.write_bytes(b"e" * size)
        return path

    return sources, _episode


async def _async(value):
    return value


def _manifest_names(project_id: str) -> set[str]:
    manifest = DriveSharedSources.load_local_manifest(project_id) or {}
    return {item["shared_name"] for item in manifest.get("shared_files", [])}


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_second_project_with_same_episode_waits_then_reuses(
    drive: FakeDrive, episodes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, episode = episodes
    ep1, ep2 = episode("Ep01.mkv"), episode("Ep02.mkv")
    a = _make_project(monkeypatch, sources, "aaaaaaaaaaaa")
    b = _make_project(monkeypatch, sources, "bbbbbbbbbbbb")
    sources[a.id] = [ep1]
    sources[b.id] = [ep1, ep2]

    drive.gate = asyncio.Event()
    assert DrivePrewarmService.schedule(a.id, reason="t") is not None
    assert DrivePrewarmService.schedule(b.id, reason="t") is not None
    await asyncio.wait_for(drive.in_flight.wait(), 5)

    # A is uploading Ep01; B is behind it in the FIFO, nothing uploaded twice.
    assert DrivePrewarmService.status(a.id)["status"] == "uploading"
    assert DrivePrewarmService.status(b.id)["status"] == "queued"
    await asyncio.sleep(0.05)
    assert drive.batches == [[_shared("Ep01.mkv")]]

    drive.gate.set()
    await DrivePrewarmService.wait(a.id)
    await DrivePrewarmService.wait(b.id)

    assert drive.batches == [[_shared("Ep01.mkv")], [_shared("Ep02.mkv")]]
    assert DrivePrewarmService.status(a.id)["status"] == "done"
    assert DrivePrewarmService.status(b.id)["status"] == "done"
    assert DrivePrewarmService.status(b.id)["reused_files"] == 1
    assert DrivePrewarmService.status(b.id)["uploaded_files"] == 1
    assert _manifest_names(a.id) == {_shared("Ep01.mkv")}
    assert _manifest_names(b.id) == {_shared("Ep01.mkv"), _shared("Ep02.mkv")}
    assert (DriveSharedSources.load_local_manifest(b.id) or {})["status"] == PREWARMED_STATUS


@pytest.mark.asyncio
async def test_export_overlapping_a_running_prewarm_waits_on_that_file_only(
    drive: FakeDrive, episodes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, episode = episodes
    ep1, ep3 = episode("Ep01.mkv"), episode("Ep03.mkv")
    a = _make_project(monkeypatch, sources, "aaaaaaaaaaaa")
    sources[a.id] = [ep1]

    drive.gate = asyncio.Event()
    drive.gate_names = {_shared("Ep01.mkv")}
    DrivePrewarmService.schedule(a.id, reason="t")
    await asyncio.wait_for(drive.in_flight.wait(), 5)

    # An export of another project needing Ep01 (in flight) + Ep03 (new).
    events: list[str] = []
    export = asyncio.create_task(
        DriveSharedSources.ensure_uploaded(
            [
                (ManifestEntry(relative_path="SPM_c/sources/Ep01.mkv", source_path=ep1), _sha("Ep01.mkv")),
                (ManifestEntry(relative_path="SPM_c/sources/Ep03.mkv", source_path=ep3), _sha("Ep03.mkv")),
            ],
            shared_folder_id="shared-1",
            on_wait=lambda: events.append("wait"),
            on_plan=lambda reused, missing: events.append(f"plan:{reused}/{missing}"),
        )
    )
    await asyncio.sleep(0.05)
    assert not export.done()
    assert events == ["wait"]
    assert drive.batches == [[_shared("Ep01.mkv")]]

    # A pre-warm with a disjoint file set is NOT blocked by A.
    other = asyncio.create_task(
        DriveSharedSources.ensure_uploaded(
            [(ManifestEntry(relative_path="SPM_d/sources/Ep09.mkv", source_path=episode("Ep09.mkv")), _sha("Ep09.mkv"))],
            shared_folder_id="shared-1",
        )
    )
    await asyncio.wait_for(other, 5)

    drive.gate.set()
    records = await asyncio.wait_for(export, 5)
    await DrivePrewarmService.wait(a.id)

    assert events == ["wait", "plan:1/1"]
    assert sorted(drive.batches) == [[_shared("Ep01.mkv")], [_shared("Ep03.mkv")], [_shared("Ep09.mkv")]]
    assert {r.shared_name for r in records} == {_shared("Ep01.mkv"), _shared("Ep03.mkv")}


@pytest.mark.asyncio
async def test_schedule_is_idempotent_and_debounced_on_matches(
    drive: FakeDrive, episodes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, episode = episodes
    a = _make_project(monkeypatch, sources, "aaaaaaaaaaaa")
    sources[a.id] = [episode("Ep01.mkv")]

    drive.gate = asyncio.Event()
    first = DrivePrewarmService.schedule(a.id, reason="t")
    again = DrivePrewarmService.schedule(a.id, reason="t")
    assert again is first  # still live → same state, no second task
    await asyncio.wait_for(drive.in_flight.wait(), 5)
    drive.gate.set()
    await DrivePrewarmService.wait(a.id)
    assert drive.batches == [[_shared("Ep01.mkv")]]

    DrivePrewarmService.schedule(a.id, reason="t")
    await DrivePrewarmService.wait(a.id)
    assert DrivePrewarmService.status(a.id)["status"] == "skipped"
    assert "already pre-warmed" in DrivePrewarmService.status(a.id)["detail"]

    # Matches changed → runs again (and reuses the present file).
    ProjectService.get_matches_file(a.id).write_text('{"matches": [1]}')
    DrivePrewarmService.schedule(a.id, reason="t")
    await DrivePrewarmService.wait(a.id)
    assert DrivePrewarmService.status(a.id)["status"] == "done"
    assert DrivePrewarmService.status(a.id)["reused_files"] == 1
    assert drive.batches == [[_shared("Ep01.mkv")]]


@pytest.mark.asyncio
async def test_disabled_flags_schedule_nothing(drive: FakeDrive, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "drive_prewarm_enabled", False)
    assert DrivePrewarmService.schedule("aaaaaaaaaaaa", reason="t") is None
    monkeypatch.setattr(settings, "drive_prewarm_enabled", True)
    monkeypatch.setattr(settings, "drive_shared_sources_enabled", False)
    assert DrivePrewarmService.schedule("aaaaaaaaaaaa", reason="t") is None
    assert DrivePrewarmService.status("aaaaaaaaaaaa")["status"] == "idle"


@pytest.mark.asyncio
async def test_project_deleted_mid_upload_releases_references_without_resurrecting_dir(
    drive: FakeDrive, episodes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, episode = episodes
    a = _make_project(monkeypatch, sources, "aaaaaaaaaaaa")
    sources[a.id] = [episode("Ep01.mkv")]
    gc_calls: list[tuple[set[str], str]] = []
    monkeypatch.setattr(
        DriveSharedSources,
        "collect_garbage",
        classmethod(
            lambda cls, released, *, exclude_project_id: (
                gc_calls.append(({i["shared_name"] for i in released["shared_files"]}, exclude_project_id))
                or {"deleted": [], "kept": []}
            )
        ),
    )

    drive.gate = asyncio.Event()
    DrivePrewarmService.schedule(a.id, reason="t")
    await asyncio.wait_for(drive.in_flight.wait(), 5)
    # Pending manifest is on disk before the shared write (GC race guard).
    assert (DriveSharedSources.load_local_manifest(a.id) or {})["status"] == "pending"

    shutil.rmtree(ProjectService.get_project_dir(a.id))
    drive.gate.set()
    await DrivePrewarmService.wait(a.id)

    assert DrivePrewarmService.status(a.id)["status"] == "cancelled"
    assert not ProjectService.get_project_dir(a.id).exists()
    assert gc_calls == [({_shared("Ep01.mkv")}, a.id)]


@pytest.mark.asyncio
async def test_request_cancel_stops_a_running_upload(
    drive: FakeDrive, episodes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, episode = episodes
    a = _make_project(monkeypatch, sources, "aaaaaaaaaaaa")
    sources[a.id] = [episode("Ep01.mkv")]

    drive.gate = asyncio.Event()
    DrivePrewarmService.schedule(a.id, reason="t")
    await asyncio.wait_for(drive.in_flight.wait(), 5)
    assert DrivePrewarmService.request_cancel(a.id) is True
    await DrivePrewarmService.wait(a.id)

    assert DrivePrewarmService.status(a.id)["status"] == "cancelled"
    assert drive.children == []  # the copy never completed
    # Name lock was released: a new upload of the same file proceeds.
    assert DriveSharedSources.names_in_flight([_shared("Ep01.mkv")]) == []
    assert DrivePrewarmService.request_cancel(a.id) is False


def test_manifest_merge_keeps_export_status_and_records(drive: FakeDrive) -> None:
    project_id = "aaaaaaaaaaaa"
    ProjectService.get_project_dir(project_id).mkdir()
    exported = SharedFileRecord(
        path_in_folder="sources/Music.mp3", size=30, sha256="cc" * 32, md5="m",
        drive_file_id="d-music", shared_name=DriveSharedSources.shared_name("cc" * 32, "Music.mp3"),
    )
    DriveSharedSources.persist_local_manifest(
        project_id, status="uploaded", records=[exported], drive_folder_id="folder-1"
    )
    new = SharedFileRecord(
        path_in_folder="sources/Ep01.mkv", size=64, sha256=_sha("Ep01.mkv"), md5=None,
        drive_file_id="", shared_name=_shared("Ep01.mkv"),
    )
    assert DrivePrewarmService._persist_manifest(project_id, [new], PREWARMED_STATUS) is True

    manifest = DriveSharedSources.load_local_manifest(project_id) or {}
    assert manifest["status"] == "uploaded"
    assert manifest["drive_folder_id"] == "folder-1"
    assert {i["shared_name"] for i in manifest["shared_files"]} == {exported.shared_name, new.shared_name}
    # Gone project → no write, no resurrected dir.
    shutil.rmtree(ProjectService.get_project_dir(project_id))
    assert DrivePrewarmService._persist_manifest(project_id, [new], PREWARMED_STATUS) is False
    assert not ProjectService.get_project_dir(project_id).exists()


@pytest.mark.asyncio
async def test_resume_pending_targets_script_and_processing_phases_only(
    drive: FakeDrive, episodes, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, episode = episodes
    script = _make_project(monkeypatch, sources, "aaaaaaaaaaaa")
    processing = _make_project(monkeypatch, sources, "bbbbbbbbbbbb")
    processing.phase = ProjectPhase.PROCESSING
    ProjectService.save(processing)
    matching = _make_project(monkeypatch, sources, "cccccccccccc")
    matching.phase = ProjectPhase.MATCH_VALIDATION
    ProjectService.save(matching)
    warmed = _make_project(monkeypatch, sources, "dddddddddddd")
    DriveSharedSources.persist_local_manifest(warmed.id, status=PREWARMED_STATUS, records=[])
    stale = _make_project(monkeypatch, sources, "eeeeeeeeeeee")
    # save() re-stamps updated_at, so age the stored record directly.
    stale_file = ProjectService.get_project_dir(stale.id) / "project.json"
    aged = json.loads(stale_file.read_text())
    aged["updated_at"] = (datetime.now() - timedelta(days=60)).isoformat()
    aged["created_at"] = aged["updated_at"]
    stale_file.write_text(json.dumps(aged))
    for p in (script, processing, matching, warmed, stale):
        sources[p.id] = [episode("Ep01.mkv")]

    scheduled = await DrivePrewarmService.resume_pending(delay_seconds=0)
    assert scheduled == 2
    for pid in (script.id, processing.id):
        await DrivePrewarmService.wait(pid)
        assert DrivePrewarmService.status(pid)["status"] == "done"
    assert DrivePrewarmService.status(matching.id)["status"] == "idle"
    assert DrivePrewarmService.status(warmed.id)["status"] == "idle"
    assert DrivePrewarmService.status(stale.id)["status"] == "idle"  # older than the window
    assert drive.batches == [[_shared("Ep01.mkv")]]
