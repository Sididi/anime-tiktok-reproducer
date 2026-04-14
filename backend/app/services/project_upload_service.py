from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..models.project_upload import ProjectUploadJob
from .account_service import AccountService
from .project_service import ProjectService
from .scheduling_service import SchedulingService
from .upload_phase import UploadPhaseService


logger = logging.getLogger("uvicorn.error")
_UNSET = object()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jobs_payload(jobs: dict[str, ProjectUploadJob]) -> dict[str, Any]:
    return {
        "jobs": [job.model_dump(mode="json") for job in jobs.values()],
    }


def _write_jobs_atomic(path: Path, jobs: dict[str, ProjectUploadJob]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(
        json.dumps(_jobs_payload(jobs), ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_jobs(path: Path) -> dict[str, ProjectUploadJob]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_jobs = payload.get("jobs", [])
    jobs: dict[str, ProjectUploadJob] = {}
    if not isinstance(raw_jobs, list):
        return jobs
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue
        try:
            job = ProjectUploadJob.model_validate(raw_job)
        except Exception:
            continue
        jobs[job.project_id] = job
    return jobs


@dataclass
class UploadRequestSpec:
    project_id: str
    account_id: str | None = None
    platforms: list[str] | None = None
    facebook_strategy: str | None = None
    youtube_strategy: str | None = None
    copyright_audio_path: str | None = None


class ProjectUploadService:
    RESTART_INTERRUPTED_ERROR = "Upload interrupted by server restart."

    def __init__(
        self,
        jobs_path: Path | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        self._jobs_path = jobs_path or (settings.data_dir / "project_upload_jobs.json")
        self._jobs = _load_jobs(self._jobs_path)
        max_workers = max_concurrent if max_concurrent is not None else settings.project_upload_max_concurrent
        self._semaphore = asyncio.Semaphore(max(1, max_workers))
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._requests: dict[str, UploadRequestSpec] = {}

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
        self._requests.clear()
        if updated:
            await asyncio.to_thread(_write_jobs_atomic, self._jobs_path, self._jobs)

    def list_jobs(self) -> list[ProjectUploadJob]:
        return sorted(
            self._jobs.values(),
            key=lambda job: (job.updated_at, job.created_at),
            reverse=True,
        )

    def get_job(self, project_id: str) -> ProjectUploadJob | None:
        return self._jobs.get(project_id)

    @staticmethod
    def _ordered_platform_results(
        platform_results: list[dict[str, Any]] | None,
        requested_platforms: list[str] | None,
    ) -> list[dict[str, Any]] | None:
        if platform_results is None:
            return None
        ordered = [dict(item) for item in platform_results if isinstance(item, dict)]
        if not requested_platforms:
            return ordered

        order = {platform: index for index, platform in enumerate(requested_platforms)}
        ordered.sort(
            key=lambda item: (
                order.get(str(item.get("platform") or ""), len(order)),
                str(item.get("platform") or ""),
            )
        )
        return ordered

    async def enqueue_upload(
        self,
        *,
        project_id: str,
        account_id: str | None = None,
        platforms: list[str] | None = None,
        facebook_strategy: str | None = None,
        youtube_strategy: str | None = None,
        copyright_audio_path: str | None = None,
    ) -> ProjectUploadJob:
        project = ProjectService.load(project_id)
        if project is None:
            raise ValueError("Project not found")

        existing = self._jobs.get(project_id)
        if existing and existing.status in {"queued", "running"}:
            return existing

        job = existing or ProjectUploadJob(project_id=project_id)
        if existing is not None:
            job.job_id = uuid.uuid4().hex[:16]
            job.created_at = _utc_now()
        job.account_id = account_id
        job.platforms = list(platforms) if platforms is not None else None
        job.facebook_strategy = facebook_strategy
        job.youtube_strategy = youtube_strategy
        job.status = "queued"
        job.phase = "queued"
        job.message = "Upload queued"
        job.error = None
        job.platform_results = None
        job.result = None

        self._requests[project_id] = UploadRequestSpec(
            project_id=project_id,
            account_id=account_id,
            platforms=list(platforms) if platforms is not None else None,
            facebook_strategy=facebook_strategy,
            youtube_strategy=youtube_strategy,
            copyright_audio_path=copyright_audio_path,
        )
        await self._publish_job(job)
        asyncio.create_task(
            self._run_job(project_id, job.job_id),
            name=f"project-upload:{project_id}",
        )
        return job

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

    async def _publish_job(self, job: ProjectUploadJob) -> None:
        job.updated_at = _utc_now()
        self._jobs[job.project_id] = job
        await asyncio.to_thread(_write_jobs_atomic, self._jobs_path, self._jobs)
        payload = job.model_dump(mode="json")
        for queue in list(self._subscribers):
            queue.put_nowait(payload)

    async def _set_job_state(
        self,
        job: ProjectUploadJob,
        *,
        status: str | None = None,
        phase: str | None = None,
        message: str | None = None,
        error: str | None = None,
        platform_results: list[dict[str, Any]] | None | object = _UNSET,
        result: dict[str, Any] | None | object = _UNSET,
    ) -> None:
        if status is not None:
            job.status = status
        if phase is not None:
            job.phase = phase
        if message is not None or status == "error":
            job.message = message
        if status is not None:
            job.error = error
        elif error is not None:
            job.error = error
        if platform_results is not _UNSET:
            job.platform_results = (
                self._ordered_platform_results(
                    platform_results if isinstance(platform_results, list) else None,
                    job.platforms,
                )
            )
        if result is not _UNSET:
            job.result = result if isinstance(result, dict) or result is None else None
        await self._publish_job(job)

    async def _run_job(self, project_id: str, job_id: str) -> None:
        await self._semaphore.acquire()
        reserved_slot_dt = None
        reserved_scheduled_at = None
        try:
            job = self._jobs.get(project_id)
            if job is None or job.job_id != job_id:
                return

            request = self._requests.get(project_id)
            if request is None:
                raise RuntimeError("Upload request payload is missing.")

            await self._set_job_state(
                job,
                status="running",
                phase="prepare",
                message="Preparing upload...",
                error=None,
                platform_results=[],
            )

            if request.account_id:
                account = AccountService.get_account(request.account_id)
                if account is not None and account.slots:
                    reserved_slot_dt, reserved_scheduled_at = await asyncio.to_thread(
                        SchedulingService.reserve_next_slot,
                        project_id,
                        request.account_id,
                    )
                    await self._set_job_state(
                        job,
                        status="running",
                        phase="scheduled",
                        message="Reserved scheduled upload slot.",
                    )

            loop = asyncio.get_running_loop()

            def progress_callback(progress: float, phase: str, message: str) -> None:
                del progress

                def _schedule_update() -> None:
                    current_job = self._jobs.get(project_id)
                    if current_job is None or current_job.job_id != job_id:
                        return
                    asyncio.create_task(
                        self._set_job_state(
                            current_job,
                            status="running",
                            phase=phase,
                            message=message,
                        )
                    )

                loop.call_soon_threadsafe(_schedule_update)

            def platform_result_callback(platform_result: dict[str, Any]) -> None:
                if not isinstance(platform_result, dict):
                    return

                def _schedule_update() -> None:
                    current_job = self._jobs.get(project_id)
                    if current_job is None or current_job.job_id != job_id:
                        return
                    existing_results = [
                        dict(item)
                        for item in (current_job.platform_results or [])
                        if isinstance(item, dict)
                    ]
                    platform_key = str(platform_result.get("platform") or "").strip().lower()
                    if platform_key:
                        existing_results = [
                            item
                            for item in existing_results
                            if str(item.get("platform") or "").strip().lower() != platform_key
                        ]
                    existing_results.append(dict(platform_result))
                    asyncio.create_task(
                        self._set_job_state(
                            current_job,
                            platform_results=existing_results,
                        )
                    )

                loop.call_soon_threadsafe(_schedule_update)

            result = await asyncio.to_thread(
                UploadPhaseService.execute_upload,
                project_id,
                account_id=request.account_id,
                platforms=request.platforms,
                facebook_strategy=request.facebook_strategy,
                youtube_strategy=request.youtube_strategy,
                copyright_audio_path=request.copyright_audio_path,
                reserved_slot_dt=reserved_slot_dt,
                reserved_scheduled_at=reserved_scheduled_at,
                progress_callback=progress_callback,
                platform_result_callback=platform_result_callback,
            )
            await self._set_job_state(
                job,
                status="complete",
                phase="complete",
                message="Upload complete.",
                error=None,
                platform_results=result.get("platform_results") if isinstance(result, dict) else None,
                result=result,
            )
        except Exception as exc:
            if reserved_slot_dt is not None or reserved_scheduled_at is not None:
                try:
                    await asyncio.to_thread(SchedulingService.clear_reserved_slot, project_id)
                except Exception:
                    logger.exception("Failed to clear reserved upload slot for %s", project_id)
            current_job = self._jobs.get(project_id)
            if current_job is not None and current_job.job_id == job_id:
                await self._set_job_state(
                    current_job,
                    status="error",
                    phase=current_job.phase or "error",
                    message=None,
                    error=str(exc),
                    result=None,
                )
            logger.exception("Project upload failed for %s", project_id)
        finally:
            self._requests.pop(project_id, None)
            self._semaphore.release()


project_upload_queue = ProjectUploadService()
