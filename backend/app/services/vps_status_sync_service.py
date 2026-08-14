"""Pull IG/TikTok publish outcomes from the VPS job store into local state.

The VPS publishes Instagram and TikTok on its own schedule and keeps the
per-platform outcome (status/url/detail) in its job store; it cannot reach
the local backend, so we pull. One throttled fetch is triggered (fire and
forget) whenever the planning events are requested:

- every fetched status lands in an in-memory cache consumed by the planning
  event-status derivation (pending/uploading included);
- terminal outcomes (uploaded/failed) are also persisted into
  project.upload_last_result["platforms"] so manager rows and future
  sessions keep them without the VPS.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .discord_service import DiscordService
from .project_service import ProjectService

logger = logging.getLogger(__name__)

VPS_PLATFORMS = ("instagram", "tiktok")

_MIN_SYNC_INTERVAL_SECONDS = 60.0


class VpsStatusSyncService:
    _lock = threading.Lock()
    _last_sync_monotonic: float = 0.0
    _sync_running = False
    # {project_id: {platform: {"status": ..., "url": ..., "detail": ...,
    #               "completed_at": ..., "attempts": ...}}}
    _cache: dict[str, dict[str, dict[str, Any]]] = {}

    @classmethod
    def cached_status(cls, project_id: str, platform: str) -> dict[str, Any] | None:
        return cls._cache.get(project_id, {}).get(platform)

    @classmethod
    def request_sync(cls) -> None:
        """Fire-and-forget a sync unless one ran recently or is in flight."""
        if not DiscordService.is_configured():
            return
        with cls._lock:
            now = time.monotonic()
            if cls._sync_running:
                return
            if now - cls._last_sync_monotonic < _MIN_SYNC_INTERVAL_SECONDS:
                return
            cls._sync_running = True
        threading.Thread(target=cls._sync, name="vps-status-sync", daemon=True).start()

    @classmethod
    def _sync(cls) -> None:
        try:
            payload = DiscordService.fetch_job_statuses()
            if not isinstance(payload, dict):
                return
            jobs = payload.get("jobs")
            if not isinstance(jobs, dict):
                return
            cache: dict[str, dict[str, dict[str, Any]]] = {}
            for project_id, job in jobs.items():
                if not isinstance(job, dict):
                    continue
                statuses = job.get("platform_statuses")
                if not isinstance(statuses, dict):
                    continue
                entry = {
                    platform: status
                    for platform, status in statuses.items()
                    if platform in VPS_PLATFORMS and isinstance(status, dict)
                }
                if entry:
                    cache[project_id] = entry
            cls._cache = cache
            cls._persist_terminal_outcomes(cache)
        except Exception:  # pragma: no cover - defensive: sync must never crash
            logger.exception("VPS status sync failed")
        finally:
            with cls._lock:
                cls._sync_running = False
                cls._last_sync_monotonic = time.monotonic()

    @classmethod
    def _persist_terminal_outcomes(
        cls, cache: dict[str, dict[str, dict[str, Any]]]
    ) -> None:
        """Write uploaded/failed outcomes into upload_last_result."""
        for project_id, platforms in cache.items():
            terminal = {
                platform: status
                for platform, status in platforms.items()
                if status.get("status") in ("uploaded", "failed")
            }
            if not terminal:
                continue
            project = ProjectService.load(project_id)
            if project is None:
                continue
            result = project.upload_last_result
            if not isinstance(result, dict):
                result = {}
            entries = result.get("platforms")
            entries = list(entries) if isinstance(entries, list) else []
            changed = False
            for platform, status in terminal.items():
                new_entry = {
                    "platform": platform,
                    "status": status.get("status"),
                    "url": status.get("url"),
                    "resource_id": None,
                    "detail": status.get("detail"),
                    "quota_exceeded": False,
                    "source": "vps",
                    "completed_at": status.get("completed_at"),
                    "attempts": status.get("attempts"),
                }
                existing_idx = next(
                    (
                        i
                        for i, e in enumerate(entries)
                        if isinstance(e, dict) and e.get("platform") == platform
                    ),
                    None,
                )
                if existing_idx is None:
                    entries.append(new_entry)
                    changed = True
                else:
                    existing = entries[existing_idx]
                    if (
                        existing.get("status") != new_entry["status"]
                        or existing.get("url") != new_entry["url"]
                        or existing.get("detail") != new_entry["detail"]
                    ):
                        entries[existing_idx] = {**existing, **new_entry}
                        changed = True
            if changed:
                result["platforms"] = entries
                project.upload_last_result = result
                ProjectService.save(project)
                logger.info(
                    "VPS outcomes persisted for %s: %s",
                    project_id,
                    {p: s.get("status") for p, s in terminal.items()},
                )
