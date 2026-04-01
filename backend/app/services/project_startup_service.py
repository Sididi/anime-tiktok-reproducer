from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import settings
from ..models.project_startup import ProjectStartupJob


logger = logging.getLogger("uvicorn.error")


ProgressCallback = Callable[[float, str], Awaitable[None] | None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jobs_payload(jobs: dict[str, ProjectStartupJob]) -> dict[str, Any]:
    return {
        "jobs": [job.model_dump(mode="json") for job in jobs.values()],
    }


def _write_jobs_atomic(path: Path, jobs: dict[str, ProjectStartupJob]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(_jobs_payload(jobs), ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_jobs(path: Path) -> dict[str, ProjectStartupJob]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_jobs = payload.get("jobs", [])
    jobs: dict[str, ProjectStartupJob] = {}
    if not isinstance(raw_jobs, list):
        return jobs
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue
        try:
            job = ProjectStartupJob.model_validate(raw_job)
        except Exception:
            continue
        jobs[job.project_id] = job
    return jobs


class ProjectStartupService:
    MAX_CONCURRENT = 1
    RESTART_INTERRUPTED_ERROR = "Startup interrupted by server restart."

    def __init__(self, jobs_path: Path | None = None) -> None:
        self._jobs_path = jobs_path or (settings.data_dir / "project_startup_jobs.json")
        self._jobs = _load_jobs(self._jobs_path)
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []

    async def startup_cleanup(self) -> None:
        updated = False
        for job in self._jobs.values():
            if job.status not in {"queued", "running"}:
                continue
            job.status = "error"
            job.phase = "interrupted"
            job.message = None
            job.error = self.RESTART_INTERRUPTED_ERROR
            job.updated_at = _utc_now()
            updated = True
        if updated:
            await asyncio.to_thread(_write_jobs_atomic, self._jobs_path, self._jobs)

    def list_jobs(self) -> list[ProjectStartupJob]:
        return sorted(
            self._jobs.values(),
            key=lambda job: (job.updated_at, job.created_at),
            reverse=True,
        )

    def get_job(self, project_id: str) -> ProjectStartupJob | None:
        return self._jobs.get(project_id)

    async def start_project(
        self,
        *,
        tiktok_url: str,
        anime_name: str | None,
        series_id: str | None,
        library_type,
    ) -> ProjectStartupJob:
        from .project_service import ProjectService

        project = ProjectService.create(
            tiktok_url=tiktok_url,
            source_path=None,
            anime_name=anime_name,
            series_id=series_id,
            library_type=library_type,
        )
        return await self.enqueue_project(project.id)

    async def enqueue_project(self, project_id: str) -> ProjectStartupJob:
        from .project_service import ProjectService

        project = ProjectService.load(project_id)
        if project is None:
            raise RuntimeError("Project not found")

        existing = self._jobs.get(project_id)
        if existing and existing.status in {"queued", "running"}:
            return existing

        job = existing or ProjectStartupJob(
            project_id=project.id,
            library_type=project.library_type,
        )
        self._sync_job_from_project(job, project)
        job.status = "queued"
        job.progress = 0.0
        job.phase = "queued"
        job.message = "Startup queued"
        job.error = None
        job.ready_url = None
        if existing is None:
            job.created_at = _utc_now()

        await self._publish_job(job)
        asyncio.create_task(self._run_job(project_id, job.job_id))
        return job

    async def retry_project(self, project_id: str) -> ProjectStartupJob:
        from .project_service import ProjectService

        project = ProjectService.load(project_id)
        if project is None:
            raise RuntimeError("Project not found")

        existing = self._jobs.get(project_id)
        if existing and existing.status in {"queued", "running"}:
            raise RuntimeError("Startup is already running for this project.")

        await self._reset_project_startup_state(project_id)
        return await self.enqueue_project(project_id)

    async def stream_all_jobs(self):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            for job in self.list_jobs():
                yield job.model_dump(mode="json")
            while True:
                yield await queue.get()
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    @staticmethod
    def _sync_job_from_project(job: ProjectStartupJob, project) -> None:
        job.project_id = project.id
        job.anime_name = project.anime_name
        job.series_id = project.series_id
        job.library_type = project.library_type
        job.tiktok_url = project.tiktok_url

    async def _publish_job(self, job: ProjectStartupJob) -> None:
        job.updated_at = _utc_now()
        self._jobs[job.project_id] = job
        await asyncio.to_thread(_write_jobs_atomic, self._jobs_path, self._jobs)
        payload = job.model_dump(mode="json")
        for queue in list(self._subscribers):
            queue.put_nowait(payload)

    async def _set_job_state(
        self,
        job: ProjectStartupJob,
        *,
        status: str | None = None,
        phase: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
        ready_url: str | None = None,
    ) -> None:
        if status is not None:
            job.status = status
        if phase is not None:
            job.phase = phase
        if progress is not None:
            job.progress = max(0.0, min(1.0, progress))
        if message is not None or status == "error":
            job.message = message
        if status is not None:
            job.error = error
        elif error is not None:
            job.error = error
        if ready_url is not None:
            job.ready_url = ready_url
        await self._publish_job(job)

    @staticmethod
    def _compute_ready_url(project_id: str) -> str:
        if settings.scenes_skip_ui_enabled:
            return f"/project/{project_id}/matches"
        return f"/project/{project_id}/scenes"

    async def _reset_project_startup_state(self, project_id: str) -> None:
        from .downloader import DownloaderService
        from .project_service import ProjectService
        from ..models.project import ProjectPhase

        project = ProjectService.load(project_id)
        if project is None:
            raise RuntimeError("Project not found")

        output_path = DownloaderService.get_output_path(project_id)
        recovery_path = output_path.with_name(f"{output_path.stem}.recovery{output_path.suffix}")
        mux_path = output_path.with_name(f"{output_path.stem}.muxed{output_path.suffix}")
        scenes_path = ProjectService.get_scenes_file(project_id)

        await asyncio.to_thread(output_path.unlink, missing_ok=True)
        await asyncio.to_thread(recovery_path.unlink, missing_ok=True)
        await asyncio.to_thread(mux_path.unlink, missing_ok=True)
        await asyncio.to_thread(scenes_path.unlink, missing_ok=True)

        project.phase = ProjectPhase.SETUP
        project.video_path = None
        project.video_duration = None
        project.video_fps = None
        project.video_width = None
        project.video_height = None
        ProjectService.save(project)

    async def _run_job(self, project_id: str, job_id: str) -> None:
        from .downloader import DownloaderService
        from .library_hydration_service import LibraryHydrationService
        from .project_service import ProjectService
        from .scene_detector import SceneDetectorService

        await self._semaphore.acquire()
        try:
            job = self._jobs.get(project_id)
            if job is None or job.job_id != job_id:
                return

            project = ProjectService.load(project_id)
            if project is None:
                raise RuntimeError("Project not found")
            self._sync_job_from_project(job, project)
            await self._set_job_state(
                job,
                status="running",
                phase="download",
                progress=0.01,
                message="Starting project startup...",
                error=None,
            )

            if not project.tiktok_url:
                raise RuntimeError("Project does not have a TikTok URL.")

            async for progress in DownloaderService.download_project_video(
                project.tiktok_url,
                project_id,
            ):
                if progress.status == "error":
                    raise RuntimeError(progress.error or "Video download failed.")
                await self._set_job_state(
                    job,
                    status="running",
                    phase="download",
                    progress=0.05 + (0.50 * float(progress.progress or 0.0)),
                    message=progress.message or "Downloading TikTok video...",
                )

            async for progress in SceneDetectorService.detect_project_scenes(
                project_id,
            ):
                if progress.status == "error":
                    raise RuntimeError(progress.error or "Scene detection failed.")
                if progress.status == "complete":
                    ready_url = self._compute_ready_url(project_id)
                    await self._set_job_state(
                        job,
                        status="running",
                        phase="scene_detection",
                        progress=0.80,
                        message=progress.message or "Scene detection complete.",
                        ready_url=ready_url,
                    )
                    break
                await self._set_job_state(
                    job,
                    status="running",
                    phase="scene_detection",
                    progress=0.55 + (0.25 * float(progress.progress or 0.0)),
                    message=progress.message or "Detecting scenes...",
                )

            project = ProjectService.load(project_id)
            if project is None:
                raise RuntimeError("Project not found")

            ready_url = job.ready_url or self._compute_ready_url(project_id)
            await self._set_job_state(
                job,
                status="running",
                phase="activation",
                progress=0.80,
                message="Activating selected library...",
                ready_url=ready_url,
            )

            if project.series_id:
                async def _activation_progress(progress_value: float, progress_message: str) -> None:
                    await self._set_job_state(
                        job,
                        status="running",
                        phase="activation",
                        progress=0.80 + (0.20 * progress_value),
                        message=progress_message,
                        ready_url=ready_url,
                    )

                await LibraryHydrationService.activate_project_series(
                    project_id=project.id,
                    library_type=project.library_type,
                    series_id=project.series_id,
                    progress_callback=_activation_progress,
                )

            await self._set_job_state(
                job,
                status="complete",
                phase="complete",
                progress=1.0,
                message="Project startup complete.",
                error=None,
                ready_url=ready_url,
            )
        except Exception as exc:
            job = self._jobs.get(project_id)
            if job is not None and job.job_id == job_id:
                await self._set_job_state(
                    job,
                    status="error",
                    phase=job.phase or "error",
                    message=None,
                    error=str(exc),
                )
            logger.exception("Project startup failed for %s", project_id)
        finally:
            self._semaphore.release()


project_startup_queue = ProjectStartupService()
