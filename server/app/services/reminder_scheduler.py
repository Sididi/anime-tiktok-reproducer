"""Background scheduler that fires platform-specific actions at their due time.

Polls every `interval` seconds; for each job, iterates `platforms_requested`
and runs due per-platform actions:

- tiktok    → stage media on arrival, create a PFM post with scheduled_at at
              sched − TIKTOK_SCHEDULE_LEAD_MINUTES, poll results from sched.
- instagram → call Instagram Graph API to publish the Reel. On success,
              update the embed. On failure, increment attempts; after
              5 attempts give up + ping the reminder channel.
- youtube   → no-op (main backend schedules natively via publishAt).
- facebook  → no-op (main backend schedules natively via video_state).

Survives VPS restarts: the scheduler is purely state-driven (re-reads
jobs.json every tick), so a restart simply resumes polling.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.models.job import (
    InstagramPublishState,
    Job,
    PlatformStatus,
    TikTokPublishState,
)
from app.services.embed_builder import build_embed
from app.services.instagram_publisher import publish_to_instagram
from app.services.job_store import JobStore
from app.services.post_for_me_publisher import (
    create_tiktok_post,
    poll_tiktok_post_result,
    stage_media_for_tiktok,
)

logger = logging.getLogger(__name__)

_IG_MAX_ATTEMPTS = 5
_TT_MAX_ATTEMPTS = 5
# Post creation lead: the PFM post (with scheduled_at = the true slot) is
# created this many minutes before the slot. Must stay <= the backend's
# TIKTOK_EDIT_LOCK_MINUTES (backend/app/services/scheduling_service.py):
# job data freezes at sched-15, the post is created from it at sched-10.
TIKTOK_SCHEDULE_LEAD_MINUTES = 10
_TT_INSTANT_PUBLISH_CUTOFF_SECONDS = 60  # sched closer than this → publish instantly
_IG_DEFAULT_POLL_INTERVAL_SECONDS = 60.0
_IG_DEFAULT_POLL_TIMEOUT_SECONDS = 4 * 60 * 60.0
_LEGACY_IG_CONTAINER_ERROR = "container status_code = ERROR"
_URL_INGEST_IG_CONTAINER_ERROR = "error code 2207077"
_RESUMABLE_HEADER_ERROR = "Invalid Header format"
_PREPARE_VIDEO_PASS_ERROR = "prepare_video: video preparation pass"
_PREPARE_VIDEO_FFMPEG_ERROR = "prepare_video: ffmpeg failed"
_PREPARE_VIDEO_FFMPEG_ERRORED = "prepare_video: ffmpeg errored"
_DOWNLOAD_STAGE_ERROR = "download:"


async def dispatch_due_actions(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    now: datetime | None = None,
) -> int:
    """Run per-platform actions for any due job. Returns count of actions taken."""
    current = _normalize_utc(now or datetime.now(tz=UTC))
    actions = 0
    for job in await store.list_all():
        for platform in job.platforms_requested:
            if _platform_due_time(job, platform) > current:
                continue
            if platform == "tiktok":
                if await _dispatch_tiktok_publish(job, store, settings, discord):
                    actions += 1
            elif platform == "instagram" and await _dispatch_instagram_publish(
                job, store, settings, discord
            ):
                actions += 1
            # youtube + facebook: nothing to do (main backend handles those)
    return actions


def _tiktok_sched(job: Job) -> datetime:
    """The user-facing TikTok publish instant (PFM fires at exactly this time)."""
    return _normalize_utc(job.platform_scheduled_at.get("tiktok") or job.slot_time)


def _platform_due_time(job: Job, platform: str) -> datetime:
    """Due time of the platform's next pending action.

    TikTok runs three phases: media staging is due as soon as the job exists;
    post creation at sched - TIKTOK_SCHEDULE_LEAD_MINUTES (PFM then publishes
    server-side at sched via scheduled_at); result polling from sched.
    The stored times are never mutated."""
    if platform != "tiktok":
        due_time = job.platform_scheduled_at.get(platform) or job.slot_time
        return _normalize_utc(due_time)
    sched = _tiktok_sched(job)
    state = job.tiktok_publish_state
    if state and state.post_id and state.stage != "failed":
        return sched                                        # poll results at slot
    if state and state.media_url:
        return sched - timedelta(minutes=TIKTOK_SCHEDULE_LEAD_MINUTES)  # create post
    return _normalize_utc(job.created_at)                   # stage media on arrival


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _record_tiktok_failure(
    job: Job, store: JobStore, settings: Settings, discord, *,
    attempts: int, detail: str | None,
) -> None:
    """Shared attempt-counted failure handling for the create/poll phases."""
    now = datetime.now(tz=UTC)
    if attempts >= _TT_MAX_ATTEMPTS:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="failed", detail=detail, attempts=attempts, completed_at=now
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        await _post_failure_ping(
            job, settings, discord, detail or "publish failed",
            platform_label="TikTok",
        )
        logger.warning(
            "TikTok publish failed for %s after %d attempts: %s",
            job.project_id, attempts, detail,
        )
    else:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(status="pending", detail=detail, attempts=attempts),
        )
        logger.info(
            "TikTok publish attempt %d/%d failed for %s: %s — will retry next tick",
            attempts, _TT_MAX_ATTEMPTS, job.project_id, detail,
        )


async def _dispatch_tiktok_publish(  # noqa: PLR0911, PLR0912, PLR0915
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    """Run every currently-due TikTok phase for this job (stage → create → poll).

    'uploading' is NOT terminal: with the in-flight registry preventing
    concurrent dispatch, seeing it here means a previous process crashed
    mid-phase. The persisted publish_state (post_id → never re-create) is the
    double-post protection."""
    current = job.platform_statuses.get("tiktok", PlatformStatus(status="pending"))
    if current.status in ("uploaded", "failed", "skipped"):
        return False
    payload = job.tiktok_payload
    if not payload:
        logger.warning(
            "Job %s has 'tiktok' in platforms_requested but no tiktok_payload",
            job.project_id,
        )
        return False

    now = datetime.now(tz=UTC)
    sched = _tiktok_sched(job)
    create_due = sched - timedelta(minutes=TIKTOK_SCHEDULE_LEAD_MINUTES)
    state = job.tiktok_publish_state

    if not settings.pfm_api_key:
        if now < create_due:
            return False  # stay quiet until the publish window
        await _record_tiktok_failure(
            job, store, settings, discord,
            attempts=current.attempts + 1,
            detail="ATR_PFM_API_KEY is not configured",
        )
        return False

    # ---- Phase 1: stage media (due on arrival; quiet retries pre-window) ----
    if not (state and (state.media_url or (state.post_id and state.stage != "failed"))):
        result = await stage_media_for_tiktok(
            api_key=settings.pfm_api_key,
            base_url=settings.pfm_base_url,
            download_url=job.drive_video_url,
            publish_state=state,
            temp_dir=settings.data_dir / "tmp" / "tiktok",
        )
        if result.publish_state is not None:
            await store.set_tiktok_publish_state(job.project_id, result.publish_state)
            state = result.publish_state
        if not result.success:
            if now < create_due:
                logger.info(
                    "TikTok media staging failed for %s (quiet attempt %d): %s",
                    job.project_id,
                    state.media_attempts if state else 0,
                    result.detail,
                )
                return False
            await _record_tiktok_failure(
                job, store, settings, discord,
                attempts=current.attempts + 1, detail=result.detail,
            )
            return False
        logger.info("TikTok media staged for %s", job.project_id)

    if now < create_due:
        return True  # staged; post creation comes due at sched - lead

    # ---- Phases 2+3 share one attempt increment per dispatch ----
    next_attempts = current.attempts + 1
    await store.merge_platform_status(
        job.project_id, "tiktok",
        PlatformStatus(status="uploading", attempts=next_attempts),
    )

    async def persist_tiktok_state(new_state: TikTokPublishState) -> None:
        await store.set_tiktok_publish_state(job.project_id, new_state)

    # ---- Phase 2: ensure the post exists (scheduled, or instant when late) ----
    instant = False
    if not (state and state.post_id and state.stage != "failed"):
        instant = (sched - now).total_seconds() < _TT_INSTANT_PUBLISH_CUTOFF_SECONDS
        result = await create_tiktok_post(
            api_key=settings.pfm_api_key,
            base_url=settings.pfm_base_url,
            social_account_id=payload["social_account_id"],
            caption=payload["caption"],
            privacy_status=payload.get("privacy_status", "public"),
            allow_comment=bool(payload.get("allow_comment", True)),
            allow_duet=bool(payload.get("allow_duet", True)),
            allow_stitch=bool(payload.get("allow_stitch", True)),
            scheduled_at=None if instant else sched,
            publish_state=state,
        )
        if result.publish_state is not None:
            await store.set_tiktok_publish_state(job.project_id, result.publish_state)
            state = result.publish_state
        if not result.success:
            await _record_tiktok_failure(
                job, store, settings, discord,
                attempts=next_attempts, detail=result.detail,
            )
            return False
        logger.info(
            "TikTok post %s for %s (post_id=%s)",
            "created for instant publish" if instant
            else f"scheduled at {sched.isoformat()}",
            job.project_id, state.post_id,
        )

    # ---- Phase 3: poll results (from sched; instant posts poll right away) ----
    if not instant and now < sched:
        return True  # PFM will fire at sched; polling comes due then

    result = await poll_tiktok_post_result(
        api_key=settings.pfm_api_key,
        base_url=settings.pfm_base_url,
        social_account_id=payload["social_account_id"],
        publish_state=state,
        progress_callback=persist_tiktok_state,
    )
    if result.publish_state is not None:
        await store.set_tiktok_publish_state(job.project_id, result.publish_state)

    if result.success:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="uploaded",
                url=result.url,
                attempts=next_attempts,
                completed_at=datetime.now(tz=UTC),
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        logger.info(
            "TikTok publish succeeded for %s (url=%s)", job.project_id, result.url
        )
        return True
    await _record_tiktok_failure(
        job, store, settings, discord,
        attempts=next_attempts, detail=result.detail,
    )
    return False


async def _dispatch_instagram_publish(
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    payload = job.instagram_payload
    if not payload:
        logger.warning(
            "Job %s has 'instagram' in platforms_requested but no instagram_payload",
            job.project_id,
        )
        return False
    current = job.platform_statuses.get("instagram", PlatformStatus(status="pending"))
    # Already terminal — nothing to do
    if current.status in ("uploaded", "failed", "skipped"):
        if _should_retry_recoverable_instagram_failure(current):
            logger.info(
                "Retrying recoverable Instagram failure for %s",
                job.project_id,
            )
        else:
            return False

    if current.status in ("uploaded", "skipped"):
        return False

    next_attempts = current.attempts + 1
    # Bump status to uploading + attempts before the call.
    # Use merge_platform_status (atomic read-merge-write under the lock) so a
    # concurrent reaction-handler write to platform_statuses['tiktok'] isn't
    # clobbered by a stale snapshot during the multi-minute IG poll window.
    await store.merge_platform_status(
        job.project_id, "instagram",
        PlatformStatus(status="uploading", attempts=next_attempts),
    )

    async def persist_instagram_state(state: InstagramPublishState) -> None:
        await store.set_instagram_publish_state(job.project_id, state)

    prepared_video_url = payload.get("prepared_video_url") or ""
    instagram_video_url = str(prepared_video_url).strip() or _instagram_video_url(job, settings)
    instagram_download_url = str(prepared_video_url).strip() or job.drive_video_url

    result = await publish_to_instagram(
        ig_user_id=payload["ig_user_id"],
        ig_access_token=payload["ig_access_token"],
        caption=payload["caption"],
        video_url=instagram_video_url,
        download_url=instagram_download_url,
        graph_api_version=payload.get("graph_api_version", "v25.0"),
        poll_interval=float(
            payload.get("poll_interval_seconds") or _IG_DEFAULT_POLL_INTERVAL_SECONDS
        ),
        poll_timeout=float(
            payload.get("poll_timeout_seconds") or _IG_DEFAULT_POLL_TIMEOUT_SECONDS
        ),
        share_to_feed=(
            True if payload.get("share_to_feed") is None else bool(payload["share_to_feed"])
        ),
        thumb_offset=payload.get("thumb_offset"),
        publish_state=job.instagram_publish_state,
        progress_callback=persist_instagram_state,
        project_id=job.project_id,
        temp_dir=settings.data_dir / "tmp" / "instagram",
    )
    if (result_state := getattr(result, "publish_state", None)) is not None:
        await store.set_instagram_publish_state(job.project_id, result_state)

    now = datetime.now(tz=UTC)
    if result.success:
        await store.merge_platform_status(
            job.project_id, "instagram",
            PlatformStatus(
                status="uploaded",
                url=result.permalink,
                attempts=next_attempts,
                completed_at=now,
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        logger.info(
            "Instagram publish succeeded for %s (permalink=%s)",
            job.project_id,
            result.permalink,
        )
        return True

    # Failure path
    if next_attempts >= _IG_MAX_ATTEMPTS:
        await store.merge_platform_status(
            job.project_id, "instagram",
            PlatformStatus(
                status="failed",
                detail=result.detail,
                attempts=next_attempts,
                completed_at=now,
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        await _post_failure_ping(
            job, settings, discord, result.detail or "publish failed",
            platform_label="Instagram",
        )
        logger.warning(
            "Instagram publish failed for %s after %d attempts: %s",
            job.project_id, next_attempts, result.detail,
        )
    else:
        # Reset to pending so next tick retries; preserve detail for visibility
        await store.merge_platform_status(
            job.project_id, "instagram",
            PlatformStatus(
                status="pending",
                detail=result.detail,
                attempts=next_attempts,
            ),
        )
        logger.info(
            "Instagram publish attempt %d/%d failed for %s: %s — will retry next tick",
            next_attempts, _IG_MAX_ATTEMPTS, job.project_id, result.detail,
        )
    return False


async def _post_failure_ping(
    job: Job, settings: Settings, discord, detail: str, *, platform_label: str
) -> None:
    role = settings.discord.reminder_role_id
    msg = (
        f"<@&{role}> {platform_label} publish failed for **{job.anime_title}** "
        f"({job.account_id}): {detail}"
    )
    try:
        await discord.post_message(settings.discord.reminder_channel_id, content=msg)
    except Exception:
        logger.exception("Failed to post %s failure ping", platform_label)


def _instagram_video_url(job: Job, settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/videos/{job.project_id}"


def _should_retry_recoverable_instagram_failure(status: PlatformStatus) -> bool:
    detail = status.detail or ""
    retryable_attempts = {
        _LEGACY_IG_CONTAINER_ERROR: _IG_MAX_ATTEMPTS,
        _URL_INGEST_IG_CONTAINER_ERROR: _IG_MAX_ATTEMPTS,
        _RESUMABLE_HEADER_ERROR: _IG_MAX_ATTEMPTS + 1,
        _PREPARE_VIDEO_PASS_ERROR: _IG_MAX_ATTEMPTS,
        _PREPARE_VIDEO_FFMPEG_ERROR: _IG_MAX_ATTEMPTS,
        _PREPARE_VIDEO_FFMPEG_ERRORED: _IG_MAX_ATTEMPTS,
        _DOWNLOAD_STAGE_ERROR: _IG_MAX_ATTEMPTS,
    }
    return (
        status.status == "failed"
        and any(
            marker in detail and status.attempts == attempts
            for marker, attempts in retryable_attempts.items()
        )
    )


async def _rerender_embed(
    project_id: str, store: JobStore, settings: Settings, discord
) -> None:
    job = await store.get(project_id)
    if job is None or job.discord_message_id is None:
        return
    try:
        embed = build_embed(job, settings.accounts, settings.public_base_url)
        await discord.edit_message(
            settings.discord.upload_channel_id, job.discord_message_id, embed=embed
        )
    except Exception:
        logger.exception("Failed to re-render embed for %s", project_id)


async def run_scheduler_loop(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    interval_seconds: float = 30.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the scheduler until `stop_event` is set."""
    logger.info("Scheduler started (interval=%.1fs)", interval_seconds)
    while True:
        try:
            await dispatch_due_actions(store=store, settings=settings, discord=discord)
        except Exception:
            logger.exception("Scheduler tick failed")
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                logger.info("Scheduler stopping")
                return
            except TimeoutError:
                continue
        await asyncio.sleep(interval_seconds)
