"""Tests for app.models.job."""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.job import Job, PlatformStatus


def _make_job(**overrides) -> Job:
    defaults = dict(
        project_id="proj_1",
        job_id="j_abc",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece Episode 1063",
        description="Description text",
        drive_video_url="https://drive.google.com/uc?export=download&id=xyz",
        slot_time=datetime(2026, 4, 26, 21, 0, tzinfo=UTC),
        platforms_requested=["youtube", "facebook", "instagram", "tiktok"],
        platform_statuses={
            "tiktok": PlatformStatus(status="pending"),
        },
        discord_message_id=None,
        reminder_message_id=None,
        created_at=datetime(2026, 4, 26, 21, 0, 1, tzinfo=UTC),
        updated_at=datetime(2026, 4, 26, 21, 0, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_job_round_trips_through_dict():
    job = _make_job(
        platform_scheduled_at={
            "instagram": datetime(2026, 4, 26, 6, 0, tzinfo=UTC),
            "tiktok": datetime(2026, 4, 26, 21, 0, tzinfo=UTC),
        }
    )
    d = job.to_dict()
    assert d["project_id"] == "proj_1"
    assert d["slot_time"] == "2026-04-26T21:00:00+00:00"
    assert d["platform_scheduled_at"]["instagram"] == "2026-04-26T06:00:00+00:00"
    assert d["platform_statuses"]["tiktok"]["status"] == "pending"

    restored = Job.from_dict(d)
    assert restored == job


def test_job_round_trip_without_platform_scheduled_at():
    job = _make_job()
    d = job.to_dict()
    d.pop("platform_scheduled_at")

    restored = Job.from_dict(d)

    assert restored.platform_scheduled_at == {}


def test_platform_status_round_trip():
    ps = PlatformStatus(status="uploaded", url="https://youtu.be/abc", detail=None)
    assert PlatformStatus.from_dict(ps.to_dict()) == ps


def test_job_with_uploaded_platform_status():
    """The 'acked' semantic is now expressed via platform_statuses completed_at."""
    completed = datetime(2026, 4, 26, 21, 5, tzinfo=UTC)
    job = _make_job(
        platform_statuses={
            "tiktok": PlatformStatus(status="uploaded", completed_at=completed),
        },
    )
    d = job.to_dict()
    assert d["platform_statuses"]["tiktok"]["status"] == "uploaded"
    assert d["platform_statuses"]["tiktok"]["completed_at"] == "2026-04-26T21:05:00+00:00"
    restored = Job.from_dict(d)
    assert restored == job
    assert restored.platform_statuses["tiktok"].completed_at == completed
