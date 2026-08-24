"""Tests for the /api/internal/cep/* routes and the maintenance loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.models.cep_launch import LAUNCH_TTL

INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}
LAUNCH_PAYLOAD = {
    "project_id": "p1",
    "requested_at": "2026-08-24T12:00:00+00:00",
    "anime_title": "One Piece",
    "discord_message_id": "m1",
    "discord_content": "**One Piece**: done",
}
JOB_PAYLOAD = {
    "project_id": "p1",
    "account_id": "anime_fr",
    "slot_time": "2026-04-26T21:00:00+00:00",
    "anime_title": "One Piece 1063",
    "description": "Posted today",
    "drive_video_url": "https://drive.google.com/uc?id=xyz",
    "platforms_requested": ["youtube", "tiktok"],
}


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


def test_requires_bearer(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        assert client.post("/api/internal/cep/launches", json=LAUNCH_PAYLOAD).status_code == 401
        assert client.get("/api/internal/cep/status").status_code == 401
        assert client.get("/api/internal/cep/launches/p1").status_code == 401
        assert client.delete("/api/internal/cep/launches/p1").status_code == 401


def test_post_while_disconnected_stores_and_marks_waiting(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.post("/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 202
        assert r.json()["connected"] is False
        assert r.json()["delivered"] is False
        discord.edit_message.assert_awaited_once()
        assert discord.edit_message.await_args.kwargs["content"] == (
            "**One Piece**: done\nPremiere: ⏳ waiting for the panel"
        )
        stored = client.get("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).json()
        assert stored["status"] == "pending"
        assert stored["delivery_count"] == 0
        assert stored["expires_at"].startswith("2026-08-31T12:00:00")
        status = client.get("/api/internal/cep/status", headers=INTERNAL_AUTH).json()
        assert status["connected"] is False
        assert status["pending_count"] == 1


def test_post_without_discord_message_skips_edit(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        payload = {**LAUNCH_PAYLOAD, "discord_message_id": None, "discord_content": None}
        assert (
            client.post(
                "/api/internal/cep/launches", json=payload, headers=INTERNAL_AUTH
            ).status_code
            == 202
        )
        discord.edit_message.assert_not_awaited()


def test_get_404_then_200_and_delete_idempotent(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        assert client.get("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).status_code == 404
        client.post("/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH)
        assert client.get("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).status_code == 200
        assert (
            client.delete("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).status_code == 204
        )
        assert (
            client.delete("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).status_code == 204
        )
        assert client.get("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).status_code == 404


def test_delete_job_cascades_to_launch(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        assert client.post(
            "/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH
        ).status_code in (200, 201)
        client.post("/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH)
        assert client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH).status_code == 204
        assert client.get("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).status_code == 404


def test_maintenance_expires_and_notifies(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    store = app.state.cep_launch_store
    hub = app.state.cep_link
    now = datetime.now(tz=UTC)

    async def _run():
        await store.upsert(
            project_id="old",
            anime_title="Old",
            requested_at=now - LAUNCH_TTL - timedelta(hours=1),
            discord_message_id="m_old",
            discord_content="**Old**: done\nPremiere: ⏳ waiting for the panel",
        )
        await store.upsert(
            project_id="fresh",
            anime_title="Fresh",
            requested_at=now,
            discord_message_id="m_fresh",
            discord_content="**Fresh**: done",
        )
        return await hub._maintenance_once()

    expired = asyncio.run(_run())
    assert [launch.project_id for launch in expired] == ["old"]
    discord.edit_message.assert_awaited_once()
    channel, message_id = discord.edit_message.await_args.args
    assert (channel, message_id) == ("222", "m_old")
    assert discord.edit_message.await_args.kwargs["content"] == (
        "**Old**: done\nPremiere: ⌛ expired (panel never connected within 7 days)"
    )
    assert asyncio.run(store.get("old")).status == "expired"
    assert asyncio.run(store.get("fresh")).status == "pending"
