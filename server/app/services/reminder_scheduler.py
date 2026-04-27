"""Background scheduler that fires reminders at slot_time.

Runs as an asyncio task spawned in the FastAPI lifespan. Every `interval`
seconds (default 30s), scans pending jobs and posts the reminder for any
whose `slot_time` has passed but `reminder_message_id` is still None.

Survives VPS restarts: the scheduler is purely state-driven (re-reads
jobs.json on every tick) so a restart simply resumes the polling.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.services.job_store import JobStore
from app.services.reminder_service import post_reminder

logger = logging.getLogger(__name__)


async def dispatch_due_reminders(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    now: datetime | None = None,
) -> int:
    """Fire reminders for any pending jobs whose slot_time has passed.

    Returns the number of reminders posted (or attempted) on this tick.
    """
    current = now or datetime.now(tz=timezone.utc)
    posted = 0
    for device_id in settings.devices:
        for job in await store.list_for_device(device_id, status="pending"):
            if job.reminder_message_id is not None:
                continue  # already reminded
            if job.slot_time > current:
                continue  # not yet due
            account = settings.accounts.get(job.account_id)
            if account is None:
                logger.warning(
                    "Job %s references unknown account %s; skipping reminder",
                    job.project_id,
                    job.account_id,
                )
                continue

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
                # Will retry on next tick.
                continue
            await store.update(
                job.project_id,
                reminder_message_id=rich_id,
                reminder_forward_message_id=forward_id,
            )
            posted += 1
            logger.info(
                "Reminder dispatched for %s (rich=%s forward=%s)",
                job.project_id,
                rich_id,
                forward_id,
            )
    return posted


async def run_scheduler_loop(
    *,
    store: JobStore,
    settings: Settings,
    discord,
    interval_seconds: float = 30.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the scheduler until `stop_event` is set (or forever if None)."""
    logger.info("Reminder scheduler started (interval=%.1fs)", interval_seconds)
    while True:
        try:
            await dispatch_due_reminders(store=store, settings=settings, discord=discord)
        except Exception:
            logger.exception("Reminder scheduler tick failed")
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                logger.info("Reminder scheduler stopping")
                return
            except asyncio.TimeoutError:
                continue
        await asyncio.sleep(interval_seconds)
