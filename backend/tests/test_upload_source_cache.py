from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.upload_phase import UploadPhaseService, UploadReadiness


def _readiness(**overrides):
    base = dict(
        status="green", metadata_exists=True, drive_video_count=0,
        drive_video_id=None, drive_video_name=None, drive_video_web_url=None,
        reasons=[], drive_folder_id="folder-1", drive_folder_url=None,
    )
    base.update(overrides)
    return UploadReadiness(**base)


@pytest.fixture
def source_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "_SOURCE_CACHE_DIR", tmp_path / "upload_source"
    )
    # isolate cross-test state
    monkeypatch.setattr(UploadPhaseService, "_source_download_errors", {})
    monkeypatch.setattr(UploadPhaseService, "_source_downloads_in_flight", set())
    monkeypatch.setattr(UploadPhaseService, "_source_locks", {})
    return tmp_path / "upload_source"


def test_cached_source_video_none_when_empty(source_cache):
    assert UploadPhaseService.cached_source_video("p1") is None


def test_ensure_source_video_downloads_from_drive(source_cache, monkeypatch):
    import app.services.upload_phase as up

    def fake_download(cls, file_id, destination):
        assert file_id == "d1"
        assert destination.name.endswith(".part")
        destination.write_bytes(b"drive-bytes")

    monkeypatch.setattr(
        up.GoogleDriveService, "download_file", classmethod(fake_download)
    )
    monkeypatch.setattr(
        up.GoogleDriveService, "get_file_size", classmethod(lambda cls, fid: 10000)
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    result = UploadPhaseService._ensure_source_video("p1", readiness)
    assert result.read_bytes() == b"drive-bytes"
    assert result.name == "final.mp4"
    # no leftover partial file
    assert list(result.parent.glob("*.part")) == []


def test_ensure_source_video_reuses_cache(source_cache, monkeypatch):
    import app.services.upload_phase as up

    calls = []
    monkeypatch.setattr(
        up.GoogleDriveService,
        "download_file",
        classmethod(lambda cls, fid, dest: calls.append(fid) or dest.write_bytes(b"x")),
    )
    monkeypatch.setattr(
        up.GoogleDriveService, "get_file_size", classmethod(lambda cls, fid: 10000)
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    UploadPhaseService._ensure_source_video("p1", readiness)
    UploadPhaseService._ensure_source_video("p1", readiness)
    assert calls == ["d1"]


def test_partial_download_is_not_ready(source_cache):
    partial_dir = source_cache / "p1"
    partial_dir.mkdir(parents=True)
    (partial_dir / "final.mp4.part").write_bytes(b"incomplete")
    assert UploadPhaseService.cached_source_video("p1") is None
    assert UploadPhaseService.source_video_status("p1")["state"] == "missing"


def test_status_ready_when_cached(source_cache):
    cache_dir = source_cache / "p1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "final.mp4").write_bytes(b"x")
    status = UploadPhaseService.source_video_status("p1")
    assert status["state"] == "ready"
    assert status["version"].endswith("-1")


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_start_download_background_success(source_cache, monkeypatch):
    import app.services.upload_phase as up

    monkeypatch.setattr(
        up.GoogleDriveService,
        "download_file",
        classmethod(lambda cls, fid, dest: dest.write_bytes(b"bg")),
    )
    monkeypatch.setattr(
        up.GoogleDriveService, "get_file_size", classmethod(lambda cls, fid: 10000)
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    status = UploadPhaseService.start_source_video_download("p1", readiness)
    assert status["state"] in ("in_progress", "ready")
    assert _wait_until(
        lambda: UploadPhaseService.source_video_status("p1")["state"] == "ready"
    )


def test_start_download_background_error(source_cache, monkeypatch):
    import app.services.upload_phase as up

    def boom(cls, fid, dest):
        raise RuntimeError("drive down")

    monkeypatch.setattr(up.GoogleDriveService, "download_file", classmethod(boom))
    monkeypatch.setattr(
        up.GoogleDriveService, "get_file_size", classmethod(lambda cls, fid: None)
    )
    readiness = _readiness(drive_video_id="d1", drive_video_name="final.mp4")
    UploadPhaseService.start_source_video_download("p1", readiness)
    assert _wait_until(
        lambda: UploadPhaseService.source_video_status("p1")["state"] == "error"
    )
    assert "drive down" in UploadPhaseService.source_video_status("p1")["detail"]


def test_start_download_short_circuits_when_ready(source_cache):
    cache_dir = source_cache / "p1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "final.mp4").write_bytes(b"x")
    status = UploadPhaseService.start_source_video_download("p1", _readiness())
    assert status["state"] == "ready"


class _FakeFilesRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeFiles:
    def __init__(self, result):
        self._result = result

    def get(self, fileId, fields, supportsAllDrives):
        return _FakeFilesRequest(self._result)


class _FakeDriveClient:
    def __init__(self, result):
        self._result = result

    def files(self):
        return _FakeFiles(self._result)


def test_get_file_size_success(monkeypatch):
    from app.services.google_drive_service import GoogleDriveService

    monkeypatch.setattr(
        GoogleDriveService,
        "_client",
        classmethod(lambda cls: _FakeDriveClient({"size": "123"})),
    )
    assert GoogleDriveService.get_file_size("f1") == 123


def test_get_file_size_missing_key(monkeypatch):
    from app.services.google_drive_service import GoogleDriveService

    monkeypatch.setattr(
        GoogleDriveService,
        "_client",
        classmethod(lambda cls: _FakeDriveClient({})),
    )
    assert GoogleDriveService.get_file_size("f1") is None


def test_get_file_size_on_exception(monkeypatch):
    from app.services.google_drive_service import GoogleDriveService

    monkeypatch.setattr(
        GoogleDriveService,
        "_client",
        classmethod(lambda cls: _FakeDriveClient(RuntimeError("boom"))),
    )
    assert GoogleDriveService.get_file_size("f1") is None


def test_source_status_reports_bytes_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(UploadPhaseService, "_SOURCE_CACHE_DIR", tmp_path)
    cache_dir = tmp_path / "p1"
    cache_dir.mkdir()
    (cache_dir / "video.mp4.part").write_bytes(b"\x00" * 1234)
    with UploadPhaseService._source_download_guard:
        UploadPhaseService._source_downloads_in_flight.add("p1")
        UploadPhaseService._source_download_totals["p1"] = 10000
    try:
        status = UploadPhaseService.source_video_status("p1")
    finally:
        with UploadPhaseService._source_download_guard:
            UploadPhaseService._source_downloads_in_flight.discard("p1")
            UploadPhaseService._source_download_totals.pop("p1", None)
    assert status["state"] == "in_progress"
    assert status["bytes_done"] == 1234
    assert status["bytes_total"] == 10000
