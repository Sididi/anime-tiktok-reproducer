"""WebSocket tests for the Premiere Link hub (/api/cep/ws)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.models.cep_launch import LAUNCH_TTL, CepLaunch
from app.services.cep_link import outcome_line, with_outcome_line

INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}
AUTH_FRAME = {"type": "auth", "token": "cep_secret", "panel_build_id": "b1", "port": 48653}
LAUNCH_PAYLOAD = {
    "project_id": "p1",
    "requested_at": "2026-08-24T12:00:00+00:00",
    "anime_title": "One Piece",
    "discord_message_id": "m1",
    "discord_content": "**One Piece**: done\nLien de génération: <http://localhost:48653/p/p1>",
}


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path, **env: str):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    discord = AsyncMock()
    discord.post_message.return_value = "msg_1"
    app.state.discord = discord
    return app, discord


def _seed(app, project_id: str, *, requested_at: datetime, ack: str | None = None):
    store = app.state.cep_launch_store

    async def _run():
        launch = await store.upsert(
            project_id=project_id,
            anime_title=project_id,
            requested_at=requested_at,
            discord_message_id=None,
            discord_content=None,
        )
        if ack:
            await store.record_ack(
                project_id, launch.launch_id, result=ack, detail=None, panel_build_id=None
            )
        return launch

    return asyncio.run(_run())


def _receive_until(ws, kind: str, *, max_frames: int = 20) -> dict:
    """Skip heartbeat pings until a frame of `kind` arrives."""
    for _ in range(max_frames):
        frame = ws.receive_json()
        if frame["type"] == kind:
            return frame
        if frame["type"] == "ping":
            ws.send_json({"type": "pong", "ts": frame["ts"]})
    raise AssertionError(f"no {kind!r} frame received")


def _wait_status(client: TestClient, project_id: str, status: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/internal/cep/launches/{project_id}", headers=INTERNAL_AUTH)
        if r.status_code == 200 and r.json()["status"] == status:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"launch {project_id} never reached {status}")


def _last_edit_content(discord) -> str:
    assert discord.edit_message.await_count >= 1
    return discord.edit_message.await_args.kwargs["content"]


# ---- helpers ---------------------------------------------------------------


def test_with_outcome_line_replaces_previous_line():
    content = "**A**: done\nLien: <url>"
    once = with_outcome_line(content, "Premiere: ⏳ waiting for the panel")
    assert once == "**A**: done\nLien: <url>\nPremiere: ⏳ waiting for the panel"
    twice = with_outcome_line(once, "Premiere: ✅ accepted 14:02")
    assert twice == "**A**: done\nLien: <url>\nPremiere: ✅ accepted 14:02"
    assert with_outcome_line(None, "Premiere: x") == "Premiere: x"


# ---- auth ------------------------------------------------------------------


def test_auth_ok_reports_status(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        with client.websocket_connect("/api/cep/ws") as ws:
            ws.send_json(AUTH_FRAME)
            frame = ws.receive_json()
            assert frame["type"] == "auth_ok"
            assert frame["pending_count"] == 0
            assert frame["heartbeat_interval_s"] == 25.0
            status = client.get("/api/internal/cep/status", headers=INTERNAL_AUTH).json()
            assert status["connected"] is True
            assert status["connections"] == 1
            assert status["panel_build_id"] == "b1"
            assert status["pending_count"] == 0
        deadline = time.time() + 2
        while time.time() < deadline:
            status = client.get("/api/internal/cep/status", headers=INTERNAL_AUTH).json()
            if not status["connected"]:
                break
            time.sleep(0.02)
        assert status["connected"] is False
        assert status["panel_build_id"] == "b1"


def test_wrong_token_closes_4401(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json({**AUTH_FRAME, "token": "nope"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_unconfigured_token_closes_4401(monkeypatch, example_yaml, example_env, tmp_server_dir):
    monkeypatch.delenv("ATR_CEP_LINK_TOKEN", raising=False)
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_non_auth_first_frame_closes_4400(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4400


def test_auth_timeout_closes_4401(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(
        monkeypatch,
        example_yaml,
        example_env,
        tmp_server_dir,
        ATR_CEP_LINK_AUTH_TIMEOUT_SECONDS="0.2",
    )
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


# ---- replay / push / ack ---------------------------------------------------


def test_replay_delivers_only_pending_unexpired(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    now = datetime.now(tz=UTC)
    _seed(app, "fresh_a", requested_at=now - timedelta(hours=1))
    _seed(app, "fresh_b", requested_at=now - timedelta(minutes=1))
    _seed(app, "expired", requested_at=now - LAUNCH_TTL - timedelta(hours=1))
    _seed(app, "done", requested_at=now, ack="accepted")
    with TestClient(app) as client:
        with client.websocket_connect("/api/cep/ws") as ws:
            ws.send_json(AUTH_FRAME)
            auth_ok = ws.receive_json()
            assert auth_ok["pending_count"] == 2
            replayed = {ws.receive_json()["project_id"] for _ in range(2)}
            assert replayed == {"fresh_a", "fresh_b"}
        stored = client.get("/api/internal/cep/launches/fresh_a", headers=INTERNAL_AUTH).json()
        assert stored["delivery_count"] == 1
        assert stored["delivered_at"] is not None


def test_live_push_then_ack_edits_discord(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        assert ws.receive_json()["type"] == "auth_ok"

        r = client.post("/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["connected"] is True
        assert body["delivered"] is True
        waiting = _last_edit_content(discord)
        assert waiting.endswith("Premiere: ⏳ waiting for the panel")
        assert waiting.startswith("**One Piece**: done")

        frame = _receive_until(ws, "launch")
        assert frame["project_id"] == "p1"
        assert frame["launch_id"] == body["launch_id"]
        assert frame["anime_title"] == "One Piece"
        assert frame["replay"] is False

        ws.send_json(
            {
                "type": "ack",
                "launch_id": frame["launch_id"],
                "project_id": "p1",
                "result": "accepted",
                "detail": None,
                "status": "queued_download",
                "queue_state": "active",
                "batch_phase": "intake",
            }
        )
        stored = _wait_status(client, "p1", "accepted")
        assert stored["panel_build_id"] == "b1"
        assert stored["acked_at"] is not None
        content = _last_edit_content(discord)
        assert "Premiere: ✅ accepted " in content
        assert "⏳" not in content
        channel, message_id = discord.edit_message.await_args.args
        assert (channel, message_id) == ("222", "m1")


@pytest.mark.parametrize(
    ("result", "detail", "expected"),
    [
        ("duplicate", None, "Premiere: ⚠️ duplicate (already run this session)"),
        ("error", "host build mismatch", "Premiere: ❌ error — host build mismatch"),
    ],
)
def test_ack_variants_render_outcome(
    monkeypatch, example_yaml, example_env, tmp_server_dir, result, detail, expected
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        ws.receive_json()
        launch_id = client.post(
            "/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH
        ).json()["launch_id"]
        _receive_until(ws, "launch")
        ws.send_json(
            {
                "type": "ack",
                "launch_id": launch_id,
                "project_id": "p1",
                "result": result,
                "detail": detail,
            }
        )
        _wait_status(client, "p1", result)
        assert _last_edit_content(discord).endswith(expected)


def test_stale_ack_is_rejected_after_reexport(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        ws.receive_json()
        first = client.post(
            "/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH
        ).json()
        _receive_until(ws, "launch")
        second = client.post(
            "/api/internal/cep/launches", json=LAUNCH_PAYLOAD, headers=INTERNAL_AUTH
        ).json()
        _receive_until(ws, "launch")
        assert first["launch_id"] != second["launch_id"]

        ws.send_json(
            {
                "type": "ack",
                "launch_id": first["launch_id"],
                "project_id": "p1",
                "result": "accepted",
            }
        )
        err = _receive_until(ws, "error")
        assert err["code"] == "unknown_launch"
        stored = client.get("/api/internal/cep/launches/p1", headers=INTERNAL_AUTH).json()
        assert stored["status"] == "pending"
        assert stored["launch_id"] == second["launch_id"]


def test_bad_frames_get_error_not_disconnect(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, _ = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        ws.receive_json()
        ws.send_text("not json")
        assert _receive_until(ws, "error")["code"] == "bad_frame"
        ws.send_json({"type": "launch"})
        assert _receive_until(ws, "error")["code"] == "unknown_type"
        ws.send_json({"type": "ack", "project_id": "p1"})
        assert _receive_until(ws, "error")["code"] == "bad_frame"
        ws.send_json({"type": "ping", "ts": "t1"})
        pong = _receive_until(ws, "pong")
        assert pong["ts"] == "t1"


# ---- heartbeat -------------------------------------------------------------


def test_server_heartbeat_timeout_closes_4408(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    app, _ = _make_app(
        monkeypatch,
        example_yaml,
        example_env,
        tmp_server_dir,
        ATR_CEP_LINK_HEARTBEAT_SECONDS="0.05",
    )
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        assert ws.receive_json()["type"] == "auth_ok"
        pings = 0
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(50):
                frame = ws.receive_json()  # never answer the pings
                if frame["type"] == "ping":
                    pings += 1
        assert exc.value.code == 4408
        assert pings >= 1


def test_pong_keeps_connection_alive(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app, _ = _make_app(
        monkeypatch,
        example_yaml,
        example_env,
        tmp_server_dir,
        ATR_CEP_LINK_HEARTBEAT_SECONDS="0.05",
    )
    with TestClient(app) as client, client.websocket_connect("/api/cep/ws") as ws:
        ws.send_json(AUTH_FRAME)
        assert ws.receive_json()["type"] == "auth_ok"
        for _ in range(6):  # ~0.3 s > 2 × interval: would have been dropped without pongs
            frame = ws.receive_json()
            assert frame["type"] == "ping"
            ws.send_json({"type": "pong", "ts": frame["ts"]})
        assert (
            client.get("/api/internal/cep/status", headers=INTERNAL_AUTH).json()["connected"]
            is True
        )


def test_outcome_line_covers_every_status():
    now = datetime(2026, 8, 24, 12, 2, tzinfo=UTC)
    base = dict(
        project_id="p",
        launch_id="l_1",
        anime_title="t",
        requested_at=now,
        created_at=now,
        updated_at=now,
        expires_at=now,
    )
    assert outcome_line(CepLaunch(**base, status="pending")).startswith("Premiere: ⏳")
    assert outcome_line(CepLaunch(**base, status="accepted", acked_at=now)).startswith(
        "Premiere: ✅ accepted "
    )
    assert outcome_line(CepLaunch(**base, status="duplicate")).startswith("Premiere: ⚠️")
    assert (
        outcome_line(CepLaunch(**base, status="error", ack_detail="x")) == "Premiere: ❌ error — x"
    )
    assert outcome_line(CepLaunch(**base, status="expired")).startswith("Premiere: ⌛")
