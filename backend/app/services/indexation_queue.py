"""Async indexation queue with SSE broadcasting."""

import asyncio
import json
import logging
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

from ..library_types import LibraryType
from ..models.torrent import IndexationJob
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService
from .event_hub import event_hub
from .indexation_preflight import IndexationPreflightService
from .library_hydration_service import LibraryHydrationService
from .pending_publish_store import PendingPublishRecord, PendingPublishStore
from .storage_box_progress import ProgressSnapshot
from .storage_box_repository import StorageBoxRepository
from .runtime_memory import log_memory, schedule_release_unused_memory

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".ts", ".m4v"}

logger = logging.getLogger("uvicorn.error")

HUB_TOPIC = "index_jobs"


class IndexationQueueService:
    MAX_CONCURRENT = 2
    # The registry lives for the whole process; without eviction it retains
    # every job ever enqueued (including per-file warning/unmatched lists).
    MAX_TERMINAL_JOBS = 50
    # Background uploads always yield to indexation/matching/other heavy
    # work: they only start when the system is idle, re-checked at this
    # cadence, and run one at a time.
    UPLOAD_IDLE_POLL_SECONDS = 5.0
    # In-process auto-retry backoff after rclone's own retries are exhausted;
    # the durable pending record additionally survives restarts.
    UPLOAD_RETRY_DELAYS = (30.0, 120.0, 600.0)

    def __init__(self) -> None:
        self._jobs: dict[str, IndexationJob] = {}
        self._job_tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._matching_lock = asyncio.Lock()
        self._active_heavy_jobs = 0
        self._active_heavy_kinds: Counter[str] = Counter()
        self._upload_semaphore = asyncio.Semaphore(1)
        self._upload_tasks: dict[str, asyncio.Task] = {}

    def _prune_terminal_jobs(self) -> None:
        terminal_ids = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in {"complete", "error"}
        ]
        for job_id in terminal_ids[: max(0, len(terminal_ids) - self.MAX_TERMINAL_JOBS)]:
            del self._jobs[job_id]

    async def acquire_heavy_slot(self, kind: str, slots: int = 1) -> None:
        """Acquire ``slots`` units of the process-wide heavyweight work budget.

        Multi-slot acquirers (fast-mode matching reserves the whole budget)
        are all serialized behind ``matching_lock``, so two of them can never
        interleave partial acquisitions — the only other acquirers are
        single-slot jobs that always complete and release, hence no deadlock.
        """
        acquired = 0
        try:
            for _ in range(slots):
                await self._semaphore.acquire()
                acquired += 1
        except BaseException:
            # A cancelled waiter (e.g. an SSE client disconnecting while the
            # match waits for its second slot) must hand back what it grabbed,
            # or the budget shrinks permanently.
            for _ in range(acquired):
                self._semaphore.release()
            raise
        self._active_heavy_jobs += 1
        self._active_heavy_kinds[kind] += 1
        log_memory(
            "heavy_job_started",
            heavy_kind=kind,
            heavy_slots=slots,
            heavy_active=self._active_heavy_jobs,
            heavy_kinds=dict(self._active_heavy_kinds),
        )

    def release_heavy_slot(self, kind: str, slots: int = 1) -> None:
        """Release a job's slots and trim native memory once truly idle."""
        if self._active_heavy_kinds[kind] > 0:
            self._active_heavy_kinds[kind] -= 1
            if self._active_heavy_kinds[kind] == 0:
                del self._active_heavy_kinds[kind]
        self._active_heavy_jobs = max(0, self._active_heavy_jobs - 1)
        for _ in range(slots):
            self._semaphore.release()

        details = {
            "heavy_kind": kind,
            "heavy_slots": slots,
            "heavy_active": self._active_heavy_jobs,
            "heavy_kinds": dict(self._active_heavy_kinds),
        }
        if self._active_heavy_jobs == 0:
            # Off the loop and debounced: this fires exactly when the UI is
            # waiting for "job complete", and a full GC + CUDA flush inline
            # here used to freeze every request for up to seconds.
            schedule_release_unused_memory("heavy_jobs_idle", **details)
        else:
            log_memory("heavy_job_finished", **details)

    @asynccontextmanager
    async def heavy_slot(self, kind: str, slots: int = 1):
        await self.acquire_heavy_slot(kind, slots=slots)
        try:
            yield
        finally:
            self.release_heavy_slot(kind, slots=slots)

    def available_heavy_slots(self) -> int:
        """Free units of the heavy budget (for pre-wait UI messages only)."""
        return self._semaphore._value

    def matching_lock(self) -> asyncio.Lock:
        """Serialize access to the process-global matcher singleton state."""
        return self._matching_lock

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
                # Upload jobs never block a new index/update of the same
                # series: the fresh publish supersedes the pending one.
                existing_job.job_type != "upload"
                and existing_job.library_type == library_type
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
        self._prune_terminal_jobs()
        self._jobs[job.id] = job
        self._broadcast(job)
        task = asyncio.create_task(self._run_job(job))
        self._job_tasks.add(task)
        task.add_done_callback(self._job_tasks.discard)
        return job.id

    @classmethod
    def _is_cuda_oom_error(cls, message: str | None) -> bool:
        return AnimeLibraryService._is_cuda_oom_error(message)

    def _reset_job_transient_fields(self, job: IndexationJob) -> None:
        job.error = None
        job.current_file = None
        job.total_files = 0
        job.completed_files = 0
        job.current_file_progress = None
        job.current_file_frames_processed = None
        job.current_file_total_frames = None
        job.current_file_batches_processed = None
        job.requested_batch_size = None
        job.effective_batch_size = None
        job.effective_decode_backend = None
        job.retry_reason = None
        self._reset_network_progress_fields(job)

    def _reset_network_progress_fields(self, job: IndexationJob) -> None:
        job.network_bytes_transferred = None
        job.network_bytes_total = None
        job.network_mib_per_sec = None
        job.network_eta_seconds = None
        job.network_active_transfers = 0

    def _make_network_progress_callback(
        self,
        job: IndexationJob,
        *,
        progress_span: tuple[float, float] | None = None,
        phase: str | None = None,
        message: str | None = None,
    ):
        """Mirror transfer snapshots onto the job's network_* fields.

        When ``progress_span`` is given, the overall job.progress also advances
        linearly with the transferred bytes across that (start, end) span, and
        the phase/message flip as soon as the first snapshot arrives — the
        callbacks drive the phase transitions, not guessed pins.
        """

        async def _on_progress(snapshot: ProgressSnapshot) -> None:
            job.network_bytes_transferred = snapshot.bytes_transferred
            job.network_bytes_total = snapshot.bytes_total
            job.network_mib_per_sec = snapshot.mib_per_sec
            job.network_eta_seconds = snapshot.eta_seconds
            job.network_active_transfers = snapshot.active_transfers
            if progress_span is not None and snapshot.bytes_total > 0:
                span_start, span_end = progress_span
                fraction = min(
                    1.0, snapshot.bytes_transferred / snapshot.bytes_total
                )
                job.progress = max(
                    job.progress, span_start + (span_end - span_start) * fraction
                )
            if phase is not None and job.phase != phase:
                job.phase = phase
                if message is not None:
                    job.message = message
            self._broadcast(job)

        return _on_progress

    def _make_hashing_progress_callback(
        self,
        job: IndexationJob,
        *,
        progress_span: tuple[float, float] = (0.90, 0.99),
    ):
        """Thread-safe packaging (hashing) progress over ``progress_span``.

        Called from hashing worker threads, so mutations are marshalled back
        onto the event loop.
        """
        loop = asyncio.get_running_loop()
        span_start, span_end = progress_span

        def _hashing_progress(bytes_hashed: int, bytes_to_hash: int) -> None:
            def _apply() -> None:
                if bytes_to_hash > 0:
                    job.progress = max(
                        job.progress,
                        span_start
                        + (span_end - span_start)
                        * min(1.0, bytes_hashed / bytes_to_hash),
                    )
                self._broadcast(job)

            loop.call_soon_threadsafe(_apply)

        return _hashing_progress

    @classmethod
    def _terminal_oom_message(cls, message: str) -> str:
        return AnimeLibraryService._terminal_cuda_oom_message(message)

    def _finalize_job_error(self, job: IndexationJob, message: str) -> None:
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

    async def _run_job(self, job: IndexationJob) -> None:
        slot_held = False
        try:
            async with AsyncExitStack() as stack:
                current_update_manifest: dict | None = None
                if job.job_type == "update":
                    # Hydration is pure network I/O: run it before taking a
                    # heavy slot so a multi-minute Storage Box download never
                    # blocks GPU work, and surface byte progress on the job
                    # (the jobs panel renders the network_* fields).
                    self._reset_job_transient_fields(job)
                    job.status = "indexing"
                    job.phase = "hydrate_index"
                    job.message = "Hydrating matcher cache from Storage Box..."
                    job.progress = 0.02
                    self._broadcast(job)
                    if not job.series_id:
                        # A finalized-but-not-yet-uploaded series is invisible
                        # remotely; its pending record is the authority.
                        pending = await asyncio.to_thread(
                            PendingPublishStore.find_by_display_name,
                            job.library_type.value,
                            job.source_name,
                        )
                        if pending is not None:
                            job.series_id = pending.series_id
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
                    # Hold the series lock across the entire update+publish so
                    # the flow cannot race with eviction, deletion, hydration,
                    # or another concurrent update on the same series. Taking
                    # it before the heavy slot matches the matching routes'
                    # order (they hydrate before acquiring slots), and index/
                    # update jobs for the same series are deduped at enqueue,
                    # so lock-then-slot cannot form a cycle with the publish
                    # path's slot-then-lock.
                    await stack.enter_async_context(
                        LibraryHydrationService._series_lock(
                            job.library_type, job.series_id
                        )
                    )
                    network_progress = self._make_network_progress_callback(job)

                    async def _hydration_progress(snapshot: ProgressSnapshot) -> None:
                        if snapshot.bytes_total:
                            job.progress = 0.02 + 0.08 * min(
                                1.0, snapshot.bytes_transferred / snapshot.bytes_total
                            )
                        await network_progress(snapshot)

                    # Local-first baseline: for a series whose upload is still
                    # pending, the remote has no current.json — the locally
                    # cached manifest written at finalize time is authoritative
                    # (and for uploaded series it skips a remote round-trip).
                    if await LibraryHydrationService.ensure_index_ready(
                        library_type=job.library_type,
                        series_id=job.series_id,
                    ):
                        current_update_manifest = (
                            await LibraryHydrationService._load_or_fetch_manifest(
                                job.library_type,
                                job.series_id,
                            )
                        )
                        job.progress = 0.10
                    else:
                        current_update_manifest = (
                            await LibraryHydrationService.ensure_series_index_hydrated(
                                library_type=job.library_type,
                                series_id=job.series_id,
                                already_locked=True,
                                network_progress_callback=_hydration_progress,
                            )
                        )
                    self._reset_network_progress_fields(job)
                    job.message = "Waiting for a processing slot..."
                    self._broadcast(job)

                await self.acquire_heavy_slot("indexation")
                slot_held = True
                self._reset_job_transient_fields(job)
                job.status = "indexing"
                job.phase = "starting"
                self._broadcast(job)
                if job.job_type == "update":
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
                    # Indexing owns the 0→0.90 span of the overall bar; the
                    # remaining 0.10 belongs to packaging (hashing) and upload.
                    job.progress = min(progress.progress, 1.0) * 0.90
                    job.phase = progress.status
                    job.message = progress.message
                    job.current_file = progress.current_file or None
                    job.total_files = progress.total_files
                    job.completed_files = progress.completed_files
                    job.current_file_progress = progress.current_file_progress
                    job.current_file_frames_processed = progress.current_file_frames_processed
                    job.current_file_total_frames = progress.current_file_total_frames
                    job.current_file_batches_processed = progress.current_file_batches_processed
                    if progress.requested_batch_size is not None:
                        job.requested_batch_size = progress.requested_batch_size
                    if progress.effective_batch_size is not None:
                        job.effective_batch_size = progress.effective_batch_size
                    if progress.effective_decode_backend is not None:
                        job.effective_decode_backend = progress.effective_decode_backend
                    if progress.retry_reason is not None:
                        job.retry_reason = progress.retry_reason
                    if progress.warnings:
                        for warning in progress.warnings:
                            if warning not in job.warnings:
                                job.warnings.append(warning)
                    if progress.status == "complete":
                        # Everything after the searcher stream is disk and
                        # network work (torrent linking, packaging, upload):
                        # hand the GPU slot back so a queued heavy job can
                        # start during the publish.
                        self.release_heavy_slot("indexation")
                        slot_held = False
                        job.phase = "link_sources"
                        job.message = "Linking fallback torrent sources..."
                        self._broadcast(job)
                        await self._link_torrents(job)
                        job.phase = "package_release"
                        job.message = "Packaging release for Storage Box..."
                        job.progress = max(job.progress, 0.90)
                        self._reset_network_progress_fields(job)
                        self._broadcast(job)
                        prepared = progress.prepared_library_paths or []
                        expected_min_episodes = None
                        merge_existing_release = False
                        if job.job_type == "update" and current_update_manifest:
                            merge_existing_release = True
                            remote_episode_keys = {
                                str(item.get("episode_key") or "").strip()
                                for item in current_update_manifest.get("episodes", [])
                                if isinstance(item, dict)
                                and str(item.get("episode_key") or "").strip()
                            }
                            prepared_episode_keys = {
                                Path(path).stem
                                for path in prepared
                                if str(path).strip()
                            }
                            expected_min_episodes = len(
                                remote_episode_keys | prepared_episode_keys
                            )
                        # A still-pending upload of an older release for this
                        # series is about to be superseded — stop its transfer
                        # before finalize deletes its record and remote staging.
                        await self._cancel_stale_upload_for(
                            job.library_type, job.source_name
                        )
                        record = await LibraryHydrationService.finalize_series_release(
                            library_type=job.library_type,
                            display_name=job.source_name,
                            series_id=job.series_id or None,
                            already_locked=(
                                job.job_type == "update" and bool(job.series_id)
                            ),
                            expected_min_episodes=expected_min_episodes,
                            merge_existing_release=merge_existing_release,
                            hashing_progress_callback=(
                                self._make_hashing_progress_callback(job)
                            ),
                        )
                        job.series_id = record.series_id
                        job.storage_release_id = record.release_id
                        job.publish_id = record.publish_id
                        job.message = (
                            "Series indexed — available locally; "
                            "Storage Box upload queued in background"
                        )
                        job.progress = 1.0
                        job.status = "complete"
                        job.error = None
                        self._reset_network_progress_fields(job)
                        AnimeMatcherService.mark_series_updated(
                            job.library_type, job.source_name
                        )
                        self.enqueue_upload(record)
                    elif progress.status == "error":
                        self._finalize_job_error(
                            job,
                            progress.error or progress.message or "Indexation failed",
                        )
                        return
                    self._broadcast(job)
        except Exception as e:
            self._finalize_job_error(job, str(e))
            logger.exception("Indexation job %s failed", job.id)
        finally:
            if slot_held:
                self.release_heavy_slot("indexation")

    def enqueue_upload(self, record: PendingPublishRecord) -> str:
        """Register a background upload job for a pending publish.

        Upload jobs share the indexation jobs registry/SSE panel but never
        touch the GPU heavy-slot budget; an idle gate defers the actual
        transfer while any indexation/matching/heavy work is active.
        """
        for existing_job in self._jobs.values():
            if (
                existing_job.job_type == "upload"
                and existing_job.publish_id == record.publish_id
                and existing_job.status in {"queued", "indexing"}
            ):
                return existing_job.id

        job = IndexationJob(
            job_type="upload",
            source_name=record.display_name,
            library_type=LibraryType(record.library_type),
            source_path=record.series_dir,
            series_id=record.series_id,
            storage_release_id=record.release_id,
            publish_id=record.publish_id,
            phase="upload_release",
            message="Storage Box upload queued...",
        )
        self._prune_terminal_jobs()
        self._jobs[job.id] = job
        self._broadcast(job)
        task = asyncio.create_task(self._run_upload_job(job, record))
        self._upload_tasks[record.publish_id] = task
        self._job_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._job_tasks.discard(done_task)
            if self._upload_tasks.get(record.publish_id) is done_task:
                del self._upload_tasks[record.publish_id]

        task.add_done_callback(_cleanup)
        return job.id

    async def cancel_upload(self, publish_id: str) -> None:
        """Cancel the queued/running upload job for ``publish_id`` and wait
        for it to settle. The pending record itself is left untouched — the
        caller decides whether it is superseded, deleted, or retried."""
        task = self._upload_tasks.get(publish_id)
        if task is None:
            return
        task.cancel()
        with suppress(BaseException):
            await task

    async def _cancel_stale_upload_for(
        self, library_type: LibraryType, display_name: str
    ) -> None:
        record = await asyncio.to_thread(
            PendingPublishStore.find_by_display_name,
            library_type.value,
            display_name,
        )
        if record is not None:
            await self.cancel_upload(record.publish_id)

    def _upload_busy_reason(self) -> str | None:
        """What the upload should yield to right now, or None when idle.

        Uploads are strictly lowest-priority: any queued/active indexation
        job or any held heavy slot (matching, transcription, forced
        alignment, indexation) defers them.
        """
        for other in self._jobs.values():
            if other.job_type != "upload" and other.status in {"queued", "indexing"}:
                return f"indexation '{other.source_name}'"
        if self._active_heavy_jobs > 0:
            kinds = ", ".join(sorted(self._active_heavy_kinds)) or "heavy work"
            return kinds
        return None

    async def _wait_until_idle(self, job: IndexationJob) -> None:
        last_reason: str | None = None
        while (reason := self._upload_busy_reason()) is not None:
            if reason != last_reason:
                last_reason = reason
                job.message = f"Upload waiting for {reason} to finish..."
                self._broadcast(job)
                logger.info("Upload job %s yielding to %s", job.id, reason)
            await asyncio.sleep(self.UPLOAD_IDLE_POLL_SECONDS)

    async def _run_upload_job(
        self, job: IndexationJob, record: PendingPublishRecord
    ) -> None:
        try:
            async with self._upload_semaphore:
                await self._wait_until_idle(job)
                job.status = "indexing"
                attempt = 0
                while True:
                    try:
                        job.error = None
                        self._reset_network_progress_fields(job)
                        job.phase = "upload_release"
                        job.message = "Uploading release to Storage Box..."
                        self._broadcast(job)
                        result = await LibraryHydrationService.run_pending_upload(
                            record,
                            progress_callback=self._make_network_progress_callback(
                                job,
                                progress_span=(0.0, 0.995),
                                phase="upload_release",
                            ),
                        )
                        break
                    except Exception as exc:
                        if attempt >= len(self.UPLOAD_RETRY_DELAYS):
                            raise
                        delay = self.UPLOAD_RETRY_DELAYS[attempt]
                        attempt += 1
                        logger.warning(
                            "Upload job %s for '%s' failed (attempt %d/%d): %s; "
                            "retrying in %.0fs",
                            job.id,
                            job.source_name,
                            attempt,
                            len(self.UPLOAD_RETRY_DELAYS) + 1,
                            exc,
                            delay,
                        )
                        job.phase = "upload_retry_wait"
                        job.message = (
                            f"Upload failed ({exc}); retrying in {int(delay)}s"
                        )
                        self._broadcast(job)
                        await asyncio.sleep(delay)

                self._reset_network_progress_fields(job)
                if result is None:
                    # Superseded/cancelled between enqueue and execution.
                    job.message = "Upload superseded by a newer publish"
                    job.phase = "superseded"
                else:
                    job.series_id = str(result["series_id"])
                    job.storage_release_id = str(result["release_id"])
                    job.message = "Release uploaded to Storage Box"
                job.progress = 1.0
                job.status = "complete"
                job.error = None
                self._broadcast(job)
        except asyncio.CancelledError:
            self._reset_job_transient_fields(job)
            job.status = "error"
            job.phase = "cancelled"
            job.error = "Upload cancelled (superseded by a newer indexation)."
            self._broadcast(job)
            raise
        except Exception as e:
            self._finalize_job_error(job, str(e))
            logger.exception("Upload job %s failed", job.id)

    async def resume_pending_uploads(self) -> None:
        """Re-enqueue every durable pending publish (called at startup).

        Re-applies the local availability state first — this is what heals
        the local 'orphan folder' left by a crash between finalize and the
        end of the upload — then queues the upload itself.
        """
        if not StorageBoxRepository.is_enabled():
            return
        records = await asyncio.to_thread(PendingPublishStore.list_all)
        for record in records:
            try:
                await LibraryHydrationService.apply_local_publish_state(record)
            except Exception:
                logger.exception(
                    "Failed to re-apply local publish state for pending "
                    "publish %s ('%s')",
                    record.publish_id,
                    record.display_name,
                )
            logger.info(
                "Resuming pending Storage Box upload %s for '%s'",
                record.publish_id,
                record.display_name,
            )
            self.enqueue_upload(record)

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
        event_hub.publish(HUB_TOPIC, key=job.id, data=job.model_dump(mode="json"))

    def list_jobs(self) -> list[IndexationJob]:
        return list(self._jobs.values())

    def gpu_semaphore(self) -> asyncio.Semaphore:
        """The shared GPU-concurrency budget (``MAX_CONCURRENT`` slots).

        Indexation jobs (:meth:`_run_job`) and ``/matches`` both acquire this so
        the 8 GB card is never oversubscribed. Slot weights encode measured
        footprints: a fast-mode matching run (``ATR_FAST_MATCHING`` GPU decode,
        PyNv sessions + fp32 SSCD + correspondences) holds ~5.3 GB in-process
        (measured 2026-07-19: 5.25 GB matching + 1.03 GB indexer subprocess +
        4x274 MB ffmpeg_cuda NVDEC decoders OOM'd the 7.65 GB card), so it
        reserves the WHOLE budget (``slots=MAX_CONCURRENT``); mainline matching
        (``ATR_FAST_MATCHING=0``) and indexation jobs are 1 slot each — two
        concurrent GPU-decode indexations still fit. A CUDA OOM inside a task
        is absorbed by the embedder's cache-clear retry
        (:meth:`AnimeMatcherService._embed_pil_batch`) and, for jobs, the
        batch-downshift retry then the terminal-OOM path; the weights make such
        contention rare rather than the norm.
        """
        return self._semaphore

    def gpu_slots_in_use(self) -> int:
        """Busy GPU slots right now (0..MAX_CONCURRENT). Reads the asyncio
        semaphore's internal counter — advisory only, used to size the NVDEC
        session budget; correctness never depends on it."""
        return self.MAX_CONCURRENT - self._semaphore._value


# Singleton
indexation_queue = IndexationQueueService()
