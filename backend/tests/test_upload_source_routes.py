from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.upload_phase import UploadPhaseService


@pytest.fixture
def client(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c


def test_status_warms_cache_and_reports(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: {"state": "in_progress"}),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-status")
    assert resp.status_code == 200
    assert resp.json() == {"state": "in_progress"}


def test_status_404_when_project_missing(client, monkeypatch):
    def raise_missing(cls, pid, readiness=None):
        raise ValueError("Project not found")

    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(raise_missing),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-status")
    assert resp.status_code == 404


def test_preview_202_while_in_flight(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video",
        classmethod(lambda cls, pid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService, "source_video_status",
        classmethod(lambda cls, pid: {"state": "in_progress"}),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-preview")
    assert resp.status_code == 202


def test_preview_404_when_absent(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video",
        classmethod(lambda cls, pid: None),
    )
    monkeypatch.setattr(
        UploadPhaseService, "source_video_status",
        classmethod(lambda cls, pid: {"state": "missing"}),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-preview")
    assert resp.status_code == 404


def test_preview_serves_cached_file(client, monkeypatch, tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"mp4-bytes")
    monkeypatch.setattr(
        UploadPhaseService, "cached_source_video",
        classmethod(lambda cls, pid: video),
    )
    resp = client.get("/api/project-manager/projects/p1/upload-source-preview")
    assert resp.status_code == 200
    assert resp.content == b"mp4-bytes"
    assert resp.headers["content-type"] == "video/mp4"


def test_old_platform_preview_routes_removed(client):
    for url in (
        "/api/project-manager/projects/p1/facebook-preview/original",
        "/api/project-manager/projects/p1/youtube-preview/original",
    ):
        assert client.get(url).status_code == 404


def test_instagram_duration_check_route_forwards_account(client, monkeypatch):
    expected = {
        "needed": False,
        "duration_seconds": 240.0,
        "speed_factor": 1.3333,
        "sped_up_available": True,
        "max_duration_seconds": 180.0,
    }
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        UploadPhaseService,
        "check_instagram_duration",
        classmethod(
            lambda cls, project_id, account_id=None: (
                seen.append((project_id, account_id)) or expected
            )
        ),
    )
    response = client.post(
        "/api/project-manager/projects/p1/instagram-check",
        json={"account_id": "anime_fr"},
    )
    assert response.status_code == 200
    assert response.json() == expected
    assert seen == [("p1", "anime_fr")]
