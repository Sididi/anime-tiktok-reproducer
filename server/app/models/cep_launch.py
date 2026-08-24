"""Premiere Link launch record: a request for the CEP panel to start a project.

Created by the main backend after a project's Drive export finishes, delivered
to the panel over the /api/cep/ws WebSocket, acknowledged by the panel. No I/O.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

CepLaunchStatus = Literal["pending", "accepted", "duplicate", "error", "expired"]
ACK_RESULTS: tuple[str, ...] = ("accepted", "duplicate", "error")
LAUNCH_TTL = timedelta(days=7)


def new_launch_id() -> str:
    return f"l_{secrets.token_hex(4)}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class CepLaunch:
    project_id: str
    launch_id: str
    anime_title: str
    requested_at: datetime
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    status: CepLaunchStatus = "pending"
    discord_message_id: str | None = None
    discord_content: str | None = None
    delivered_at: datetime | None = None
    delivery_count: int = 0
    acked_at: datetime | None = None
    ack_detail: str | None = None
    panel_build_id: str | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "launch_id": self.launch_id,
            "anime_title": self.anime_title,
            "requested_at": _iso(self.requested_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "expires_at": _iso(self.expires_at),
            "status": self.status,
            "discord_message_id": self.discord_message_id,
            "discord_content": self.discord_content,
            "delivered_at": _iso(self.delivered_at),
            "delivery_count": self.delivery_count,
            "acked_at": _iso(self.acked_at),
            "ack_detail": self.ack_detail,
            "panel_build_id": self.panel_build_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CepLaunch:
        return cls(
            project_id=d["project_id"],
            launch_id=d["launch_id"],
            anime_title=str(d.get("anime_title") or ""),
            requested_at=_parse(d["requested_at"]),  # type: ignore[arg-type]
            created_at=_parse(d["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse(d["updated_at"]),  # type: ignore[arg-type]
            expires_at=_parse(d["expires_at"]),  # type: ignore[arg-type]
            status=d.get("status", "pending"),
            discord_message_id=d.get("discord_message_id"),
            discord_content=d.get("discord_content"),
            delivered_at=_parse(d.get("delivered_at")),
            delivery_count=int(d.get("delivery_count", 0)),
            acked_at=_parse(d.get("acked_at")),
            ack_detail=d.get("ack_detail"),
            panel_build_id=d.get("panel_build_id"),
        )
