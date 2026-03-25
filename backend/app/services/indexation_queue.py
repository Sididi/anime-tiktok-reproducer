"""Async indexation queue with SSE broadcasting."""

import asyncio
import json
import logging
from pathlib import Path

from ..library_types import LibraryType
from ..models.torrent import IndexationJob
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService
from .library_hydration_service import LibraryHydrationService
from .storage_box_repository import StorageBoxRepository

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".ts", ".m4v"}

logger = logging.getLogger("uvicorn.error")


class IndexationQueueService:
    MAX_CONCURRENT = 1

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
        source_name = anime_name or Path(source_path).name
        for existing_job in self._jobs.values():
            if (
                existing_job.library_type == library_type
                and existing_job.source_name == source_name
                and existing_job.status in {"queued", "indexing"}
            ):
                return existing_job.id

        job = IndexationJob(
            source_name=source_name,
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
                    job.phase = "link_sources"
                    job.message = "Linking fallback torrent sources..."
                    self._broadcast(job)
                    await self._link_torrents(job)
                    job.phase = "package_release"
                    job.message = "Packaging release for Storage Box..."
                    job.progress = max(job.progress, 0.96)
                    self._broadcast(job)
                    job.phase = "upload_release"
                    job.message = "Uploading release to Storage Box..."
                    job.progress = max(job.progress, 0.98)
                    self._broadcast(job)
                    publish_result = await StorageBoxRepository.publish_series(
                        library_type=job.library_type,
                        display_name=job.source_name,
                    )
                    job.series_id = str(publish_result["series_id"])
                    job.storage_release_id = str(publish_result["release_id"])
                    await LibraryHydrationService.sync_local_series_state(
                        library_type=job.library_type,
                        series_id=job.series_id,
                        release_id=job.storage_release_id,
                    )
                    job.message = "Published release to Storage Box"
                    job.progress = 1.0
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

    async def _link_torrents(self, job: IndexationJob) -> None:
        """Best-effort torrent linking after successful indexation."""
        try:
            from .qbittorrent import QBittorrentClient
            from .torrent_linker import TorrentLinkerService
        except ImportError:
            logger.debug("httpx not installed, skipping torrent linking")
            return

        library_root = AnimeLibraryService.get_library_path(
            library_type=job.library_type
        )
        source_dir = library_root / job.source_name
        if not source_dir.exists():
            return

        # Collect original source paths from .atr_source.json files
        source_files: list[Path] = []
        for meta_file in source_dir.glob("*.atr_source.json"):
            try:
                meta = json.loads(meta_file.read_text())
                original = Path(meta["source_path"])
                source_files.append(original)
            except (KeyError, json.JSONDecodeError):
                continue

        if not source_files:
            return

        qbt = QBittorrentClient()
        try:
            metadata, unmatched = await TorrentLinkerService.link_files_to_torrents(
                source_files, qbt
            )
            # Preserve existing protection status if metadata already exists
            existing = TorrentLinkerService.load_metadata(source_dir)
            if existing:
                metadata.purge_protection = existing.purge_protection
            if metadata.torrents:
                TorrentLinkerService.save_metadata(source_dir, metadata)
            job.linked_torrents = len(metadata.torrents)
            job.unmatched_files = [p.name for p in unmatched]
            if unmatched:
                logger.warning(
                    "%d file(s) not linked to any torrent for %s: %s",
                    len(unmatched),
                    job.source_name,
                    [p.name for p in unmatched],
                )
            if metadata.torrents:
                logger.info(
                    "Linked %d torrent(s) for %s",
                    len(metadata.torrents),
                    job.source_name,
                )
            self._broadcast(job)
        except Exception:
            logger.debug("Torrent linking failed for %s (non-fatal)", job.source_name)
        finally:
            await qbt.close()

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
