"""Data shapes for TikTok jobs. No I/O, no dependencies on services."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

PlatformStatusName = Literal["pending", "uploading", "uploaded", "skipped", "failed"]
JobStatus = Literal["pending", "acked"]


@dataclass(frozen=True)
class PlatformStatus:
    status: PlatformStatusName
    url: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "url": self.url, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlatformStatus:
        return cls(status=d["status"], url=d.get("url"), detail=d.get("detail"))


@dataclass
class TikTokJob:
    project_id: str
    job_id: str
    account_id: str
    device_id: str
    anime_title: str
    description: str
    drive_video_url: str
    slot_time: datetime
    platforms_requested: list[str]
    status: JobStatus
    platform_statuses: dict[str, PlatformStatus]
    discord_message_id: str | None
    reminder_message_id: str | None
    acked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    reminder_forward_message_id: str | None = None

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
            "status": self.status,
            "platform_statuses": {
                p: ps.to_dict() for p, ps in self.platform_statuses.items()
            },
            "discord_message_id": self.discord_message_id,
            "reminder_message_id": self.reminder_message_id,
            "reminder_forward_message_id": self.reminder_forward_message_id,
            "acked_at": self.acked_at.isoformat() if self.acked_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TikTokJob:
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
            status=d["status"],
            platform_statuses={
                p: PlatformStatus.from_dict(ps) for p, ps in d["platform_statuses"].items()
            },
            discord_message_id=d.get("discord_message_id"),
            reminder_message_id=d.get("reminder_message_id"),
            reminder_forward_message_id=d.get("reminder_forward_message_id"),
            acked_at=datetime.fromisoformat(d["acked_at"]) if d.get("acked_at") else None,
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )
