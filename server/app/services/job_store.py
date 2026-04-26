"""JSON-file persistence for TikTokJob. Async-safe via asyncio.Lock."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import fields as _dc_fields
from datetime import UTC, datetime
from pathlib import Path

from app.models.job import TikTokJob


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
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def create(self, job: TikTokJob) -> None:
        async with self._lock:
            jobs = self._read()
            if job.project_id in jobs:
                return  # idempotent
            jobs[job.project_id] = job.to_dict()
            self._write(jobs)

    async def get(self, project_id: str) -> TikTokJob | None:
        async with self._lock:
            jobs = self._read()
            d = jobs.get(project_id)
            return TikTokJob.from_dict(d) if d else None

    async def update(self, project_id: str, **fields) -> TikTokJob:
        valid = {f.name for f in _dc_fields(TikTokJob)}
        unknown = set(fields) - valid
        if unknown:
            raise ValueError(f"Unknown TikTokJob field(s): {sorted(unknown)}")
        async with self._lock:
            jobs = self._read()
            if project_id not in jobs:
                raise KeyError(project_id)
            job = TikTokJob.from_dict(jobs[project_id])
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

    async def list_for_device(
        self, device_id: str, *, status: str | None = None
    ) -> list[TikTokJob]:
        async with self._lock:
            jobs = self._read()
            result: list[TikTokJob] = []
            for d in jobs.values():
                if d["device_id"] != device_id:
                    continue
                if status is not None and d["status"] != status:
                    continue
                result.append(TikTokJob.from_dict(d))
            return result
