from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..models import Project
from .platform_reschedule_service import PlatformRescheduleService
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")

# Backoff steps applied as (retries -> wait-since-last-attempt).
_BACKOFF_STEPS: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
_MAX_RETRIES_BEFORE_ALERT = 5
_POLL_INTERVAL_SECONDS = 60


async def _post_discord_alert(message: str) -> None:
    """Best-effort Discord alert — silenced on error."""
    try:
        from .discord_service import DiscordService  # noqa: PLC0415
        await DiscordService.post_alert(message)
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Discord alert failed: %s", exc)


class RescheduleRetryService:
    @classmethod
    def _backoff_for_retries(cls, retries: int) -> timedelta:
        idx = min(retries, len(_BACKOFF_STEPS) - 1)
        return _BACKOFF_STEPS[idx]

    @staticmethod
    def _coerce_dt(value) -> datetime | None:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @classmethod
    async def run_once(cls) -> None:
        now_utc = datetime.now(timezone.utc)
        for project in ProjectService.list_all():
            pending = dict(project.reschedule_pending or {})
            if not pending:
                continue
            updated = False
            for platform, entry in list(pending.items()):
                last_attempt = cls._coerce_dt(entry.get("last_attempt_at")) or now_utc
                retries = int(entry.get("retries") or 0)
                target = cls._coerce_dt(entry.get("target_scheduled_at"))
                if target is None:
                    continue
                if now_utc - last_attempt < cls._backoff_for_retries(retries):
                    continue

                result = await asyncio.to_thread(
                    PlatformRescheduleService.notify, project, platform, target
                )
                if result.status == "ok":
                    pending.pop(platform, None)
                    updated = True
                    continue

                retries += 1
                entry["retries"] = retries
                entry["last_error"] = result.error or entry.get("last_error")
                entry["last_attempt_at"] = now_utc
                pending[platform] = entry
                updated = True

                if retries >= _MAX_RETRIES_BEFORE_ALERT:
                    await _post_discord_alert(
                        f"[reschedule-retry] project={project.id} platform={platform} "
                        f"failed {retries} times: {entry.get('last_error')}"
                    )

            if updated:
                project.reschedule_pending = pending
                ProjectService.save(project)

    @classmethod
    async def run_loop(cls, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await cls.run_once()
            except Exception:
                logger.exception("RescheduleRetryService.run_once failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
