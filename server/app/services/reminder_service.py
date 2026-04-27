"""Posts the cross-channel reminder.

Two-message design:
1. A rich embed message — role ping in `content`, embed showing
   account/avatar / anime title / device / Paris-time slot / TikTok description.
2. A separate "forward" message that quotes the original upload-channel embed
   via `message_reference: {type: 1, ...}`. If native forward is rejected
   (older guild, missing perms), falls back to a plain message-URL paste.

Returns `(rich_message_id, forward_message_id)`. Either may be None on failure.
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import AccountConfig
from app.models.job import TikTokJob
from app.services.embed_builder import format_french_datetime

logger = logging.getLogger(__name__)


def _ping_text(*, role_id: str, anime_title: str, account_name: str) -> str:
    return (
        f"<@&{role_id}> Time to post **{anime_title}** "
        f"on **{account_name}** — open the mobile app to share."
    )


def build_reminder_embed(
    job: TikTokJob,
    account: AccountConfig,
    public_base_url: str,
    *,
    display_tz: str = "Europe/Paris",
) -> dict[str, Any]:
    """Pure function: build the reminder embed dict."""
    avatar_url = f"{public_base_url.rstrip('/')}/api/avatars/{account.avatar}"
    when = format_french_datetime(job.slot_time, tz=display_tz)
    safe_desc = job.description.replace("```", "ʼʼʼ")
    return {
        "author": {"name": account.name, "icon_url": avatar_url},
        "title": job.anime_title,
        "description": f"⏰ **{when}**",
        "fields": [
            {"name": "📱 Device", "value": job.device_id, "inline": True},
            {"name": "📺 Compte", "value": account.name, "inline": True},
            {
                "name": "Description TikTok",
                "value": f"```\n{safe_desc}\n```",
                "inline": False,
            },
        ],
    }


async def post_reminder(
    discord,
    *,
    job: TikTokJob,
    account: AccountConfig,
    public_base_url: str,
    upload_channel_id: str,
    reminder_channel_id: str,
    role_id: str,
    guild_id: str,
) -> tuple[str | None, str | None]:
    """Post the reminder. Returns (rich_message_id, forward_message_id).

    The rich message carries the role ping + the embed; the forward
    message references the original upload-channel embed (or pastes its URL
    on fallback). Either id may be None if its post failed.
    """
    rich_id = await _post_rich(
        discord,
        channel_id=reminder_channel_id,
        content=_ping_text(
            role_id=role_id,
            anime_title=job.anime_title,
            account_name=account.name,
        ),
        embed=build_reminder_embed(job, account, public_base_url),
    )

    if not job.discord_message_id:
        # Nothing to forward.
        return rich_id, None

    forward_ref = {
        "type": 1,
        "channel_id": upload_channel_id,
        "message_id": job.discord_message_id,
    }
    forward_id: str | None = None
    try:
        forward_id = await discord.post_message(
            reminder_channel_id,
            message_reference=forward_ref,
        )
    except Exception as e:
        logger.warning("Native forward failed (%s); falling back to URL paste", e)
        url = (
            f"https://discord.com/channels/{guild_id}/"
            f"{upload_channel_id}/{job.discord_message_id}"
        )
        try:
            forward_id = await discord.post_message(reminder_channel_id, content=url)
        except Exception as e2:
            logger.warning("URL fallback also failed: %s", e2)

    return rich_id, forward_id


async def _post_rich(
    discord,
    *,
    channel_id: str,
    content: str,
    embed: dict[str, Any],
) -> str | None:
    try:
        return await discord.post_message(channel_id, content=content, embed=embed)
    except Exception as e:
        logger.warning("Reminder rich-message post failed: %s", e)
        return None
