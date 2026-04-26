"""Tests for app.services.reminder_service."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.reminder_service import post_reminder


async def test_forward_path_uses_message_reference_and_role_ping():
    """Q1: native forward succeeds first try."""
    discord = AsyncMock()
    discord.post_message.return_value = "rem_42"

    msg_id = await post_reminder(
        discord,
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        embed_message_id="m_embed",
        anime_title="One Piece 1063",
        account_name="Anime FR",
        device_name="iphone_13_pro",
        role_id="r_99",
        guild_id="g_1",
    )

    assert msg_id == "rem_42"
    assert discord.post_message.call_count == 1
    args = discord.post_message.call_args
    assert args.kwargs["message_reference"] == {
        "type": 1,
        "channel_id": "c_upload",
        "message_id": "m_embed",
    }
    assert "<@&r_99>" in args.kwargs["content"]
    assert "Anime FR" in args.kwargs["content"]


async def test_falls_back_to_url_when_forward_fails():
    """Q2 fallback: any error on forward -> retry without message_reference."""

    async def post_side_effect(*args, **kwargs):
        if kwargs.get("message_reference"):
            raise httpx.HTTPStatusError(
                "forbidden", request=httpx.Request("POST", "u"),
                response=httpx.Response(403)
            )
        return "rem_43"

    discord = AsyncMock()
    discord.post_message.side_effect = post_side_effect

    msg_id = await post_reminder(
        discord,
        upload_channel_id="c_upload",
        reminder_channel_id="c_reminder",
        embed_message_id="m_embed",
        anime_title="One Piece 1063",
        account_name="Anime FR",
        device_name="iphone_13_pro",
        role_id="r_99",
        guild_id="g_1",
    )

    assert msg_id == "rem_43"
    assert discord.post_message.call_count == 2
    fallback_call = discord.post_message.call_args
    assert fallback_call.kwargs.get("message_reference") is None
    fallback_url = (
        "https://discord.com/channels/g_1/c_upload/m_embed"
    )
    assert fallback_url in fallback_call.kwargs["content"]
