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
        local_video_path=None, local_video_name=None,
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


def test_ensure_source_video_copies_local(source_cache, tmp_path):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"local-bytes")
    readiness = _readiness(
        local_video_path=str(video), local_video_name="output.mp4"
    )
    result = UploadPhaseService._ensure_source_video("p1", readiness)
    assert result.read_bytes() == b"local-bytes"
    assert result.name == "output.mp4"
    assert UploadPhaseService.cached_source_video("p1") == result


def test_ensure_source_video_downloads_from_drive(source_cache, monkeypatch):
    import app.services.upload_phase as up

    def fake_download(cls, file_id, destination):
        assert file_id == "d1"
        assert destination.name.endswith(".part")
        destination.write_bytes(b"drive-bytes")

    monkeypatch.setattr(
        up.GoogleDriveService, "download_file", classmethod(fake_download)
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
    assert UploadPhaseService.source_video_status("p1")["state"] == "ready"


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
