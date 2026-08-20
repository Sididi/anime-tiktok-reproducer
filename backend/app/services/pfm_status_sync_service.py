"""Pull TikTok publish outcomes from Post for Me into local state.

Since the 2026-08 PFM migration the backend creates the PFM post itself
(project.tiktok_pfm); PFM fires server-side at scheduled_at. This service
resolves the outcome after the fact:

- a throttled fire-and-forget sync runs whenever the planning events are
  requested (like VpsStatusSyncService);
- a 60s background loop (main.py lifespan) covers the app-left-open case so
  results land shortly after publish time;
- terminal outcomes are persisted into project.upload_last_result["platforms"]
  (source "pfm") and mirrored into tiktok_pfm.stage/url; failures ping
  Discord via the existing proxy.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncio

from .post_for_me_client import PostForMeClient, PostForMeError
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")

_MIN_SYNC_INTERVAL_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 60
# Start checking a scheduled post's result slightly before its publish
# instant, so an early PFM fire is caught on the first pass.
_DUE_MARGIN = timedelta(minutes=2)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class PfmStatusSyncService:
    _lock = threading.Lock()
    _last_sync_monotonic: float = 0.0
    _sync_running = False

    @classmethod
    def request_sync(cls) -> None:
        """Fire-and-forget a sync unless one ran recently or is in flight."""
        from ..config import settings  # noqa: PLC0415

        if not settings.pfm_api_key:
            return
        with cls._lock:
            now = time.monotonic()
            if cls._sync_running:
                return
            if now - cls._last_sync_monotonic < _MIN_SYNC_INTERVAL_SECONDS:
                return
            cls._sync_running = True
        threading.Thread(target=cls._sync, name="pfm-status-sync", daemon=True).start()

    @classmethod
    def _sync(cls) -> None:
        try:
            cls.sync_once()
        except Exception:  # pragma: no cover - defensive: sync must never crash
            logger.exception("PFM status sync failed")
        finally:
            with cls._lock:
                cls._sync_running = False
                cls._last_sync_monotonic = time.monotonic()

    @classmethod
    def sync_once(cls) -> None:
        """Resolve pending PFM posts whose publish instant has (nearly) passed."""
        now = datetime.now(timezone.utc)
        for project in ProjectService.list_all():
            state = project.tiktok_pfm
            if state is None or not state.post_id:
                continue
            if state.stage not in ("post_scheduled", "post_created"):
                continue
            scheduled_at = _normalize_dt(state.scheduled_at)
            if (
                state.stage == "post_scheduled"
                and scheduled_at is not None
                and scheduled_at - _DUE_MARGIN > now
            ):
                continue  # not due yet
            try:
                outcome = PostForMeClient.fetch_outcome(
                    state.post_id, state.social_account_id
                )
            except PostForMeError as exc:
                logger.warning(
                    "PFM result fetch failed for %s (post %s): %s",
                    project.id,
                    state.post_id,
                    exc.detail,
                )
                continue
            state.last_polled_at = now
            if outcome is None:
                # Still pending on PFM's side. Once past the publish instant
                # the post is processing: reflect that in the stage so the
                # reschedule paths treat it as immutable.
                if (
                    state.stage == "post_scheduled"
                    and scheduled_at is not None
                    and scheduled_at <= now
                ):
                    state.stage = "post_created"
                project.tiktok_pfm = state
                cls._save_quiet(project)
                continue
            if outcome.success:
                state.stage = "published"
                state.url = outcome.url
                state.last_error = None
                project.tiktok_pfm = state
                cls._persist_result(project, status="uploaded", url=outcome.url, detail=None)
                cls._update_embed(project, status="uploaded", url=outcome.url, detail=None)
                logger.info(
                    "PFM TikTok publish confirmed project=%s url=%s", project.id, outcome.url
                )
            else:
                state.stage = "failed"
                state.last_error = outcome.detail
                project.tiktok_pfm = state
                cls._persist_result(
                    project, status="failed", url=None, detail=outcome.detail
                )
                cls._update_embed(
                    project, status="failed", url=None, detail=outcome.detail
                )
                cls._ping_failure(project, outcome.detail)

    @classmethod
    def _save_quiet(cls, project) -> None:
        try:
            ProjectService.save(project)
        except Exception:
            logger.warning("Failed to persist tiktok_pfm for %s", project.id, exc_info=True)

    @classmethod
    def _persist_result(
        cls, project, *, status: str, url: str | None, detail: str | None
    ) -> None:
        """Write the terminal outcome into upload_last_result (same entry shape
        as VpsStatusSyncService._persist_terminal_outcomes, source 'pfm')."""
        result = project.upload_last_result
        if not isinstance(result, dict):
            result = {}
        entries = result.get("platforms")
        entries = list(entries) if isinstance(entries, list) else []
        new_entry: dict[str, Any] = {
            "platform": "tiktok",
            "status": status,
            "url": url,
            "resource_id": None,
            "detail": detail,
            "quota_exceeded": False,
            "source": "pfm",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "attempts": None,
        }
        existing_idx = next(
            (
                i
                for i, e in enumerate(entries)
                if isinstance(e, dict) and e.get("platform") == "tiktok"
            ),
            None,
        )
        if existing_idx is None:
            entries.append(new_entry)
        else:
            entries[existing_idx] = {**entries[existing_idx], **new_entry}
        result["platforms"] = entries
        project.upload_last_result = result
        cls._save_quiet(project)

    @classmethod
    def _update_embed(
        cls, project, *, status: str, url: str | None, detail: str | None
    ) -> None:
        """Refresh the Discord embed's TikTok row (best-effort, swallowed)."""
        try:
            from .discord_service import DiscordService  # noqa: PLC0415

            DiscordService.update_job_platform(
                project.id, "tiktok", status=status, url=url, detail=detail
            )
        except Exception:  # pragma: no cover - best-effort
            logger.warning("PFM embed update failed for %s", project.id, exc_info=True)

    @classmethod
    def _ping_failure(cls, project, detail: str | None) -> None:
        try:
            from .discord_service import DiscordService  # noqa: PLC0415

            DiscordService.post_message(
                f"❌ TikTok (Post for Me) publish failed for "
                f"**{project.anime_name or project.id}**: {detail or 'unknown error'}"
            )
        except Exception:  # pragma: no cover - best-effort
            logger.warning("PFM failure Discord ping failed", exc_info=True)

    @classmethod
    async def run_loop(cls, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                cls.request_sync()
            except Exception:
                logger.exception("PfmStatusSyncService.request_sync failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
