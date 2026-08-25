"""Background job registry for per-scene extensive zoom searches.

The /matches page triggers these while the owner scrolls, so unlike the
inline SSE matching routes the work must survive page navigation: jobs live
here in the process, run one at a time behind the shared matching lock, and
broadcast state changes to any number of SSE subscribers.  A completed job
stays in the registry (with an ``acknowledged`` flag) until the owner clicks
its alert away, so a page refresh re-raises pending alerts.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .executors import heavy_executor
from ..library_types import LibraryType
from .event_hub import event_hub

logger = logging.getLogger("uvicorn.error")

HUB_TOPIC = "zoom_jobs"

TERMINAL_STATUSES = {"complete", "error", "cancelled"}


class SceneFingerprint(BaseModel, frozen=True):
    """Layout of the scene a job was computed for: scene count plus the
    scene's own bounds. A recompute/merge renumbers scenes, so a result whose
    fingerprint no longer matches must not be spliced onto the new layout."""

    count: int
    start: float
    end: float


class ZoomSearchJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    scene_index: int
    status: Literal["queued", "running", "complete", "error", "cancelled"] = "queued"
    message: str = ""
    changed: bool | None = None
    applied: bool | None = None
    old_match: dict | None = None
    new_match: dict | None = None
    # Actual persisted scene state.  Unlike ``new_match`` this is populated
    # when zoom search only enriches the primary's manual alternatives.
    result_match: dict | None = None
    candidates_added: int = 0
    # Set when the run starts; lets the client ignore a result whose scene
    # layout has changed since (pre-recompute jobs replayed from the hub).
    scene_fingerprint: SceneFingerprint | None = None
    error: str | None = None
    acknowledged: bool = False
    created_at: float = Field(default_factory=time.time)


class ZoomSearchService:
    MAX_TERMINAL_JOBS = 30

    def __init__(self) -> None:
        self._jobs: dict[str, ZoomSearchJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    # ------------------------------------------------------------------
    # registry surface

    async def enqueue(self, project_id: str, scene_index: int) -> ZoomSearchJob:
        for job in self._jobs.values():
            if (
                job.project_id == project_id
                and job.scene_index == scene_index
                and job.status in {"queued", "running"}
            ):
                return job
        job = ZoomSearchJob(project_id=project_id, scene_index=scene_index)
        self._jobs[job.id] = job
        self._cancel_events[job.id] = threading.Event()
        self._broadcast(job)
        task = asyncio.create_task(self._run(job))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, job_id=job.id: self._tasks.pop(job_id, None))
        return job

    def list_jobs(self, project_id: str) -> list[ZoomSearchJob]:
        return [
            job for job in self._jobs.values() if job.project_id == project_id
        ]

    def list_all_jobs(self) -> list[ZoomSearchJob]:
        """Every registered job (the shared event stream is not per-project)."""
        return list(self._jobs.values())

    def ack(self, job_id: str) -> ZoomSearchJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        job.acknowledged = True
        self._broadcast(job)
        self._prune_terminal_jobs()
        return job

    def invalidate_project(self, project_id: str, reason: str) -> None:
        """Cancel every live job of a project and drop its unseen completed
        ones (scene indices are about to renumber, so their results would
        land on the wrong scene).

        Completed-but-unacknowledged jobs are cancelled too: their alert and
        frozen ``result_match`` describe the previous scene layout, and the
        hub replays them on every snapshot until they are acknowledged.
        """
        for job in self._jobs.values():
            if job.project_id != project_id:
                continue
            live = job.status in {"queued", "running"}
            stale_alert = job.status == "complete" and not job.acknowledged
            if not (live or stale_alert):
                continue
            if live:
                event = self._cancel_events.get(job.id)
                if event is not None:
                    event.set()
                task = self._tasks.get(job.id)
                if task is not None:
                    task.cancel()
            job.status = "cancelled"
            job.acknowledged = True
            job.message = f"Cancelled: {reason}"
            self._broadcast(job)
        self._prune_terminal_jobs()

    # ------------------------------------------------------------------
    # internals

    def _broadcast(self, job: ZoomSearchJob) -> None:
        event_hub.publish(
            HUB_TOPIC,
            key=job.id,
            data=job.model_dump(mode="json"),
            project_id=job.project_id,
        )

    def _prune_terminal_jobs(self) -> None:
        terminal = [
            (job.acknowledged, job.created_at, job_id)
            for job_id, job in self._jobs.items()
            if job.status in TERMINAL_STATUSES
        ]
        excess = len(terminal) - self.MAX_TERMINAL_JOBS
        if excess <= 0:
            return
        # Acknowledged first, then oldest: an unseen alert must survive the
        # registry cap for as long as possible.
        terminal.sort(key=lambda item: (not item[0], item[1]))
        for _, _, job_id in terminal[:excess]:
            self._jobs.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    def _fail(self, job: ZoomSearchJob, message: str) -> None:
        job.status = "error"
        job.error = message
        job.message = message
        self._broadcast(job)
        self._prune_terminal_jobs()

    async def _run(self, job: ZoomSearchJob) -> None:
        try:
            await self._run_inner(job)
        except asyncio.CancelledError:
            if job.status not in TERMINAL_STATUSES:
                job.status = "cancelled"
                job.message = "Cancelled"
                self._broadcast(job)
        except Exception as exc:
            logger.exception(
                "Zoom search job failed (project=%s scene=%s)",
                job.project_id,
                job.scene_index,
            )
            if job.status not in TERMINAL_STATUSES:
                self._fail(job, str(exc))
        finally:
            if job.status in TERMINAL_STATUSES:
                self._cancel_events.pop(job.id, None)
            self._prune_terminal_jobs()

    async def _run_inner(self, job: ZoomSearchJob) -> None:
        from ..models import SceneMatch
        from . import fast_matching
        from .anime_library import AnimeLibraryService
        from .anime_matcher import AnimeMatcherService
        from .indexation_queue import indexation_queue
        from .library_hydration_service import LibraryHydrationService
        from .project_service import ProjectService
        from .zoom_rematch import ZoomRematchService, splice_match

        project = await ProjectService.aload(job.project_id)
        if not project:
            return self._fail(job, "Project not found")
        if project.library_type == LibraryType.PURE:
            return self._fail(job, "Zoom search is not available for pure projects")

        scenes = await ProjectService.aload_scenes(job.project_id)
        if not scenes or not scenes.scenes:
            return self._fail(job, "No scenes found")
        if not 0 <= job.scene_index < len(scenes.scenes):
            return self._fail(job, "Scene index out of range")

        matches = await ProjectService.aload_matches(job.project_id)
        if not matches or not matches.matches:
            return self._fail(job, "No matches found")

        video_path = Path(project.video_path) if project.video_path else None
        if not video_path or not video_path.exists():
            return self._fail(job, "Video not found")

        scene = scenes.scenes[job.scene_index]
        fingerprint = self._scene_fingerprint(scenes, job.scene_index)
        job.scene_fingerprint = fingerprint
        existing_match = next(
            (m for m in matches.matches if m.scene_index == scene.index), None
        )
        if existing_match is None:
            existing_match = SceneMatch(
                scene_index=scene.index,
                episode="",
                start_time=0.0,
                end_time=0.0,
                confidence=0.0,
                speed_ratio=1.0,
                was_no_match=True,
            )
        captured = (
            existing_match.episode,
            existing_match.start_time,
            existing_match.end_time,
        )

        if project.series_id:
            try:
                await LibraryHydrationService.ensure_matcher_ready_for_project(
                    project_id=project.id,
                    library_type=project.library_type,
                    series_id=project.series_id,
                )
            except Exception as exc:
                return self._fail(job, f"Library not ready: {exc}")

        source_path = AnimeLibraryService.get_library_path(project.library_type)
        if not source_path.exists():
            return self._fail(job, "Source path not found")

        cancel_event = self._cancel_events[job.id]
        loop = asyncio.get_running_loop()

        # Same weighting as /matches/find: fast-mode GPU decode cannot share
        # the card with another heavy job, so it reserves the whole budget.
        slots = (
            indexation_queue.MAX_CONCURRENT
            if fast_matching.decode_enabled()
            else 1
        )
        async with indexation_queue.matching_lock():
            async with indexation_queue.heavy_slot("zoom_search", slots=slots):
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                job.status = "running"
                job.message = "Searching…"
                self._broadcast(job)

                init_success = await loop.run_in_executor(
                    heavy_executor(),
                    AnimeMatcherService._init_searcher,
                    source_path,
                    project.library_type,
                    project.anime_name,
                )
                if not init_success:
                    return self._fail(job, "Failed to initialize anime_searcher")

                outcome = await loop.run_in_executor(
                    heavy_executor(),
                    partial(
                        ZoomRematchService.search_scene_sync,
                        video_path,
                        scenes,
                        project.library_type,
                        project.anime_name,
                        scene_index=job.scene_index,
                        existing_match=existing_match,
                        cancel_event=cancel_event,
                        context_matches=matches,
                    ),
                )

        if cancel_event.is_set():
            raise asyncio.CancelledError

        # Apply on the event loop: matches.json has no lock, but every writer
        # (route handlers included) runs here, so load→mutate→save is atomic
        # with respect to them.
        applied = False
        result_match = None
        candidates_added = 0
        fresh_scenes = await ProjectService.aload_scenes(job.project_id)
        if (
            fresh_scenes is None
            or self._scene_fingerprint(fresh_scenes, job.scene_index) != fingerprint
        ):
            job.status = "cancelled"
            job.acknowledged = True
            job.message = "Cancelled: scene layout changed during the search"
            self._broadcast(job)
            return

        fresh_matches = await ProjectService.aload_matches(job.project_id)
        current = None
        if fresh_matches:
            current = next(
                (m for m in fresh_matches.matches if m.scene_index == scene.index),
                None,
            )
        edited_mid_run = current is not None and (
            current.episode,
            current.start_time,
            current.end_time,
        ) != captured

        if outcome.changed and outcome.new_match and fresh_matches:
            if edited_mid_run:
                # The owner already fixed this scene by hand; offer the
                # result and every other scored track as alternatives instead
                # of overwriting them.
                from ..models import AlternativeMatch

                found = outcome.new_match
                before = len(current.alternatives)
                current.alternatives = ZoomRematchService.merge_alternatives(
                    current,
                    [
                        AlternativeMatch(
                            episode=found.episode,
                            start_time=found.start_time,
                            end_time=found.end_time,
                            confidence=found.confidence,
                            speed_ratio=found.speed_ratio,
                            algorithm="zoom_search",
                        )
                    ],
                    outcome.alternatives,
                    current.alternatives,
                )
                candidates_added = max(0, len(current.alternatives) - before)
                await ProjectService.asave_matches(job.project_id, fresh_matches)
                result_match = current
            elif splice_match(fresh_matches, scene.index, outcome.new_match):
                await ProjectService.asave_matches(job.project_id, fresh_matches)
                applied = True
                result_match = outcome.new_match
                candidates_added = len(outcome.new_match.alternatives)
        elif fresh_matches and current is not None and outcome.alternatives:
            # Even a confirmed primary produced useful, paid-for native zoom
            # evidence.  Persist it for the manual modal: zoom results are
            # only exact-deduped (earlier runs' entries first, so a
            # rediscovered interval changes nothing), the other alternatives
            # keep their cluster dedup.
            before_dump = [
                candidate.model_dump() for candidate in current.alternatives
            ]
            existing_evidence = [
                candidate
                for candidate in current.alternatives
                if ZoomRematchService.is_zoom_evidence(candidate)
            ]
            existing_other = [
                candidate
                for candidate in current.alternatives
                if not ZoomRematchService.is_zoom_evidence(candidate)
            ]
            current.alternatives = ZoomRematchService.merge_alternatives(
                current,
                existing_other,
                evidence=[*existing_evidence, *outcome.alternatives],
            )
            after_dump = [
                candidate.model_dump() for candidate in current.alternatives
            ]
            if after_dump != before_dump:
                candidates_added = max(1, len(after_dump) - len(before_dump))
                await ProjectService.asave_matches(job.project_id, fresh_matches)
                result_match = current

        job.status = "complete"
        job.changed = outcome.changed
        job.applied = applied
        job.old_match = outcome.old_match.model_dump()
        job.new_match = (
            outcome.new_match.model_dump() if outcome.new_match else None
        )
        job.result_match = result_match.model_dump() if result_match else None
        job.candidates_added = candidates_added
        if edited_mid_run and outcome.changed:
            job.message = "Result saved as an alternative (scene was edited)"
        elif outcome.changed:
            job.message = "Match updated"
        elif candidates_added:
            suffix = "candidate" if candidates_added == 1 else "candidates"
            job.message = (
                f"Existing match confirmed — {candidates_added} new AI {suffix}"
            )
        else:
            job.message = "Existing match confirmed"
        self._broadcast(job)
        self._prune_terminal_jobs()

    @staticmethod
    def _scene_fingerprint(scenes, scene_index: int) -> SceneFingerprint | None:
        if not 0 <= scene_index < len(scenes.scenes):
            return None
        scene = scenes.scenes[scene_index]
        return SceneFingerprint(
            count=len(scenes.scenes),
            start=round(scene.start_time, 3),
            end=round(scene.end_time, 3),
        )


zoom_search_service = ZoomSearchService()
