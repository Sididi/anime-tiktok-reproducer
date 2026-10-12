import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock

import httplib2
from googleapiclient.errors import HttpError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.google_drive_service import GoogleDriveService


def fake_drive(*, errors=None, transport_error=None):
    batches = []
    retried = []

    class Request:
        def __init__(self, file_id):
            self.file_id = file_id

        def execute(self):
            retried.append(self.file_id)
            return {"id": "copy-" + self.file_id}

    class Batch:
        def __init__(self, callback):
            self.callback = callback
            self.parts = []
            batches.append(self)

        def add(self, request, request_id):
            self.parts.append((request, request_id))

        def execute(self):
            if transport_error:
                raise transport_error
            for request, request_id in self.parts:
                self.callback(request_id, {"id": "copy-" + request.file_id}, (errors or {}).get(request.file_id))

    drive = MagicMock()
    drive.files.return_value.copy.side_effect = lambda fileId, **kw: Request(fileId)
    drive.new_batch_http_request.side_effect = lambda callback: Batch(callback)
    return drive, batches, retried


def test_archive_copy_batches_respect_limit_and_check_every_part():
    drive, batches, retried = fake_drive()
    copies = [(str(i), "file", "destination") for i in range(207)]
    GoogleDriveService._execute_archive_copies(copies, drive=drive)
    assert [len(batch.parts) for batch in batches] == [100, 100, 7]
    assert retried == []


def test_archive_copy_retries_only_explicitly_failed_parts():
    error = HttpError(httplib2.Response({"status": "429"}), b'{"error":{"message":"rate limited"}}')
    drive, batches, retried = fake_drive(errors={"bad": error})
    GoogleDriveService._execute_archive_copies([("ok", "a", "d"), ("bad", "b", "d")], drive=drive)
    assert len(batches) == 1
    assert retried == ["bad"]


def test_archive_copy_partial_failure_is_not_reported_as_success():
    error = HttpError(httplib2.Response({"status": "403"}), b'{"error":{"message":"forbidden"}}')
    drive, _, retried = fake_drive(errors={"bad": error})
    with pytest.raises(HttpError):
        GoogleDriveService._execute_archive_copies([("ok", "a", "d"), ("bad", "b", "d")], drive=drive)
    assert retried == []


def test_archive_transport_failure_does_not_blindly_repeat_copies():
    drive, batches, retried = fake_drive(transport_error=TimeoutError("lost batch response"))
    with pytest.raises(TimeoutError):
        GoogleDriveService._execute_archive_copies([("file", "a", "d")], drive=drive)
    assert len(batches) == 1
    assert retried == []


def test_archive_root_creation_is_shared_by_concurrent_projects(monkeypatch):
    monkeypatch.setattr(GoogleDriveService, "_archive_root_cache", None)
    calls = []

    def ensure(*args, **kwargs):
        calls.append(1)
        time.sleep(0.03)
        return {"id": "archive-root"}

    monkeypatch.setattr(GoogleDriveService, "_ensure_child_folder", ensure)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: GoogleDriveService._archive_root("parent", drive=object()), range(12)))
    assert calls == [1]
    assert all(result["id"] == "archive-root" for result in results)


@pytest.mark.asyncio
async def test_delete_route_limits_concurrency_and_releases_failed_slots(monkeypatch):
    from app.api.routes import project_manager as routes

    monkeypatch.setattr(routes, "_delete_slots", asyncio.Semaphore(3))
    running = 0
    peak = 0

    async def delete(project_id, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.02)
            if project_id == "fail":
                raise ValueError("failed")
            return {"status": "deleted"}
        finally:
            running -= 1

    monkeypatch.setattr(routes, "_delete_managed_project", delete)
    results = await asyncio.gather(*(routes.delete_managed_project(id) for id in ["fail", *map(str, range(8))]), return_exceptions=True)
    assert peak == 3
    assert running == 0
    assert sum(isinstance(result, ValueError) for result in results) == 1


def test_shared_gc_and_manifest_removal_are_one_serial_section(monkeypatch):
    from app.models import Project
    from app.services import upload_phase as module

    calls = []
    monkeypatch.setattr(module.ProjectService, "load", lambda pid: Project(id=pid))
    monkeypatch.setattr(module.GoogleDriveService, "is_configured", lambda: True)
    monkeypatch.setattr("app.services.cep_link_service.CepLinkService.delete_launch", lambda pid: None)
    monkeypatch.setattr(module.DrivePrewarmService, "request_cancel", lambda pid: None)
    monkeypatch.setattr(module.DriveSharedSources, "load_local_manifest", lambda pid: {"project_id": pid})

    def gc(manifest, **kwargs):
        calls.append((manifest["project_id"], "gc"))
        time.sleep(0.03)
        return {}

    def archive(project, **kwargs):
        calls.append((project.id, "archive"))
        return True

    monkeypatch.setattr(module.DriveSharedSources, "collect_garbage", gc)
    monkeypatch.setattr(module.ProjectService, "archive", archive)
    monkeypatch.setattr(module.UploadPhaseService, "cleanup_project_caches", lambda pid: None)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(module.UploadPhaseService.managed_delete, ["a", "b"]))
    assert [stage for pid, stage in calls] == ["gc", "archive", "gc", "archive"]
    assert calls[0][0] == calls[1][0]
    assert calls[2][0] == calls[3][0]
