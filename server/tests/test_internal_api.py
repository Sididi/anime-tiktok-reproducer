"""Tests for /api/internal/* endpoints."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from app.models.job import Job, PlatformStatus, TikTokPublishState
from app.services.post_for_me_publisher import TikTokPublishResult
from app.services.reminder_scheduler import dispatch_due_actions, wait_for_inflight


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


def test_create_job_stores_tiktok_payload(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    tiktok = {
        "social_account_id": "spc_1",
        "caption": "cap",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": True,
        "allow_stitch": False,
    }
    payload = {**JOB_PAYLOAD, "tiktok": tiktok}
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
    assert r.status_code == 200

    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.tiktok_payload == {
        **tiktok,
        "post_for_me_platform": "tiktok",
    }


def test_create_job_rejects_unknown_tiktok_connector(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"
    payload = {
        **JOB_PAYLOAD,
        "tiktok": {
            "social_account_id": "spc_1",
            "post_for_me_platform": "tiktok_enterprise",
            "caption": "cap",
        },
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/internal/jobs", json=payload, headers=INTERNAL_AUTH
        )
    assert response.status_code == 422


def test_legacy_tiktok_payload_without_connector_remains_idempotent(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"
    tiktok = {
        "social_account_id": "spc_1",
        "caption": "cap",
        "privacy_status": "public",
        "allow_comment": True,
        "allow_duet": True,
        "allow_stitch": True,
    }
    payload = {**JOB_PAYLOAD, "tiktok": tiktok}
    with TestClient(app) as client:
        first = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
        assert first.status_code == 200
        asyncio.run(
            app.state.job_store.update(
                "p1",
                tiktok_payload=tiktok,
                tiktok_publish_state=TikTokPublishState(
                    post_id="post_live", stage="post_created"
                ),
            )
        )
        repeated = client.post(
            "/api/internal/jobs", json=payload, headers=INTERNAL_AUTH
        )
    assert repeated.status_code == 200
    job = asyncio.run(app.state.job_store.get("p1"))
    assert "post_for_me_platform" not in job.tiktok_payload
    assert job.tiktok_publish_state.post_id == "post_live"


def test_create_job_blocks_connector_switch_with_live_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"
    payload = {
        **JOB_PAYLOAD,
        "tiktok": {
            "social_account_id": "spc_consumer",
            "caption": "cap",
        },
    }
    with TestClient(app) as client:
        first = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
        assert first.status_code == 200
        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="post_live", stage="post_created"),
            )
        )
        switched = {
            **payload,
            "tiktok": {
                "social_account_id": "spc_business",
                "post_for_me_platform": "tiktok_business",
                "caption": "cap",
            },
        }
        response = client.post(
            "/api/internal/jobs", json=switched, headers=INTERNAL_AUTH
        )
    assert response.status_code == 409
    assert "tiktok_target_locked" in response.json()["detail"]
    job = asyncio.run(app.state.job_store.get("p1"))
    assert job.tiktok_payload["social_account_id"] == "spc_consumer"
    assert job.tiktok_publish_state.post_id == "post_live"


async def test_connector_switch_cannot_race_inflight_post_creation(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    app.state.settings = replace(app.state.settings, pfm_api_key="key")
    discord.post_message.return_value = "msg_embed"
    create_started = asyncio.Event()
    allow_create_to_finish = asyncio.Event()
    create_calls: list[dict] = []

    async def create_post(**kwargs):
        create_calls.append(kwargs)
        create_started.set()
        await allow_create_to_finish.wait()
        return TikTokPublishResult(
            success=True,
            publish_state=TikTokPublishState(
                media_url="https://media.example/video.mp4",
                post_id="post_consumer",
                stage="post_created",
            ),
        )

    async def poll_post(**_kwargs):
        return TikTokPublishResult(
            success=True,
            url="https://tiktok.com/@a/video/1",
            publish_state=TikTokPublishState(
                media_url="https://media.example/video.mp4",
                post_id="post_consumer",
                stage="published",
                url="https://tiktok.com/@a/video/1",
            ),
        )

    monkeypatch.setattr(
        "app.services.reminder_scheduler.create_tiktok_post", create_post
    )
    monkeypatch.setattr(
        "app.services.reminder_scheduler.poll_tiktok_post_result", poll_post
    )

    consumer_payload = {
        **JOB_PAYLOAD,
        "tiktok": {"social_account_id": "spc_consumer", "caption": "cap"},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/api/internal/jobs", json=consumer_payload, headers=INTERNAL_AUTH
        )
        assert first.status_code == 200
        await app.state.job_store.set_tiktok_publish_state(
            "p1",
            TikTokPublishState(
                media_url="https://media.example/video.mp4",
                stage="media_uploaded",
            ),
        )

        actions = await dispatch_due_actions(
            store=app.state.job_store,
            settings=app.state.settings,
            discord=discord,
        )
        assert actions == 1
        await asyncio.wait_for(create_started.wait(), timeout=1)

        business_payload = {
            **consumer_payload,
            "tiktok": {
                "social_account_id": "spc_business",
                "post_for_me_platform": "tiktok_business",
                "caption": "cap",
            },
        }
        switch_task = asyncio.create_task(
            client.post(
                "/api/internal/jobs", json=business_payload, headers=INTERNAL_AUTH
            )
        )
        await asyncio.sleep(0)
        assert not switch_task.done()

        allow_create_to_finish.set()
        await wait_for_inflight()
        switched = await asyncio.wait_for(switch_task, timeout=1)

    assert switched.status_code == 409
    assert create_calls[0]["social_account_id"] == "spc_consumer"
    job = await app.state.job_store.get("p1")
    assert job is not None
    assert job.tiktok_payload["social_account_id"] == "spc_consumer"
    assert job.tiktok_publish_state.post_id == "post_consumer"


def test_create_job_allows_connector_switch_after_definitive_failure(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"
    payload = {
        **JOB_PAYLOAD,
        "tiktok": {"social_account_id": "spc_consumer", "caption": "cap"},
    }
    with TestClient(app) as client:
        first = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
        assert first.status_code == 200
        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(
                    post_id="post_failed",
                    stage="failed",
                    last_error="reached_active_user_cap",
                ),
            )
        )
        switched = {
            **payload,
            "tiktok": {
                "social_account_id": "spc_business",
                "post_for_me_platform": "tiktok_business",
                "caption": "cap",
            },
        }
        response = client.post(
            "/api/internal/jobs", json=switched, headers=INTERNAL_AUTH
        )
    assert response.status_code == 200
    job = asyncio.run(app.state.job_store.get("p1"))
    assert job.tiktok_payload["social_account_id"] == "spc_business"
    assert job.tiktok_payload["post_for_me_platform"] == "tiktok_business"
    assert job.tiktok_publish_state is None


def test_update_job_replaces_tiktok_payload_and_resets_state(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    payload = {
        **JOB_PAYLOAD,
        "tiktok": {"social_account_id": "spc_1", "caption": "a"},
    }
    with TestClient(app) as client:
        first = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
        assert first.status_code == 200

        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1", TikTokPublishState(post_id="stale", stage="published")
            )
        )

        changed = {
            **payload,
            "tiktok": {"social_account_id": "spc_1", "caption": "b"},
        }
        second = client.post("/api/internal/jobs", json=changed, headers=INTERNAL_AUTH)
    assert second.status_code == 200

    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.tiktok_payload["caption"] == "b"
    assert job.tiktok_publish_state is None


def test_create_job_without_tiktok_payload_is_allowed(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    discord.post_message.return_value = "msg_embed"

    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
    assert r.status_code == 200

    job = asyncio.run(app.state.job_store.get("p1"))
    assert job is not None
    assert job.tiktok_payload is None


def test_delete_job_cancels_scheduled_pfm_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    monkeypatch.setenv("ATR_PFM_API_KEY", "key")   # BEFORE app creation: Settings
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    deleted = AsyncMock()
    monkeypatch.setattr("app.api.internal.delete_tiktok_post", deleted)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 200
        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="sp_X", stage="post_scheduled"),
            )
        )
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
        assert r.status_code == 204
    deleted.assert_awaited_once()
    assert deleted.await_args.kwargs["post_id"] == "sp_X"


def test_delete_job_ignores_pfm_delete_failure(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    monkeypatch.setenv("ATR_PFM_API_KEY", "key")
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    deleted = AsyncMock(side_effect=RuntimeError("pfm down"))
    monkeypatch.setattr("app.api.internal.delete_tiktok_post", deleted)
    with TestClient(app) as client:
        client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="sp_X", stage="post_scheduled"),
            )
        )
        r = client.delete("/api/internal/jobs/p1", headers=INTERNAL_AUTH)
        assert r.status_code == 204          # deletion proceeds despite PFM error


def test_upsert_with_changed_payload_cancels_scheduled_pfm_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    monkeypatch.setenv("ATR_PFM_API_KEY", "key")   # BEFORE app creation: Settings
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    deleted = AsyncMock()
    monkeypatch.setattr("app.api.internal.delete_tiktok_post", deleted)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 200
        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="sp_X", stage="post_scheduled"),
            )
        )
        changed_payload = {
            **JOB_PAYLOAD,
            "drive_video_url": "https://drive.google.com/uc?id=different",
        }
        r = client.post(
            "/api/internal/jobs", json=changed_payload, headers=INTERNAL_AUTH
        )
        assert r.status_code == 200
    deleted.assert_awaited_once()
    assert deleted.await_args.kwargs["post_id"] == "sp_X"
    job = asyncio.run(app.state.job_store.get("p1"))
    assert job.tiktok_publish_state is None


def test_upsert_does_not_cancel_published_post(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    from app.models.job import TikTokPublishState  # noqa: PLC0415

    monkeypatch.setenv("ATR_PFM_API_KEY", "key")
    app, discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    deleted = AsyncMock()
    monkeypatch.setattr("app.api.internal.delete_tiktok_post", deleted)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 200
        asyncio.run(
            app.state.job_store.set_tiktok_publish_state(
                "p1",
                TikTokPublishState(post_id="sp_X", stage="published"),
            )
        )
        changed_payload = {
            **JOB_PAYLOAD,
            "drive_video_url": "https://drive.google.com/uc?id=different",
        }
        r = client.post(
            "/api/internal/jobs", json=changed_payload, headers=INTERNAL_AUTH
        )
        assert r.status_code == 200
    deleted.assert_not_awaited()


def test_list_job_statuses_returns_projection_without_payloads(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    import app.api.internal as internal_module

    # Reset the module-level snapshot cache so this test is isolated.
    internal_module._status_snapshot = {}
    internal_module._status_snapshot_at = 0.0

    app, _discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    payload = dict(JOB_PAYLOAD)
    payload["tiktok"] = {"social_account_id": "pfm_1", "caption": "cap"}

    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=payload, headers=INTERNAL_AUTH)
        assert r.status_code == 200
        r = client.post(
            "/api/internal/jobs/p1/platform-status",
            json={"platform": "tiktok", "status": "uploaded", "url": "https://tiktok.com/@a/video/1"},
            headers=INTERNAL_AUTH,
        )
        assert r.status_code == 200

        # Cache still holds the pre-update snapshot? No: first GET builds it now.
        r = client.get("/api/internal/jobs", headers=INTERNAL_AUTH)
        assert r.status_code == 200
        jobs = r.json()["jobs"]
        assert "p1" in jobs
        entry = jobs["p1"]
        assert entry["platform_statuses"]["tiktok"]["status"] == "uploaded"
        assert entry["platform_statuses"]["tiktok"]["url"] == "https://tiktok.com/@a/video/1"
        # Secrets/payloads must never be exposed.
        assert "tiktok_payload" not in entry
        assert "instagram_payload" not in entry

        # Unauthenticated is rejected.
        r = client.get("/api/internal/jobs")
        assert r.status_code in (401, 403)


def test_list_job_statuses_is_throttled(
    monkeypatch, example_yaml: Path, example_env, tmp_server_dir: Path
):
    import app.api.internal as internal_module

    internal_module._status_snapshot = {}
    internal_module._status_snapshot_at = 0.0

    app, _discord = _make_app(monkeypatch, example_yaml, example_env, tmp_server_dir)
    with TestClient(app) as client:
        r = client.post("/api/internal/jobs", json=JOB_PAYLOAD, headers=INTERNAL_AUTH)
        assert r.status_code == 200
        r = client.get("/api/internal/jobs", headers=INTERNAL_AUTH)
        assert r.status_code == 200
        assert "p1" in r.json()["jobs"]

        # A job created inside the TTL window is not visible until it expires:
        # the snapshot is served from cache (max one store read per window).
        payload2 = dict(JOB_PAYLOAD)
        payload2["project_id"] = "p2"
        r = client.post("/api/internal/jobs", json=payload2, headers=INTERNAL_AUTH)
        assert r.status_code == 200
        r = client.get("/api/internal/jobs", headers=INTERNAL_AUTH)
        assert "p2" not in r.json()["jobs"]

        # After the TTL the snapshot refreshes.
        internal_module._status_snapshot_at = 0.0
        r = client.get("/api/internal/jobs", headers=INTERNAL_AUTH)
        assert "p2" in r.json()["jobs"]
