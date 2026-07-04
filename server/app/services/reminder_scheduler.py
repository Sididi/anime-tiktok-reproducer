"""Background scheduler that fires platform-specific actions at their due time.

Polls every `interval` seconds; for each job, iterates `platforms_requested`
and runs due per-platform actions:

- tiktok    → publish the video via Post for Me (managed TikTok API).
              Retries like Instagram; after 5 attempts give up + ping.
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
from datetime import UTC, datetime

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
from app.services.post_for_me_publisher import TikTokPublishResult, publish_to_tiktok

logger = logging.getLogger(__name__)

_IG_MAX_ATTEMPTS = 5
_TT_MAX_ATTEMPTS = 5
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


def _platform_due_time(job: Job, platform: str) -> datetime:
    due_time = job.platform_scheduled_at.get(platform) or job.slot_time
    return _normalize_utc(due_time)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _dispatch_tiktok_publish(
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    payload = job.tiktok_payload
    if not payload:
        logger.warning(
            "Job %s has 'tiktok' in platforms_requested but no tiktok_payload",
            job.project_id,
        )
        return False
    current = job.platform_statuses.get("tiktok", PlatformStatus(status="pending"))
    # 'uploading' is NOT terminal: the scheduler loop is sequential, so seeing it
    # at tick start means the process crashed mid-publish. Re-dispatch; the
    # persisted tiktok_publish_state (post_id → poll instead of create) is the
    # double-post protection.
    if current.status in ("uploaded", "failed", "skipped"):
        return False

    next_attempts = current.attempts + 1
    # merge_platform_status is atomic under the store lock (see the Instagram
    # dispatcher for the rationale).
    await store.merge_platform_status(
        job.project_id, "tiktok",
        PlatformStatus(status="uploading", attempts=next_attempts),
    )

    async def persist_tiktok_state(state: TikTokPublishState) -> None:
        await store.set_tiktok_publish_state(job.project_id, state)

    if not settings.pfm_api_key:
        result = TikTokPublishResult(
            success=False, detail="ATR_PFM_API_KEY is not configured"
        )
    else:
        result = await publish_to_tiktok(
            api_key=settings.pfm_api_key,
            base_url=settings.pfm_base_url,
            social_account_id=payload["social_account_id"],
            caption=payload["caption"],
            download_url=job.drive_video_url,
            privacy_status=payload.get("privacy_status", "public"),
            allow_comment=bool(payload.get("allow_comment", True)),
            allow_duet=bool(payload.get("allow_duet", True)),
            allow_stitch=bool(payload.get("allow_stitch", True)),
            publish_state=job.tiktok_publish_state,
            progress_callback=persist_tiktok_state,
            temp_dir=settings.data_dir / "tmp" / "tiktok",
        )
    if result.publish_state is not None:
        await store.set_tiktok_publish_state(job.project_id, result.publish_state)

    now = datetime.now(tz=UTC)
    if result.success:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="uploaded",
                url=result.url,
                attempts=next_attempts,
                completed_at=now,
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        logger.info(
            "TikTok publish succeeded for %s (url=%s)", job.project_id, result.url
        )
        return True

    if next_attempts >= _TT_MAX_ATTEMPTS:
        await store.merge_platform_status(
            job.project_id, "tiktok",
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
            platform_label="TikTok",
        )
        logger.warning(
            "TikTok publish failed for %s after %d attempts: %s",
            job.project_id, next_attempts, result.detail,
        )
    else:
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="pending",
                detail=result.detail,
                attempts=next_attempts,
            ),
        )
        logger.info(
            "TikTok publish attempt %d/%d failed for %s: %s — will retry next tick",
            next_attempts, _TT_MAX_ATTEMPTS, job.project_id, result.detail,
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
