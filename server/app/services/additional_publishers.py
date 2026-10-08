"""Durable scheduling and manual reminders for directly integrated publishers.

Each platform's activity lock prevents cancellation/rescheduling from racing
its submission. A brief project guard coordinates whole-job mutations.
Adapters persist checkpoints before non-idempotent API requests.
Credentials and rotated tokens stay on the authenticated server, never in the
public status projection or Discord messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from app.models.job import PlatformStatus
from app.services.bilibili_publisher import BilibiliPublisher
from app.services.dailymotion_publisher import DailymotionPublisher
from app.services.embed_builder import build_embed
from app.services.instagram_publisher import _download_video
from app.services.snapchat_publisher import SnapchatPublisher

PLATFORMS = ("dailymotion", "snapchat", "bilibili")
_LABELS = {"dailymotion": "Dailymotion", "snapchat": "Snapchat", "bilibili": "Bilibili"}
_CREDENTIAL_LOCKS: dict[str, asyncio.Lock] = {}
logger = logging.getLogger(__name__)


def publisher_for(platform: str, credentials: dict):
    if platform == "dailymotion":
        return DailymotionPublisher(credentials)
    if platform == "snapchat":
        return SnapchatPublisher(credentials)
    if platform == "bilibili":
        return BilibiliPublisher(credentials)
    raise ValueError("Unknown publisher")


def credential_key(platform: str, credentials: dict) -> str:
    # Changing the configured original token/app invalidates the saved rotation.
    identity = {
        k: credentials.get(k)
        for k in (
            "client_id",
            "client_secret",
            "profile_id",
            "mid",
            "openid",
            "refresh_token",
            "access_token",
        )
    }
    return hashlib.sha256((platform + json.dumps(identity, sort_keys=True)).encode()).hexdigest()


def needs_video_download(platform: str, state: dict | None) -> bool:
    """Recovery after final submission must not depend on an expiring Drive URL."""
    current = state or {}
    if platform == "bilibili":
        return not (current.get("resource_id") or current.get("submission_started"))
    if platform == "snapchat":
        return not (
            current.get("stage") == "submitting"
            or (current.get("stage") == "submitted" and current.get("resource_id"))
        )
    if platform == "dailymotion":
        # A known private/public resource can be reconciled using its API ID.
        return not (current.get("resource_id") or current.get("stage") == "creating")
    return True


def _save_credentials(path: Path, credentials: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".oauth-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(credentials, output)
        os.replace(temporary, path)  # mkstemp uses mode 0600
    finally:
        Path(temporary).unlink(missing_ok=True)


def due_at(job, platform: str) -> datetime:
    value = job.platform_scheduled_at.get(platform) or job.slot_time
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if job.publisher_payloads[platform].get("mode") == "manual":
        value -= timedelta(minutes=5)
    return value


def worthwhile(job, platform: str) -> bool:
    if platform not in job.publisher_payloads:
        return False
    status = job.platform_statuses.get(platform, PlatformStatus(status="pending"))
    if status.status in {"uploaded", "failed", "skipped"}:
        return False
    return not (
        job.publisher_payloads[platform].get("mode") == "manual"
        and platform in job.publisher_reminders
    )


async def update_embed(job, settings, discord, *, store=None) -> None:
    if store is not None:
        # Serialize edits separately from publication and refresh the snapshot
        # after waiting, so a stale completion cannot overwrite a newer embed.
        async with store.publisher_embed_lock(job.project_id):
            latest = await store.get(job.project_id)
            if latest is not None:
                await update_embed(latest, settings, discord)
        return
    if job.discord_message_id:
        try:
            await discord.edit_message(
                settings.discord.upload_channel_id,
                job.discord_message_id,
                embed=build_embed(job, settings.accounts, settings.public_base_url),
            )
        except Exception:
            logger.warning("Publisher embed update failed for %s", job.project_id)


async def cleanup_reminder(job, platform: str, store, settings, discord) -> None:
    message_id = job.publisher_reminders.get(platform)
    if message_id:
        try:
            await discord.delete_message(settings.discord.reminder_channel_id, message_id)
        except Exception:
            logger.warning("Publisher reminder cleanup failed for %s/%s", job.project_id, platform)
        await store.update(
            job.project_id,
            publisher_reminders={platform: None}, merge_maps=("publisher_reminders",),
        )


async def acknowledge(job, platform: str, store, settings, discord) -> None:
    async with store.publisher_activity(job.project_id, platform, wait=True):
        latest = await store.get(job.project_id)
        if latest is None or latest.publisher_payloads.get(platform, {}).get("mode") != "manual":
            return
        status = latest.platform_statuses.get(platform)
        if status is None or status.status != "pending":
            return
        latest = await store.merge_platform_status(
            job.project_id,
            platform,
            PlatformStatus(
                status="uploaded", detail="Posté manuellement", completed_at=datetime.now(UTC)
            ),
        )
        await cleanup_reminder(latest, platform, store, settings, discord)
        await update_embed(latest, settings, discord, store=store)


async def dispatch(job, platform: str, store, settings, discord) -> None:
    async with store.publisher_activity(job.project_id, platform) as acquired:
        if not acquired:
            return
        latest = await store.get(job.project_id)
        if latest is None or not worthwhile(latest, platform):
            return
        if due_at(latest, platform) > datetime.now(UTC):
            return  # A queued task may have been rescheduled since the tick.
        payload = latest.publisher_payloads[platform]
        if payload.get("mode") == "manual":
            if datetime.now(UTC) - due_at(latest, platform) > timedelta(hours=6):
                await store.merge_platform_status(
                    job.project_id,
                    platform,
                    PlatformStatus(
                        status="failed",
                        detail="Manual upload reminder missed while server was offline",
                    ),
                )
                return
            title = payload.get("credentials", {}).get("title", latest.anime_title)
            message_id = await discord.post_message(
                settings.discord.reminder_channel_id,
                content=(
                    f"📤 **{_LABELS[platform]} — {title}**\n"
                    f"{payload.get('description', '')[:1200]}\n"
                    f"{payload.get('video_url') or latest.drive_video_url}\n"
                    "Publie la vidéo puis réagis ✅ sur ce rappel pour confirmer cette plateforme."
                ),
            )
            if message_id:
                await store.update(
                    job.project_id,
                    publisher_reminders={platform: str(message_id)},
                    merge_maps=("publisher_reminders",),
                )
            return

        previous = latest.platform_statuses.get(platform, PlatformStatus(status="pending"))
        await store.merge_platform_status(
            job.project_id,
            platform,
            PlatformStatus(status="uploading", attempts=previous.attempts + 1),
        )

        async def on_state(state):
            await store.update(
                job.project_id, publisher_states={platform: state},
                merge_maps=("publisher_states",),
            )

        video_path = None
        try:
            credentials = dict(payload.get("credentials") or {})
            key = credential_key(platform, credentials)
            credential_path = settings.data_dir / "publisher_credentials" / f"{key}.json"
            async with _CREDENTIAL_LOCKS.setdefault(key, asyncio.Lock()):
                if credential_path.exists():
                    credentials.update(json.loads(credential_path.read_text()))

                async def on_credentials(updated):
                    # Preserve content metadata on the job, cache only auth fields.
                    auth = {
                        k: v
                        for k, v in updated.items()
                        if k in {"access_token", "refresh_token", "expires_at", "expires_in"}
                    }
                    _save_credentials(credential_path, auth)

                checkpoint = latest.publisher_states.get(platform)
                if needs_video_download(platform, checkpoint):
                    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
                        video_path = await _download_video(
                            client,
                            payload.get("video_url") or latest.drive_video_url,
                            temp_dir=settings.data_dir / "publisher_tmp",
                        )
                # These adapter recovery branches never read the local source.
                result = await publisher_for(platform, credentials).publish(
                    video_path or Path(),
                    description=payload.get("description", ""),
                    state=checkpoint,
                    on_state=on_state,
                    on_credentials=on_credentials,
                )
            status = PlatformStatus(
                status="uploaded" if result.success else "failed",
                url=result.url,
                detail=result.detail,
                completed_at=datetime.now(UTC) if result.success else None,
                attempts=previous.attempts + 1,
            )
        except Exception as exc:
            # Fail closed: operator reviews/requeues; a crash checkpoint remains
            # available to the adapter to prevent duplicate remote submissions.
            logger.warning(
                "Publisher failed for %s/%s (%s)", job.project_id, platform, type(exc).__name__
            )
            status = PlatformStatus(
                status="failed",
                detail=(
                    f"{_LABELS[platform]} publishing failed ({type(exc).__name__}); "
                    "check credentials and provider status"
                ),
                attempts=previous.attempts + 1,
            )
        finally:
            if video_path is not None:
                video_path.unlink(missing_ok=True)
        updated = await store.merge_platform_status(job.project_id, platform, status)
        await update_embed(updated, settings, discord, store=store)


def submission_locked(platform: str, state: dict) -> bool:
    """True when cancelling could conceal an accepted or indeterminate post."""
    if platform == "bilibili":
        return bool(state.get("resource_id") or state.get("submission_started"))
    return state.get("stage") in {"creating", "submitting", "submitted"}


async def cancel_staged(job, platform: str) -> None:
    state = job.publisher_states.get(platform) or {}
    if platform == "dailymotion" and state.get("resource_id"):
        publisher = publisher_for(platform, job.publisher_payloads[platform]["credentials"])
        await publisher.delete(state["resource_id"])
