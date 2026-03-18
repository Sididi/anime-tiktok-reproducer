"""Async indexation queue with SSE broadcasting."""

import asyncio
import logging
from pathlib import Path

from ..library_types import LibraryType
from ..models.torrent import IndexationJob
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService

logger = logging.getLogger("uvicorn.error")


class IndexationQueueService:
    MAX_CONCURRENT = 2

    def __init__(self) -> None:
        self._jobs: dict[str, IndexationJob] = {}
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._subscribers: list[asyncio.Queue[dict]] = []

    async def enqueue(
        self,
        source_path: str,
        library_type: LibraryType,
        anime_name: str | None,
        fps: float,
    ) -> str:
        job = IndexationJob(
            source_name=anime_name or Path(source_path).name,
            library_type=library_type,
            source_path=source_path,
            fps=fps,
        )
        self._jobs[job.id] = job
        self._broadcast(job)
        asyncio.create_task(self._run_job(job))
        return job.id

    async def _run_job(self, job: IndexationJob) -> None:
        await self._semaphore.acquire()
        try:
            job.status = "indexing"
            self._broadcast(job)
            async for progress in AnimeLibraryService.index_anime(
                source_folder=Path(job.source_path),
                library_type=job.library_type,
                anime_name=job.source_name,
                fps=job.fps,
            ):
                job.progress = progress.progress
                job.phase = progress.status
                job.message = progress.message
                if progress.status == "complete":
                    job.status = "complete"
                    AnimeMatcherService.mark_series_updated(
                        job.library_type, job.source_name
                    )
                elif progress.status == "error":
                    job.status = "error"
                    job.error = progress.error
                self._broadcast(job)
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            self._broadcast(job)
            logger.exception("Indexation job %s failed", job.id)
        finally:
            self._semaphore.release()

    def _broadcast(self, job: IndexationJob) -> None:
        data = job.model_dump(mode="json")
        for queue in self._subscribers:
            queue.put_nowait(data)

    async def stream_all_jobs(self):
        """Yield job state dicts for SSE streaming."""
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            # Send current state first
            for job in self._jobs.values():
                yield job.model_dump(mode="json")
            # Then stream updates
            while True:
                data = await queue.get()
                yield data
        finally:
            self._subscribers.remove(queue)

    def list_jobs(self) -> list[IndexationJob]:
        return list(self._jobs.values())


# Singleton
indexation_queue = IndexationQueueService()
