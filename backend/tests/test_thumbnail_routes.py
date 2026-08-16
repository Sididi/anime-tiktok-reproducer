from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.services.thumbnail_service import ThumbnailService
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


@pytest.mark.skip(reason="Thumbnail feature disabled 2026-08-16 (owner request); re-enable with the feature")
def test_candidates_delegates_to_start_candidates_build(client, monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download",
        classmethod(lambda cls, pid, readiness=None: {"state": "in_progress"}),
    )
    snapshot = {
        "state": "partial",
        "version": "1-1",
        "pending": 1,
        "candidates": [
            {
                "index": 0, "label": "Scène 1 · début", "timestamp_ms": 50,
                "source": "clean",
                "image_url": "/project-manager/projects/p1/thumbnail-frame/0?v=1-1",
            },
            {"index": 1, "label": "Scène 1 · fin", "timestamp_ms": 900, "source": "pending"},
        ],
    }
    monkeypatch.setattr(
        ThumbnailService, "start_candidates_build",
        classmethod(lambda cls, pid: snapshot),
    )
    resp = client.get("/api/project-manager/projects/p1/thumbnail-candidates")
    assert resp.status_code == 200
    assert resp.json() == snapshot


@pytest.mark.skip(reason="Thumbnail feature disabled 2026-08-16 (owner request); re-enable with the feature")
def test_candidates_404_when_project_missing(client, monkeypatch):
    def raise_missing(cls, pid, readiness=None):
        raise ValueError("Project not found")

    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download", classmethod(raise_missing)
    )
    resp = client.get("/api/project-manager/projects/p1/thumbnail-candidates")
    assert resp.status_code == 404


@pytest.mark.skip(reason="Thumbnail feature disabled 2026-08-16 (owner request); re-enable with the feature")
def test_candidates_warm_failure_is_best_effort(client, monkeypatch):
    """A non-ValueError warm failure must not 500 — the builder still runs
    off the clean-frame path, which doesn't need the source cache."""
    def raise_generic(cls, pid, readiness=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        UploadPhaseService, "start_source_video_download", classmethod(raise_generic)
    )
    snapshot = {"state": "in_progress", "version": "1-1", "pending": 0, "candidates": []}
    called: list[str] = []
    monkeypatch.setattr(
        ThumbnailService, "start_candidates_build",
        classmethod(lambda cls, pid: called.append(pid) or snapshot),
    )
    resp = client.get("/api/project-manager/projects/p1/thumbnail-candidates")
    assert resp.status_code == 200
    assert resp.json() == snapshot
    assert called == ["p1"]


def test_frame_served_and_404(client, monkeypatch, tmp_path):
    jpg = tmp_path / "cand_0.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xd9")
    monkeypatch.setattr(
        ThumbnailService, "cached_frame_path",
        classmethod(lambda cls, pid, index: jpg if index == 0 else None),
    )
    ok = client.get("/api/project-manager/projects/p1/thumbnail-frame/0")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/jpeg"
    missing = client.get("/api/project-manager/projects/p1/thumbnail-frame/3")
    assert missing.status_code == 404


def test_upload_route_forwards_thumbnail_timestamp(client, monkeypatch):
    captured: dict = {}

    async def fake_enqueue(self, **kwargs):
        captured.update(kwargs)
        from app.models.project_upload import ProjectUploadJob  # noqa: PLC0415
        return ProjectUploadJob(project_id=kwargs["project_id"])

    monkeypatch.setattr(
        "app.services.project_upload_service.ProjectUploadService.enqueue_upload",
        fake_enqueue,
    )
    resp = client.post(
        "/api/project-manager/projects/p1/upload",
        json={
            "account_id": "acc",
            "thumbnail_timestamp_ms": 2350,
            "thumbnail_candidate_index": 3,
        },
    )
    assert resp.status_code == 200
    assert captured["thumbnail_timestamp_ms"] == 2350
    assert captured["thumbnail_candidate_index"] == 3
