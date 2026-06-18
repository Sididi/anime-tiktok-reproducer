"""Tests for /api/internal/* endpoints."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.models.job import Job, PlatformStatus


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    discord = AsyncMock()
    discord.post_message.return_value = "msg_1"
    app.state.discord = discord
    return app, discord


JOB_PAYLOAD = {
    "project_id": "p1",
    "account_id": "anime_fr",
    "slot_time": "2026-04-26T21:00:00+00:00",
    "anime_title": "One Piece 1063",
    "description": "Posted today",
    "drive_video_url": "https://drive.google.com/uc?id=xyz",
    "platforms_requested": ["youtube", "tiktok"],
}
INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}


def test_create_job_posts_only_embed_not_reminder(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """Reminder is now deferred to the background scheduler; create_job
    should post ONLY the embed, never the reminder."""
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["discord_message_id"] == "msg_embed"
    # ONLY the embed in the upload channel. The reminder is fired later by
    # the scheduler when slot_time arrives.
    assert discord.post_message.call_count == 1


def test_create_job_idempotent(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"
    with TestClient(app) as client:
        r1 = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r2 = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
    assert r1.json()["discord_message_id"] == "msg_embed"
    assert r2.json()["discord_message_id"] == "msg_embed"  # same, no re-post
    assert discord.post_message.call_count == 1


def test_platform_status_edits_embed(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.post(
            "/api/internal/jobs/p1/platform-status",
            json={"platform": "youtube", "status": "uploaded", "url": "https://youtu.be/x"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    discord.edit_message.assert_called()


def test_delete_job_removes_messages(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """create_job posts only the embed; delete_job should remove just it.

    Reminder + reminder-forward messages are deleted iff they exist; for a job
    where the scheduler hasn't run yet, only the embed exists."""
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
    assert r.status_code == 204
    # Only the embed message exists pre-scheduler.
    assert discord.delete_message.call_count == 1


def test_delete_job_removes_reminder_and_forward_when_present(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """If the scheduler has already fired the reminder + forward, delete_job
    must remove all three messages (embed, reminder, forward)."""
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    # Pre-populate the store with a job that already has all three msg ids.
    now = datetime(2026, 4, 27, 21, 0, tzinfo=UTC)
    job = Job(
        project_id="p1",
        job_id="j_x",
        account_id="anime_fr",
        device_id="iphone_13_pro",
        anime_title="X",
        description="d",
        drive_video_url="u",
        slot_time=now,
        platforms_requested=["tiktok"],
        platform_statuses={"tiktok": PlatformStatus(status="pending")},
        discord_message_id="msg_embed",
        reminder_message_id="msg_rich",
        reminder_forward_message_id="msg_forward",
        created_at=now,
        updated_at=now,
    )

    asyncio.run(app.state.job_store.create(job))

    with TestClient(app) as client:
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
    assert r.status_code == 204
    # embed + reminder + forward
    assert discord.delete_message.call_count == 3


def test_delete_missing_returns_404(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.delete("/api/internal/jobs/never", headers=INTERNAL_AUTH)
    assert r.status_code == 404


def test_generic_message_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_generic"
    with TestClient(app) as client:
        r = client.post(
            "/api/internal/discord/messages",
            json={"content": "hello"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    assert r.json()["message_id"] == "msg_generic"
    discord.post_message.assert_called_once()


def test_generic_message_edit(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.patch(
            "/api/internal/discord/messages/m_42",
            json={"content": "updated"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    discord.edit_message.assert_called_once()


def test_generic_message_delete(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.delete("/api/internal/discord/messages/m_42", headers=INTERNAL_AUTH)
    assert r.status_code == 200
    discord.delete_message.assert_called_once()


def test_unauthenticated_rejected(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD)
    assert r.status_code == 401


def test_platform_status_rejects_invalid_status(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.post(
            "/api/internal/jobs/p1/platform-status",
            json={"platform": "youtube", "status": "bogus"},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 422  # Pydantic validation error


def test_create_job_persists_instagram_payload(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    payload = {
        **JOB_PAYLOAD,
        "platforms_requested": ["youtube", "instagram", "tiktok"],
        "instagram": {
            "ig_user_id": "ig_user_42",
            "ig_access_token": "ig_token_secret",
            "caption": "Hello from IG",
            "prepared_video_url": "https://drive.usercontent.google.com/download?id=ig_prepared",
            "graph_api_version": "v25.0",
        },
        "platform_statuses": {
            "instagram": {
                "status": "failed",
                "detail": "Instagram video preparation failed",
            }
        },
    }
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
    assert r.status_code == 200

    # Verify the IG payload is persisted on the job.
    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.instagram_payload == {
        "ig_user_id": "ig_user_42",
        "ig_access_token": "ig_token_secret",
        "caption": "Hello from IG",
        "prepared_video_url": "https://drive.usercontent.google.com/download?id=ig_prepared",
        "graph_api_version": "v25.0",
    }
    assert job.platform_statuses["instagram"].status == "failed"
    assert job.platform_statuses["instagram"].detail == "Instagram video preparation failed"


def test_create_existing_job_updates_changed_payload_and_clears_instagram_state(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import InstagramPublishState  # noqa: PLC0415

    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    payload = {
        **JOB_PAYLOAD,
        "platforms_requested": ["instagram"],
        "instagram": {
            "ig_user_id": "ig_user_42",
            "ig_access_token": "ig_token_secret",
            "caption": "Hello from IG",
            "prepared_video_url": "https://drive.usercontent.google.com/download?id=old",
        },
    }
    with TestClient(app) as client:
        first = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
        assert first.status_code == 200

        asyncio.run(
            app.state.job_store.set_instagram_publish_state(
                "p1",
                InstagramPublishState(container_id="stale", stage="uploaded"),
            )
        )

        changed = {
            **payload,
            "instagram": {
                **payload["instagram"],
                "prepared_video_url": "https://drive.usercontent.google.com/download?id=new",
            },
        }
        second = client.post("/api/internal/jobs", json=changed, headers=INTERNAL_AUTH)
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]

    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.instagram_payload["prepared_video_url"].endswith("id=new")
    assert job.instagram_publish_state is None
    assert job.platform_statuses["instagram"].status == "pending"
    discord.edit_message.assert_called()


def test_create_job_persists_platform_scheduled_at(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    payload = {
        **JOB_PAYLOAD,
        "platforms_requested": ["instagram", "tiktok"],
        "platform_scheduled_at": {
            "instagram": "2026-04-26T06:01:00+00:00",
            "tiktok": "2026-04-26T21:00:00+00:00",
        },
        "instagram": {
            "ig_user_id": "ig_user_42",
            "ig_access_token": "ig_token_secret",
            "caption": "Hello from IG",
        },
    }
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
    assert r.status_code == 200

    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.platform_scheduled_at["instagram"].isoformat() == "2026-04-26T06:01:00+00:00"
    assert job.platform_scheduled_at["tiktok"].isoformat() == "2026-04-26T21:00:00+00:00"


def test_create_job_without_instagram_field_persists_none(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """Backwards-compatibility: omitting `instagram` from the payload is fine."""
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
    assert r.status_code == 200

    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.instagram_payload is None
