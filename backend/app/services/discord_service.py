from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from ..config import settings


@dataclass
class DiscordMessage:
    id: str
    content: str


class DiscordService:
    """Thin wrapper around Discord webhook operations."""

    @classmethod
    def is_configured(cls) -> bool:
        return bool(settings.discord_webhook_url)

    @classmethod
    def _messages_url(cls, message_id: str) -> str:
        webhook_url = settings.discord_webhook_url
        if webhook_url is None:
            raise RuntimeError("Discord webhook is not configured")
        return f"{webhook_url}/messages/{message_id}"

    @classmethod
    def post_message(cls, content: str) -> DiscordMessage | None:
        if not cls.is_configured():
            return None
        webhook_url = settings.discord_webhook_url
        if webhook_url is None:
            raise RuntimeError("Discord webhook URL unexpectedly None")
        response = requests.post(
            webhook_url,
            params={"wait": "true"},
            json={"content": content},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return DiscordMessage(id=str(payload["id"]), content=payload.get("content", content))

    @classmethod
    def delete_message(cls, message_id: str) -> bool:
        if not cls.is_configured() or not message_id:
            return False
        response = requests.delete(cls._messages_url(message_id), timeout=20)
        if response.status_code in (200, 204, 404):
            return True
        response.raise_for_status()
        return True

    @classmethod
    def get_message(cls, message_id: str) -> DiscordMessage | None:
        if not cls.is_configured() or not message_id:
            return None
        response = requests.get(cls._messages_url(message_id), timeout=20)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return DiscordMessage(id=str(payload["id"]), content=payload.get("content", ""))

    @classmethod
    def edit_message(cls, message_id: str, content: str) -> DiscordMessage | None:
        if not cls.is_configured() or not message_id:
            return None
        response = requests.patch(
            cls._messages_url(message_id),
            json={"content": content},
            timeout=20,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return DiscordMessage(id=str(payload["id"]), content=payload.get("content", content))
