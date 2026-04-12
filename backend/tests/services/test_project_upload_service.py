from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from app.library_types import LibraryType
from app.models.project import Project
from app.models.project_upload import ProjectUploadJob
from app.services.account_service import AccountConfig, AccountService
from app.services.project_service import ProjectService
from app.services.project_upload_service import ProjectUploadService
from app.services.upload_phase import UploadPhaseService


async def _wait_for_terminal_job(
    service: ProjectUploadService,
    project_id: str,
) -> ProjectUploadJob:
    for _ in range(400):
        job = service.get_job(project_id)
        if job is not None and job.status in {"complete", "error"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for upload job")


async def _wait_for(predicate) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


@pytest.mark.asyncio
async def test_enqueue_upload_returns_existing_active_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path, max_concurrent=1)
    project = Project(id="proj-upload-active", library_type=LibraryType.ANIME)
    started = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, project_id: project if project_id == project.id else None))

    def _fake_execute(cls, project_id: str, **kwargs):
        del cls, project_id, kwargs
        started.set()
        assert release.wait(timeout=2)
        return {"ok": True}

    monkeypatch.setattr(UploadPhaseService, "execute_upload", classmethod(_fake_execute))

    first = await service.enqueue_upload(project_id=project.id)
    await _wait_for(started.is_set)
    second = await service.enqueue_upload(project_id=project.id)

    assert first.job_id == second.job_id
    assert service.get_job(project.id) is not None
    assert service.get_job(project.id).status == "running"

    release.set()
    terminal = await _wait_for_terminal_job(service, project.id)
    assert terminal.status == "complete"
    assert terminal.result == {"ok": True}


@pytest.mark.asyncio
async def test_enqueue_upload_honors_global_parallel_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path, max_concurrent=2)
    projects = {
        "proj-1": Project(id="proj-1", library_type=LibraryType.ANIME),
        "proj-2": Project(id="proj-2", library_type=LibraryType.ANIME),
        "proj-3": Project(id="proj-3", library_type=LibraryType.ANIME),
    }
    start_order: list[str] = []
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    third_started = threading.Event()

    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, project_id: projects.get(project_id)))

    def _fake_execute(cls, project_id: str, **kwargs):
        del cls, kwargs
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            start_order.append(project_id)
            if len(start_order) >= 3:
                third_started.set()
        if len(start_order) <= 2:
            assert release.wait(timeout=2)
        time.sleep(0.02)
        with state_lock:
            active -= 1
        return {"project_id": project_id}

    monkeypatch.setattr(UploadPhaseService, "execute_upload", classmethod(_fake_execute))

    for project_id in projects:
        await service.enqueue_upload(project_id=project_id)

    await _wait_for(lambda: len(start_order) >= 2)
    await asyncio.sleep(0.05)

    assert len(start_order) == 2
    assert not third_started.is_set()
    assert service.get_job("proj-3").status == "queued"

    release.set()
    await _wait_for(third_started.is_set)

    terminals = await asyncio.gather(*[
        _wait_for_terminal_job(service, project_id)
        for project_id in projects
    ])

    assert max_active == 2
    assert all(job.status == "complete" for job in terminals)


@pytest.mark.asyncio
async def test_startup_cleanup_marks_running_jobs_interrupted(
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path, max_concurrent=1)
    service._jobs = {
        "queued-project": ProjectUploadJob(project_id="queued-project", status="queued"),
        "running-project": ProjectUploadJob(project_id="running-project", status="running"),
        "complete-project": ProjectUploadJob(project_id="complete-project", status="complete"),
    }

    await service.startup_cleanup()

    assert service.get_job("queued-project").status == "error"
    assert service.get_job("queued-project").phase == "interrupted"
    assert service.get_job("running-project").status == "error"
    assert service.get_job("complete-project").status == "complete"


@pytest.mark.asyncio
async def test_same_account_parallel_uploads_reserve_distinct_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path, max_concurrent=2)
    projects = {
        "proj-slot-1": Project(
            id="proj-slot-1",
            library_type=LibraryType.ANIME,
            output_language="fr",
        ),
        "proj-slot-2": Project(
            id="proj-slot-2",
            library_type=LibraryType.ANIME,
            output_language="fr",
        ),
    }
    saved_projects = projects
    recorded_slots: dict[str, str] = {}

    account = AccountConfig(
        id="acct-1",
        name="Demo",
        language="fr",
        supported_types=[LibraryType.ANIME],
        slots=["14:00", "16:00"],
    )

    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, project_id: saved_projects.get(project_id)))
    monkeypatch.setattr(ProjectService, "list_all", classmethod(lambda cls: list(saved_projects.values())))
    monkeypatch.setattr(ProjectService, "save", classmethod(lambda cls, project: saved_projects.__setitem__(project.id, project)))
    monkeypatch.setattr(AccountService, "get_account", classmethod(lambda cls, account_id: account if account_id == account.id else None))

    def _fake_execute(cls, project_id: str, **kwargs):
        del cls
        reserved_slot_dt = kwargs.get("reserved_slot_dt")
        assert reserved_slot_dt is not None
        recorded_slots[project_id] = reserved_slot_dt.isoformat()
        return {"project_id": project_id}

    monkeypatch.setattr(UploadPhaseService, "execute_upload", classmethod(_fake_execute))

    await asyncio.gather(
        service.enqueue_upload(project_id="proj-slot-1", account_id=account.id),
        service.enqueue_upload(project_id="proj-slot-2", account_id=account.id),
    )

    await asyncio.gather(
        _wait_for_terminal_job(service, "proj-slot-1"),
        _wait_for_terminal_job(service, "proj-slot-2"),
    )

    assert len(set(recorded_slots.values())) == 2
    assert saved_projects["proj-slot-1"].scheduled_slot != saved_projects["proj-slot-2"].scheduled_slot
    assert saved_projects["proj-slot-1"].scheduled_account_id == account.id
    assert saved_projects["proj-slot-2"].scheduled_account_id == account.id


@pytest.mark.asyncio
async def test_failed_upload_clears_reserved_slot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    jobs_path = tmp_path / "project_upload_jobs.json"
    service = ProjectUploadService(jobs_path=jobs_path, max_concurrent=1)
    project = Project(
        id="proj-slot-failure",
        library_type=LibraryType.ANIME,
        output_language="fr",
    )
    saved_projects = {project.id: project}
    account = AccountConfig(
        id="acct-2",
        name="Demo",
        language="fr",
        supported_types=[LibraryType.ANIME],
        slots=["14:00"],
    )

    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, project_id: saved_projects.get(project_id)))
    monkeypatch.setattr(ProjectService, "list_all", classmethod(lambda cls: list(saved_projects.values())))
    monkeypatch.setattr(ProjectService, "save", classmethod(lambda cls, project: saved_projects.__setitem__(project.id, project)))
    monkeypatch.setattr(AccountService, "get_account", classmethod(lambda cls, account_id: account if account_id == account.id else None))

    def _fake_execute(cls, project_id: str, **kwargs):
        del cls, project_id, kwargs
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(UploadPhaseService, "execute_upload", classmethod(_fake_execute))

    await service.enqueue_upload(project_id=project.id, account_id=account.id)
    terminal = await _wait_for_terminal_job(service, project.id)

    assert terminal.status == "error"
    assert "exploded" in str(terminal.error)
    assert saved_projects[project.id].scheduled_account_id is None
    assert saved_projects[project.id].scheduled_at is None
    assert saved_projects[project.id].scheduled_slot is None
