from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


JOB_PAYLOAD = {
    "project_id": "p1",
    "account_id": "anime_fr",
    "slot_time": "2026-05-07T14:00:00+00:00",
    "anime_title": "Test",
    "description": "d",
    "drive_video_url": "https://drive.google.com/uc?id=x",
    "platforms_requested": ["instagram"],
    "instagram": {
        "ig_user_id": "ig",
        "ig_access_token": "tok",
        "caption": "c",
    },
}
INTERNAL_AUTH = {"Authorization": "Bearer internal_secret"}


def _make_app(monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path):
    monkeypatch.setenv("ATR_TIKTOK_SERVER_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_AVATARS_DIR", str(tmp_server_dir / "avatars"))
    monkeypatch.setenv("ATR_TIKTOK_SERVER_DATA_DIR", str(tmp_server_dir / "data"))
    from app.main import create_app  # noqa: PLC0415

    app = create_app()
    app.state.discord = AsyncMock()
    app.state.discord.post_message = AsyncMock(return_value="msg_1")
    return app


def test_patch_job_slot_updates_slot_time(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 200

        new_slot = "2026-05-08T18:00:00+00:00"
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={
                "slot_time": new_slot,
                "platform_scheduled_at": {"instagram": "2026-05-08T18:11:00+00:00"},
            },
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["slot_time"].startswith("2026-05-08T18:00:00")


def test_patch_job_slot_404_for_missing(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.patch(
            "/api/internal/jobs/missing/slot",
            json={"slot_time": "2026-05-08T18:00:00+00:00"},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 404


def test_delete_job_removes_it(monkeypatch, example_yaml, example_env, tmp_server_dir):
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
        assert r.status_code == 204
        # Subsequent PATCH should now 404.
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={"slot_time": "2026-05-09T14:00:00+00:00"},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 404
