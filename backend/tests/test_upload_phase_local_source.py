from __future__ import annotations

import sys
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


def test_ensure_drive_video_passthrough_when_drive_id_present():
    readiness = _readiness(drive_video_id="d1", drive_video_name="output.mp4")
    file_id, name = UploadPhaseService._ensure_drive_video(object(), readiness)
    assert (file_id, name) == ("d1", "output.mp4")


def test_ensure_drive_video_upserts_local(tmp_path, monkeypatch):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"v")
    readiness = _readiness(local_video_path=str(video), local_video_name="output.mp4")

    import app.services.upload_phase as up
    monkeypatch.setattr(up.GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    seen = {}
    monkeypatch.setattr(
        up.GoogleDriveService, "upsert_local_file",
        classmethod(lambda cls, **kw: seen.update(kw) or {"id": "new-id"}),
    )
    file_id, name = UploadPhaseService._ensure_drive_video(object(), readiness)
    assert file_id == "new-id" and name == "output.mp4"
    assert seen["parent_id"] == "folder-1"
