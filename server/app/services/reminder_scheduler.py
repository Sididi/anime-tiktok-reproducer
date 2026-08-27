"""Background scheduler that fires platform-specific actions at their due time.

Polls every `interval` seconds; for each job, iterates `platforms_requested`
and runs due per-platform actions:

- tiktok    → two modes (2026-08):
              * PFM accounts: RETIRED server-side — the main backend creates
                the Post for Me post directly (native scheduled_at); the
                dispatcher is kept commented.
              * MANUAL accounts (job.tiktok_manual, no Post for Me id): post
                ONE reminder in the reminder channel at
                sched - TIKTOK_MANUAL_REMINDER_LEAD_MINUTES; the ✅ reaction
                listener marks the row uploaded and deletes the reminder.
- instagram → call Instagram Graph API to publish the Reel. On success,
              update the embed. On failure, increment attempts; after
              5 attempts give up + ping the reminder channel.
- youtube   → no-op (main backend schedules natively via publishAt).
- facebook  → no-op for jobs without a facebook_payload (main backend
              schedules natively via video_state within Meta's ~29d window).
              Jobs WITH a facebook_payload are long-range holds (target beyond
              the window): at T - FACEBOOK_CONVERT_LEAD_DAYS the server either
              uploads the prepared video as a native scheduled post (CREATE
              hold) or pushes an existing post's scheduled_publish_time to the
              real target (RETIME hold).

Survives VPS restarts: the scheduler is purely state-driven (re-reads
jobs.json every tick), so a restart simply resumes polling.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.models.job import (
    FacebookPublishState,
    InstagramPublishState,
    Job,
    PlatformStatus,
    TikTokPublishState,
)
from app.services.embed_builder import build_embed
from app.services.facebook_publisher import (
    create_facebook_scheduled_post,
    retime_facebook_scheduled_post,
)
from app.services.instagram_publisher import publish_to_instagram
from app.services.job_store import JobStore
from app.services.post_for_me_publisher import (
    create_tiktok_post,
    poll_tiktok_post_result,
    stage_media_for_tiktok,
)
from app.services.reminder_service import post_reminder

logger = logging.getLogger(__name__)

_IG_MAX_ATTEMPTS = 5
_TT_MAX_ATTEMPTS = 5
_FB_MAX_ATTEMPTS = 5
# Long-range Facebook holds convert to a native scheduled post this many days
# before the target (Meta's scheduled_publish_time window is ~29 days; the
# 1-day margin absorbs clock drift and slow ticks).
FACEBOOK_CONVERT_LEAD_DAYS = 28
# Quota errors (403 reached_active_user_cap) are NOT retried on a long spaced
# window: the owner prefers a fast terminal failure + Discord ping so the
# video can be posted manually near its slot, over an hour-long retry window
# that may still fail (owner decision 2026-07-24, reverting the 07-21 pacing).
# Post creation lead: the PFM post (with scheduled_at = the true slot) is
# created this many minutes before the slot. Must stay <= the backend's
# TIKTOK_EDIT_LOCK_MINUTES (backend/app/services/scheduling_service.py):
# job data freezes at sched-15, the post is created from it at sched-10.
TIKTOK_SCHEDULE_LEAD_MINUTES = 10
_TT_INSTANT_PUBLISH_CUTOFF_SECONDS = 60  # sched closer than this → publish instantly
# Manual TikTok mode: the single reminder fires this many minutes before the
# TikTok publish instant (owner decision 2026-08-24: one warning, 5 min).
TIKTOK_MANUAL_REMINDER_LEAD_MINUTES = 5
# Never post a reminder for a slot this far in the past (a redeploy/restart
# must not spam the channel with reminders for long-gone slots).
_MANUAL_REMINDER_MAX_LATE = timedelta(hours=6)
_IG_DEFAULT_POLL_INTERVAL_SECONDS = 60.0
_IG_DEFAULT_POLL_TIMEOUT_SECONDS = 4 * 60 * 60.0
_LEGACY_IG_CONTAINER_ERROR = "container status_code = ERROR"
_URL_INGEST_IG_CONTAINER_ERROR = "error code 2207077"
_RESUMABLE_HEADER_ERROR = "Invalid Header format"
_PREPARE_VIDEO_PASS_ERROR = "prepare_video: video preparation pass"
_PREPARE_VIDEO_FFMPEG_ERROR = "prepare_video: ffmpeg failed"
_PREPARE_VIDEO_FFMPEG_ERRORED = "prepare_video: ffmpeg errored"
_DOWNLOAD_STAGE_ERROR = "download:"


# (project_id, platform) → running dispatch task. In-memory only: after a
# process restart this is empty, so a job persisted as 'uploading' is
# correctly treated as crashed-mid-phase and re-dispatched.
_IN_FLIGHT: dict[tuple[str, str], asyncio.Task] = {}


def _dispatch_worthwhile(job: Job, platform: str) -> bool:
    """Cheap pre-checks so terminal/misconfigured jobs never spawn a task."""
    status = job.platform_statuses.get(platform, PlatformStatus(status="pending"))
    if platform == "tiktok" and job.tiktok_manual:
        return (
            status.status == "pending"
            and not job.reminder_cancelled
            and job.reminder_message_id is None
        )
    if platform == "tiktok":
        if status.status in ("uploaded", "failed", "skipped"):
            return False
        if not job.tiktok_payload:
            logger.warning(
                "Job %s has 'tiktok' in platforms_requested but no tiktok_payload",
                job.project_id,
            )
            return False
        return True
    if platform == "facebook":
        # Only long-range holds are server-dispatched; jobs without a
        # facebook_payload are scheduled natively by the main backend.
        if not job.facebook_payload:
            return False
        return status.status not in ("uploaded", "failed", "skipped")
    # instagram
    if status.status in ("uploaded", "skipped"):
        return False
    if not job.instagram_payload:
        # Expected for display-only rows (2026-08): an urgent-immediate
        # Instagram publish is backend-side — the row exists on the job for
        # the Discord embed but carries no payload and is never dispatched.
        logger.debug(
            "Job %s has 'instagram' in platforms_requested but no instagram_payload "
            "(display-only row)",
            job.project_id,
        )
        return False
    if status.status == "failed":
        if _should_retry_recoverable_instagram_failure(status):
            logger.info("Retrying recoverable Instagram failure for %s", job.project_id)
            return True
        return False
    return True


async def _run_dispatch(key: tuple[str, str], action) -> None:
    try:
        await action
    except Exception:
        logger.exception("Dispatch crashed for %s/%s", key[0], key[1])
    finally:
        _IN_FLIGHT.pop(key, None)


async def wait_for_inflight() -> None:
    """Await completion of every in-flight dispatch task (tests + shutdown)."""
    while _IN_FLIGHT:
        await asyncio.wait(list(_IN_FLIGHT.values()))


async def dispatch_due_actions(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    now: datetime | None = None,
) -> int:
    """Start a background dispatch task for every due (job, platform) action
    not already in flight. Returns the number of tasks started; use
    wait_for_inflight() to await their completion."""
    current = _normalize_utc(now or datetime.now(tz=UTC))
    started = 0
    for job in await store.list_all():
        for platform in job.platforms_requested:
            # 2026-08 PFM MIGRATION: TikTok is now scheduled by the main
            # backend directly against Post for Me (native scheduled_at,
            # backend/app/services/post_for_me_client.py). New jobs no longer
            # carry "tiktok" in platforms_requested; the dispatcher below is
            # kept COMMENTED (never delete) in case the relay path is needed
            # again. Legacy in-flight jobs with a live post_scheduled PFM post
            # publish server-side via PFM regardless.
            # if platform == "tiktok":
            #     dispatcher = _dispatch_tiktok_publish
            # elif platform == "instagram":
            if platform == "tiktok" and job.tiktok_manual:
                # Manual TikTok mode: single Discord reminder at sched - 5 min.
                dispatcher = _dispatch_tiktok_reminder
            elif platform == "instagram":
                dispatcher = _dispatch_instagram_publish
            elif platform == "facebook" and job.facebook_payload:
                # Long-range hold (target beyond Meta's ~29d window).
                dispatcher = _dispatch_facebook_hold
            else:
                continue  # youtube + native facebook + tiktok: scheduled natively/by backend
            key = (job.project_id, platform)
            if key in _IN_FLIGHT:
                continue
            if not _dispatch_worthwhile(job, platform):
                continue
            if _platform_due_time(job, platform) > current:
                continue
            action = dispatcher(job, store, settings, discord)
            _IN_FLIGHT[key] = asyncio.create_task(_run_dispatch(key, action))
            started += 1
    return started


def _tiktok_sched(job: Job) -> datetime:
    """The user-facing TikTok publish instant (PFM fires at exactly this time)."""
    return _normalize_utc(job.platform_scheduled_at.get("tiktok") or job.slot_time)


def _platform_due_time(job: Job, platform: str) -> datetime:
    """Due time of the platform's next pending action.

    Manual TikTok jobs are due (reminder) at sched - TIKTOK_MANUAL_REMINDER_LEAD_MINUTES.
    PFM TikTok runs three phases: media staging is due as soon as the job exists;
    post creation at sched - TIKTOK_SCHEDULE_LEAD_MINUTES (PFM then publishes
    server-side at sched via scheduled_at); result polling from sched.
    Facebook long-range holds convert at sched - FACEBOOK_CONVERT_LEAD_DAYS.
    The stored times are never mutated."""
    if platform == "facebook" and job.facebook_payload:
        sched = _normalize_utc(job.platform_scheduled_at.get(platform) or job.slot_time)
        return sched - timedelta(days=FACEBOOK_CONVERT_LEAD_DAYS)
    if platform != "tiktok":
        due_time = job.platform_scheduled_at.get(platform) or job.slot_time
        return _normalize_utc(due_time)
    sched = _tiktok_sched(job)
    if job.tiktok_manual:
        return sched - timedelta(minutes=TIKTOK_MANUAL_REMINDER_LEAD_MINUTES)
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


async def _dispatch_tiktok_reminder(  # noqa: PLR0911
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    """Manual TikTok mode: post the single reminder once it is due.

    Single-shot by construction: `reminder_message_id` is persisted right after
    posting, and `_dispatch_worthwhile` never re-dispatches a job that has one
    (or is cancelled / no longer pending)."""
    # Re-read: a ✅ ack or a cancel may have landed since the tick snapshot.
    latest = await store.get(job.project_id)
    if latest is None or not latest.tiktok_manual:
        return False
    job = latest
    if job.reminder_cancelled or job.reminder_message_id is not None:
        return False
    tt = job.platform_statuses.get("tiktok", PlatformStatus(status="pending"))
    if tt.status != "pending":
        return False
    account = settings.accounts.get(job.account_id)
    if account is None:
        logger.warning(
            "Job %s references unknown account %s; skipping TikTok reminder",
            job.project_id, job.account_id,
        )
        return False
    sched = _tiktok_sched(job)
    now = datetime.now(tz=UTC)
    if now - sched > _MANUAL_REMINDER_MAX_LATE:
        logger.warning(
            "TikTok reminder for %s skipped: slot %s is more than %s in the past",
            job.project_id, sched.isoformat(), _MANUAL_REMINDER_MAX_LATE,
        )
        return False
    message_id = await post_reminder(discord, job=job, account=account, settings=settings)
    if message_id is None:
        return False  # logged by post_reminder; retried next tick
    await store.update(job.project_id, reminder_message_id=message_id)
    logger.info(
        "TikTok manual reminder posted for %s (message=%s, slot=%s)",
        job.project_id, message_id, sched.isoformat(),
    )
    return True


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
        if attempts == 1:
            await _post_failure_ping(
                job, settings, discord,
                f"{detail or 'publish failed'} "
                f"(attempt 1/{_TT_MAX_ATTEMPTS}, retrying)",
                platform_label="TikTok",
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

    instant = False
    async with store.tiktok_publish_transition(job.project_id):
        # Staging may have overlapped a job/connector update. Refresh every
        # target-sensitive value under the transition lock before creating a
        # post, so a stale dispatcher can never publish to its old target.
        refreshed = await store.get(job.project_id)
        if refreshed is None or "tiktok" not in refreshed.platforms_requested:
            return False
        job = refreshed
        current = job.platform_statuses.get(
            "tiktok", PlatformStatus(status="pending")
        )
        if current.status in ("uploaded", "failed", "skipped"):
            return False
        payload = job.tiktok_payload
        if not payload:
            return False
        state = job.tiktok_publish_state
        sched = _tiktok_sched(job)
        create_due = sched - timedelta(minutes=TIKTOK_SCHEDULE_LEAD_MINUTES)
        now = datetime.now(tz=UTC)

        if now < create_due:
            return True  # staged; post creation comes due at sched - lead

        has_live_post = bool(state and state.post_id and state.stage != "failed")
        if not has_live_post and not (state and state.media_url):
            # A concurrent job update reset the staged state before this lock
            # was acquired. The next scheduler tick will stage the media for
            # the refreshed job; never create from the stale snapshot.
            return True

        # ---- Phases 2+3 share one attempt increment per dispatch ----
        next_attempts = current.attempts + 1
        await store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(status="uploading", attempts=next_attempts),
        )

        # ---- Phase 2: ensure the post exists (scheduled, or instant when late) ----
        if not has_live_post:
            instant = (
                sched - now
            ).total_seconds() < _TT_INSTANT_PUBLISH_CUTOFF_SECONDS
            result = await create_tiktok_post(
                api_key=settings.pfm_api_key,
                base_url=settings.pfm_base_url,
                social_account_id=payload["social_account_id"],
                post_for_me_platform=payload.get("post_for_me_platform", "tiktok"),
                caption=payload["caption"],
                privacy_status=payload.get("privacy_status", "public"),
                allow_comment=bool(payload.get("allow_comment", True)),
                allow_duet=bool(payload.get("allow_duet", True)),
                allow_stitch=bool(payload.get("allow_stitch", True)),
                # THUMBNAIL FEATURE DISABLED (2026-08-16, owner request):
                # covers are not forwarded to PFM. Uncomment to re-enable.
                # thumbnail_timestamp_ms=payload.get("thumbnail_timestamp_ms"),
                # thumbnail_url=payload.get("thumbnail_url"),
                scheduled_at=None if instant else sched,
                publish_state=state,
            )
            if result.publish_state is not None:
                await store.set_tiktok_publish_state(
                    job.project_id, result.publish_state
                )
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

    async def persist_tiktok_state(new_state: TikTokPublishState) -> None:
        await store.set_tiktok_publish_state(job.project_id, new_state)

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
        return False
    current = job.platform_statuses.get("instagram", PlatformStatus(status="pending"))
    if current.status in ("uploaded", "skipped"):
        return False
    if current.status == "failed" and not _should_retry_recoverable_instagram_failure(current):
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

    # `prepared_video_url` is whatever the backend decided Instagram must
    # ingest: a dedicated cut/sped-up artifact (output_instagram.mp4) or, since
    # 2026-08-27, the final video itself when nothing had to change. Either
    # way this server downloads it, validates it and re-hosts it for Meta, so
    # the two cases are indistinguishable here. Jobs created without any
    # prepared URL fall back to the job's Drive video for the download and to
    # this server's /api/videos proxy for Meta-side ingestion.
    prepared_video_url = str(payload.get("prepared_video_url") or "").strip()
    instagram_video_url = prepared_video_url or _instagram_video_url(job, settings)
    instagram_download_url = prepared_video_url or job.drive_video_url
    logger.info(
        "Instagram publish for %s: source=%s",
        job.project_id,
        "prepared_video_url" if prepared_video_url else "job.drive_video_url (no prepared media)",
    )

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
        # THUMBNAIL FEATURE DISABLED (2026-08-16, owner request):
        # covers are not forwarded to the Graph container. Uncomment to re-enable.
        # thumb_offset=payload.get("thumb_offset"),
        # cover_url=payload.get("cover_url"),
        publish_state=job.instagram_publish_state,
        progress_callback=persist_instagram_state,
        project_id=job.project_id,
        temp_dir=settings.data_dir / "tmp" / "instagram",
        max_duration_seconds=float(payload.get("max_duration_seconds") or 90.0),
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


async def _dispatch_facebook_hold(
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    """Convert a long-range Facebook hold once inside Meta's window.

    RETIME hold (payload carries video_id): push the parked native post's
    scheduled_publish_time to the real target. CREATE hold: upload the
    backend-prepared video as a native scheduled post."""
    payload = job.facebook_payload or {}
    state = job.facebook_publish_state or FacebookPublishState()
    sched = _normalize_utc(job.platform_scheduled_at.get("facebook") or job.slot_time)
    next_attempts = state.attempts + 1

    page_id = str(payload.get("page_id") or "")
    token = str(payload.get("page_access_token") or "")
    video_id = payload.get("video_id") or state.video_id
    if not page_id or not token:
        detail = "facebook hold: missing page_id/page_access_token"
        await store.merge_platform_status(
            job.project_id, "facebook",
            PlatformStatus(status="failed", detail=detail, attempts=next_attempts,
                           completed_at=datetime.now(tz=UTC)),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        return False

    if video_id:
        result = await retime_facebook_scheduled_post(
            page_id=page_id,
            page_access_token=token,
            video_id=str(video_id),
            scheduled_at=sched,
            graph_api_version=payload.get("graph_api_version"),
        )
        stage_on_success = "retimed"
    else:
        result = await create_facebook_scheduled_post(
            page_id=page_id,
            page_access_token=token,
            title=str(payload.get("title") or ""),
            description=str(payload.get("description") or ""),
            prepared_video_url=str(payload.get("prepared_video_url") or ""),
            scheduled_at=sched,
            graph_api_version=payload.get("graph_api_version"),
        )
        stage_on_success = "created"

    if result.success:
        await store.set_facebook_publish_state(
            job.project_id,
            FacebookPublishState(
                video_id=result.video_id, stage=stage_on_success, attempts=next_attempts
            ),
        )
        await store.merge_platform_status(
            job.project_id, "facebook",
            PlatformStatus(
                status="uploaded",
                url=result.url,
                detail=f"Scheduled natively at {sched.isoformat()} ({stage_on_success})",
                attempts=next_attempts,
                completed_at=datetime.now(tz=UTC),
            ),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        logger.info(
            "Facebook hold %s for %s (video_id=%s at=%s)",
            stage_on_success, job.project_id, result.video_id, sched.isoformat(),
        )
        return True

    await store.set_facebook_publish_state(
        job.project_id,
        FacebookPublishState(
            video_id=result.video_id or (str(video_id) if video_id else None),
            stage="failed" if next_attempts >= _FB_MAX_ATTEMPTS else state.stage,
            attempts=next_attempts,
            last_error=result.detail,
        ),
    )
    if next_attempts >= _FB_MAX_ATTEMPTS:
        await store.merge_platform_status(
            job.project_id, "facebook",
            PlatformStatus(status="failed", detail=result.detail,
                           attempts=next_attempts, completed_at=datetime.now(tz=UTC)),
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        await _post_failure_ping(
            job, settings, discord, result.detail or "hold conversion failed",
            platform_label="Facebook",
        )
        logger.warning(
            "Facebook hold failed for %s after %d attempts: %s",
            job.project_id, next_attempts, result.detail,
        )
    else:
        await store.merge_platform_status(
            job.project_id, "facebook",
            PlatformStatus(status="pending", detail=result.detail, attempts=next_attempts),
        )
        if next_attempts == 1:
            await _post_failure_ping(
                job, settings, discord,
                f"{result.detail or 'hold conversion failed'} "
                f"(attempt 1/{_FB_MAX_ATTEMPTS}, retrying)",
                platform_label="Facebook",
            )
        logger.info(
            "Facebook hold attempt %d/%d failed for %s: %s — will retry next tick",
            next_attempts, _FB_MAX_ATTEMPTS, job.project_id, result.detail,
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
