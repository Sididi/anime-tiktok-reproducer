"""Internal API routes consumed by the main backend."""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import require_internal_token
from app.models.job import PlatformStatus, TikTokJob
from app.services.embed_builder import build_embed

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/internal",
    dependencies=[Depends(require_internal_token)],
)


class CreateJobRequest(BaseModel):
    project_id: str
    account_id: str
    slot_time: datetime
    anime_title: str
    description: str
    drive_video_url: str
    platforms_requested: list[str]


class CreateJobResponse(BaseModel):
    job_id: str
    discord_message_id: str | None


class PlatformStatusRequest(BaseModel):
    platform: str
    status: Literal["pending", "uploading", "uploaded", "skipped", "failed"]
    url: str | None = None
    detail: str | None = None


class GenericMessageRequest(BaseModel):
    channel_id: str | None = None
    content: str | None = None
    embed: dict | None = None


class GenericMessageEditRequest(BaseModel):
    channel_id: str | None = None
    content: str | None = None
    embed: dict | None = None


@router.post("/jobs", response_model=CreateJobResponse)
async def create_job(req: CreateJobRequest, request: Request) -> CreateJobResponse:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    if req.account_id not in settings.accounts:
        raise HTTPException(400, f"Unknown account {req.account_id!r}")
    account = settings.accounts[req.account_id]

    existing = await store.get(req.project_id)
    if existing is not None:
        return CreateJobResponse(
            job_id=existing.job_id, discord_message_id=existing.discord_message_id
        )

    now = datetime.now(tz=UTC)
    platform_statuses = {
        p: PlatformStatus(status="pending") for p in req.platforms_requested
    }
    job = TikTokJob(
        project_id=req.project_id,
        job_id=f"j_{secrets.token_hex(4)}",
        account_id=req.account_id,
        device_id=account.device,
        anime_title=req.anime_title,
        description=req.description,
        drive_video_url=req.drive_video_url,
        slot_time=req.slot_time,
        platforms_requested=list(req.platforms_requested),
        status="pending",
        platform_statuses=platform_statuses,
        discord_message_id=None,
        reminder_message_id=None,
        reminder_forward_message_id=None,
        acked_at=None,
        created_at=now,
        updated_at=now,
    )

    embed_msg_id: str | None = None
    try:
        embed = build_embed(job, settings.accounts, settings.public_base_url)
        embed_msg_id = await discord.post_message(
            settings.discord.upload_channel_id, embed=embed
        )
        job.discord_message_id = embed_msg_id
    except Exception as e:
        logger.warning("Embed post failed for %s: %s", job.project_id, e)

    # Reminder is NOT posted here. The background scheduler
    # (app.services.reminder_scheduler) fires it at slot_time.
    await store.create(job)
    return CreateJobResponse(job_id=job.job_id, discord_message_id=embed_msg_id)


@router.post("/jobs/{project_id}/platform-status")
async def platform_status(
    project_id: str, req: PlatformStatusRequest, request: Request
) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    new_status = PlatformStatus(status=req.status, url=req.url, detail=req.detail)
    existing = job.platform_statuses.get(req.platform)
    if existing == new_status:
        return {"ok": True, "noop": True}

    job.platform_statuses[req.platform] = new_status
    updated = await store.update(
        project_id, platform_statuses=job.platform_statuses
    )

    if updated.discord_message_id:
        try:
            embed = build_embed(updated, settings.accounts, settings.public_base_url)
            await discord.edit_message(
                settings.discord.upload_channel_id,
                updated.discord_message_id,
                embed=embed,
            )
        except Exception as e:
            logger.warning("Embed edit failed for %s: %s", project_id, e)

    return {"ok": True, "noop": False}


@router.delete("/jobs/{project_id}")
async def delete_job(project_id: str, request: Request) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await store.get(project_id)
    if job is None:
        return {"ok": True, "deleted": False}

    if job.discord_message_id:
        try:
            await discord.delete_message(
                settings.discord.upload_channel_id, job.discord_message_id
            )
        except Exception as e:
            logger.warning("Embed delete failed for %s: %s", project_id, e)
    if job.reminder_message_id:
        try:
            await discord.delete_message(
                settings.discord.reminder_channel_id, job.reminder_message_id
            )
        except Exception as e:
            logger.warning("Reminder delete failed for %s: %s", project_id, e)
    if job.reminder_forward_message_id:
        try:
            await discord.delete_message(
                settings.discord.reminder_channel_id,
                job.reminder_forward_message_id,
            )
        except Exception as e:
            logger.warning("Reminder forward delete failed for %s: %s", project_id, e)

    await store.delete(project_id)
    return {"ok": True, "deleted": True}


@router.post("/discord/messages")
async def post_discord_message(req: GenericMessageRequest, request: Request) -> dict:
    settings = request.app.state.settings
    discord = request.app.state.discord
    channel_id = req.channel_id or settings.discord.upload_channel_id
    msg_id = await discord.post_message(channel_id, content=req.content, embed=req.embed)
    return {"message_id": msg_id}


@router.patch("/discord/messages/{message_id}")
async def patch_discord_message(
    message_id: str, req: GenericMessageEditRequest, request: Request
) -> dict:
    settings = request.app.state.settings
    discord = request.app.state.discord
    channel_id = req.channel_id or settings.discord.upload_channel_id
    await discord.edit_message(
        channel_id, message_id, content=req.content, embed=req.embed
    )
    return {"ok": True}


@router.delete("/discord/messages/{message_id}")
async def delete_discord_message(
    message_id: str, request: Request, channel_id: str | None = None
) -> dict:
    settings = request.app.state.settings
    discord = request.app.state.discord
    target_channel = channel_id or settings.discord.upload_channel_id
    await discord.delete_message(target_channel, message_id)
    return {"ok": True}
