"""Mobile API: per-device-bearer-token routes consumed by the React Native app."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.dependencies import require_device_token
from app.models.job import PlatformStatus
from app.services.embed_builder import build_embed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mobile")


def _avatar_url(public_base_url: str, filename: str) -> str:
    return f"{public_base_url.rstrip('/')}/api/avatars/{filename}"


@router.get("/me")
async def me(request: Request, device_id: str = Depends(require_device_token)) -> dict:
    settings = request.app.state.settings
    accounts = [
        {
            "id": acc.id,
            "name": acc.name,
            "avatar_url": _avatar_url(settings.public_base_url, acc.avatar),
        }
        for acc in settings.accounts.values()
        if acc.device == device_id
    ]
    return {"device_id": device_id, "accounts": accounts}


@router.get("/jobs")
async def list_jobs(
    request: Request, device_id: str = Depends(require_device_token)
) -> list[dict]:
    settings = request.app.state.settings
    store = request.app.state.job_store
    jobs = await store.list_for_device(device_id, status="pending")
    out: list[dict] = []
    for j in jobs:
        account = settings.accounts[j.account_id]
        out.append(
            {
                "job_id": j.job_id,
                "project_id": j.project_id,
                "account_id": j.account_id,
                "account_name": account.name,
                "account_avatar_url": _avatar_url(settings.public_base_url, account.avatar),
                "anime_title": j.anime_title,
                "description": j.description,
                "slot_time": j.slot_time.isoformat(),
                "status": j.status,
            }
        )
    return out


async def _job_for_device_or_404(request: Request, job_id: str, device_id: str):
    store = request.app.state.job_store
    # job_id-based lookup: scan jobs, since store is keyed by project_id.
    # Volume is tiny so this is fine.
    for j in await store.list_for_device(device_id):
        if j.job_id == job_id:
            return j
    raise HTTPException(404, "Job not found")


@router.get("/jobs/{job_id}/video-url")
async def video_url(
    job_id: str, request: Request, device_id: str = Depends(require_device_token)
) -> dict:
    job = await _job_for_device_or_404(request, job_id, device_id)
    return {"video_url": job.drive_video_url}


@router.post("/jobs/{job_id}/ack")
async def ack(
    job_id: str, request: Request, device_id: str = Depends(require_device_token)
) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await _job_for_device_or_404(request, job_id, device_id)
    if job.status == "acked":
        return {"ok": True, "status": "acked"}

    job.platform_statuses["tiktok"] = PlatformStatus(status="uploaded")
    updated = await store.update(
        job.project_id,
        status="acked",
        acked_at=datetime.now(tz=timezone.utc),
        platform_statuses=job.platform_statuses,
    )

    if updated.discord_message_id:
        try:
            embed = build_embed(
                updated, settings.accounts, settings.devices, settings.public_base_url
            )
            await discord.edit_message(
                settings.discord.upload_channel_id,
                updated.discord_message_id,
                embed=embed,
            )
            await discord.add_reaction(
                settings.discord.upload_channel_id, updated.discord_message_id, "✅"
            )
        except Exception as e:
            logger.warning("Discord ack-side updates failed for %s: %s", job.project_id, e)

    return {"ok": True, "status": updated.status}
