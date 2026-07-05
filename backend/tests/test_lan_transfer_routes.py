# backend/tests/test_lan_transfer_routes.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr("app.config.settings.lan_transfer_token", "test-token")
    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c


AUTH = {"X-ATR-LAN-Token": "test-token"}


def test_ping_requires_token(client):
    assert client.get("/api/lan/ping").status_code == 401


def test_ping_rejects_wrong_token(client):
    resp = client.get("/api/lan/ping", headers={"X-ATR-LAN-Token": "wrong"})
    assert resp.status_code == 401


def test_ping_returns_api_version(client):
    resp = client.get("/api/lan/ping", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "api_version": 1}


def test_ping_503_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.lan_transfer_token", None)
    resp = client.get("/api/lan/ping", headers=AUTH)
    assert resp.status_code == 503


def test_manifest_and_file_download(client, monkeypatch, tmp_path):
    from app.services.export_service import ManifestEntry

    src = tmp_path / "tts_edited.wav"
    src.write_bytes(b"RIFFxxxx")
    entries = [ManifestEntry(relative_path="SPM_x_p1/tts_edited.wav", source_path=src)]

    class _P:
        id = "p1"
        drive_folder_id = None

    monkeypatch.setattr("app.api.routes.lan_transfer._load_project_or_404", lambda pid: _P())
    monkeypatch.setattr(
        "app.services.lan_transfer_service.LanTransferService._build_entries",
        classmethod(lambda cls, project: ("SPM_x_p1", entries)),
    )

    manifest = client.get("/api/lan/projects/p1/manifest", headers=AUTH).json()
    assert manifest["files"] == [{"relative_path": "tts_edited.wav", "size": 8}]

    resp = client.get("/api/lan/projects/p1/files/tts_edited.wav", headers=AUTH)
    assert resp.status_code == 200 and resp.content == b"RIFFxxxx"

    assert client.get("/api/lan/projects/p1/files/missing.bin", headers=AUTH).status_code == 404


def test_manifest_returns_409_when_no_matches(client, monkeypatch):
    class _P:
        id = "p1"
        drive_folder_id = None

    monkeypatch.setattr("app.api.routes.lan_transfer._load_project_or_404", lambda pid: _P())
    monkeypatch.setattr(
        "app.services.lan_transfer_service.ProjectService.load_matches",
        lambda project_id: None,
    )

    resp = client.get("/api/lan/projects/p1/manifest", headers=AUTH)
    assert resp.status_code == 409
