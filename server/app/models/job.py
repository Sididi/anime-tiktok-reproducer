"""Data shapes for the platform-agnostic Job. No I/O, no dependencies on services."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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


@dataclass(frozen=True)
class InstagramPublishState:
    """Sanitized resumable Instagram publish state.

    This intentionally excludes access tokens and upload headers. The upload URI
    is persisted so a container created before a crash can be retried.
    """

    container_id: str | None = None
    upload_uri: str | None = None
    stage: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    upload_completed_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_status_code: str | None = None
    last_status_detail: str | None = None
    last_status_payload_summary: dict[str, Any] | None = None
    media_id: str | None = None
    permalink: str | None = None
    upload_method: str | None = None
    fallback_reason: str | None = None
    prepared_media_filename: str | None = None
    prepared_media_token: str | None = None
    prepared_media_size: int | None = None
    prepared_media_expires_at: datetime | None = None
    prepared_media_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "container_id": self.container_id,
            "upload_uri": self.upload_uri,
            "stage": self.stage,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "upload_completed_at": (
                self.upload_completed_at.isoformat() if self.upload_completed_at else None
            ),
            "last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
            "last_status_code": self.last_status_code,
            "last_status_detail": self.last_status_detail,
            "last_status_payload_summary": self.last_status_payload_summary,
            "media_id": self.media_id,
            "permalink": self.permalink,
            "upload_method": self.upload_method,
            "fallback_reason": self.fallback_reason,
            "prepared_media_filename": self.prepared_media_filename,
            "prepared_media_token": self.prepared_media_token,
            "prepared_media_size": self.prepared_media_size,
            "prepared_media_expires_at": (
                self.prepared_media_expires_at.isoformat()
                if self.prepared_media_expires_at
                else None
            ),
            "prepared_media_url": self.prepared_media_url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> InstagramPublishState | None:
        if not isinstance(d, dict):
            return None

        def _dt(key: str) -> datetime | None:
            value = d.get(key)
            if not value:
                return None
            return datetime.fromisoformat(str(value))

        summary = d.get("last_status_payload_summary")
        return cls(
            container_id=d.get("container_id"),
            upload_uri=d.get("upload_uri"),
            stage=d.get("stage"),
            created_at=_dt("created_at"),
            expires_at=_dt("expires_at"),
            upload_completed_at=_dt("upload_completed_at"),
            last_polled_at=_dt("last_polled_at"),
            last_status_code=d.get("last_status_code"),
            last_status_detail=d.get("last_status_detail"),
            last_status_payload_summary=summary if isinstance(summary, dict) else None,
            media_id=d.get("media_id"),
            permalink=d.get("permalink"),
            upload_method=d.get("upload_method"),
            fallback_reason=d.get("fallback_reason"),
            prepared_media_filename=d.get("prepared_media_filename"),
            prepared_media_token=d.get("prepared_media_token"),
            prepared_media_size=(
                int(d["prepared_media_size"])
                if d.get("prepared_media_size") is not None
                else None
            ),
            prepared_media_expires_at=_dt("prepared_media_expires_at"),
            prepared_media_url=d.get("prepared_media_url"),
        )


@dataclass(frozen=True)
class TikTokPublishState:
    """Resumable Post for Me publish state (no secrets: the API key stays in env).

    Once `post_id` is set, retries poll that post's results instead of creating
    a new post — this is the double-post guard.
    """

    post_id: str | None = None
    media_url: str | None = None
    stage: str | None = None  # media_uploaded | post_created | published | failed
    created_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_error: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "media_url": self.media_url,
            "stage": self.stage,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_polled_at": (
                self.last_polled_at.isoformat() if self.last_polled_at else None
            ),
            "last_error": self.last_error,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TikTokPublishState | None:
        if not isinstance(d, dict):
            return None

        def _dt(key: str) -> datetime | None:
            value = d.get(key)
            if not value:
                return None
            return datetime.fromisoformat(str(value))

        return cls(
            post_id=d.get("post_id"),
            media_url=d.get("media_url"),
            stage=d.get("stage"),
            created_at=_dt("created_at"),
            last_polled_at=_dt("last_polled_at"),
            last_error=d.get("last_error"),
            url=d.get("url"),
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
    instagram_publish_state: InstagramPublishState | None = None
    tiktok_payload: dict | None = None
    tiktok_publish_state: TikTokPublishState | None = None
    platform_scheduled_at: dict[str, datetime] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

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
            "platform_scheduled_at": {
                platform: scheduled_at.isoformat()
                for platform, scheduled_at in self.platform_scheduled_at.items()
            },
            "platforms_requested": list(self.platforms_requested),
            "platform_statuses": {
                p: ps.to_dict() for p, ps in self.platform_statuses.items()
            },
            "discord_message_id": self.discord_message_id,
            "reminder_message_id": self.reminder_message_id,
            "reminder_forward_message_id": self.reminder_forward_message_id,
            "reminder_cancelled": self.reminder_cancelled,
            "instagram_payload": self.instagram_payload,
            "instagram_publish_state": (
                self.instagram_publish_state.to_dict()
                if self.instagram_publish_state
                else None
            ),
            "tiktok_payload": self.tiktok_payload,
            "tiktok_publish_state": (
                self.tiktok_publish_state.to_dict()
                if self.tiktok_publish_state
                else None
            ),
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
            platform_scheduled_at={
                platform: datetime.fromisoformat(scheduled_at)
                for platform, scheduled_at in d.get("platform_scheduled_at", {}).items()
            },
            platforms_requested=list(d["platforms_requested"]),
            platform_statuses={
                p: PlatformStatus.from_dict(ps) for p, ps in d["platform_statuses"].items()
            },
            discord_message_id=d.get("discord_message_id"),
            reminder_message_id=d.get("reminder_message_id"),
            reminder_forward_message_id=d.get("reminder_forward_message_id"),
            reminder_cancelled=bool(d.get("reminder_cancelled", False)),
            instagram_payload=d.get("instagram_payload"),
            instagram_publish_state=InstagramPublishState.from_dict(
                d.get("instagram_publish_state")
            ),
            tiktok_payload=d.get("tiktok_payload"),
            tiktok_publish_state=TikTokPublishState.from_dict(
                d.get("tiktok_publish_state")
            ),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )
