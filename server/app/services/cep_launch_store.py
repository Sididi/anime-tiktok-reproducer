"""JSON-file persistence for CepLaunch (Premiere Link). Async-safe via asyncio.Lock.

Same write discipline as JobStore: one file, temp file + os.replace.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.models.cep_launch import ACK_RESULTS, LAUNCH_TTL, CepLaunch, new_launch_id


class CepLaunchStore:
    """Single JSON file at `path`, schema: {"launches": {project_id: <launch-dict>}}."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    # ---- raw file I/O --------------------------------------------------------
    def _read(self) -> dict[str, dict]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text())
            return data.get("launches", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, launches: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".cep_launches.", suffix=".json", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"launches": launches}, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ---- API -----------------------------------------------------------------
    async def upsert(
        self,
        *,
        project_id: str,
        anime_title: str,
        requested_at: datetime,
        discord_message_id: str | None,
        discord_content: str | None,
        now: datetime | None = None,
    ) -> CepLaunch:
        """Create or replace the launch for `project_id` with a fresh launch_id.

        A re-export while a launch is still pending supersedes it; a stale ack
        carrying the old launch_id is then ignored by `record_ack`.
        """
        now = now or datetime.now(tz=UTC)
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=UTC)
        launch = CepLaunch(
            project_id=project_id,
            launch_id=new_launch_id(),
            anime_title=anime_title,
            requested_at=requested_at,
            created_at=now,
            updated_at=now,
            expires_at=requested_at + LAUNCH_TTL,
            status="pending",
            discord_message_id=discord_message_id,
            discord_content=discord_content,
        )
        async with self._lock:
            launches = self._read()
            launches[project_id] = launch.to_dict()
            self._write(launches)
        return launch

    async def get(self, project_id: str) -> CepLaunch | None:
        async with self._lock:
            d = self._read().get(project_id)
            return CepLaunch.from_dict(d) if d else None

    async def list_all(self) -> list[CepLaunch]:
        async with self._lock:
            return [CepLaunch.from_dict(d) for d in self._read().values()]

    async def list_pending(self, now: datetime | None = None) -> list[CepLaunch]:
        """Deliverable launches: still pending and not past their TTL."""
        now = now or datetime.now(tz=UTC)
        return [
            launch
            for launch in await self.list_all()
            if launch.status == "pending" and not launch.is_expired(now)
        ]

    async def mark_delivered(
        self, project_id: str, launch_id: str, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(tz=UTC)
        async with self._lock:
            launches = self._read()
            d = launches.get(project_id)
            if not d or d.get("launch_id") != launch_id:
                return
            launch = CepLaunch.from_dict(d)
            launch.delivered_at = now
            launch.delivery_count += 1
            launch.updated_at = now
            launches[project_id] = launch.to_dict()
            self._write(launches)

    async def record_ack(
        self,
        project_id: str,
        launch_id: str,
        *,
        result: str,
        detail: str | None,
        panel_build_id: str | None,
        now: datetime | None = None,
    ) -> CepLaunch | None:
        """Apply the panel's ack. Returns None when the launch is unknown or
        `launch_id` is not the current one (stale ack after a re-export)."""
        if result not in ACK_RESULTS:
            raise ValueError(f"Unknown ack result {result!r}")
        now = now or datetime.now(tz=UTC)
        async with self._lock:
            launches = self._read()
            d = launches.get(project_id)
            if not d or d.get("launch_id") != launch_id:
                return None
            launch = CepLaunch.from_dict(d)
            launch.status = result  # type: ignore[assignment]
            launch.acked_at = now
            launch.ack_detail = detail
            launch.panel_build_id = panel_build_id
            launch.updated_at = now
            launches[project_id] = launch.to_dict()
            self._write(launches)
            return launch

    async def expire_stale(self, now: datetime | None = None) -> list[CepLaunch]:
        """Flip pending launches past their TTL to `expired`; returns the changed ones."""
        now = now or datetime.now(tz=UTC)
        changed: list[CepLaunch] = []
        async with self._lock:
            launches = self._read()
            for project_id, d in list(launches.items()):
                launch = CepLaunch.from_dict(d)
                if launch.status != "pending" or not launch.is_expired(now):
                    continue
                launch.status = "expired"
                launch.updated_at = now
                launches[project_id] = launch.to_dict()
                changed.append(launch)
            if changed:
                self._write(launches)
        return changed

    async def delete(self, project_id: str) -> CepLaunch | None:
        async with self._lock:
            launches = self._read()
            d = launches.pop(project_id, None)
            if d is None:
                return None
            self._write(launches)
            return CepLaunch.from_dict(d)
