"""Tests for app.services.discord_client.DiscordClient using respx."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.services.discord_client import DiscordClient


@respx.mock
async def test_post_message_sends_bot_auth_and_returns_id():
    route = respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg_42"})
    )
    async with DiscordClient(bot_token="abc") as client:
        msg_id = await client.post_message("c1", content="hello")
    assert msg_id == "msg_42"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bot abc"
    assert b'"content":"hello"' in sent.content


@respx.mock
async def test_post_message_with_embed_and_message_reference():
    route = respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(200, json={"id": "msg_43"})
    )
    async with DiscordClient(bot_token="abc") as client:
        await client.post_message(
            "c1",
            embed={"title": "T", "fields": []},
            message_reference={"type": 1, "channel_id": "c0", "message_id": "m0"},
        )
    assert route.called
    sent_body = route.calls.last.request.content
    assert b'"embeds":[' in sent_body
    assert b'"message_reference":{' in sent_body


@respx.mock
async def test_edit_message():
    route = respx.patch("https://discord.com/api/v10/channels/c1/messages/m1").mock(
        return_value=httpx.Response(200, json={"id": "m1"})
    )
    async with DiscordClient(bot_token="abc") as client:
        await client.edit_message("c1", "m1", embed={"title": "x"})
    assert route.called


@respx.mock
async def test_delete_message():
    route = respx.delete("https://discord.com/api/v10/channels/c1/messages/m1").mock(
        return_value=httpx.Response(204)
    )
    async with DiscordClient(bot_token="abc") as client:
        await client.delete_message("c1", "m1")
    assert route.called


@respx.mock
async def test_add_reaction_url_encodes_emoji():
    route = respx.put(
        "https://discord.com/api/v10/channels/c1/messages/m1/reactions/%E2%9C%85/@me"
    ).mock(return_value=httpx.Response(204))
    async with DiscordClient(bot_token="abc") as client:
        await client.add_reaction("c1", "m1", "✅")
    assert route.called


@respx.mock
async def test_retries_on_429_with_retry_after():
    """When Discord returns 429, the client should sleep and retry."""
    responses = [
        httpx.Response(429, json={"retry_after": 0.01}),
        httpx.Response(200, json={"id": "msg_99"}),
    ]
    route = respx.post(
        "https://discord.com/api/v10/channels/c1/messages"
    ).mock(side_effect=responses)
    async with DiscordClient(bot_token="abc") as client:
        msg_id = await client.post_message("c1", content="hi")
    assert msg_id == "msg_99"
    assert route.call_count == 2


@respx.mock
async def test_5xx_propagates_after_retry_budget_exhausted():
    respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with DiscordClient(bot_token="abc", max_retries=2) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_message("c1", content="hi")
