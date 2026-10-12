"""Request-independent progress jobs, observed through the shared event hub.

Only the short submission uses a per-tab HTTP request. The server owns the
generator, including its finalizers, even when every observer disconnects.
Queued/running jobs become explicit errors on restart; completed results are
kept for reconnects, with a bounded terminal history.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..utils.file_tasks import file_io
from .atomic_files import write_text_atomic
from .event_hub import event_hub

logger = logging.getLogger("uvicorn.error")
TOPIC = "progress_jobs"
Work = Callable[[], Awaitable[StreamingResponse]]


class ProgressJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    project_id: str
    kind: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: Literal["queued", "running", "complete", "error"] = "queued"
    result_available: bool = False
    event: dict[str, Any] = Field(default_factory=lambda: {
        "status": "starting", "progress": 0, "message": "En attente…",
    })
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProgressJobs:
    MAX_TERMINAL_JOBS = 100

    def __init__(self, path: Path | None = None):
        self.path = path or settings.data_dir / "progress_jobs.json"
        self.jobs: dict[str, ProgressJob] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        if self.path.exists():
            for raw in json.loads(self.path.read_text()).get("jobs", []):
                job = ProgressJob.model_validate(raw)
                self.jobs[job.id] = job

    def list_jobs(self, project_id: str | None = None) -> list[ProgressJob]:
        return [job for job in self.jobs.values()
                if project_id is None or job.project_id == project_id]

    async def _persist(self):
        async with self._persist_lock:
            payload = json.dumps({"jobs": [job.model_dump(mode="json") for job in self.jobs.values()]})
            await file_io(write_text_atomic, self.path, payload)

    def _publish(self, job: ProgressJob):
        job.updated_at = datetime.now(timezone.utc)
        event_hub.publish(TOPIC, key=job.id, project_id=job.project_id,
                          data=job.model_dump(mode="json"))

    def _result_path(self, job_id: str) -> Path:
        return self.path.with_suffix(".results") / f"{job_id}.json"

    async def result(self, job: ProgressJob) -> dict[str, Any]:
        """Large transcripts/matches go only to their observer, never all tabs."""
        payload = job.model_dump(mode="json")
        if job.result_available:
            payload["event"] = await file_io(lambda: json.loads(self._result_path(job.id).read_text()))
        payload["result_included"] = True
        return payload

    async def _prune(self):
        terminal = [job for job in self.jobs.values() if job.status in {"complete", "error"}]
        terminal.sort(key=lambda job: job.updated_at)
        for job in terminal[:-self.MAX_TERMINAL_JOBS]:
            self.jobs.pop(job.id, None)
            await file_io(self._result_path(job.id).unlink, missing_ok=True)
        if len(terminal) > self.MAX_TERMINAL_JOBS:
            event_hub.refresh_topic(TOPIC)

    async def startup_cleanup(self):
        from .project_service import ProjectService
        for job in list(self.jobs.values()):
            if await ProjectService.aload(job.project_id) is None:
                self.jobs.pop(job.id, None)
                await file_io(self._result_path(job.id).unlink, missing_ok=True)
            elif job.status in {"queued", "running"}:
                job.status = "error"
                job.event = {"status": "error", "error": "Traitement interrompu par le redémarrage du serveur. Relancez-le."}
                self._publish(job)
        await self._prune()
        await self._persist()

    async def start(self, project_id: str, kind: str, parameters: dict[str, Any], work: Work) -> ProgressJob:
        from .temporary_source_service import TemporarySourceService
        async with self._lock:
            if project_id in TemporarySourceService._deleting:
                raise HTTPException(409, "Project is being deleted")
            for job in self.jobs.values():
                if (job.project_id, job.kind) == (project_id, kind) and job.status in {"queued", "running"}:
                    if job.parameters != parameters:
                        raise HTTPException(409, "This operation is already running with different parameters")
                    return job
            job = ProgressJob(project_id=project_id, kind=kind, parameters=parameters)
            self.jobs[job.id] = job
            try:
                await self._persist()
            except BaseException:
                self.jobs.pop(job.id, None)
                raise
            self._publish(job)
            task = asyncio.create_task(self._run(job, work), name=f"progress:{kind}:{project_id}")
            self.tasks[job.id] = task
            def finished(done):
                self.tasks.pop(job.id, None)
                # A task cancelled before its first turn never enters _run's
                # finally block. Settle it before cancel/shutdown persists.
                if done.cancelled() and job.status in {"queued", "running"}:
                    job.status = "error"
                    job.event = {"status": "error", "error": "Traitement annulé."}
                    self._publish(job)
                if not done.cancelled() and done.exception() is not None:
                    logger.error("Progress job finalization failed: %s", done.exception())
            task.add_done_callback(finished)
            return job

    async def _run(self, job: ProgressJob, work: Work):
        response = None
        terminal = None
        final_status = "error"
        final_event = {"status": "error", "error": "Traitement interrompu."}
        try:
            job.status = "running"
            self._publish(job)
            response = await work()
            if not isinstance(response, StreamingResponse):
                raise RuntimeError("Expected a progress stream")
            pending = ""
            async for chunk in response.body_iterator:
                pending += chunk if isinstance(chunk, str) else bytes(chunk).decode("utf-8")
                while "\n\n" in pending:
                    frame, pending = pending.split("\n\n", 1)
                    data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
                    if not data:
                        continue
                    event = json.loads(data)
                    if event.get("status") in {"complete", "error"}:
                        terminal = event
                    else:
                        job.event = event
                        self._publish(job)
            if terminal is None:
                raise RuntimeError("Progress stream ended without a result")
            # Publish completion only AFTER the producer's post-yield phase
            # updates and cleanup. Observers may immediately start the next step.
            if terminal.get("status") == "complete":
                # Keep the registry and shared snapshots small. A batch of
                # 20 tabs must not deserialize every other project's words
                # and match alternatives, nor rewrite all results per update.
                await file_io(lambda: write_text_atomic(self._result_path(job.id), json.dumps(terminal)))
                job.result_available = True
                final_event = {key: value for key, value in terminal.items()
                               if key in {"status", "progress", "message", "error"}}
            else:
                final_event = terminal
            final_status = "error" if terminal.get("status") == "error" else "complete"
        except asyncio.CancelledError:
            final_event = {"status": "error", "error": "Traitement annulé."}
            raise
        except Exception as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            final_event = {"status": "error", "error": str(detail)}
            logger.exception("Progress job %s failed", job.id)
        finally:
            if response is not None and hasattr(response, "body_iterator"):
                with suppress(Exception):
                    await response.body_iterator.aclose()
            if final_status == "error":
                job.result_available = False
                await file_io(self._result_path(job.id).unlink, missing_ok=True)
            job.status = final_status
            job.event = final_event
            self._publish(job)
            await self._prune()
            await self._persist()

    async def cancel_project(self, project_id: str):
        # Submission persists before creating the task. Synchronize the
        # snapshot with it so deletion cannot miss an acknowledged writer.
        async with self._lock:
            tasks = [self.tasks[job.id] for job in self.list_jobs(project_id) if job.id in self.tasks]
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._persist()

    async def remove_project(self, project_id: str):
        await self.cancel_project(project_id)
        for job in self.list_jobs(project_id):
            self.jobs.pop(job.id, None)
            await file_io(self._result_path(job.id).unlink, missing_ok=True)
        await self._persist()
        event_hub.refresh_topic(TOPIC)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._persist()


progress_jobs = ProgressJobs()


async def progress_response(project_id: str, kind: str, parameters: dict[str, Any], work: Work, prefer: str | None):
    """Keep the legacy SSE contract for CLI clients; the UI uses respond-async."""
    if prefer != "respond-async":
        return await work()
    from .project_service import ProjectService
    if await ProjectService.aload(project_id) is None:
        raise HTTPException(404, "Project not found")
    job = await progress_jobs.start(project_id, kind, parameters, work)
    return JSONResponse(job.model_dump(mode="json"), status_code=202,
                        headers={"Preference-Applied": "respond-async"})
