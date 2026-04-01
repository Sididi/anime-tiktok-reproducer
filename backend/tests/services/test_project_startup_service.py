from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.library_types import LibraryType
from app.models import Project, Scene
from app.services.downloader import DownloadProgress, DownloaderService
from app.services.library_hydration_service import LibraryHydrationService
from app.services.project_service import ProjectService
from app.services.project_startup_service import ProjectStartupService
from app.services.scene_detector import SceneDetectionProgress, SceneDetectorService


async def _wait_for_terminal_job(
    service: ProjectStartupService,
    project_id: str,
) -> object:
    for _ in range(200):
        job = service.get_job(project_id)
        if job is not None and job.status in {"complete", "error"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for startup job")


@pytest.mark.asyncio
async def test_project_startup_success_sets_ready_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_startup_jobs.json"
    service = ProjectStartupService(jobs_path=jobs_path)
    project = Project(
        id="proj-startup-success",
        tiktok_url="https://www.tiktok.com/@demo/video/123",
        anime_name="Demo",
        series_id="series-1",
        library_type=LibraryType.ANIME,
    )

    monkeypatch.setattr(ProjectService, "create", lambda **kwargs: project)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: project if project_id == project.id else None)
    monkeypatch.setattr(ProjectService, "save", lambda saved_project: None)
    monkeypatch.setattr(ProjectService, "get_scenes_file", lambda project_id: tmp_path / f"{project_id}-scenes.json")
    monkeypatch.setattr(DownloaderService, "get_output_path", classmethod(lambda cls, project_id: tmp_path / project_id / "tiktok.mp4"))
    monkeypatch.setattr(DownloaderService, "download_project_video", classmethod(_fake_download_success))
    monkeypatch.setattr(SceneDetectorService, "detect_project_scenes", classmethod(_fake_detect_success))
    monkeypatch.setattr(
        LibraryHydrationService,
        "activate_project_series",
        classmethod(_fake_activate_success),
    )
    monkeypatch.setattr("app.services.project_startup_service.settings.scenes_skip_ui_enabled", False)

    await service.start_project(
        tiktok_url=project.tiktok_url or "",
        anime_name=project.anime_name,
        series_id=project.series_id,
        library_type=project.library_type,
    )
    job = await _wait_for_terminal_job(service, project.id)

    assert job.status == "complete"
    assert job.ready_url == f"/project/{project.id}/scenes"
    assert jobs_path.exists()
    payload = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert payload["jobs"][0]["project_id"] == project.id
    assert payload["jobs"][0]["status"] == "complete"


@pytest.mark.asyncio
async def test_project_startup_retry_reuses_same_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_startup_jobs.json"
    service = ProjectStartupService(jobs_path=jobs_path)
    project = Project(
        id="proj-startup-retry",
        tiktok_url="https://www.tiktok.com/@demo/video/456",
        anime_name="Retry Demo",
        series_id="series-2",
        library_type=LibraryType.ANIME,
    )
    attempts = {"count": 0}

    monkeypatch.setattr(ProjectService, "load", lambda project_id: project if project_id == project.id else None)
    monkeypatch.setattr(ProjectService, "save", lambda saved_project: None)
    monkeypatch.setattr(ProjectService, "get_scenes_file", lambda project_id: tmp_path / f"{project_id}-scenes.json")
    monkeypatch.setattr(DownloaderService, "get_output_path", classmethod(lambda cls, project_id: tmp_path / project_id / "tiktok.mp4"))
    monkeypatch.setattr(
        DownloaderService,
        "download_project_video",
        classmethod(lambda cls, url, project_id: _fake_download_fail_once(attempts)),
    )
    monkeypatch.setattr(SceneDetectorService, "detect_project_scenes", classmethod(_fake_detect_success))
    monkeypatch.setattr(
        LibraryHydrationService,
        "activate_project_series",
        classmethod(_fake_activate_success),
    )
    monkeypatch.setattr("app.services.project_startup_service.settings.scenes_skip_ui_enabled", True)

    await service.enqueue_project(project.id)
    first_job = await _wait_for_terminal_job(service, project.id)
    assert first_job.status == "error"
    assert "stalled" in str(first_job.error)

    await service.retry_project(project.id)
    second_job = await _wait_for_terminal_job(service, project.id)
    assert second_job.status == "complete"
    assert second_job.project_id == project.id
    assert second_job.ready_url == f"/project/{project.id}/matches"
    assert attempts["count"] == 2


async def _fake_download_success(cls, url: str, project_id: str):
    yield DownloadProgress("starting", 0.0, "Preparing download...")
    yield DownloadProgress("complete", 1.0, "Download complete!")


async def _fake_detect_success(cls, project_id: str, threshold: float = 18.0, min_scene_len: int = 10):
    yield SceneDetectionProgress("processing", 0.5, "Analyzing scene boundaries...")
    yield SceneDetectionProgress(
        "complete",
        1.0,
        "Detected 1 scenes",
        [Scene(index=0, start_time=0.0, end_time=1.0)],
    )


async def _fake_activate_success(
    cls,
    *,
    project_id: str,
    library_type: LibraryType,
    series_id: str,
    progress_callback=None,
):
    if progress_callback is not None:
        await progress_callback(0.5, "Hydrating matcher cache (1/2): manifest.fragment.json")
        await progress_callback(1.0, "Library activation complete.")
    return {"series_id": series_id, "hydration_status": "index_ready"}


async def _fake_download_fail_once(attempts: dict[str, int]):
    attempts["count"] += 1
    yield DownloadProgress("starting", 0.0, "Preparing download...")
    if attempts["count"] == 1:
        yield DownloadProgress("error", 0.0, "", error="Download stalled after 60 seconds")
        return
    yield DownloadProgress("complete", 1.0, "Download complete!")
