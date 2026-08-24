from __future__ import annotations

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
    "platforms_requested": ["tiktok", "instagram"],
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


def test_patch_merges_platform_scheduled_at(monkeypatch, example_yaml, example_env, tmp_server_dir):
    """Updating one platform's slot must NOT wipe other platforms' entries."""
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)

        # First, set IG entry only.
        client.patch(
            "/api/internal/jobs/p1/slot",
            json={"platform_scheduled_at": {"instagram": "2026-05-08T18:00:00+00:00"}},
            headers=INTERNAL_AUTH,
        )
        # Now move only TT — IG entry must survive.
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={"platform_scheduled_at": {"tiktok": "2026-05-09T14:00:00+00:00"}},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["platform_scheduled_at"]["instagram"].startswith(
            "2026-05-08T18:00:00"
        )
        assert body["platform_scheduled_at"]["tiktok"].startswith(
            "2026-05-09T14:00:00"
        )
        # Top-level slot_time was never sent so it stays at the original.
        assert body["slot_time"].startswith("2026-05-07T14:00:00")


def test_patch_sets_reminder_cancelled(monkeypatch, example_yaml, example_env, tmp_server_dir):
    """Cancelling the TT reminder is a one-field PATCH that doesn't touch
    slot_time or platform_scheduled_at."""
    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={"reminder_cancelled": True},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["reminder_cancelled"] is True
        # Original slot_time preserved.
        assert body["slot_time"].startswith("2026-05-07T14:00:00")


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


def _set_job(app, **fields):
    import asyncio  # noqa: PLC0415

    return asyncio.run(app.state.job_store.update("p1", **fields))


def _get_job(app):
    import asyncio  # noqa: PLC0415

    return asyncio.run(app.state.job_store.get("p1"))


def test_patch_tiktok_time_rearms_posted_manual_reminder(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    """Moving a manual job's TikTok slot into the future after the reminder was
    posted deletes that reminder so a fresh one fires at the new T-5."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord = app.state.discord  # reset to None once the lifespan exits
    with TestClient(app) as client:
        client.post(
            "/api/internal/jobs", json={**JOB_PAYLOAD, "tiktok_manual": True},
            headers=INTERNAL_AUTH,
        )
        _set_job(app, reminder_message_id="m_old")
        new_time = datetime.now(tz=UTC) + timedelta(hours=2)
        r = client.patch(
            "/api/internal/jobs/p1/slot",
            json={"platform_scheduled_at": {"tiktok": new_time.isoformat()}},
            headers=INTERNAL_AUTH,
        )
    assert r.status_code == 200
    job = _get_job(app)
    assert job.reminder_message_id is None
    assert job.reminder_cancelled is False
    discord.delete_message.assert_called_once_with("333", "m_old")


def test_patch_tiktok_time_inside_lead_keeps_posted_reminder(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord = app.state.discord
    with TestClient(app) as client:
        client.post(
            "/api/internal/jobs", json={**JOB_PAYLOAD, "tiktok_manual": True},
            headers=INTERNAL_AUTH,
        )
        _set_job(app, reminder_message_id="m_old")
        soon = datetime.now(tz=UTC) + timedelta(minutes=3)  # T-5 already passed
        client.patch(
            "/api/internal/jobs/p1/slot",
            json={"platform_scheduled_at": {"tiktok": soon.isoformat()}},
            headers=INTERNAL_AUTH,
        )
    assert _get_job(app).reminder_message_id == "m_old"
    discord.delete_message.assert_not_called()


def test_patch_tiktok_time_on_pfm_job_never_touches_reminder_fields(
    monkeypatch, example_yaml, example_env, tmp_server_dir
):
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    app = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord = app.state.discord
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        _set_job(app, reminder_message_id="m_old")
        new_time = datetime.now(tz=UTC) + timedelta(hours=2)
        client.patch(
            "/api/internal/jobs/p1/slot",
            json={"platform_scheduled_at": {"tiktok": new_time.isoformat()}},
            headers=INTERNAL_AUTH,
        )
    assert _get_job(app).reminder_message_id == "m_old"
    discord.delete_message.assert_not_called()
