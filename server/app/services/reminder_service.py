"""Posts the cross-channel reminder, with Q1 native forward + Q2 URL fallback."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _ping_text(*, role_id: str, anime_title: str, account_name: str, device_name: str) -> str:
    return (
        f"<@&{role_id}> Time to post **{anime_title}** "
        f"on **{account_name}** ({device_name})"
    )


async def post_reminder(
    discord,
    *,
    upload_channel_id: str,
    reminder_channel_id: str,
    embed_message_id: str,
    anime_title: str,
    account_name: str,
    device_name: str,
    role_id: str,
    guild_id: str,
) -> str:
    """Post the reminder. Returns the reminder message id."""
    base_content = _ping_text(
        role_id=role_id,
        anime_title=anime_title,
        account_name=account_name,
        device_name=device_name,
    )
    forward_ref = {
        "type": 1,
        "channel_id": upload_channel_id,
        "message_id": embed_message_id,
    }
    try:
        return await discord.post_message(
            reminder_channel_id,
            content=base_content,
            message_reference=forward_ref,
        )
    except Exception as e:
        logger.warning("Native forward failed (%s); falling back to URL paste", e)
        url = f"https://discord.com/channels/{guild_id}/{upload_channel_id}/{embed_message_id}"
        return await discord.post_message(
            reminder_channel_id,
            content=f"{base_content}\n{url}",
        )
