"""Pre-warm the shared Drive folder as soon as a project's matches lock.

The /processing Drive export is bound by Google's per-stream upload rate
(measured 7–38 MB/s per file, drifting with the time of day), and a project's
export bytes are almost entirely its source episodes. Those episodes are known
the moment the project enters the script phase — several phases (script, TTS,
processing) before anyone presses "export". This service ships them to
``_SPM_SHARED_SOURCES`` in the background right then, through the same
:class:`DriveSharedSources` path the export uses, so the export later finds
them present and only uploads the small files plus the remote manifest.

Coordination:

* one pre-warm batch runs at a time (FIFO), so a batch gets the whole link;
* inside :meth:`DriveSharedSources.ensure_uploaded` every shared name is
  locked for its list→upload→verify span. A second project reaching the
  script phase with the same episodes, or an export starting while a
  pre-warm is still uploading, waits on exactly those names, re-lists and
  reuses — the same bytes are never uploaded twice;
* the GC race guard holds: the project's local manifest references its files
  (``status: pending``) before any shared write, and every manifest write
  merges with what is already on disk so an overlapping export's references
  are never dropped. A project deleted mid-flight is cancelled and the
  references it held are released through the normal GC.

Nothing here is required for correctness of the export: with the flag off, or
when a pre-warm fails, the export simply uploads what is missing itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from ..models import Project, ProjectPhase, SceneMatch
from .drive_shared_sources import DriveSharedSources, SharedFileRecord
from .executors import run_heavy
from .export_service import ExportService, ManifestEntry
from .google_drive_service import GoogleDriveService
from .project_service import ProjectService
from .rclone_runner import RcloneStats
from .source_hash_service import SourceHashService

logger = logging.getLogger("uvicorn.error")

PENDING_STATUS = "pending"
PREWARMED_STATUS = "prewarmed"
UPLOADED_STATUS = "uploaded"
# Projects in these phases have locked matches and no completed export yet.
RESUME_PHASES = frozenset({ProjectPhase.SCRIPT_RESTRUCTURE, ProjectPhase.PROCESSING})
RESUME_DELAY_SECONDS = 20.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _naive(value: datetime) -> datetime:
    """Project timestamps are naive local; normalise aware ones for comparison."""
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


@dataclass
class PrewarmState:
    project_id: str
    reason: str
    # queued → hashing → waiting? → uploading → done | skipped | failed | cancelled
    status: str = "queued"
    detail: str = ""
    queued_at: str = field(default_factory=_utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    total_files: int = 0
    reused_files: int = 0
    uploaded_files: int = 0
    total_bytes: int = 0
    transferred_bytes: int = 0
    speed_bytes_per_sec: float | None = None
    # Files this run holds references to (for release if the project goes).
    records: list[SharedFileRecord] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "reason": self.reason,
            "status": self.status,
            "detail": self.detail,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_files": self.total_files,
            "reused_files": self.reused_files,
            "uploaded_files": self.uploaded_files,
            "total_bytes": self.total_bytes,
            "transferred_bytes": self.transferred_bytes,
            "speed_bytes_per_sec": self.speed_bytes_per_sec,
        }


class DrivePrewarmService:
    _states: dict[str, PrewarmState] = {}
    _tasks: dict[str, "asyncio.Task[None]"] = {}
    # matches.json signature of the last successful run per project, so
    # re-entering the script phase without touching matches is a no-op.
    _done_signatures: dict[str, str] = {}
    _slot: asyncio.Lock | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(settings.drive_prewarm_enabled) and DriveSharedSources.is_enabled()

    @classmethod
    def status(cls, project_id: str) -> dict[str, Any]:
        state = cls._states.get(project_id)
        if state is None:
            return {"project_id": project_id, "status": "idle", "enabled": cls.is_enabled()}
        payload = state.to_dict()
        payload["enabled"] = cls.is_enabled()
        return payload

    @classmethod
    def schedule(cls, project_id: str, *, reason: str) -> PrewarmState | None:
        """Queue a pre-warm for ``project_id`` (idempotent while one is live).

        Must be called from the event loop thread. Returns ``None`` when the
        feature is off.
        """
        if not cls.is_enabled():
            return None
        live = cls._tasks.get(project_id)
        if live is not None and not live.done():
            return cls._states.get(project_id)
        state = PrewarmState(project_id=project_id, reason=reason)
        cls._states[project_id] = state
        loop = asyncio.get_running_loop()
        cls._loop = loop
        task = loop.create_task(
            cls._run(project_id, state), name=f"drive-prewarm:{project_id}"
        )
        cls._tasks[project_id] = task
        task.add_done_callback(lambda done, pid=project_id: cls._on_task_done(pid, done))
        logger.info("Drive pre-warm queued: project_id=%s reason=%s", project_id, reason)
        return state

    @classmethod
    def request_cancel(cls, project_id: str) -> bool:
        """Cancel a queued/running pre-warm. Safe to call from any thread."""
        task = cls._tasks.get(project_id)
        if task is None or task.done():
            return False
        loop = cls._loop
        if loop is None or loop.is_closed():
            return False
        loop.call_soon_threadsafe(task.cancel)
        return True

    @classmethod
    async def wait(cls, project_id: str) -> None:
        """Await the live pre-warm for ``project_id`` (tests, tooling)."""
        task = cls._tasks.get(project_id)
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                return

    @classmethod
    async def resume_pending(cls, *, delay_seconds: float = RESUME_DELAY_SECONDS) -> int:
        """Queue pre-warms for script/processing-phase projects after a restart."""
        if not cls.is_enabled():
            return 0
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        projects = await ProjectService.alist_all()
        max_age = timedelta(days=max(0, int(settings.drive_prewarm_resume_max_age_days)))
        cutoff = datetime.now() - max_age
        ordered = sorted(
            (p for p in projects if p.phase in RESUME_PHASES),
            key=lambda p: (p.updated_at or p.created_at or datetime.min),
            reverse=True,
        )
        scheduled = 0
        for project in ordered:
            touched = project.updated_at or project.created_at
            if touched is None or _naive(touched) < cutoff:
                # Abandoned for longer than the window: warm it only when the
                # user actually reaches the script phase again.
                continue
            manifest = await asyncio.to_thread(
                DriveSharedSources.load_local_manifest, project.id
            )
            if manifest and manifest.get("status") in (UPLOADED_STATUS, PREWARMED_STATUS):
                continue
            if cls.schedule(project.id, reason="startup-resume") is not None:
                scheduled += 1
        if scheduled:
            logger.info("Drive pre-warm resume queued %d project(s)", scheduled)
        return scheduled

    # ------------------------------------------------------------------ #
    # Sync helpers (heavy pool)                                            #
    # ------------------------------------------------------------------ #

    @classmethod
    def collect_entries(cls, project: Project, matches: list[SceneMatch]) -> list[ManifestEntry]:
        """The ``sources/`` entries the export will ship for this project."""
        folder = ExportService.output_folder_name(project)
        entries: list[ManifestEntry] = []
        seen: set[str] = set()

        def _add(path: Path) -> None:
            if path.name in seen:
                return
            seen.add(path.name)
            entries.append(
                ManifestEntry(relative_path=f"{folder}/sources/{path.name}", source_path=path)
            )

        for source_path in ExportService._collect_episode_sources(project, matches):
            _add(source_path)
        music_path = ExportService._resolve_selected_music_path(project)
        if music_path is not None:
            _add(music_path)
        return entries

    @staticmethod
    def _matches_signature(project_id: str) -> str | None:
        try:
            stat = ProjectService.get_matches_file(project_id).stat()
        except OSError:
            return None
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    @classmethod
    def _persist_manifest(
        cls, project_id: str, records: list[SharedFileRecord], status: str
    ) -> bool:
        """Merge ``records`` into the project's local manifest.

        Returns False (writes nothing) when the project dir is gone — a
        manifest must never resurrect a deleted project as a live reference.
        An export's ``uploaded`` status and folder id are preserved.
        """
        if not ProjectService.get_project_dir(project_id).is_dir():
            return False
        existing = DriveSharedSources.load_local_manifest(project_id) or {}
        merged = DriveSharedSources.merge_records(existing, records)
        final_status = (
            UPLOADED_STATUS if existing.get("status") == UPLOADED_STATUS else status
        )
        DriveSharedSources.persist_local_manifest(
            project_id,
            status=final_status,
            records=merged,
            drive_folder_id=str(existing.get("drive_folder_id") or "") or None,
        )
        return True

    # ------------------------------------------------------------------ #
    # Task body                                                            #
    # ------------------------------------------------------------------ #

    @classmethod
    def _on_task_done(cls, project_id: str, task: "asyncio.Task[None]") -> None:
        if cls._tasks.get(project_id) is task:
            cls._tasks.pop(project_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Drive pre-warm task crashed: project_id=%s error=%s", project_id, exc
            )

    @classmethod
    def _queue_slot(cls) -> asyncio.Lock:
        if cls._slot is None:
            cls._slot = asyncio.Lock()
        return cls._slot

    @staticmethod
    def _finish(state: PrewarmState, status: str, detail: str) -> None:
        state.status = status
        state.detail = detail
        state.finished_at = _utc_now_iso()
        logger.info(
            "Drive pre-warm %s: project_id=%s %s", status, state.project_id, detail
        )

    @classmethod
    async def _run(cls, project_id: str, state: PrewarmState) -> None:
        try:
            async with cls._queue_slot():
                await cls._execute(project_id, state)
        except asyncio.CancelledError:
            cls._finish(state, "cancelled", "cancelled")
            await cls._release_if_project_gone(project_id, state)
            raise
        except Exception as exc:
            cls._finish(state, "failed", str(exc))
            logger.warning(
                "Drive pre-warm failed: project_id=%s error=%s",
                project_id,
                exc,
                exc_info=True,
            )

    @classmethod
    async def _execute(cls, project_id: str, state: PrewarmState) -> None:
        state.status = "hashing"
        state.started_at = _utc_now_iso()

        project = await ProjectService.aload(project_id)
        if project is None:
            return cls._finish(state, "skipped", "project not found")
        if not GoogleDriveService.is_configured():
            return cls._finish(state, "skipped", "Google Drive is not configured")
        matches = await ProjectService.aload_matches(project_id)
        if matches is None or not matches.matches:
            return cls._finish(state, "skipped", "project has no matches")

        signature = await asyncio.to_thread(cls._matches_signature, project_id)
        if signature is not None and cls._done_signatures.get(project_id) == signature:
            return cls._finish(state, "skipped", "already pre-warmed for the current matches")

        entries = await run_heavy(cls.collect_entries, project, matches.matches)
        _, externalized = DriveSharedSources.partition_entries(entries)
        if not externalized:
            return cls._finish(state, "done", "no sources above the shared-file threshold")

        hashes = await run_heavy(
            SourceHashService.sha256_for_many,
            [entry.source_path for entry in externalized],
        )
        pairs = [(entry, hashes[entry.source_path]) for entry in externalized]
        planned = [
            SharedFileRecord(
                path_in_folder=DriveSharedSources.path_in_folder(entry),
                size=entry.source_path.stat().st_size,
                sha256=sha256,
                md5=None,
                drive_file_id="",
                shared_name=DriveSharedSources.shared_name(sha256, entry.source_path.name),
            )
            for entry, sha256 in pairs
        ]
        state.records = planned
        state.total_files = len(planned)
        state.total_bytes = sum(record.size for record in planned)

        # GC race guard: our references are on disk before any shared write.
        if not await run_heavy(cls._persist_manifest, project_id, planned, PENDING_STATUS):
            return cls._finish(state, "skipped", "project was deleted before upload")

        shared_folder_id = await run_heavy(DriveSharedSources.ensure_shared_folder)

        busy = DriveSharedSources.names_in_flight(record.shared_name for record in planned)
        if busy:
            state.status = "waiting"
            state.detail = f"{len(busy)} file(s) already being uploaded by another job"
        else:
            state.status = "uploading"

        def _on_stats(stats: RcloneStats) -> None:
            state.status = "uploading"
            state.transferred_bytes = int(stats.bytes_transferred)
            state.speed_bytes_per_sec = float(stats.speed_bytes_per_sec)

        def _on_plan(reused: int, missing: int) -> None:
            state.reused_files = reused
            state.uploaded_files = missing
            state.status = "uploading" if missing else "verifying"
            state.detail = f"{reused} reused, {missing} to upload"

        uploaded = await DriveSharedSources.ensure_uploaded(
            pairs,
            shared_folder_id=shared_folder_id,
            stats_callback=_on_stats,
            on_plan=_on_plan,
        )
        state.records = uploaded
        state.transferred_bytes = max(state.transferred_bytes, 0)

        if not await run_heavy(cls._persist_manifest, project_id, uploaded, PREWARMED_STATUS):
            await cls._release_if_project_gone(project_id, state)
            return cls._finish(
                state, "cancelled", "project was deleted during upload; references released"
            )
        if signature is not None:
            cls._done_signatures[project_id] = signature
        cls._finish(
            state,
            "done",
            f"{state.reused_files} reused, {state.uploaded_files} uploaded "
            f"({state.total_bytes} bytes referenced)",
        )

    @classmethod
    async def _release_if_project_gone(cls, project_id: str, state: PrewarmState) -> None:
        """After a cancel/delete: drop the references this run held.

        Only acts when the project dir no longer exists (so a mere cancel of
        a live project keeps its manifest — the export will reconcile it).
        """
        if not state.records:
            return
        if await asyncio.to_thread(ProjectService.get_project_dir(project_id).is_dir):
            return
        released = {"shared_files": [record.to_dict() for record in state.records]}
        try:
            result = await run_heavy(
                DriveSharedSources.collect_garbage,
                released,
                exclude_project_id=project_id,
            )
            logger.info(
                "Drive pre-warm released references of deleted project %s: "
                "deleted=%s kept=%s",
                project_id,
                result.get("deleted"),
                result.get("kept"),
            )
        except Exception as exc:
            logger.warning(
                "Drive pre-warm could not release references of deleted project %s: %s",
                project_id,
                exc,
            )
