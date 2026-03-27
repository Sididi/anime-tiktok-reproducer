"""Async indexation queue with SSE broadcasting."""

import asyncio
import json
import logging
from pathlib import Path

from ..library_types import LibraryType
from ..models.torrent import IndexationJob
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService
from .indexation_preflight import IndexationPreflightService
from .library_hydration_service import LibraryHydrationService
from .storage_box_repository import StorageBoxRepository

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".ts", ".m4v"}

logger = logging.getLogger("uvicorn.error")


class IndexationQueueService:
    MAX_CONCURRENT = 2
    CUDA_OOM_RETRY_LIMIT = 1
    CUDA_OOM_RETRY_BACKOFF_SECONDS = 0.0
    _CUDA_OOM_MARKERS = (
        "cuda out of memory",
        "torch.cuda.outofmemoryerror",
        "tried to allocate",
        "pytorch_cuda_alloc_conf",
        "expandable_segments:true",
    )

    def __init__(self) -> None:
        self._jobs: dict[str, IndexationJob] = {}
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._subscribers: list[asyncio.Queue[dict]] = []
        self._oom_retry_counts: dict[str, int] = {}

    async def enqueue(
        self,
        source_path: str,
        library_type: LibraryType,
        anime_name: str | None,
        fps: float,
        job_type: str = "index",
        series_id: str | None = None,
    ) -> str:
        source_name = anime_name or Path(source_path).name
        normalized_target = IndexationPreflightService.normalize_target_name(source_name)
        for existing_job in self._jobs.values():
            if (
                existing_job.library_type == library_type
                and IndexationPreflightService.normalize_target_name(existing_job.source_name)
                == normalized_target
                and existing_job.status in {"queued", "indexing"}
            ):
                return existing_job.id

        job = IndexationJob(
            job_type=job_type,
            source_name=source_name,
            library_type=library_type,
            source_path=source_path,
            fps=fps,
            series_id=series_id,
        )
        self._jobs[job.id] = job
        self._broadcast(job)
        asyncio.create_task(self._run_job(job))
        return job.id

    @classmethod
    def _is_cuda_oom_error(cls, message: str | None) -> bool:
        if not message:
            return False
        normalized = message.casefold()
        return all(marker in normalized for marker in ("cuda", "memory")) or any(
            marker in normalized for marker in cls._CUDA_OOM_MARKERS
        )

    def _reset_job_transient_fields(self, job: IndexationJob) -> None:
        job.error = None
        job.current_file = None
        job.total_files = 0
        job.completed_files = 0
        job.current_file_progress = None
        job.current_file_frames_processed = None
        job.current_file_total_frames = None
        job.current_file_batches_processed = None

    @classmethod
    def _terminal_oom_message(cls, message: str) -> str:
        guidance = (
            " Retry also failed; this job exceeded available VRAM under the current parallel load."
        )
        return message if message.endswith(guidance.strip()) else f"{message}{guidance}"

    def _should_retry_cuda_oom(self, job: IndexationJob, message: str | None) -> bool:
        return self._is_cuda_oom_error(message) and (
            self._oom_retry_counts.get(job.id, 0) < self.CUDA_OOM_RETRY_LIMIT
        )

    def _queue_cuda_oom_retry(self, job: IndexationJob) -> None:
        self._oom_retry_counts[job.id] = self._oom_retry_counts.get(job.id, 0) + 1
        self._reset_job_transient_fields(job)
        job.status = "queued"
        job.phase = "retry_wait"
        job.progress = 0.0
        job.message = (
            "CUDA OOM detected; waiting to retry once with the same settings."
        )
        self._broadcast(job)

    def _finalize_job_error(self, job: IndexationJob, message: str) -> bool:
        if self._should_retry_cuda_oom(job, message):
            self._queue_cuda_oom_retry(job)
            return True

        self._reset_job_transient_fields(job)
        job.status = "error"
        job.phase = "error"
        job.message = None
        job.error = (
            self._terminal_oom_message(message)
            if self._is_cuda_oom_error(message)
            else message
        )
        self._broadcast(job)
        self._oom_retry_counts.pop(job.id, None)
        return False

    async def _retry_job(self, job: IndexationJob) -> None:
        backoff = max(0.0, float(self.CUDA_OOM_RETRY_BACKOFF_SECONDS))
        if backoff > 0:
            await asyncio.sleep(backoff)
        else:
            await asyncio.sleep(0)
        await self._run_job(job)

    async def _run_job(self, job: IndexationJob) -> None:
        retry_requested = False
        await self._semaphore.acquire()
        try:
            self._reset_job_transient_fields(job)
            job.status = "indexing"
            job.phase = "starting"
            if self._oom_retry_counts.get(job.id, 0) > 0:
                job.message = "Retrying after CUDA OOM with the same settings..."
            self._broadcast(job)
            if job.job_type == "update":
                job.phase = "hydrate_index"
                job.message = "Hydrating matcher cache from Storage Box..."
                job.progress = 0.02
                self._broadcast(job)
                if not job.series_id:
                    resolved_series_id = await StorageBoxRepository.find_remote_series_id_by_name(
                        job.library_type,
                        job.source_name,
                    )
                    job.series_id = str(resolved_series_id or "")
                if not job.series_id:
                    raise RuntimeError(
                        f"Remote series not found for update target '{job.source_name}'."
                    )
                await LibraryHydrationService.ensure_series_index_hydrated(
                    library_type=job.library_type,
                    series_id=job.series_id,
                )
                source_files = self._collect_direct_video_files(Path(job.source_path))
                if not source_files:
                    raise RuntimeError(f"No video files found in {job.source_path}")
                progress_stream = AnimeLibraryService.update_anime(
                    library_type=job.library_type,
                    anime_name=job.source_name,
                    source_paths=source_files,
                )
            else:
                progress_stream = AnimeLibraryService.index_anime(
                    source_folder=Path(job.source_path),
                    library_type=job.library_type,
                    anime_name=job.source_name,
                    fps=job.fps,
                )

            async for progress in progress_stream:
                job.progress = progress.progress
                job.phase = progress.status
                job.message = progress.message
                job.current_file = progress.current_file or None
                job.total_files = progress.total_files
                job.completed_files = progress.completed_files
                job.current_file_progress = progress.current_file_progress
                job.current_file_frames_processed = progress.current_file_frames_processed
                job.current_file_total_frames = progress.current_file_total_frames
                job.current_file_batches_processed = progress.current_file_batches_processed
                if progress.warnings:
                    for warning in progress.warnings:
                        if warning not in job.warnings:
                            job.warnings.append(warning)
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
                    publish_result = await LibraryHydrationService.publish_series_release(
                        library_type=job.library_type,
                        display_name=job.source_name,
                        series_id=job.series_id or None,
                    )
                    job.series_id = str(publish_result["series_id"])
                    job.storage_release_id = str(publish_result["release_id"])
                    job.message = "Published release to Storage Box"
                    job.progress = 1.0
                    job.status = "complete"
                    job.error = None
                    self._oom_retry_counts.pop(job.id, None)
                    AnimeMatcherService.mark_series_updated(
                        job.library_type, job.source_name
                    )
                elif progress.status == "error":
                    retry_requested = self._finalize_job_error(
                        job,
                        progress.error or progress.message or "Indexation failed",
                    )
                    if retry_requested:
                        break
                    return
                self._broadcast(job)
        except Exception as e:
            retry_requested = self._finalize_job_error(job, str(e))
            if not retry_requested:
                logger.exception("Indexation job %s failed", job.id)
        finally:
            self._semaphore.release()

        if retry_requested:
            asyncio.create_task(self._retry_job(job))

    @staticmethod
    def _collect_direct_video_files(source_folder: Path) -> list[Path]:
        return sorted(
            entry
            for entry in source_folder.iterdir()
            if (
                entry.is_file()
                and entry.suffix.lower() in VIDEO_EXTENSIONS
                and not AnimeLibraryService.is_transient_library_video_path(entry)
            )
        )

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
