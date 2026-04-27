"""Data shapes for the platform-agnostic Job. No I/O, no dependencies on services."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

PlatformStatusName = Literal["pending", "uploading", "uploaded", "skipped", "failed"]


@dataclass(frozen=True)
class PlatformStatus:
    status: PlatformStatusName
    url: str | None = None
    detail: str | None = None
    completed_at: datetime | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "url": self.url,
            "detail": self.detail,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlatformStatus:
        ca = d.get("completed_at")
        return cls(
            status=d["status"],
            url=d.get("url"),
            detail=d.get("detail"),
            completed_at=datetime.fromisoformat(ca) if ca else None,
            attempts=int(d.get("attempts", 0)),
        )


@dataclass
class Job:
    project_id: str
    job_id: str
    account_id: str
    device_id: str
    anime_title: str
    description: str
    drive_video_url: str
    slot_time: datetime
    platforms_requested: list[str]
    platform_statuses: dict[str, PlatformStatus]
    discord_message_id: str | None
    reminder_message_id: str | None
    reminder_forward_message_id: str | None = None
    reminder_cancelled: bool = False
    instagram_payload: dict | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "job_id": self.job_id,
            "account_id": self.account_id,
            "device_id": self.device_id,
            "anime_title": self.anime_title,
            "description": self.description,
            "drive_video_url": self.drive_video_url,
            "slot_time": self.slot_time.isoformat(),
            "platforms_requested": list(self.platforms_requested),
            "platform_statuses": {
                p: ps.to_dict() for p, ps in self.platform_statuses.items()
            },
            "discord_message_id": self.discord_message_id,
            "reminder_message_id": self.reminder_message_id,
            "reminder_forward_message_id": self.reminder_forward_message_id,
            "reminder_cancelled": self.reminder_cancelled,
            "instagram_payload": self.instagram_payload,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        return cls(
            project_id=d["project_id"],
            job_id=d["job_id"],
            account_id=d["account_id"],
            device_id=d["device_id"],
            anime_title=d["anime_title"],
            description=d["description"],
            drive_video_url=d["drive_video_url"],
            slot_time=datetime.fromisoformat(d["slot_time"]),
            platforms_requested=list(d["platforms_requested"]),
            platform_statuses={
                p: PlatformStatus.from_dict(ps) for p, ps in d["platform_statuses"].items()
            },
            discord_message_id=d.get("discord_message_id"),
            reminder_message_id=d.get("reminder_message_id"),
            reminder_forward_message_id=d.get("reminder_forward_message_id"),
            reminder_cancelled=bool(d.get("reminder_cancelled", False)),
            instagram_payload=d.get("instagram_payload"),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )
