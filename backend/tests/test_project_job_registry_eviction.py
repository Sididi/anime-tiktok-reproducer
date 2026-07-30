"""Job registries in ProjectUploadService / ProjectStartupService are keyed by
project_id, persisted to JSON, and reloaded at boot: without eviction they grow
forever (RAM + disk) with every project ever touched."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.library_types import LibraryType
from app.models.project_startup import ProjectStartupJob
from app.models.project_upload import ProjectUploadJob
from app.services.project_startup_service import ProjectStartupService
from app.services.project_upload_service import ProjectUploadService


def _seed_upload_job(service: ProjectUploadService, project_id: str, status: str) -> None:
    job = ProjectUploadJob(project_id=project_id)
    job.status = status
    service._jobs[project_id] = job


def _seed_startup_job(service: ProjectStartupService, project_id: str, status: str) -> None:
    job = ProjectStartupJob(project_id=project_id, library_type=LibraryType.ANIME)
    job.status = status
    service._jobs[project_id] = job


@pytest.mark.asyncio
async def test_upload_remove_project_jobs_evicts_and_persists(tmp_path: Path) -> None:
    jobs_path = tmp_path / "upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path)
    _seed_upload_job(service, "p1", "complete")
    _seed_upload_job(service, "p2", "complete")

    await service.remove_project_jobs("p1")

    assert [j.project_id for j in service.list_jobs()] == ["p2"]
    reloaded = ProjectUploadService(jobs_path=jobs_path)
    assert [j.project_id for j in reloaded.list_jobs()] == ["p2"]


@pytest.mark.asyncio
async def test_upload_startup_cleanup_prunes_deleted_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_path = tmp_path / "upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path)
    _seed_upload_job(service, "live", "complete")
    _seed_upload_job(service, "stale", "complete")

    monkeypatch.setattr(
        "app.services.project_upload_service.ProjectService.load",
        classmethod(lambda cls, pid: object() if pid == "live" else None),
    )
    await service.startup_cleanup()

    assert [j.project_id for j in service.list_jobs()] == ["live"]
    reloaded = ProjectUploadService(jobs_path=jobs_path)
    assert [j.project_id for j in reloaded.list_jobs()] == ["live"]


@pytest.mark.asyncio
async def test_startup_remove_project_jobs_evicts_and_persists(tmp_path: Path) -> None:
    jobs_path = tmp_path / "startup_jobs.json"
    service = ProjectStartupService(jobs_path=jobs_path)
    _seed_startup_job(service, "p1", "complete")
    _seed_startup_job(service, "p2", "error")

    await service.remove_project_jobs("p1")

    assert [j.project_id for j in service.list_jobs()] == ["p2"]
    reloaded = ProjectStartupService(jobs_path=jobs_path)
    assert [j.project_id for j in reloaded.list_jobs()] == ["p2"]


@pytest.mark.asyncio
async def test_startup_cleanup_prunes_deleted_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs_path = tmp_path / "startup_jobs.json"
    service = ProjectStartupService(jobs_path=jobs_path)
    _seed_startup_job(service, "live", "complete")
    _seed_startup_job(service, "stale", "complete")

    from app.services.project_service import ProjectService

    monkeypatch.setattr(
        ProjectService,
        "load",
        classmethod(lambda cls, pid: object() if pid == "live" else None),
    )
    await service.startup_cleanup()

    assert [j.project_id for j in service.list_jobs()] == ["live"]
    reloaded = ProjectStartupService(jobs_path=jobs_path)
    assert [j.project_id for j in reloaded.list_jobs()] == ["live"]
