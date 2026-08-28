"""One Drive export job per project, independent of the browser that started it.

The /processing page drives the export over an SSE response. The upload used
to run inside that response's task: a browser that gave up (the page's stall
watchdog, a closed tab) silently orphaned a still-running upload — no
Discord / Premiere-Link notification when it eventually finished, and every
retry answered "upload already in progress" until then.

Now the job is a detached task registered per project. Any number of SSE
streams may attach to it (a retry or a reopened page reconnects to the
running job and receives its latest frame first), and the completion work
(persisting the folder id, notifications) runs whether or not anyone is
still watching.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger("uvicorn.error")

ProgressCallback = Callable[[dict[str, Any]], None]
Runner = Callable[[ProgressCallback], Awaitable[dict[str, Any]]]
OnComplete = Callable[[dict[str, Any]], Awaitable[None] | None]

_END = object()


@dataclass
class DriveExportJob:
    project_id: str
    task: "asyncio.Task[None] | None" = None
    latest: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    finished: bool = False
    _subscribers: list["asyncio.Queue[Any]"] = field(default_factory=list, repr=False)

    def publish(self, frame: dict[str, Any]) -> None:
        self.latest = frame
        for queue in list(self._subscribers):
            queue.put_nowait(frame)

    def _finish(self) -> None:
        self.finished = True
        for queue in list(self._subscribers):
            queue.put_nowait(_END)

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """Latest frame first (late attachers catch up), then live frames until the end."""
        if self.finished:
            if self.latest is not None:
                yield self.latest
            return
        queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            if self.latest is not None:
                yield self.latest
            while True:
                item = await queue.get()
                if item is _END:
                    return
                yield item
        finally:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass


class DriveExportJobs:
    _jobs: dict[str, DriveExportJob] = {}

    @classmethod
    def running(cls, project_id: str) -> DriveExportJob | None:
        job = cls._jobs.get(project_id)
        if job is None or job.finished:
            return None
        return job

    @classmethod
    def start_or_attach(
        cls,
        project_id: str,
        *,
        runner: Runner,
        on_complete: OnComplete | None = None,
    ) -> tuple[DriveExportJob, bool]:
        """Return ``(job, attached)``; must be called on the event loop thread.

        ``runner(progress_callback)`` performs the upload and returns its
        result; ``on_complete(result)`` runs afterwards even when no client is
        attached any more.
        """
        live = cls.running(project_id)
        if live is not None:
            return live, True
        job = DriveExportJob(project_id=project_id)
        cls._jobs[project_id] = job
        job.task = asyncio.get_running_loop().create_task(
            cls._run(job, runner, on_complete), name=f"drive-export:{project_id}"
        )
        return job, False

    @classmethod
    async def wait(cls, project_id: str) -> None:
        """Await the live job for ``project_id`` (tests, tooling)."""
        job = cls._jobs.get(project_id)
        if job is not None and job.task is not None:
            try:
                await job.task
            except (asyncio.CancelledError, Exception):
                return

    @classmethod
    async def _run(
        cls, job: DriveExportJob, runner: Runner, on_complete: OnComplete | None
    ) -> None:
        try:
            try:
                job.result = await runner(job.publish)
            except asyncio.CancelledError:
                job.error = RuntimeError("Drive upload cancelled")
                job._finish()
                raise
            except Exception as exc:
                job.error = exc
                logger.warning(
                    "Drive export failed: project_id=%s error=%s",
                    job.project_id,
                    exc,
                    exc_info=True,
                )
                job._finish()
                return
            # Subscribers get their terminal frame before the follow-up work
            # (Discord, Premiere Link) runs; that work no longer depends on a
            # browser still listening.
            job._finish()
            if on_complete is not None:
                try:
                    outcome = on_complete(job.result)
                    if inspect.isawaitable(outcome):
                        await outcome
                except Exception:
                    logger.warning(
                        "Drive export completion hook failed: project_id=%s",
                        job.project_id,
                        exc_info=True,
                    )
        finally:
            if cls._jobs.get(job.project_id) is job:
                cls._jobs.pop(job.project_id, None)
