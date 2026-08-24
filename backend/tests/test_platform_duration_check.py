from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import app.services.upload_phase as up
from app.services.google_drive_service import DriveVideoMetadataLookupError
from app.services.upload_phase import (
    UploadPhaseService,
    UploadPreflightUnavailableError,
    UploadReadiness,
)


def _readiness(**overrides):
    base = dict(
        status="green", metadata_exists=True, drive_video_count=0,
        drive_video_id="d1", drive_video_name="final.mp4",
        drive_video_web_url=None, reasons=[], drive_folder_id="folder-1",
        drive_folder_url=None,
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
        UploadPhaseService, "_compute_preflight_readiness",
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
        "max_duration_seconds": 90.0,
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


def test_preflight_reuses_recent_manager_drive_result(tmp_path, monkeypatch):
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}")
    project = SimpleNamespace(
        id="p1",
        drive_folder_id="folder-1",
        drive_folder_url="https://drive.example/folder-1",
        upload_last_result=None,
    )
    monkeypatch.setattr(UploadPhaseService, "_drive_video_cache", {})
    UploadPhaseService._cache_drive_video(
        project_id="p1",
        folder_id="folder-1",
        folder_url=project.drive_folder_url,
        video_files=[{"id": "d1", "name": "final.mp4"}],
    )
    monkeypatch.setattr(
        up.ProjectService,
        "get_metadata_file",
        classmethod(lambda cls, project_id: metadata),
    )
    monkeypatch.setattr(
        UploadPhaseService,
        "compute_readiness",
        classmethod(lambda cls, p: pytest.fail("must not query Drive again")),
    )

    readiness = UploadPhaseService._compute_preflight_readiness(project)

    assert readiness.status == "green"
    assert readiness.drive_video_id == "d1"


def test_unindexed_export_falls_back_to_container_header(check_env, monkeypatch):
    """A just-uploaded export has no Drive videoMediaMetadata for a while; the
    MP4 header still knows the duration, so the check must not fail."""
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: None),
    )
    monkeypatch.setattr(
        up.GoogleDriveService, "probe_video_duration_from_header",
        classmethod(lambda cls, fid: 142.266),
    )
    monkeypatch.setattr(
        UploadPhaseService, "_ensure_source_video",
        classmethod(lambda cls, pid, r: pytest.fail("must not download")),
    )

    result = _run_check(monkeypatch, _readiness(), max_duration=180.0)

    assert result["needed"] is False
    assert result["duration_seconds"] == 142.27


def test_missing_metadata_fails_fast_without_downloading(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(lambda cls, fid: None),
    )
    monkeypatch.setattr(
        up.GoogleDriveService, "probe_video_duration_from_header",
        classmethod(lambda cls, fid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService,
        "_ensure_source_video",
        classmethod(
            lambda cls, project_id, readiness: pytest.fail("must not download")
        ),
    )

    with pytest.raises(UploadPreflightUnavailableError, match="still processing"):
        _run_check(monkeypatch, _readiness())


def test_drive_lookup_failure_fails_fast_without_downloading(check_env, monkeypatch):
    monkeypatch.setattr(
        up.GoogleDriveService, "get_video_duration_seconds",
        classmethod(
            lambda cls, fid: (_ for _ in ()).throw(
                DriveVideoMetadataLookupError("network unavailable")
            )
        ),
    )
    monkeypatch.setattr(
        UploadPhaseService, "_ensure_source_video",
        classmethod(lambda cls, pid, r: pytest.fail("must not download")),
    )
    with pytest.raises(UploadPreflightUnavailableError, match="could not be reached"):
        _run_check(monkeypatch, _readiness())


def test_instagram_operational_limit_is_clamped_to_three_minutes(monkeypatch):
    account = SimpleNamespace(
        max_reel_duration_for=lambda platform: 900 if platform == "instagram" else 14400
    )
    monkeypatch.setattr(
        up.AccountService,
        "get_account",
        classmethod(lambda cls, account_id: account),
    )
    assert UploadPhaseService._account_reel_limit("a1", "instagram") == 180.0
    assert UploadPhaseService._account_reel_limit("a1", "facebook") == 14400.0
