"""Manual-TikTok reminder: ONE message in the reminder (schedule-alerts) channel.

Posted by the scheduler at ``tiktok scheduled_at - TIKTOK_MANUAL_REMINDER_LEAD_MINUTES``
for jobs flagged ``tiktok_manual`` (account has TikTok slots but no Post for
Me id). The message carries the role ping, a jump link to the original
upload-channel embed, and a compact embed with everything needed to post from
the phone. A ✅ reaction on either message (see reaction_listener) marks the
TikTok row as posted and deletes the reminder.

History: the 2026-04 version posted two messages (rich embed + native forward)
and was deleted in 46153e4 when Post for Me replaced manual posting. This is
the 2026-08 single-message revival for manual-mode accounts.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

from app.config import AccountConfig, Settings
from app.models.job import Job
from app.services.embed_builder import format_french_datetime
from app.services.job_store import JobStore

logger = logging.getLogger(__name__)


def tiktok_reminder_time(job: Job):
    """The TikTok publish instant shown in the reminder (per-platform time first)."""
    return job.platform_scheduled_at.get("tiktok") or job.slot_time


def jump_url(settings: Settings, job: Job) -> str | None:
    """Discord deep link to the original upload-channel embed, if it exists."""
    if not job.discord_message_id:
        return None
    return (
        f"https://discord.com/channels/{settings.discord.guild_id}/"
        f"{settings.discord.upload_channel_id}/{job.discord_message_id}"
    )


def build_reminder_content(settings: Settings, job: Job, account: AccountConfig) -> str:
    link = jump_url(settings, job)
    first = (
        f"<@&{settings.discord.reminder_role_id}> ⏰ TikTok à poster dans 5 min : "
        f"**{job.anime_title}** sur **{account.name}**"
    )
    if link:
        first += f" → {link}"
    return first + "\nRéagis ✅ ici ou sur le message d'origine une fois posté."


def build_reminder_embed(
    job: Job, account: AccountConfig, public_base_url: str
) -> dict[str, Any]:
    """Pure function: build the reminder embed dict."""
    avatar_url = f"{public_base_url.rstrip('/')}/api/avatars/{account.avatar}"
    fields: list[dict[str, Any]] = [
        {"name": "📺 Compte", "value": account.name, "inline": True},
    ]
    if job.device_id:
        fields.append({"name": "📱 Device", "value": job.device_id, "inline": True})
    fields.extend([
        {
            "name": "Description TikTok",
            # Plain text on purpose (same rationale as embed_builder): Discord
            # mobile "copy" returns raw source, so backticks/escapes would end
            # up pasted into TikTok.
            "value": job.description,
            "inline": False,
        },
        {"name": "Lien vidéo", "value": job.drive_video_url, "inline": False},
    ])
    return {
        "author": {"name": account.name, "icon_url": avatar_url},
        "title": job.anime_title,
        "description": f"⏰ **{format_french_datetime(tiktok_reminder_time(job))}**",
        "fields": fields,
    }


async def post_reminder(
    discord, *, job: Job, account: AccountConfig, settings: Settings
) -> str | None:
    """Post the single reminder message. Returns its id, or None on failure."""
    try:
        return await discord.post_message(
            settings.discord.reminder_channel_id,
            content=build_reminder_content(settings, job, account),
            embed=build_reminder_embed(job, account, settings.public_base_url),
        )
    except Exception as e:
        logger.warning("Reminder post failed for %s: %s", job.project_id, e)
        return None


async def cleanup_reminder(discord, store: JobStore, settings: Settings, job: Job) -> bool:
    """Delete the reminder message(s) for `job` and clear their ids in the store.

    Also removes the legacy forward message if an old job still carries one.
    Returns True when at least one message was deleted."""
    deleted_anything = False
    for message_id, label in (
        (job.reminder_message_id, "reminder"),
        (job.reminder_forward_message_id, "reminder forward"),
    ):
        if not message_id:
            continue
        try:
            await discord.delete_message(settings.discord.reminder_channel_id, message_id)
            deleted_anything = True
        except Exception:
            logger.warning(
                "Failed to delete %s message %s for %s",
                label, message_id, job.project_id, exc_info=True,
            )
    if job.reminder_message_id or job.reminder_forward_message_id:
        # KeyError = job deleted concurrently; nothing left to clear.
        with contextlib.suppress(KeyError):
            await store.update(
                job.project_id,
                reminder_message_id=None,
                reminder_forward_message_id=None,
            )
    return deleted_anything
