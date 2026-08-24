"""Premiere Link client — asks the VPS to auto-launch a project in the
Premiere Pro CEP panel once its Drive export has finished.

The VPS (`server/`) brokers between this backend and the panel: we POST a
launch request over the existing internal API (same bearer token as the
Discord/job calls), the VPS stores it durably, pushes it to the panel over a
WebSocket and edits the project's Discord message with the outcome.

Nothing here may block or fail the export route: a failed request is parked
on `project.cep_launch_request` and retried by `run_loop` (startup + every
minute, backoff, Discord alert after 5 failures, dropped after 7 days).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import settings
from .discord_service import DiscordService, _client, _swallow
from .project_service import ProjectService

logger = logging.getLogger("uvicorn.error")

LAUNCH_TTL = timedelta(days=7)
_POLL_INTERVAL_SECONDS = 60
_PAYLOAD_KEYS = (
    "project_id",
    "requested_at",
    "anime_title",
    "discord_message_id",
    "discord_content",
)


class CepLinkService:
    @classmethod
    def is_configured(cls) -> bool:
        return bool(settings.cep_link_enabled) and DiscordService.is_configured()

    # ---- request building / transport ---------------------------------------
    @classmethod
    def build_request(
        cls,
        project,
        *,
        discord_message_id: str | None,
        discord_content: str | None,
        requested_at: datetime | None = None,
    ) -> dict[str, Any]:
        stamp = requested_at or datetime.now(timezone.utc)
        return {
            "project_id": project.id,
            "requested_at": stamp.isoformat(),
            "anime_title": project.anime_name or "",
            "discord_message_id": discord_message_id,
            "discord_content": discord_content,
        }

    @classmethod
    def send_launch(cls, payload: dict[str, Any]) -> dict[str, Any]:
        """Raw POST; raises httpx errors so callers decide how to retry."""
        with _client() as c:
            r = c.post("/api/internal/cep/launches", json=payload)
            r.raise_for_status()
            return r.json()

    @classmethod
    def request_launch(
        cls,
        project,
        *,
        discord_message_id: str | None,
        discord_content: str | None,
    ) -> bool:
        """Ask the VPS to launch `project` in the panel. Never raises.

        On failure the request is parked on `project.cep_launch_request` for
        the retry loop; the caller persists the project either way.
        """
        if not cls.is_configured():
            return False
        payload = cls.build_request(
            project,
            discord_message_id=discord_message_id,
            discord_content=discord_content,
        )
        try:
            result = cls.send_launch(payload)
        except Exception as exc:
            logger.warning(
                "Premiere Link launch request failed for %s (will retry): %s",
                project.id, exc,
            )
            project.cep_launch_request = {
                **payload,
                "retries": 0,
                "last_error": str(exc),
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            }
            return False
        project.cep_launch_request = None
        logger.info(
            "Premiere Link launch queued for %s: launch_id=%s connected=%s delivered=%s",
            project.id,
            result.get("launch_id"),
            result.get("connected"),
            result.get("delivered"),
        )
        return True

    @classmethod
    @_swallow("Premiere Link delete_launch")
    def delete_launch(cls, project_id: str) -> bool | None:
        """Drop any pending launch for a deleted project (404 is fine)."""
        with _client() as c:
            r = c.delete(f"/api/internal/cep/launches/{project_id}")
            return r.status_code in (200, 204, 404)

    # ---- durable retry -------------------------------------------------------
    @classmethod
    async def retry_once(cls) -> None:
        if not cls.is_configured():
            return
        from .reschedule_retry_service import (  # noqa: PLC0415 - avoid import cycles
            _MAX_RETRIES_BEFORE_ALERT,
            RescheduleRetryService,
            _post_discord_alert,
        )

        now = datetime.now(timezone.utc)
        for project in ProjectService.list_all():
            entry = dict(project.cep_launch_request or {})
            if not entry:
                continue
            requested_at = RescheduleRetryService._coerce_dt(entry.get("requested_at"))
            if requested_at is None or now - requested_at > LAUNCH_TTL:
                logger.warning(
                    "Dropping stale Premiere Link launch request for %s (requested %s)",
                    project.id, entry.get("requested_at"),
                )
                project.cep_launch_request = None
                ProjectService.save(project)
                continue

            last_attempt = RescheduleRetryService._coerce_dt(entry.get("last_attempt_at")) or now
            retries = int(entry.get("retries") or 0)
            if now - last_attempt < RescheduleRetryService._backoff_for_retries(retries):
                continue

            payload = {key: entry.get(key) for key in _PAYLOAD_KEYS}
            try:
                result = await asyncio.to_thread(cls.send_launch, payload)
            except Exception as exc:
                retries += 1
                entry["retries"] = retries
                entry["last_error"] = str(exc)
                entry["last_attempt_at"] = now.isoformat()
                project.cep_launch_request = entry
                ProjectService.save(project)
                if retries == _MAX_RETRIES_BEFORE_ALERT:
                    await _post_discord_alert(
                        f"[premiere-link] project={project.id} launch request "
                        f"failed {retries} times: {exc}"
                    )
                continue

            project.cep_launch_request = None
            ProjectService.save(project)
            logger.info(
                "Premiere Link launch queued after retry for %s: launch_id=%s",
                project.id, result.get("launch_id"),
            )

    @classmethod
    async def run_loop(cls, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await cls.retry_once()
            except Exception:
                logger.exception("CepLinkService.retry_once failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                continue
