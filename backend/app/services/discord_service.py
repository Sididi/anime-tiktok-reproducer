"""Thin HTTP client to the TikTok VPS server. Replaces direct Discord webhook calls.

All Discord-related operations (posting messages, embed jobs, reactions) are
proxied through the VPS server at `settings.tiktok_server_base_url`. Network
errors are logged and swallowed so the main backend's pipeline never blocks on
Discord-related failures.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class DiscordMessage:
    id: str
    content: str = ""


def _client() -> httpx.Client:
    base = settings.tiktok_server_base_url
    if not base:
        raise RuntimeError("TikTok server base URL not configured")
    token = settings.tiktok_server_internal_token or ""
    return httpx.Client(
        base_url=base.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def _swallow(label: str):
    """Context-manager-like decorator that logs+swallows httpx errors."""

    def wrap(fn):
        def inner(*args, **kwargs):
            if not DiscordService.is_configured():
                return None
            try:
                return fn(*args, **kwargs)
            except httpx.HTTPError as e:
                logger.warning("%s failed: %s", label, e)
                return None

        return inner

    return wrap


class DiscordService:
    """Public API preserved for back-compat; calls go to VPS internally."""

    # ---- Configuration check -------------------------------------------------
    @classmethod
    def is_configured(cls) -> bool:
        return bool(
            settings.tiktok_server_base_url and settings.tiktok_server_internal_token
        )

    # ---- Generic message endpoints (used by processing.py, etc.) -------------
    @classmethod
    @_swallow("Discord post_message")
    def post_message(cls, content: str) -> DiscordMessage | None:
        with _client() as c:
            r = c.post("/api/internal/discord/messages", json={"content": content})
            r.raise_for_status()
            return DiscordMessage(id=str(r.json()["message_id"]), content=content)

    @classmethod
    @_swallow("Discord edit_message")
    def edit_message(cls, message_id: str, content: str) -> DiscordMessage | None:
        if not message_id:
            return None
        with _client() as c:
            r = c.patch(
                f"/api/internal/discord/messages/{message_id}",
                json={"content": content},
            )
            r.raise_for_status()
            return DiscordMessage(id=message_id, content=content)

    @classmethod
    @_swallow("Discord delete_message")
    def delete_message(cls, message_id: str) -> bool | None:
        if not message_id:
            return False
        with _client() as c:
            r = c.delete(f"/api/internal/discord/messages/{message_id}")
            return r.status_code in (200, 204, 404)

    # ---- Job-oriented endpoints (upload_phase.py) ----------------------------
    @classmethod
    @_swallow("Discord create_job")
    def create_job(
        cls,
        *,
        project_id: str,
        account_id: str,
        slot_time: datetime,
        anime_title: str,
        description: str,
        drive_video_url: str,
        platforms_requested: list[str],
        instagram: dict | None = None,
    ) -> dict[str, Any] | None:
        body = {
            "project_id": project_id,
            "account_id": account_id,
            "slot_time": slot_time.isoformat(),
            "anime_title": anime_title,
            "description": description,
            "drive_video_url": drive_video_url,
            "platforms_requested": list(platforms_requested),
        }
        if instagram is not None:
            body["instagram"] = instagram
        with _client() as c:
            r = c.post("/api/internal/jobs", json=body)
            r.raise_for_status()
            return r.json()

    @classmethod
    @_swallow("Discord update_job_platform")
    def update_job_platform(
        cls,
        project_id: str,
        platform: str,
        *,
        status: str,
        url: str | None = None,
        detail: str | None = None,
    ) -> None:
        body = {"platform": platform, "status": status, "url": url, "detail": detail}
        with _client() as c:
            r = c.post(f"/api/internal/jobs/{project_id}/platform-status", json=body)
            r.raise_for_status()
            return None

    @classmethod
    @_swallow("Discord delete_job")
    def delete_job(cls, project_id: str) -> None:
        with _client() as c:
            r = c.delete(f"/api/internal/jobs/{project_id}")
            r.raise_for_status()
            return None
