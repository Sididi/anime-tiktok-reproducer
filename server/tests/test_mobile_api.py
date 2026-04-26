"""Tests for /api/mobile/* endpoints."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

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
MOBILE_AUTH = {"Authorization": "Bearer mobile_secret"}


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app

    app = create_app()
    discord = AsyncMock()
    discord.post_message.return_value = "msg_x"
    app.state.discord = discord
    return app, discord


def test_me_returns_device_and_accounts(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/mobile/me", headers=MOBILE_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "iphone_13_pro"
    assert {a["id"] for a in body["accounts"]} == {"anime_fr"}
    assert body["accounts"][0]["avatar_url"].endswith("/api/avatars/anime_fr.jpg")


def test_jobs_list_filters_by_device_pending_only(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.get("/api/mobile/jobs", headers=MOBILE_AUTH)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    item = items[0]
    assert item["project_id"] == "p1"
    assert item["status"] == "pending"
    assert item["account_avatar_url"].endswith("/api/avatars/anime_fr.jpg")


def test_video_url_returned(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r_create = client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        )
        job_id = r_create.json()["job_id"]
        r = client.get(f"/api/mobile/jobs/{job_id}/video-url", headers=MOBILE_AUTH)
    assert r.status_code == 200
    assert r.json()["video_url"] == "https://drive.google.com/uc?id=xyz"


def test_ack_marks_acked_and_adds_reaction(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        r_create = client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        )
        job_id = r_create.json()["job_id"]
        r = client.post(f"/api/mobile/jobs/{job_id}/ack", headers=MOBILE_AUTH)
        # Confirm acked job is gone from pending list
        r_list = client.get("/api/mobile/jobs", headers=MOBILE_AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "acked"
    discord.add_reaction.assert_called_once()
    discord.edit_message.assert_called()
    assert r_list.json() == []


def test_ack_idempotent(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.side_effect = ["msg_embed", "msg_reminder"]
    with TestClient(app) as client:
        r_create = client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        )
        job_id = r_create.json()["job_id"]
        client.post(f"/api/mobile/jobs/{job_id}/ack", headers=MOBILE_AUTH)
        r2 = client.post(f"/api/mobile/jobs/{job_id}/ack", headers=MOBILE_AUTH)
    assert r2.status_code == 200
    # add_reaction called only once total
    assert discord.add_reaction.call_count == 1


def test_unauthenticated_mobile_rejected(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.get("/api/mobile/jobs")
    assert r.status_code == 401
