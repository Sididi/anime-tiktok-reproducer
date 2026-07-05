# backend/tests/test_upload_readiness_local_first.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.lan_transfer_service import LanTransferService


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    out = tmp_path / "p1" / "output"
    out.mkdir(parents=True)
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ExportService.get_output_dir",
        classmethod(lambda cls, pid: tmp_path / pid / "output"),
    )
    return out


def test_find_local_video_prefers_output_mp4(output_dir):
    (output_dir / "output.mp4").write_bytes(b"v")
    (output_dir / "ATR_alt.mp4").write_bytes(b"v")
    found = LanTransferService.find_local_upload_video("p1")
    assert found is not None and found.name == "output.mp4"


def test_find_local_video_single_atr(output_dir):
    (output_dir / "ATR_final.mp4").write_bytes(b"v")
    found = LanTransferService.find_local_upload_video("p1")
    assert found is not None and found.name == "ATR_final.mp4"


def test_find_local_video_ignores_proxies_and_conflicts(output_dir):
    (output_dir / "ATR_a__atr_proxy.mp4").write_bytes(b"v")
    assert LanTransferService.find_local_upload_video("p1") is None
    (output_dir / "ATR_a.mp4").write_bytes(b"v")
    (output_dir / "ATR_b.mp4").write_bytes(b"v")
    assert LanTransferService.find_local_upload_video("p1") is None


def test_readiness_green_with_local_video_and_no_drive(output_dir, monkeypatch):
    (output_dir / "output.mp4").write_bytes(b"v")
    from app.services.upload_phase import UploadPhaseService
    from app.services import upload_phase as up

    class _P:
        id = "p1"
        drive_folder_id = None
        drive_folder_url = None

    monkeypatch.setattr(up.ProjectService, "get_metadata_file", classmethod(lambda cls, pid: output_dir / "output.mp4"))  # any existing file
    monkeypatch.setattr(up.GoogleDriveService, "is_configured", classmethod(lambda cls: True))

    def _boom(*a, **kw):
        raise AssertionError("Drive must not be queried when a local video exists")

    monkeypatch.setattr(up.ExportService, "detect_upload_video_in_drive_root", classmethod(lambda cls, *a: _boom()))
    monkeypatch.setattr(up.GoogleDriveService, "find_project_folder_by_name", classmethod(lambda cls, *a, **kw: _boom()))

    readiness = UploadPhaseService.compute_readiness(_P())
    assert readiness.status == "green"
    assert readiness.local_video_name == "output.mp4"
