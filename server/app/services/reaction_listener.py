"""Discord gateway listener for ✅ reactions on job embeds (manual TikTok mode).

Runs alongside the existing httpx REST client. Connects to the gateway,
listens for `MessageReactionAdd` events, and on ✅ from a non-bot user
on either the upload-channel embed OR the reminder message of a
`tiktok_manual` job, triggers the manual-ack flow:

  1. Mark platform_statuses['tiktok'] = uploaded (detail "Posté manuellement")
  2. If the reminder hasn't fired yet, set reminder_cancelled=True
  3. Re-render the upload-channel embed (✅ TikTok line)
  4. Bot adds its own ✅ reaction on the upload-channel embed
  5. Delete the reminder message if present

PFM-published jobs (tiktok_manual=False) are ignored: a ✅ on their embed must
never override the Post for Me state.

History: disabled 2026-07 when Post for Me replaced manual posting (commented,
never deleted); revived 2026-08 for accounts without a Post for Me id.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import discord

from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services.embed_builder import build_embed
from app.services.job_store import JobStore
from app.services.reminder_service import cleanup_reminder

logger = logging.getLogger(__name__)

_ACK_EMOJI = "✅"


class ReactionListener:
    """Bot connected to Discord gateway. Single-purpose: react to ✅ reactions."""

    def __init__(
        self,
        *,
        bot_token: str,
        store: JobStore,
        settings: Settings,
        rest_discord_client,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = False  # we don't read content
        intents.reactions = True
        self._client = discord.Client(intents=intents)
        self._token = bot_token
        self._store = store
        self._settings = settings
        self._rest = rest_discord_client
        self._task: asyncio.Task | None = None
        # Cached bot user id — populated once the gateway identifies.
        # Can be overridden in tests without touching the property-locked Client.
        self._bot_user_id: int | None = None

        @self._client.event
        async def on_ready():
            if self._client.user:
                self._bot_user_id = self._client.user.id
                logger.info("ReactionListener ready as bot user %s", self._bot_user_id)

        @self._client.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            await self._handle_reaction(payload)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        # Filter: emoji must be ✅
        if str(payload.emoji) != _ACK_EMOJI:
            return
        # Filter: not from the bot itself.
        # Use the cached _bot_user_id first (set after gateway READY, or injected
        # in tests), falling back to the live client.user property.
        bot_id = self._bot_user_id or (self._client.user and self._client.user.id)
        if bot_id and payload.user_id == bot_id:
            return

        # Look up the job by message_id (could be the upload-channel embed
        # OR the rich reminder).
        job = await self._find_job_by_message(str(payload.message_id))
        if job is None:
            return
        if not job.tiktok_manual:
            logger.debug(
                "✅ reaction on %s ignored: job %s is not in manual TikTok mode",
                payload.message_id, job.project_id,
            )
            return
        existing_tiktok = job.platform_statuses.get(
            "tiktok", PlatformStatus(status="pending")
        )
        if existing_tiktok.status != "pending":
            # Idempotent re-ack (or cancelled row): nothing to rewrite, just
            # make sure the reminder is gone.
            await self._cleanup_reminder(job)
            return

        logger.info(
            "✅ reaction on %s by user %s → marking tiktok done for %s",
            payload.message_id,
            payload.user_id,
            job.project_id,
        )

        now = datetime.now(tz=UTC)
        # merge_platform_status is atomic under the lock — won't clobber a
        # concurrent IG publish writing to platform_statuses['instagram'].
        await self._store.merge_platform_status(
            job.project_id, "tiktok",
            PlatformStatus(
                status="uploaded",
                detail="Posté manuellement",
                completed_at=now,
                attempts=existing_tiktok.attempts,
            ),
        )
        # Cancel the reminder if it hasn't fired yet (separate, orthogonal field).
        if job.reminder_message_id is None:
            await self._store.update(job.project_id, reminder_cancelled=True)

        # Re-render upload-channel embed + add bot's own ✅ reaction.
        if job.discord_message_id:
            try:
                latest = await self._store.get(job.project_id)
                if latest is not None:
                    embed = build_embed(
                        latest, self._settings.accounts, self._settings.public_base_url
                    )
                    await self._rest.edit_message(
                        self._settings.discord.upload_channel_id,
                        job.discord_message_id,
                        embed=embed,
                    )
                    await self._rest.add_reaction(
                        self._settings.discord.upload_channel_id,
                        job.discord_message_id,
                        _ACK_EMOJI,
                    )
            except Exception:
                logger.exception("Failed to re-render embed after ack")

        # Delete the reminder message if it exists (operator reacted AFTER
        # the reminder fired).
        await self._cleanup_reminder(job)

    async def _find_job_by_message(self, message_id: str) -> Job | None:
        """Match the message_id against any job's discord_message_id or reminder_message_id."""
        for j in await self._store.list_all():
            if j.discord_message_id == message_id:
                return j
            if j.reminder_message_id == message_id:
                return j
        return None

    async def _cleanup_reminder(self, job: Job) -> None:
        """Delete the reminder message(s) for this job (shared helper)."""
        await cleanup_reminder(self._rest, self._store, self._settings, job)

    async def start(self) -> None:
        """Start the gateway connection in a background task."""
        self._task = asyncio.create_task(self._client.start(self._token))
        logger.info("ReactionListener gateway connection starting")

    async def stop(self) -> None:
        """Close the gateway and wait for the task to finish."""
        await self._client.close()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                # Connection errors during shutdown are expected (e.g. connector
                # already closed, test environment with no real gateway token).
                logger.debug("ReactionListener task raised during stop", exc_info=True)
        logger.info("ReactionListener gateway connection stopped")
