"""Detached Drive export job + the /exports/gdrive SSE route on top of it.

The upload must survive the browser giving up (stall watchdog, closed tab):
completion work runs regardless, and a retry attaches to the running job.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import processing as processing_routes
from app.models import Project, ProjectPhase
from app.services.drive_export_job import DriveExportJobs
from app.services.export_service import ExportService
from app.services.project_service import ProjectService


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(DriveExportJobs, "_jobs", {})
    yield


@pytest.mark.asyncio
async def test_completion_runs_without_any_subscriber() -> None:
    gate = asyncio.Event()
    completed: list[dict[str, Any]] = []

    async def _runner(progress):
        progress({"phase": "manifest", "message": "m1"})
        await gate.wait()
        progress({"phase": "upload", "message": "u1"})
        return {"folder_url": "u", "folder_id": "f"}

    job, attached = DriveExportJobs.start_or_attach("p1", runner=_runner, on_complete=completed.append)
    assert attached is False
    await asyncio.sleep(0.01)
    # The browser went away; nobody is attached. The job still completes.
    assert DriveExportJobs.running("p1") is job
    gate.set()
    await DriveExportJobs.wait("p1")
    assert job.result == {"folder_url": "u", "folder_id": "f"}
    assert completed == [{"folder_url": "u", "folder_id": "f"}]
    assert DriveExportJobs.running("p1") is None


@pytest.mark.asyncio
async def test_late_attacher_gets_latest_frame_then_live_frames() -> None:
    gate = asyncio.Event()

    async def _runner(progress):
        progress({"phase": "manifest", "message": "m1"})
        progress({"phase": "upload", "message": "u1"})
        await gate.wait()
        progress({"phase": "persist", "message": "p1"})
        return {"folder_url": "u", "folder_id": "f"}

    job, _ = DriveExportJobs.start_or_attach("p1", runner=_runner)
    await asyncio.sleep(0.01)
    same, attached = DriveExportJobs.start_or_attach("p1", runner=_runner)
    assert same is job and attached is True

    seen: list[str] = []

    async def _consume() -> None:
        async for frame in job.frames():
            seen.append(frame["message"])

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.01)
    assert seen == ["u1"]  # only the latest frame is replayed
    gate.set()
    await asyncio.wait_for(consumer, 2)
    assert seen == ["u1", "p1"]
    assert job.finished and job.error is None


@pytest.mark.asyncio
async def test_failure_is_reported_and_registry_released() -> None:
    async def _runner(progress):
        progress({"phase": "manifest", "message": "m1"})
        raise RuntimeError("boom")

    job, _ = DriveExportJobs.start_or_attach("p1", runner=_runner)
    frames = [frame async for frame in job.frames()]
    assert [f["message"] for f in frames] == ["m1"]
    assert isinstance(job.error, RuntimeError)
    assert DriveExportJobs.running("p1") is None
    # A fresh start after a failure creates a new job.
    again, attached = DriveExportJobs.start_or_attach("p1", runner=_runner)
    assert again is not job and attached is False
    await DriveExportJobs.wait("p1")


# --------------------------------------------------------------------------- #
# Route                                                                        #
# --------------------------------------------------------------------------- #


class _Matches:
    matches = [object()]


async def _frames(response) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        for line in text.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


@pytest.fixture
def route_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    project = Project(id="p1", anime_name="A", phase=ProjectPhase.PROCESSING)

    async def _aload(cls, project_id):
        return project if project_id == "p1" else None

    async def _aload_matches(cls, project_id):
        return _Matches()

    saved: list[Project] = []
    monkeypatch.setattr(ProjectService, "aload", classmethod(_aload))
    monkeypatch.setattr(ProjectService, "aload_matches", classmethod(_aload_matches))
    monkeypatch.setattr(ProjectService, "save", classmethod(lambda cls, p: saved.append(p)))
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        processing_routes,
        "_notify_drive_upload_complete",
        lambda project_id, folder_url: notified.append((project_id, folder_url)),
    )
    gate = asyncio.Event()

    async def _upload(cls, project, matches, *, progress_callback=None):
        progress_callback({"phase": "manifest", "message": "Preparing Drive manifest (3 files, 1.0 KB)", "file_count": 3, "total_bytes": 1000})
        await gate.wait()
        progress_callback({"phase": "persist", "message": "Finishing upload metadata"})
        return {"folder_id": "fid", "folder_url": "https://drive/fid", "file_count": 3, "total_bytes": 1000}

    monkeypatch.setattr(ExportService, "upload_manifest_to_drive", classmethod(_upload))
    return project, gate, saved, notified


@pytest.mark.asyncio
async def test_route_streams_and_completes(route_env) -> None:
    project, gate, saved, notified = route_env
    response = await processing_routes.upload_to_gdrive("p1")
    gate.set()
    frames = await _frames(response)
    assert frames[0]["message"] == "Preparing Drive upload..."
    assert [f.get("phase") for f in frames[1:]] == ["manifest", "persist", "complete"]
    assert frames[-1]["status"] == "complete"
    assert frames[-1]["folder_url"] == "https://drive/fid"
    await DriveExportJobs.wait("p1")
    assert project.drive_folder_id == "fid" and project.drive_export_uploaded_once is True
    assert saved == [project]
    assert notified == [("p1", "https://drive/fid")]


@pytest.mark.asyncio
async def test_route_retry_attaches_and_orphaned_upload_still_notifies(route_env) -> None:
    project, gate, saved, notified = route_env
    first = await processing_routes.upload_to_gdrive("p1")
    # The browser reads the first frames, then its stall watchdog aborts.
    iterator = first.body_iterator
    await iterator.__anext__()
    await iterator.__anext__()
    await iterator.aclose()
    await asyncio.sleep(0.01)
    assert DriveExportJobs.running("p1") is not None

    # "Please retry" → the new stream attaches to the same job and catches up.
    second = await processing_routes.upload_to_gdrive("p1")
    gate.set()
    frames = await _frames(second)
    assert frames[0]["message"] == "Reconnected to the running Drive upload..."
    assert frames[1]["phase"] == "manifest"  # replayed latest frame
    assert frames[-1]["status"] == "complete"
    await DriveExportJobs.wait("p1")
    assert notified == [("p1", "https://drive/fid")]  # exactly once
    assert saved == [project]


@pytest.mark.asyncio
async def test_route_reports_failure(route_env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(cls, project, matches, *, progress_callback=None):
        raise RuntimeError("rclone drive sync exited with code 1")

    monkeypatch.setattr(ExportService, "upload_manifest_to_drive", classmethod(_boom))
    response = await processing_routes.upload_to_gdrive("p1")
    frames = await _frames(response)
    assert frames[-1]["status"] == "error"
    assert "exited with code 1" in frames[-1]["error"]
    assert DriveExportJobs.running("p1") is None
