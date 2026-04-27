"""Tests for /api/internal/* endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

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
    assert r.status_code == 200
    # Only the embed message exists pre-scheduler.
    assert discord.delete_message.call_count == 1


def test_delete_job_removes_reminder_and_forward_when_present(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    """If the scheduler has already fired the reminder + forward, delete_job
    must remove all three messages (embed, reminder, forward)."""
    from app.models.job import PlatformStatus, Job
    from datetime import datetime, timezone as _tz

    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    # Pre-populate the store with a job that already has all three msg ids.
    now = datetime(2026, 4, 27, 21, 0, tzinfo=_tz.utc)
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

    import asyncio
    asyncio.run(app.state.job_store.create(job))

    with TestClient(app) as client:
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
    assert r.status_code == 200
    # embed + reminder + forward
    assert discord.delete_message.call_count == 3


def test_delete_missing_returns_200(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.delete("/api/internal/jobs/never", headers=INTERNAL_AUTH)
    assert r.status_code == 200


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
