"""Internal API routes consumed by the main backend."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from app.auth.dependencies import require_internal_token
from app.models.job import Job, PlatformStatus
from app.services import additional_publishers
from app.services.embed_builder import build_embed
from app.services.post_for_me_publisher import delete_tiktok_post
from app.services.reminder_scheduler import TIKTOK_MANUAL_REMINDER_LEAD_MINUTES
from app.services.reminder_service import cleanup_reminder

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/internal",
    dependencies=[Depends(require_internal_token)],
)


class InstagramPayload(BaseModel):
    ig_user_id: str
    ig_access_token: str
    caption: str
    # Public direct-download URL of the video Instagram must ingest. Either a
    # dedicated artifact (cut / sped-up output_instagram.mp4) or the final
    # video itself when the backend found nothing to change; None only on
    # legacy jobs (the scheduler then downloads job.drive_video_url).
    prepared_video_url: str | None = None
    graph_api_version: str = "v25.0"
    poll_interval_seconds: float | None = None
    poll_timeout_seconds: float | None = None
    share_to_feed: bool | None = None
    thumb_offset: int | None = None
    max_duration_seconds: float | None = None
    cover_url: str | None = None


class TikTokPayload(BaseModel):
    social_account_id: str
    caption: str
    post_for_me_platform: Literal["tiktok", "tiktok_business"] = "tiktok"
    privacy_status: str = "public"
    allow_comment: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
    thumbnail_timestamp_ms: int | None = None
    thumbnail_url: str | None = None


class FacebookPayload(BaseModel):
    """Long-range Facebook hold (target beyond Meta's ~29d native window).

    CREATE hold: prepared_video_url set, video_id absent — the scheduler
    uploads it as a native scheduled post at T - FACEBOOK_CONVERT_LEAD_DAYS.
    RETIME hold: video_id set — the existing native post (parked inside the
    window by the backend) gets its scheduled_publish_time pushed to T."""

    page_id: str
    page_access_token: str
    title: str = ""
    description: str = ""
    prepared_video_url: str | None = None
    video_id: str | None = None
    graph_api_version: str = "v25.0"


class InitialPlatformStatus(BaseModel):
    status: Literal["pending", "uploading", "uploaded", "skipped", "failed"]
    url: str | None = None
    detail: str | None = None


class PublisherPayload(BaseModel):
    mode: Literal["api", "manual"]
    credentials: dict[str, Any] = Field(default_factory=dict, repr=False)
    description: str = ""
    video_url: str | None = None


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
    facebook: FacebookPayload | None = None
    # Manual TikTok mode (2026-08): account has TikTok slots but no Post for
    # Me id → the server posts one reminder at sched - 5 min and the ✅
    # reaction listener marks the row posted.
    tiktok_manual: bool = False
    publishers: dict[Literal["dailymotion", "snapchat", "bilibili"], PublisherPayload] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_publishers(self):
        if not set(self.publishers).issubset(self.platforms_requested):
            raise ValueError("Publisher payloads must belong to requested platforms")
        if any(
            (self.platform_statuses or {}).get(p) is not None
            and self.platform_statuses[p].status != "pending"
            for p in self.publishers
        ):
            raise ValueError("New publisher jobs must start pending")
        return self


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
    return req.tiktok.model_dump(exclude_none=True) if req.tiktok else None


def _facebook_payload(req: CreateJobRequest) -> dict | None:
    return req.facebook.model_dump(exclude_none=True) if req.facebook else None


def _normalized_tiktok_payload(payload: dict | None) -> dict | None:
    """Treat legacy payloads without a connector as consumer TikTok payloads."""
    if payload is None:
        return None
    normalized = dict(payload)
    normalized.setdefault("post_for_me_platform", "tiktok")
    return normalized


def _tiktok_target(payload: dict | None) -> tuple[str, str] | None:
    normalized = _normalized_tiktok_payload(payload)
    if normalized is None:
        return None
    return (
        str(normalized.get("social_account_id") or ""),
        str(normalized["post_for_me_platform"]),
    )


def _job_payload_changed(
    job: Job,
    req: CreateJobRequest,
    instagram_payload: dict | None,
    tiktok_payload: dict | None,
    facebook_payload: dict | None = None,
) -> bool:
    return (
        job.account_id != req.account_id
        or job.slot_time != req.slot_time
        or job.platform_scheduled_at != dict(req.platform_scheduled_at or {})
        or job.anime_title != req.anime_title
        or job.description != req.description
        or job.drive_video_url != req.drive_video_url
        or job.platforms_requested != list(req.platforms_requested)
        or job.publisher_payloads != {p: v.model_dump() for p, v in req.publishers.items()}
        or job.tiktok_manual != req.tiktok_manual
        or job.instagram_payload != instagram_payload
        or job.facebook_payload != facebook_payload
        or _normalized_tiktok_payload(job.tiktok_payload)
        != _normalized_tiktok_payload(tiktok_payload)
    )


@asynccontextmanager
async def _publisher_lock(
    request: Request, project_id: str, *, platforms: set[str] | None = None,
    include_fallback_slots: bool = False,
):
    store = request.app.state.job_store
    project_lock = store.publisher_lock(project_id)
    if project_lock.locked():
        raise HTTPException(409, "Publisher operation in progress; retry after it completes")
    claimed = []
    try:
        async with project_lock:
            if platforms is None:
                if store.publisher_busy(project_id):
                    raise HTTPException(
                        409, "Publisher operation in progress; retry after it completes"
                    )
                # Whole-job replacement/deletion excludes every publisher.
                yield
                return
            affected = set(platforms)
            if include_fallback_slots:
                job = await store.get(project_id)
                if job:
                    affected.update(
                        p for p in job.publisher_payloads if p not in job.platform_scheduled_at
                    )
            if not affected:
                # Metadata-only slot changes still exclude whole-job deletion.
                affected.add("__job_fields__")
            if store.publisher_busy(project_id, affected):
                raise HTTPException(
                    409, "Publisher operation in progress; retry after it completes"
                )
            for platform in sorted(affected):
                lock = store.publisher_platform_lock(project_id, platform)
                await lock.acquire()
                claimed.append(lock)
        # Provider/Discord calls can take time: only the affected platform locks
        # remain held, while other publishers can upload or mutate their jobs.
        yield
    finally:
        for lock in reversed(claimed):
            lock.release()


@router.post("/jobs", response_model=CreateJobResponse)
async def create_job(req: CreateJobRequest, request: Request) -> CreateJobResponse:
    async with _publisher_lock(request, req.project_id):
        existing = await request.app.state.job_store.get(req.project_id)
        if (
            existing
            and existing.publisher_payloads
            and _job_payload_changed(
                existing, req, _instagram_payload(req), _tiktok_payload(req), _facebook_payload(req)
            )
        ):
            # Never erase a submitted/uncertain checkpoint through a job upsert.
            if any(existing.publisher_states.values()) or any(
                existing.platform_statuses.get(p, PlatformStatus(status="pending")).status
                == "uploaded"
                for p in existing.publisher_payloads
            ):
                raise HTTPException(
                    409,
                    "Existing publisher submission must be reconciled before replacing this job",
                )
            for platform in list(existing.publisher_reminders):
                await additional_publishers.cleanup_reminder(
                    existing,
                    platform,
                    request.app.state.job_store,
                    request.app.state.settings,
                    request.app.state.discord,
                )
                existing = await request.app.state.job_store.get(req.project_id)
        return await _create_job(req, request)


async def _create_job(req: CreateJobRequest, request: Request) -> CreateJobResponse:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    if req.account_id not in settings.accounts:
        raise HTTPException(400, f"Unknown account {req.account_id!r}")
    account = settings.accounts[req.account_id]

    # 2026-08 PFM MIGRATION: the main backend now creates the TikTok Post for
    # Me post directly at upload time; new jobs arrive without a tiktok
    # payload or "tiktok" in platforms_requested. The tiktok handling below
    # (target lock, PFM cancel on payload change) stays in place for legacy
    # in-flight jobs and is inert for new ones.
    instagram_payload = _instagram_payload(req)
    tiktok_payload = _tiktok_payload(req)
    facebook_payload = _facebook_payload(req)
    platform_statuses = _initial_platform_statuses(req)
    updated: Job | None = None
    async with store.tiktok_publish_transition(req.project_id):
        # Re-read while holding the same transition lock used for PFM post
        # creation. Once a publisher has created a post, its post_id is therefore
        # guaranteed to be visible to the target-lock check below.
        existing = await store.get(req.project_id)
        if existing is not None:
            if not _job_payload_changed(
                existing, req, instagram_payload, tiktok_payload, facebook_payload
            ):
                return CreateJobResponse(
                    job_id=existing.job_id,
                    discord_message_id=existing.discord_message_id,
                )

            state = existing.tiktok_publish_state
            if (
                _tiktok_target(existing.tiktok_payload) != _tiktok_target(tiktok_payload)
                and state is not None
                and state.post_id
                and state.stage != "failed"
            ):
                raise HTTPException(
                    409,
                    "tiktok_target_locked: existing TikTok post has a non-definitive "
                    "or successful result; keep polling it instead of switching connectors",
                )
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
                        req.project_id,
                        state.post_id,
                        e,
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
                tiktok_manual=req.tiktok_manual,
                instagram_payload=instagram_payload,
                instagram_publish_state=None,
                tiktok_payload=tiktok_payload,
                tiktok_publish_state=None,
                facebook_payload=facebook_payload,
                facebook_publish_state=None,
                publisher_payloads={p: v.model_dump() for p, v in req.publishers.items()},
                publisher_states=existing.publisher_states,
                publisher_reminders=existing.publisher_reminders,
            )

    if updated is not None:
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
        tiktok_manual=req.tiktok_manual,
        instagram_payload=instagram_payload,
        tiktok_payload=tiktok_payload,
        facebook_payload=facebook_payload,
        publisher_payloads={p: v.model_dump() for p, v in req.publishers.items()},
        created_at=now,
        updated_at=now,
    )

    embed_msg_id: str | None = None
    try:
        embed = build_embed(job, settings.accounts, settings.public_base_url)
        embed_msg_id = await discord.post_message(settings.discord.upload_channel_id, embed=embed)
        job.discord_message_id = embed_msg_id
    except Exception as e:
        logger.warning("Embed post failed for %s: %s", job.project_id, e)

    # The manual-TikTok reminder is NOT posted here. The background scheduler
    # (app.services.reminder_scheduler) fires it at sched - 5 min.
    await store.create(job)
    return CreateJobResponse(job_id=job.job_id, discord_message_id=embed_msg_id)


@router.post("/jobs/{project_id}/platform-status")
async def platform_status(project_id: str, req: PlatformStatusRequest, request: Request) -> dict:
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
    fields: dict[str, object] = {"platform_statuses": job.platform_statuses}
    tiktok_terminal = req.platform == "tiktok" and req.status in ("uploaded", "skipped", "failed")
    if tiktok_terminal:
        # Manual TikTok mode: a terminal row (backend cancel, external ack)
        # means the reminder is moot — never fire it, drop it if posted.
        fields["reminder_cancelled"] = True
    updated = await store.update(project_id, **fields)
    if tiktok_terminal and (job.reminder_message_id or job.reminder_forward_message_id):
        await cleanup_reminder(discord, store, settings, updated)

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


class FacebookHoldRequest(BaseModel):
    """Create-or-update a long-range Facebook hold on a project's job.

    Used when a NATIVE post is rescheduled beyond Meta's ~29d window: the
    backend parks the post at a placeholder inside the window and this hold
    retimes it to the real target at T - FACEBOOK_CONVERT_LEAD_DAYS."""

    account_id: str
    scheduled_at: datetime
    facebook: FacebookPayload
    anime_title: str | None = None


@router.post("/jobs/{project_id}/facebook-hold")
async def facebook_hold(project_id: str, req: FacebookHoldRequest, request: Request) -> dict:
    settings = request.app.state.settings
    store = request.app.state.job_store

    if req.account_id not in settings.accounts:
        raise HTTPException(400, f"Unknown account {req.account_id!r}")
    account = settings.accounts[req.account_id]

    payload = req.facebook.model_dump(exclude_none=True)
    job = await store.get(project_id)
    if job is None:
        now = datetime.now(tz=UTC)
        job = Job(
            project_id=project_id,
            job_id=f"j_{secrets.token_hex(4)}",
            account_id=req.account_id,
            device_id=account.device,
            anime_title=req.anime_title or project_id,
            description="",
            drive_video_url="",
            slot_time=req.scheduled_at,
            platform_scheduled_at={"facebook": req.scheduled_at},
            platforms_requested=["facebook"],
            platform_statuses={"facebook": PlatformStatus(status="pending")},
            discord_message_id=None,
            reminder_message_id=None,
            reminder_forward_message_id=None,
            facebook_payload=payload,
            created_at=now,
            updated_at=now,
        )
        await store.create(job)
        return {"ok": True, "created": True}

    merged_times = dict(job.platform_scheduled_at or {})
    merged_times["facebook"] = req.scheduled_at
    platforms = list(job.platforms_requested)
    if "facebook" not in platforms:
        platforms.append("facebook")
    statuses = dict(job.platform_statuses)
    statuses["facebook"] = PlatformStatus(status="pending")
    await store.update(
        project_id,
        platform_scheduled_at=merged_times,
        platforms_requested=platforms,
        platform_statuses=statuses,
        facebook_payload=payload,
        facebook_publish_state=None,
    )
    return {"ok": True, "created": False}


@router.patch("/jobs/{project_id}/slot")
async def update_job_slot(project_id: str, req: UpdateSlotRequest, request: Request) -> dict:
    async with _publisher_lock(
        request, project_id, platforms=set(req.platform_scheduled_at or {}),
        include_fallback_slots=req.slot_time is not None,
    ):
        store = request.app.state.job_store
        job = await store.get(project_id)
        if job:
            affected = set(req.platform_scheduled_at or {})
            if req.slot_time is not None:
                affected.update(
                    p for p in job.publisher_payloads if p not in job.platform_scheduled_at
                )
            for platform in affected:
                if platform not in job.publisher_payloads:
                    continue
                status = job.platform_statuses.get(platform)
                if additional_publishers.submission_locked(
                    platform, job.publisher_states.get(platform, {})
                ) or (status and status.status == "uploaded"):
                    raise HTTPException(
                        409,
                        f"{platform} has started publishing; its submission cannot be rescheduled",
                    )
                await additional_publishers.cleanup_reminder(
                    job, platform, store, request.app.state.settings, request.app.state.discord
                )
                job = await store.get(project_id)
        return await _update_job_slot(project_id, req, request)


async def _update_job_slot(project_id: str, req: UpdateSlotRequest, request: Request) -> dict:
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
        fields["platform_scheduled_at"] = dict(req.platform_scheduled_at)
    if req.reminder_cancelled is not None:
        fields["reminder_cancelled"] = req.reminder_cancelled
    rearm_reminder = False
    if (
        job.tiktok_manual
        and req.platform_scheduled_at is not None
        and "tiktok" in req.platform_scheduled_at
        and job.reminder_message_id is not None
    ):
        new_tiktok = req.platform_scheduled_at["tiktok"]
        if new_tiktok.tzinfo is None:
            new_tiktok = new_tiktok.replace(tzinfo=UTC)
        reminder_due = new_tiktok - timedelta(minutes=TIKTOK_MANUAL_REMINDER_LEAD_MINUTES)
        if reminder_due > datetime.now(tz=UTC):
            # The TikTok slot moved past the already-posted reminder: drop it
            # so the scheduler posts a fresh one at the new sched - 5 min.
            rearm_reminder = True
            fields.setdefault("reminder_cancelled", False)
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
    updated = await store.update(project_id, merge_maps=("platform_scheduled_at",), **fields)
    if rearm_reminder:
        await cleanup_reminder(
            request.app.state.discord, store, request.app.state.settings, updated
        )
        updated = await store.get(project_id) or updated
    return {
        "project_id": updated.project_id,
        "slot_time": updated.slot_time.isoformat(),
        "platform_scheduled_at": {
            p: dt.isoformat() for p, dt in updated.platform_scheduled_at.items()
        },
        "reminder_cancelled": updated.reminder_cancelled,
    }


@router.post("/jobs/{project_id}/platforms/{platform}/cancel")
async def cancel_publisher(project_id: str, platform: str, request: Request) -> dict:
    if platform not in additional_publishers.PLATFORMS:
        raise HTTPException(400, "Unsupported publisher")
    async with _publisher_lock(request, project_id, platforms={platform}):
        store = request.app.state.job_store
        job = await store.get(project_id)
        if job is None or platform not in job.publisher_payloads:
            raise HTTPException(404, "Publisher job not found")
        status = job.platform_statuses.get(platform)
        if status and status.status == "skipped":
            return {"status": "cancelled"}
        if additional_publishers.submission_locked(
            platform, job.publisher_states.get(platform, {})
        ) or (status and status.status == "uploaded"):
            raise HTTPException(
                409, "Publishing has started; inspect/remove the remote post before cancelling"
            )
        try:
            await additional_publishers.cancel_staged(job, platform)
        except Exception:
            raise HTTPException(
                502, "Could not delete staged remote video; cancellation not confirmed"
            ) from None
        job = await store.update(
            project_id,
            platform_statuses={platform: PlatformStatus(status="skipped", detail="cancelled")},
            publisher_states={platform: None},
            merge_maps=("platform_statuses", "publisher_states"),
        )
        await additional_publishers.cleanup_reminder(
            job, platform, store, request.app.state.settings, request.app.state.discord
        )
        await additional_publishers.update_embed(
            job, request.app.state.settings, request.app.state.discord, store=store
        )
        return {"status": "cancelled"}


class RetryPublisherRequest(BaseModel):
    credentials: dict[str, Any] | None = Field(default=None, repr=False)


@router.post("/jobs/{project_id}/platforms/{platform}/retry")
async def retry_publisher(
    project_id: str, platform: str, req: RetryPublisherRequest, request: Request
) -> dict:
    if platform not in additional_publishers.PLATFORMS:
        raise HTTPException(400, "Unsupported publisher")
    async with _publisher_lock(request, project_id, platforms={platform}):
        store = request.app.state.job_store
        job = await store.get(project_id)
        if job is None or platform not in job.publisher_payloads:
            raise HTTPException(404, "Publisher job not found")
        status = job.platform_statuses.get(platform)
        if status is None or status.status != "failed":
            raise HTTPException(409, "Only failed publisher jobs can be retried")
        if req.credentials is not None:
            payload = dict(job.publisher_payloads[platform])
            original = payload.get("credentials") or {}
            # A rotated authorization can change; the destination cannot.
            for key in ("client_id", "profile_id", "mid", "openid"):
                if original.get(key) != req.credentials.get(key):
                    raise HTTPException(409, "Cannot change publisher identity while retrying")
            payload["credentials"] = {**original, **req.credentials}
            job = await store.update(
                project_id, publisher_payloads={platform: payload},
                merge_maps=("publisher_payloads",),
            )
        # Preserve checkpoints. The adapter reconciles known IDs or refuses to
        # resubmit an unknown outcome, including after this explicit retry.
        job = await store.merge_platform_status(
            project_id, platform, PlatformStatus(status="pending")
        )
        await additional_publishers.update_embed(
            job, request.app.state.settings, request.app.state.discord, store=store
        )
        return {"status": "pending"}


@router.delete("/jobs/{project_id}", status_code=204)
async def delete_job(project_id: str, request: Request) -> None:
    async with _publisher_lock(request, project_id):
        job = await request.app.state.job_store.get(project_id)
        if job:
            for platform, state in job.publisher_states.items():
                status = job.platform_statuses.get(platform)
                if additional_publishers.submission_locked(platform, state) and (
                    not status or status.status != "uploaded"
                ):
                    raise HTTPException(
                        409, "Unresolved remote submission; reconcile before deleting job"
                    )
                if not status or status.status != "uploaded":
                    try:
                        await additional_publishers.cancel_staged(job, platform)
                    except Exception:
                        raise HTTPException(502, "Could not delete staged remote video") from None
            for platform in list(job.publisher_reminders):
                await additional_publishers.cleanup_reminder(
                    job,
                    platform,
                    request.app.state.job_store,
                    request.app.state.settings,
                    request.app.state.discord,
                )
                job = await request.app.state.job_store.get(project_id)
        await _delete_job(project_id, request)


async def _delete_job(project_id: str, request: Request) -> None:
    settings = request.app.state.settings
    store = request.app.state.job_store
    discord = request.app.state.discord

    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, f"Job for project {project_id!r} not found")

    if job.discord_message_id:
        try:
            await discord.delete_message(settings.discord.upload_channel_id, job.discord_message_id)
        except Exception as e:
            logger.warning("Embed delete failed for %s: %s", project_id, e)
    await cleanup_reminder(discord, store, settings, job)

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
                project_id,
                state.post_id,
                e,
            )

    await store.delete(project_id)
    # A Premiere Link launch may still be pending for a project whose publish
    # job is being torn down; never replay it afterwards.
    await request.app.state.cep_launch_store.delete(project_id)


# ---- Premiere Link (VPS-brokered CEP auto-launch) ---------------------------


class CepLaunchRequest(BaseModel):
    project_id: str
    requested_at: datetime
    anime_title: str = ""
    discord_message_id: str | None = None
    discord_content: str | None = None


@router.post("/cep/launches", status_code=202)
async def create_cep_launch(req: CepLaunchRequest, request: Request) -> dict:
    """Queue (or supersede) the launch of `project_id` in the Premiere panel.

    Stored durably first, then pushed live if a panel is connected; otherwise
    it is replayed the next time one connects (until the 7-day TTL)."""
    store = request.app.state.cep_launch_store
    hub = request.app.state.cep_link
    launch = await store.upsert(
        project_id=req.project_id,
        anime_title=req.anime_title,
        requested_at=req.requested_at,
        discord_message_id=req.discord_message_id,
        discord_content=req.discord_content,
    )
    await hub.notify_outcome(launch)
    delivered = await hub.push(launch)
    return {
        "launch_id": launch.launch_id,
        "status": launch.status,
        "connected": hub.status()["connected"],
        "delivered": delivered,
    }


@router.get("/cep/launches/{project_id}")
async def get_cep_launch(project_id: str, request: Request) -> dict:
    launch = await request.app.state.cep_launch_store.get(project_id)
    if launch is None:
        raise HTTPException(404, f"No Premiere launch for project {project_id!r}")
    return launch.to_dict()


@router.delete("/cep/launches/{project_id}", status_code=204)
async def delete_cep_launch(project_id: str, request: Request) -> None:
    """Idempotent: the backend calls this for every project deletion, whether
    or not a launch (or a publish job) ever existed."""
    await request.app.state.cep_launch_store.delete(project_id)


@router.get("/cep/status")
async def cep_status(request: Request) -> dict:
    hub = request.app.state.cep_link
    pending = await request.app.state.cep_launch_store.list_pending(datetime.now(UTC))
    return {**hub.status(), "pending_count": len(pending)}


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
    await discord.edit_message(channel_id, message_id, content=req.content, embed=req.embed)
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


# ---------------------------------------------------------------------------
# Stored publish-status read (planning sync).
#
# Returns ONLY what the job store already holds — never calls Post for Me or
# Instagram, and never exposes payloads (they contain access tokens).
# Snapshot rebuilds are throttled: at most one store read per
# _STATUS_SNAPSHOT_TTL_SECONDS; callers inside the window get the cached copy.
_STATUS_SNAPSHOT_TTL_SECONDS = 10.0
_status_snapshot: dict = {}
_status_snapshot_at: float = 0.0


def _job_status_projection(job: Job) -> dict:
    return {
        "project_id": job.project_id,
        "slot_time": job.slot_time.isoformat(),
        "platform_scheduled_at": {
            platform: scheduled_at.isoformat()
            for platform, scheduled_at in job.platform_scheduled_at.items()
        },
        "platforms_requested": list(job.platforms_requested),
        "platform_statuses": {p: ps.to_dict() for p, ps in job.platform_statuses.items()},
        "reminder_cancelled": job.reminder_cancelled,
        "tiktok_manual": job.tiktok_manual,
        # Long-range Facebook hold: the native post id once created/known, so
        # the backend can reschedule with a single Graph call (never expose
        # the payload itself — it carries the page access token).
        "facebook_video_id": (
            job.facebook_publish_state.video_id if job.facebook_publish_state else None
        ),
        "updated_at": job.updated_at.isoformat(),
    }


@router.get("/jobs")
async def list_job_statuses(request: Request) -> dict:
    global _status_snapshot, _status_snapshot_at
    import time

    now = time.monotonic()
    if _status_snapshot and now - _status_snapshot_at < _STATUS_SNAPSHOT_TTL_SECONDS:
        return _status_snapshot

    store = request.app.state.job_store
    jobs = await store.list_all()
    _status_snapshot = {"jobs": {job.project_id: _job_status_projection(job) for job in jobs}}
    _status_snapshot_at = now
    return _status_snapshot


class ReconcilePublisherRequest(BaseModel):
    """An operator explicitly reports what they verified in the provider Studio."""

    outcome: Literal["published", "not_published"]
    confirm: Literal[True]
    resource_id: str | None = Field(default=None, max_length=200)


@router.post("/jobs/{project_id}/platforms/{platform}/reconcile")
async def reconcile_publisher(  # noqa: PLR0912 - provider-specific recovery states
    project_id: str,
    platform: str,
    req: ReconcilePublisherRequest,
    request: Request,
) -> dict:
    if platform not in additional_publishers.PLATFORMS:
        raise HTTPException(400, "Unsupported publisher")
    async with _publisher_lock(request, project_id, platforms={platform}):
        store = request.app.state.job_store
        job = await store.get(project_id)
        if job is None or platform not in job.publisher_payloads:
            raise HTTPException(404, "Publisher job not found")
        if job.publisher_payloads[platform].get("mode") != "api":
            raise HTTPException(409, "Manual publishers use their reminder acknowledgment")
        previous = job.platform_statuses.get(platform)
        if previous is None or previous.status != "failed":
            raise HTTPException(409, "Only failed publisher jobs can be reconciled")

        state = dict(job.publisher_states.get(platform) or {})
        now = datetime.now(UTC)
        if req.outcome == "published":
            resource_id = str(req.resource_id or state.get("resource_id") or "").strip()
            valid_id = resource_id and all(
                c.isascii() and (c.isalnum() or c in "-_") for c in resource_id
            )
            if not valid_id:
                raise HTTPException(400, "A valid provider resource_id is required for publication")
            url = None
            if platform == "bilibili":
                if not resource_id.startswith("BV"):
                    raise HTTPException(400, "Bilibili resource_id must be a BV video ID")
                state.update(resource_id=resource_id, submission_started=True)
                url = f"https://www.bilibili.com/video/{resource_id}"
            else:
                state.update(stage="submitted", resource_id=resource_id)
                if platform == "dailymotion":
                    url = f"https://www.dailymotion.com/video/{resource_id}"
                elif platform == "snapchat":
                    url = f"https://www.snapchat.com/spotlight/{resource_id}"
                state["url"] = url
            status = PlatformStatus(
                status="uploaded",
                url=url,
                detail="Publication verified manually in the provider Studio",
                completed_at=now,
                attempts=previous.attempts,
            )
        else:
            if req.resource_id is not None:
                raise HTTPException(400, "Do not supply resource_id for not_published")
            state.pop("url", None)
            if platform == "bilibili":
                state.pop("resource_id", None)
                state["submission_started"] = False
            elif platform == "snapchat":
                state.pop("resource_id", None)
                state["stage"] = "media_ready" if state.get("media_id") else "pending"
            else:
                # Preserve a known private video so retry can reconcile it.
                # A lost create response has no recoverable ID; the operator
                # must remove any private orphan before permitting a new one.
                state["stage"] = "private" if state.get("resource_id") else "pending"
            status = PlatformStatus(
                status="failed",
                attempts=previous.attempts,
                detail="Operator confirmed no publication; retry or cancel is now available",
            )
        state.update(reconciled_at=now.isoformat(), reconciled_outcome=req.outcome)
        job = await store.update(
            project_id,
            publisher_states={platform: state},
            platform_statuses={platform: status},
            merge_maps=("publisher_states", "platform_statuses"),
        )
        await additional_publishers.update_embed(
            job, request.app.state.settings, request.app.state.discord, store=store
        )
        return {"status": status.status, "outcome": req.outcome, "url": status.url}
