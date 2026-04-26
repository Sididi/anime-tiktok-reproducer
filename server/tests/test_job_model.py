"""Tests for app.models.job."""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.job import PlatformStatus, TikTokJob


def _make_job(**overrides) -> TikTokJob:
    defaults = dict(
        project_id="proj_1",
        job_id="j_abc",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="One Piece Episode 1063",
        description="Description text",
        drive_video_url="https://drive.google.com/uc?export=download&id=xyz",
        slot_time=datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc),
        platforms_requested=["youtube", "facebook", "instagram", "tiktok"],
        status="pending",
        platform_statuses={
            "tiktok": PlatformStatus(status="pending"),
        },
        discord_message_id=None,
        reminder_message_id=None,
        acked_at=None,
        created_at=datetime(2026, 4, 26, 21, 0, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 4, 26, 21, 0, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return TikTokJob(**defaults)


def test_job_round_trips_through_dict():
    job = _make_job()
    d = job.to_dict()
    assert d["project_id"] == "proj_1"
    assert d["status"] == "pending"
    assert d["slot_time"] == "2026-04-26T21:00:00+00:00"
    assert d["platform_statuses"]["tiktok"]["status"] == "pending"

    restored = TikTokJob.from_dict(d)
    assert restored == job


def test_platform_status_round_trip():
    ps = PlatformStatus(status="uploaded", url="https://youtu.be/abc", detail=None)
    assert PlatformStatus.from_dict(ps.to_dict()) == ps


def test_job_with_acked_state():
    job = _make_job(
        status="acked",
        acked_at=datetime(2026, 4, 26, 21, 5, tzinfo=timezone.utc),
    )
    d = job.to_dict()
    assert d["status"] == "acked"
    assert d["acked_at"] == "2026-04-26T21:05:00+00:00"
    assert TikTokJob.from_dict(d) == job
