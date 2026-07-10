"""Internal API routes consumed by the main backend."""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import require_internal_token
from app.models.job import Job, PlatformStatus
from app.services.embed_builder import build_embed
from app.services.post_for_me_publisher import delete_tiktok_post

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/internal",
    dependencies=[Depends(require_internal_token)],
)


class InstagramPayload(BaseModel):
    ig_user_id: str
    ig_access_token: str
    caption: str
    prepared_video_url: str | None = None
    graph_api_version: str = "v25.0"
    poll_interval_seconds: float | None = None
    poll_timeout_seconds: float | None = None
    share_to_feed: bool | None = None
    thumb_offset: int | None = None


class TikTokPayload(BaseModel):
    social_account_id: str
    caption: str
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True


class InitialPlatformStatus(BaseModel):
    status: Literal["pending", "uploading", "uploaded", "skipped", "failed"]
    url: str | None = None
    detail: str | None = None


class CreateJobRequest(BaseModel):
    project_id: str
    account_id: str
    slot_time: datetime
    platform_scheduled_at: dict[str, datetime] | None = None
    anime_title: str
    description: str
    drive_video_url: str
    platforms_requested: list[str]
    platform_statuses: dict[str, InitialPlatformStatus] | None = None
    instagram: InstagramPayload | None = None
    tiktok: TikTokPayload | None = None


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


class UpdateSlotRequest(BaseModel):
    """Partial update to a job's scheduling state.

    All fields are optional so callers can move a single platform without
    touching the others. `platform_scheduled_at` is merged into the existing
    map (keys present in the request override; keys absent are preserved).
    """

    slot_time: datetime | None = None
    platform_scheduled_at: dict[str, datetime] | None = None
    reminder_cancelled: bool | None = None


def _initial_platform_statuses(req: CreateJobRequest) -> dict[str, PlatformStatus]:
    statuses = {p: PlatformStatus(status="pending") for p in req.platforms_requested}
    for platform, status in (req.platform_statuses or {}).items():
        if platform not in statuses:
            continue
        statuses[platform] = PlatformStatus(
            status=status.status,
            url=status.url,
            detail=status.detail,
        )
    return statuses


def _instagram_payload(req: CreateJobRequest) -> dict | None:
    return req.instagram.model_dump(exclude_none=True) if req.instagram else None


def _tiktok_payload(req: CreateJobRequest) -> dict | None:
    return req.tiktok.model_dump() if req.tiktok else None


def _job_payload_changed(
    job: Job,
    req: CreateJobRequest,
    instagram_payload: dict | None,
    tiktok_payload: dict | None,
) -> bool:
    return (
        job.account_id != req.account_id
        or job.slot_time != req.slot_time
        or job.platform_scheduled_at != dict(req.platform_scheduled_at or {})
        or job.anime_title != req.anime_title
        or job.description != req.description
        or job.drive_video_url != req.drive_video_url
        or job.platforms_requested != list(req.platforms_requested)
        or job.instagram_payload != instagram_payload
        or job.tiktok_payload != tiktok_payload
    )


@router.post("/jobs", response_model=CreateJobResponse)
async def create_job(req: CreateJobRequest, request: Request) -> CreateJobResponse:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    if req.account_id not in settings.accounts:
        raise HTTPException(400, f"Unknown account {req.account_id!r}")
    account = settings.accounts[req.account_id]

    existing = await store.get(req.project_id)
    instagram_payload = _instagram_payload(req)
    tiktok_payload = _tiktok_payload(req)
    platform_statuses = _initial_platform_statuses(req)
    if existing is not None:
        if not _job_payload_changed(existing, req, instagram_payload, tiktok_payload):
            return CreateJobResponse(
                job_id=existing.job_id, discord_message_id=existing.discord_message_id
            )

        updated = await store.update(
            req.project_id,
            account_id=req.account_id,
            device_id=account.device,
            anime_title=req.anime_title,
            description=req.description,
            drive_video_url=req.drive_video_url,
            slot_time=req.slot_time,
            platform_scheduled_at=dict(req.platform_scheduled_at or {}),
            platforms_requested=list(req.platforms_requested),
            platform_statuses=platform_statuses,
            instagram_payload=instagram_payload,
            instagram_publish_state=None,
            tiktok_payload=tiktok_payload,
            tiktok_publish_state=None,
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
                logger.warning("Embed edit failed for %s: %s", req.project_id, e)
        return CreateJobResponse(
            job_id=updated.job_id, discord_message_id=updated.discord_message_id
        )

    now = datetime.now(tz=UTC)
    job = Job(
        project_id=req.project_id,
        job_id=f"j_{secrets.token_hex(4)}",
        account_id=req.account_id,
        device_id=account.device,
        anime_title=req.anime_title,
        description=req.description,
        drive_video_url=req.drive_video_url,
        slot_time=req.slot_time,
        platform_scheduled_at=dict(req.platform_scheduled_at or {}),
        platforms_requested=list(req.platforms_requested),
        platform_statuses=platform_statuses,
        discord_message_id=None,
        reminder_message_id=None,
        reminder_forward_message_id=None,
        instagram_payload=instagram_payload,
        tiktok_payload=tiktok_payload,
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


@router.patch("/jobs/{project_id}/slot")
async def update_job_slot(
    project_id: str, req: UpdateSlotRequest, request: Request
) -> dict:
    store = request.app.state.job_store
    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, f"Job for project {project_id!r} not found")

    fields: dict[str, object] = {}
    if req.slot_time is not None:
        fields["slot_time"] = req.slot_time
    if req.platform_scheduled_at is not None:
        # Merge into the existing per-platform map so partial updates (e.g.
        # moving only the TikTok slot) don't wipe other platforms' entries.
        merged = dict(job.platform_scheduled_at or {})
        merged.update(dict(req.platform_scheduled_at))
        fields["platform_scheduled_at"] = merged
    if req.reminder_cancelled is not None:
        fields["reminder_cancelled"] = req.reminder_cancelled
    if not fields:
        # Nothing to change.
        return {
            "project_id": job.project_id,
            "slot_time": job.slot_time.isoformat(),
            "platform_scheduled_at": {
                p: dt.isoformat() for p, dt in job.platform_scheduled_at.items()
            },
            "reminder_cancelled": job.reminder_cancelled,
        }
    updated = await store.update(project_id, **fields)
    return {
        "project_id": updated.project_id,
        "slot_time": updated.slot_time.isoformat(),
        "platform_scheduled_at": {
            p: dt.isoformat() for p, dt in updated.platform_scheduled_at.items()
        },
        "reminder_cancelled": updated.reminder_cancelled,
    }


@router.delete("/jobs/{project_id}", status_code=204)
async def delete_job(project_id: str, request: Request) -> None:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, f"Job for project {project_id!r} not found")

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

    state = job.tiktok_publish_state
    if (
        state is not None
        and state.post_id
        and state.stage == "post_scheduled"
        and settings.pfm_api_key
    ):
        try:
            await delete_tiktok_post(
                api_key=settings.pfm_api_key,
                post_id=state.post_id,
                base_url=settings.pfm_base_url,
            )
        except Exception as e:
            logger.warning(
                "PFM scheduled-post delete failed for %s (post_id=%s): %s",
                project_id, state.post_id, e,
            )

    await store.delete(project_id)


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
