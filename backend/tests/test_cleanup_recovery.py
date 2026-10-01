"""Missing downloads must not silently advance Pure projects past cleanup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.cleanup import router
from app.library_types import LibraryType
from app.models import ProjectPhase
from app.models.cleanup import CleanFeedRect, CleanupState, CleanupZone
from app.services.project_service import ProjectService
from app.services.project_startup_service import ProjectStartupService


@pytest.fixture
def pure_project(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", tmp_path / "projects")
    monkeypatch.setattr(ProjectService, "sync_project_pin", lambda project: None)
    project = ProjectService.create(library_type=LibraryType.PURE)
    project.cleanup = CleanupState(
        clean_feed_rect=CleanFeedRect(x=0, y=0.2, w=1, h=0.5),
        zones=[CleanupZone(kind="subtitle", x=0.1, y=0.6, w=0.8, h=0.2)],
    )
    ProjectService.save(project)
    return project


@pytest.mark.parametrize("video_state", ["not_downloaded", "missing", "directory", "present"])
def test_skip_cleanup_requires_a_downloaded_file(pure_project, tmp_path, video_state):
    path = tmp_path / "source.mp4"
    if video_state != "not_downloaded":
        pure_project.video_path = str(path)
    if video_state == "present":
        path.write_bytes(b"video")
    elif video_state == "directory":
        path.mkdir()
    ProjectService.save(pure_project)
    app = FastAPI()
    app.include_router(router, prefix="/api")

    with TestClient(app) as client:
        response = client.post(f"/api/projects/{pure_project.id}/cleanup/skip")

    saved = ProjectService.load(pure_project.id)
    if video_state == "present":
        assert response.status_code == 200
        assert saved.phase == ProjectPhase.SCENE_DETECTION
    else:
        assert response.status_code == 400
        assert response.json()["detail"] == "Download the source video before continuing"
        assert saved.phase == ProjectPhase.SETUP


@pytest.mark.asyncio
async def test_download_retry_keeps_rectangles_and_discards_rendered_output(pure_project, tmp_path):
    project_dir = ProjectService.get_project_dir(pure_project.id)
    source = project_dir / "tiktok.mp4"
    cleaned = project_dir / "tiktok_clean.mp4"
    source.write_bytes(b"source")
    cleaned.write_bytes(b"cleaned")
    cleanup_dir = project_dir / "cleanup"
    cleanup_dir.mkdir()
    (cleanup_dir / "cached-span.npz").write_bytes(b"cached")
    pure_project.video_path = str(cleaned)
    pure_project.original_video_path = str(source)
    pure_project.phase = ProjectPhase.CLEANUP
    pure_project.cleanup.status = "complete"
    pure_project.cleanup.progress = 1
    pure_project.cleanup.cleaned_video_path = str(cleaned)
    ProjectService.save(pure_project)

    service = ProjectStartupService(jobs_path=tmp_path / "jobs.json")
    await service._reset_project_startup_state(pure_project.id)

    saved = ProjectService.load(pure_project.id)
    assert saved.video_path is None
    assert saved.original_video_path is None
    assert saved.phase == ProjectPhase.SETUP
    assert saved.cleanup.zones == pure_project.cleanup.zones
    assert saved.cleanup.clean_feed_rect == pure_project.cleanup.clean_feed_rect
    assert saved.cleanup.status == "idle"
    assert saved.cleanup.progress == 0
    assert saved.cleanup.cleaned_video_path is None
    assert not source.exists()
    assert not cleaned.exists()
    assert not cleanup_dir.exists()
