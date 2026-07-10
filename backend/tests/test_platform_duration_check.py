from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import app.services.upload_phase as up
from app.services.upload_phase import UploadPhaseService, UploadReadiness


def _readiness(**overrides):
    base = dict(
        status="green", metadata_exists=True, drive_video_count=0,
        drive_video_id="d1", drive_video_name="final.mp4",
        drive_video_web_url=None, reasons=[], drive_folder_id="folder-1",
        drive_folder_url=None, local_video_path=None, local_video_name=None,
    )
    base.update(overrides)
    return UploadReadiness(**base)


@pytest.fixture
def check_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "_SOURCE_CACHE_DIR", tmp_path / "upload_source"
    )
    monkeypatch.setattr(UploadPhaseService, "_source_download_errors", {})
    monkeypatch.setattr(UploadPhaseService, "_source_downloads_in_flight", set())
    monkeypatch.setattr(UploadPhaseService, "_source_locks", {})
    monkeypatch.setattr(
        up.ProjectService, "load",
        classmethod(lambda cls, pid: SimpleNamespace(id=pid)),
    )
    started = []
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: started.append(pid) or {"state": "in_progress"}),
    )
    return started


def _run_check(monkeypatch, readiness, *, probe_media=None, max_duration=90.0, max_speed=1.4):
    monkeypatch.setattr(
        UploadPhaseService, "compute_readiness",
        classmethod(lambda cls, project: readiness),
    )
    return UploadPhaseService._check_platform_duration(
        "p1", None,
        cleanup_stale=lambda: None,
        is_enabled=lambda account_id: True,
        probe_media=probe_media or (lambda **kw: (None, "no probe expected")),
        max_duration=max_duration,
        max_speed=max_speed,
    )


def test_under_limit_via_drive_metadata_no_download(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: 80.0),
    )
    result = _run_check(monkeypatch, _readiness())
    assert result == {
        "needed": False, "duration_seconds": 80.0,
        "speed_factor": 1.0, "sped_up_available": False,
    }
    assert check_env == []  # no background download for short videos


def test_over_limit_via_drive_metadata_triggers_background_download(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: 117.0),
    )
    result = _run_check(monkeypatch, _readiness())
    assert result["needed"] is True
    assert result["duration_seconds"] == 117.0
    assert result["speed_factor"] == 1.3
    assert result["sped_up_available"] is True
    assert check_env == ["p1"]


def test_local_video_probed_in_place(check_env, tmp_path, monkeypatch):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"v")
    probed = []

    def probe(video_path):
        probed.append(video_path)
        return SimpleNamespace(duration_seconds=200.0), None

    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: pytest.fail("should not hit Drive")),
    )
    readiness = _readiness(
        local_video_path=str(video), local_video_name="output.mp4"
    )
    result = _run_check(monkeypatch, readiness, probe_media=probe, max_duration=180.0)
    assert probed == [video]
    assert result["needed"] is True


def test_missing_metadata_falls_back_to_download_probe(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: None),
    )
    ensured = []

    def fake_ensure(cls, project_id, readiness):
        ensured.append(project_id)
        path = cls._source_cache_dir(project_id) / "final.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(
        UploadPhaseService, "_ensure_source_video", classmethod(fake_ensure)
    )

    def probe(video_path):
        return SimpleNamespace(duration_seconds=100.0), None

    result = _run_check(monkeypatch, _readiness(), probe_media=probe)
    assert ensured == ["p1"]
    assert result["needed"] is True
    assert result["duration_seconds"] == 100.0


def test_unprobeable_fallback_raises(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService, "_ensure_source_video",
        classmethod(lambda cls, pid, r: Path("/nonexistent/final.mp4")),
    )
    with pytest.raises(ValueError, match="Unable to probe video duration"):
        _run_check(monkeypatch, _readiness(), probe_media=lambda **kw: (None, "bad file"))
