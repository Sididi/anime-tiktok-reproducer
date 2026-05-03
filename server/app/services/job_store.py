"""JSON-file persistence for Job. Async-safe via asyncio.Lock."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from dataclasses import fields as _dc_fields
from datetime import UTC, datetime
from pathlib import Path

from app.models.job import Job, PlatformStatus


class JobStore:
    """Single JSON file at `path`, schema: {"jobs": {project_id: <job-dict>}}."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

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

    async def update(self, project_id: str, **fields) -> Job:
        valid = {f.name for f in _dc_fields(Job)}
        unknown = set(fields) - valid
        if unknown:
            raise ValueError(f"Unknown Job field(s): {sorted(unknown)}")
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = Job.from_dict(jobs[project_id])
            for k, v in fields.items():
                setattr(job, k, v)
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
