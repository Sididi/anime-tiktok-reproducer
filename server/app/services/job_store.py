"""JSON-file persistence for Job. Async-safe via asyncio.Lock."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import fields as _dc_fields
from datetime import UTC, datetime
from pathlib import Path

from app.models.job import (
    FacebookPublishState,
    InstagramPublishState,
    Job,
    PlatformStatus,
    TikTokPublishState,
)


class JobStore:
    """Single JSON file at `path`, schema: {"jobs": {project_id: <job-dict>}}."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        # Serializes the only target-sensitive part of TikTok publishing:
        # checking/replacing a job target versus creating and persisting its
        # Post for Me post. Media staging and result polling deliberately stay
        # outside this lock because they can take many minutes.
        self._tiktok_publish_transitions: dict[str, asyncio.Lock] = {}
        self._publisher_transitions: dict[str, asyncio.Lock] = {}
        self._publisher_activities: dict[tuple[str, str], asyncio.Lock] = {}
        self._publisher_embeds: dict[str, asyncio.Lock] = {}

    def publisher_lock(self, project_id: str) -> asyncio.Lock:
        """Brief project guard for claiming publisher activities or replacing a job."""
        return self._publisher_transitions.setdefault(project_id, asyncio.Lock())

    def publisher_platform_lock(self, project_id: str, platform: str) -> asyncio.Lock:
        """Long operations on one platform never block a different platform."""
        return self._publisher_activities.setdefault((project_id, platform), asyncio.Lock())

    def publisher_embed_lock(self, project_id: str) -> asyncio.Lock:
        return self._publisher_embeds.setdefault(project_id, asyncio.Lock())

    def publisher_busy(self, project_id: str, platforms: set[str] | None = None) -> bool:
        return any(
            lock.locked() and pid == project_id and (platforms is None or platform in platforms)
            for (pid, platform), lock in self._publisher_activities.items()
        )

    @contextlib.asynccontextmanager
    async def publisher_activity(
        self, project_id: str, platform: str, *, wait: bool = False,
    ) -> AsyncIterator[bool]:
        """Claim a platform under the project guard, then release the guard.

        Acknowledgments wait outside the project guard so they cannot deadlock
        whole-job mutations or lose a reaction to a just-created reminder.
        Scheduler duplicates simply skip an already-active platform.
        """
        lock = self.publisher_platform_lock(project_id, platform)
        while True:
            async with self.publisher_lock(project_id):
                if not lock.locked():
                    await lock.acquire()
                    break
            if not wait:
                yield False
                return
            async with lock:
                pass
        try:
            yield True
        finally:
            lock.release()

    @contextlib.asynccontextmanager
    async def tiktok_publish_transition(
        self, project_id: str
    ) -> AsyncIterator[None]:
        lock = self._tiktok_publish_transitions.setdefault(
            project_id, asyncio.Lock()
        )
        async with lock:
            yield

    def _read(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text())
            return data.get("jobs", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, jobs: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp file in same dir, then os.replace.
        fd, tmp = tempfile.mkstemp(prefix=".jobs.", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"jobs": jobs}, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    async def create(self, job: Job) -> None:
        async with self._lock:
            jobs = self._read()
            if job.project_id in jobs:
                return  # idempotent
            jobs[job.project_id] = job.to_dict()
            self._write(jobs)

    async def get(self, project_id: str) -> Job | None:
        async with self._lock:
            jobs = self._read()
            d = jobs.get(project_id)
            return Job.from_dict(d) if d else None

    async def update(self, project_id: str, *, merge_maps: tuple[str, ...] = (), **fields) -> Job:
        valid = {f.name for f in _dc_fields(Job)}
        unknown = set(fields) - valid
        if unknown:
            raise ValueError(f"Unknown Job field(s): {sorted(unknown)}")
        if set(merge_maps) - {
            "platform_scheduled_at", "platform_statuses", "publisher_payloads",
            "publisher_states", "publisher_reminders",
        }:
            raise ValueError("Unsupported Job map merge")
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            for k, v in fields.items():
                resolved = v
                if k in merge_maps:
                    merged = dict(getattr(job, k))
                    for key, value in v.items():
                        if value is None:
                            merged.pop(key, None)
                        else:
                            merged[key] = value
                    resolved = merged
                setattr(job, k, resolved)
            job.updated_at = datetime.now(tz=UTC)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job

    async def delete(self, project_id: str) -> None:
        async with self._lock:
            jobs = self._read()
            if project_id in jobs:
                del jobs[project_id]
                self._write(jobs)

    async def list_all(self) -> list[Job]:
        async with self._lock:
            jobs = self._read()
            return [Job.from_dict(d) for d in jobs.values()]

    async def merge_platform_status(
        self, project_id: str, platform: str, status: PlatformStatus
    ) -> Job:
        """Atomically merge `status` into platform_statuses[platform] under the lock.

        Avoids the read-then-write race where a stale snapshot of
        platform_statuses (e.g. captured at the start of a long IG publish)
        would clobber a concurrent write to a different platform key
        (e.g. a reaction handler marking tiktok=uploaded mid-publish).
        """
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            job.platform_statuses = {**job.platform_statuses, platform: status}
            job.updated_at = datetime.now(tz=UTC)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job

    async def set_instagram_publish_state(
        self, project_id: str, state: InstagramPublishState | None
    ) -> Job:
        """Atomically replace the sanitized Instagram publish state."""
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            job.instagram_publish_state = state
            job.updated_at = datetime.now(tz=UTC)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job

    async def set_tiktok_publish_state(
        self, project_id: str, state: TikTokPublishState | None
    ) -> Job:
        """Atomically replace the resumable TikTok publish state."""
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            job.tiktok_publish_state = state
            job.updated_at = datetime.now(tz=UTC)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job

    async def set_facebook_publish_state(
        self, project_id: str, state: FacebookPublishState | None
    ) -> Job:
        """Atomically replace the long-range Facebook hold state."""
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            job.facebook_publish_state = state
            job.updated_at = datetime.now(tz=UTC)
            jobs[project_id] = job.to_dict()
            self._write(jobs)
            return job
