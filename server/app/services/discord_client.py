"""REST-only Discord client using httpx with bot-token auth."""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://discord.com/api/v10"


class DiscordClient:
    def __init__(self, *, bot_token: str, max_retries: int = 3) -> None:
        self._token = bot_token
        self._max_retries = max_retries
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DiscordClient":
        self._client = httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bot {self._token}"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> httpx.Response:
        assert self._client is not None, "use within `async with` block"
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(method, path, json=json_body)
            except httpx.RequestError as e:
                last_exc = e
                if attempt >= self._max_retries:
                    raise
                await asyncio.sleep(min(2**attempt, 5))
                continue
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 1.0))
                logger.warning("Discord rate-limited; sleeping %.2fs", retry_after)
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code >= 500 and attempt < self._max_retries:
                await asyncio.sleep(min(2**attempt, 5))
                continue
            resp.raise_for_status()
            return resp
        raise last_exc or RuntimeError("retries exhausted")

    async def post_message(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embed: dict | None = None,
        message_reference: dict | None = None,
    ) -> str:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        if message_reference is not None:
            body["message_reference"] = message_reference
        resp = await self._request("POST", f"/channels/{channel_id}/messages", json_body=body)
        return resp.json()["id"]

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        content: str | None = None,
        embed: dict | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if content is not None:
            body["content"] = content
        if embed is not None:
            body["embeds"] = [embed]
        await self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}", json_body=body
        )

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        encoded = urllib.parse.quote(emoji, safe="")
        await self._request(
            "PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me"
        )
