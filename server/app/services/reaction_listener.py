"""Discord gateway listener for ✅ reactions on job embeds.

Runs alongside the existing httpx REST client. Connects to the gateway,
listens for `MessageReactionAdd` events, and on ✅ from a non-bot user
on either the upload-channel embed OR the rich reminder, triggers the
manual-ack flow:

  1. Mark platform_statuses['tiktok'] = uploaded(completed_at=now)
  2. If reminder hasn't fired yet, set reminder_cancelled=True
  3. Re-render the upload-channel embed (✅ TikTok line)
  4. Bot adds its own ✅ reaction (visual confirmation)
  5. Delete reminder messages (rich + forward) if present
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from app.config import Settings
from app.models.job import Job, PlatformStatus
from app.services.embed_builder import build_embed
from app.services.job_store import JobStore

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

        logger.info(
            "✅ reaction on %s by user %s → marking tiktok done for %s",
            payload.message_id,
            payload.user_id,
            job.project_id,
        )

        now = datetime.now(tz=timezone.utc)
        existing_tiktok = job.platform_statuses.get(
            "tiktok", PlatformStatus(status="pending")
        )
        new_statuses = {
            **job.platform_statuses,
            "tiktok": PlatformStatus(
                status="uploaded",
                completed_at=now,
                attempts=existing_tiktok.attempts,
            ),
        }
        updates: dict = {"platform_statuses": new_statuses}
        # Cancel the reminder if it hasn't fired yet.
        if job.reminder_message_id is None:
            updates["reminder_cancelled"] = True
        await self._store.update(job.project_id, **updates)

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

        # Delete reminder messages if they exist (operator either reacted
        # AFTER reminder fired, or this is a redundant ack).
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
        """Delete any reminder + forward messages for this job."""
        deleted_anything = False
        if job.reminder_message_id:
            try:
                await self._rest.delete_message(
                    self._settings.discord.reminder_channel_id,
                    job.reminder_message_id,
                )
                deleted_anything = True
            except Exception:
                logger.warning("Failed to delete reminder rich message", exc_info=True)
        if job.reminder_forward_message_id:
            try:
                await self._rest.delete_message(
                    self._settings.discord.reminder_channel_id,
                    job.reminder_forward_message_id,
                )
                deleted_anything = True
            except Exception:
                logger.warning("Failed to delete reminder forward message", exc_info=True)
        if deleted_anything:
            await self._store.update(
                job.project_id,
                reminder_message_id=None,
                reminder_forward_message_id=None,
            )

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
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                # Connection errors during shutdown are expected (e.g. connector
                # already closed, test environment with no real gateway token).
                logger.debug("ReactionListener task raised during stop", exc_info=True)
        logger.info("ReactionListener gateway connection stopped")
