"""Tests for app.models.job."""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.job import InstagramPublishState, Job, PlatformStatus, TikTokPublishState


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


def test_platform_status_retry_not_before_round_trip():
    ps = PlatformStatus(
        status="pending",
        attempts=5,
        retry_not_before=datetime(2026, 7, 21, 15, 30, tzinfo=UTC),
    )
    assert PlatformStatus.from_dict(ps.to_dict()) == ps


def test_platform_status_from_dict_without_retry_not_before():
    ps = PlatformStatus.from_dict({"status": "pending"})
    assert ps.retry_not_before is None


def test_instagram_publish_state_round_trip_with_fallback_fields():
    state = InstagramPublishState(
        container_id="container_video_url",
        stage="uploaded",
        created_at=datetime(2026, 4, 26, 21, 0, tzinfo=UTC),
        upload_completed_at=datetime(2026, 4, 26, 21, 1, tzinfo=UTC),
        upload_method="video_url",
        fallback_reason="rupload zero-byte ingest",
        prepared_media_filename="proj-token.mp4",
        prepared_media_token="token",
        prepared_media_size=12345,
        prepared_media_expires_at=datetime(2026, 4, 27, 21, 0, tzinfo=UTC),
        prepared_media_url="https://tiktok.sididi.tv/api/instagram/prepared/proj/token.mp4",
    )

    restored = InstagramPublishState.from_dict(state.to_dict())

    assert restored == state


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


def _job_dict_minimal() -> dict:
    return {
        "project_id": "p1",
        "job_id": "j_1",
        "account_id": "anime_fr",
        "device_id": "iphone_16",
        "anime_title": "Title",
        "description": "desc",
        "drive_video_url": "https://drive/x",
        "slot_time": "2026-07-04T12:00:00+00:00",
        "platforms_requested": ["tiktok"],
        "platform_statuses": {},
        "discord_message_id": None,
        "reminder_message_id": None,
        "created_at": "2026-07-04T10:00:00+00:00",
        "updated_at": "2026-07-04T10:00:00+00:00",
    }


def test_tiktok_publish_state_round_trip():
    state = TikTokPublishState(
        post_id="post_123",
        media_url="https://media.postforme.dev/abc.mp4",
        stage="post_created",
        created_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        last_polled_at=datetime(2026, 7, 4, 12, 5, tzinfo=UTC),
        last_error=None,
        url=None,
    )
    restored = TikTokPublishState.from_dict(state.to_dict())
    assert restored == state


def test_tiktok_publish_state_from_dict_none():
    assert TikTokPublishState.from_dict(None) is None


def test_job_round_trips_tiktok_fields():
    d = _job_dict_minimal()
    d["tiktok_payload"] = {"social_account_id": "spc_1", "caption": "hi"}
    d["tiktok_publish_state"] = {"post_id": "post_1", "stage": "published"}
    job = Job.from_dict(d)
    assert job.tiktok_payload == {"social_account_id": "spc_1", "caption": "hi"}
    assert job.tiktok_publish_state.post_id == "post_1"
    out = job.to_dict()
    assert out["tiktok_payload"] == d["tiktok_payload"]
    assert out["tiktok_publish_state"]["post_id"] == "post_1"


def test_job_defaults_tiktok_fields_absent():
    job = Job.from_dict(_job_dict_minimal())
    assert job.tiktok_payload is None
    assert job.tiktok_publish_state is None


def test_tiktok_publish_state_media_attempts_round_trip():
    state = TikTokPublishState(media_attempts=3, stage="post_scheduled")
    d = state.to_dict()
    assert d["media_attempts"] == 3
    restored = TikTokPublishState.from_dict(d)
    assert restored.media_attempts == 3
    assert restored.stage == "post_scheduled"


def test_tiktok_publish_state_media_attempts_defaults_to_zero_for_legacy_dicts():
    legacy = {"post_id": "sp_1", "stage": "post_created"}
    restored = TikTokPublishState.from_dict(legacy)
    assert restored.media_attempts == 0
