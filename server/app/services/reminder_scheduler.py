"""Background scheduler that fires platform-specific actions at slot_time.

Polls every `interval` seconds; for each job whose slot_time has passed,
iterates `platforms_requested` and runs the per-platform action:

- tiktok    → post reminder (rich embed + forward) in the reminder channel.
              Skipped if `reminder_message_id` is already set or
              `reminder_cancelled` is True (operator reacted before slot).
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
from datetime import datetime, timezone

from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services.embed_builder import build_embed
from app.services.instagram_publisher import publish_to_instagram
from app.services.job_store import JobStore
from app.services.reminder_service import post_reminder

logger = logging.getLogger(__name__)

_IG_MAX_ATTEMPTS = 5


async def dispatch_due_actions(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    now: datetime | None = None,
) -> int:
    """Run per-platform actions for any due job. Returns count of actions taken."""
    current = now or datetime.now(tz=timezone.utc)
    actions = 0
    for job in await store.list_all():
        if job.slot_time > current:
            continue
        for platform in job.platforms_requested:
            if platform == "tiktok":
                if await _dispatch_tiktok_reminder(job, store, settings, discord):
                    actions += 1
            elif platform == "instagram":
                if await _dispatch_instagram_publish(job, store, settings, discord):
                    actions += 1
            # youtube + facebook: nothing to do (main backend handles those)
    return actions


async def _dispatch_tiktok_reminder(
    job: Job, store: JobStore, settings: Settings, discord
) -> bool:
    if job.reminder_cancelled:
        return False
    if job.reminder_message_id is not None:
        return False
    # Don't post if tiktok platform is already uploaded (e.g. operator reacted)
    tt = job.platform_statuses.get("tiktok", PlatformStatus(status="pending"))
    if tt.status != "pending":
        return False
    account = settings.accounts.get(job.account_id)
    if account is None:
        logger.warning(
            "Job %s references unknown account %s; skipping TikTok reminder",
            job.project_id,
            job.account_id,
        )
        return False
    rich_id, forward_id = await post_reminder(
        discord,
        job=job,
        account=account,
        public_base_url=settings.public_base_url,
        upload_channel_id=settings.discord.upload_channel_id,
        reminder_channel_id=settings.discord.reminder_channel_id,
        role_id=settings.discord.reminder_role_id,
        guild_id=settings.discord.guild_id,
    )
    if rich_id is None:
        return False
    await store.update(
        job.project_id,
        reminder_message_id=rich_id,
        reminder_forward_message_id=forward_id,
    )
    logger.info(
        "TikTok reminder dispatched for %s (rich=%s forward=%s)",
        job.project_id,
        rich_id,
        forward_id,
    )
    return True


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
        return False

    next_attempts = current.attempts + 1
    # Bump status to uploading + attempts before the call
    new_uploading = PlatformStatus(status="uploading", attempts=next_attempts)
    await store.update(
        job.project_id,
        platform_statuses={**job.platform_statuses, "instagram": new_uploading},
    )

    result = await publish_to_instagram(
        ig_user_id=payload["ig_user_id"],
        ig_access_token=payload["ig_access_token"],
        caption=payload["caption"],
        video_url=job.drive_video_url,
        graph_api_version=payload.get("graph_api_version", "v25.0"),
    )

    now = datetime.now(tz=timezone.utc)
    if result.success:
        await store.update(
            job.project_id,
            platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                status="uploaded",
                url=result.permalink,
                attempts=next_attempts,
                completed_at=now,
            )},
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
        await store.update(
            job.project_id,
            platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                status="failed",
                detail=result.detail,
                attempts=next_attempts,
                completed_at=now,
            )},
        )
        await _rerender_embed(job.project_id, store, settings, discord)
        await _post_failure_ping(job, settings, discord, result.detail or "publish failed")
        logger.warning(
            "Instagram publish failed for %s after %d attempts: %s",
            job.project_id, next_attempts, result.detail,
        )
    else:
        # Reset to pending so next tick retries; preserve detail for visibility
        await store.update(
            job.project_id,
            platform_statuses={**job.platform_statuses, "instagram": PlatformStatus(
                status="pending",
                detail=result.detail,
                attempts=next_attempts,
            )},
        )
        logger.info(
            "Instagram publish attempt %d/%d failed for %s: %s — will retry next tick",
            next_attempts, _IG_MAX_ATTEMPTS, job.project_id, result.detail,
        )
    return False


async def _post_failure_ping(
    job: Job, settings: Settings, discord, detail: str
) -> None:
    role = settings.discord.reminder_role_id
    msg = (
        f"<@&{role}> Instagram publish failed for **{job.anime_title}** "
        f"({job.account_id}): {detail}"
    )
    try:
        await discord.post_message(settings.discord.reminder_channel_id, content=msg)
    except Exception:
        logger.exception("Failed to post Instagram failure ping")


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
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(interval_seconds)
