"""Tests for the rewritten DiscordService (HTTP client to the VPS server)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.discord_service import DiscordMessage, DiscordService


@pytest.fixture(autouse=True)
def _set_vps_env(monkeypatch):
    monkeypatch.setattr(
        "app.services.discord_service.settings.tiktok_server_base_url",
        "https://tiktok.sididi.tv",
    )
    monkeypatch.setattr(
        "app.services.discord_service.settings.tiktok_server_internal_token",
        "internal_secret",
    )


def test_is_configured_true_when_both_set():
    assert DiscordService.is_configured() is True


def test_is_configured_false_when_url_missing(monkeypatch):
    monkeypatch.setattr(
        "app.services.discord_service.settings.tiktok_server_base_url", None
    )
    assert DiscordService.is_configured() is False


@respx.mock
def test_post_message_calls_generic_endpoint():
    route = respx.post("https://tiktok.sididi.tv/api/internal/discord/messages").mock(
        return_value=httpx.Response(200, json={"message_id": "msg_42"})
    )
    msg = DiscordService.post_message("hello")
    assert isinstance(msg, DiscordMessage)
    assert msg.id == "msg_42"
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer internal_secret"
    assert b'"content":"hello"' in sent.content


@respx.mock
def test_edit_message_calls_generic_patch():
    route = respx.patch(
        "https://tiktok.sididi.tv/api/internal/discord/messages/m_1"
    ).mock(return_value=httpx.Response(200))
    DiscordService.edit_message("m_1", "new content")
    assert route.called


@respx.mock
def test_delete_message_calls_generic_delete():
    route = respx.delete(
        "https://tiktok.sididi.tv/api/internal/discord/messages/m_1"
    ).mock(return_value=httpx.Response(200))
    DiscordService.delete_message("m_1")
    assert route.called


@respx.mock
def test_create_job_returns_response_dict():
    route = respx.post("https://tiktok.sididi.tv/api/internal/jobs").mock(
        return_value=httpx.Response(
            200, json={"job_id": "j_x", "discord_message_id": "msg_100"}
        )
    )
    res = DiscordService.create_job(
        project_id="p1",
        account_id="anime_fr",
        slot_time=datetime(2026, 4, 26, 21, 0, tzinfo=timezone.utc),
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive.google.com/uc?id=xyz",
        platforms_requested=["youtube", "tiktok"],
    )
    assert res == {"job_id": "j_x", "discord_message_id": "msg_100"}
    assert route.called
    sent_body = route.calls.last.request.content
    assert b'"project_id":"p1"' in sent_body
    assert b'"account_id":"anime_fr"' in sent_body


@respx.mock
def test_update_job_platform():
    route = respx.post(
        "https://tiktok.sididi.tv/api/internal/jobs/p1/platform-status"
    ).mock(return_value=httpx.Response(200, json={"ok": True}))
    DiscordService.update_job_platform(
        "p1", "youtube", status="uploaded", url="https://youtu.be/x"
    )
    assert route.called


@respx.mock
def test_delete_job():
    route = respx.delete("https://tiktok.sididi.tv/api/internal/jobs/p1").mock(
        return_value=httpx.Response(200, json={"ok": True, "deleted": True})
    )
    DiscordService.delete_job("p1")
    assert route.called


def test_post_message_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        "app.services.discord_service.settings.tiktok_server_base_url", None
    )
    assert DiscordService.post_message("anything") is None


@respx.mock
def test_post_message_swallows_network_errors():
    respx.post("https://tiktok.sididi.tv/api/internal/discord/messages").mock(
        side_effect=httpx.ConnectError("boom")
    )
    # Must not raise
    assert DiscordService.post_message("x") is None


@respx.mock
def test_create_job_with_instagram_payload():
    route = respx.post("https://tiktok.sididi.tv/api/internal/jobs").mock(
        return_value=httpx.Response(200, json={"job_id": "j_x", "discord_message_id": "m_1"})
    )
    DiscordService.create_job(
        project_id="p1",
        account_id="anime_fr",
        slot_time=datetime(2026, 4, 27, 21, 0, tzinfo=timezone.utc),
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        platforms_requested=["instagram"],
        instagram={
            "ig_user_id": "ig_42",
            "ig_access_token": "ig_token",
            "caption": "hi",
            "prepared_video_url": "https://drive.usercontent.google.com/download?id=ig",
            "graph_api_version": "v25.0",
            "poll_interval_seconds": 60,
            "poll_timeout_seconds": 14400,
        },
        platform_statuses={
            "instagram": {
                "status": "failed",
                "detail": "Instagram video preparation failed",
            }
        },
    )
    assert route.called
    sent = route.calls.last.request.content
    assert b'"instagram":{' in sent or b'"instagram": {' in sent
    assert b'"ig_user_id":"ig_42"' in sent or b'"ig_user_id": "ig_42"' in sent
    assert b'"prepared_video_url":"https://drive.usercontent.google.com/download?id=ig"' in sent
    assert b'"poll_interval_seconds":60' in sent or b'"poll_interval_seconds": 60' in sent
    assert b'"poll_timeout_seconds":14400' in sent or b'"poll_timeout_seconds": 14400' in sent
    assert b'"platform_statuses":{' in sent or b'"platform_statuses": {' in sent
    assert b'"status":"failed"' in sent or b'"status": "failed"' in sent


@respx.mock
def test_create_job_forwards_tiktok_payload():
    route = respx.post("https://tiktok.sididi.tv/api/internal/jobs").mock(
        return_value=httpx.Response(200, json={"job_id": "j_x", "discord_message_id": "m_1"})
    )
    DiscordService.create_job(
        project_id="p1",
        account_id="anime_fr",
        slot_time=datetime(2026, 4, 27, 21, 0, tzinfo=timezone.utc),
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        platforms_requested=["tiktok"],
        tiktok={"social_account_id": "spc_1", "caption": "c"},
    )
    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["tiktok"] == {"social_account_id": "spc_1", "caption": "c"}


@respx.mock
def test_create_job_omits_tiktok_key_when_none():
    route = respx.post("https://tiktok.sididi.tv/api/internal/jobs").mock(
        return_value=httpx.Response(200, json={"job_id": "j_x", "discord_message_id": "m_1"})
    )
    DiscordService.create_job(
        project_id="p1",
        account_id="anime_fr",
        slot_time=datetime(2026, 4, 27, 21, 0, tzinfo=timezone.utc),
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        platforms_requested=["youtube"],
        tiktok=None,
    )
    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert "tiktok" not in sent_body


@respx.mock
def test_create_job_sends_platform_scheduled_at():
    route = respx.post("https://tiktok.sididi.tv/api/internal/jobs").mock(
        return_value=httpx.Response(200, json={"job_id": "j_x", "discord_message_id": "m_1"})
    )
    DiscordService.create_job(
        project_id="p1",
        account_id="anime_fr",
        slot_time=datetime(2026, 4, 27, 20, 17, tzinfo=timezone.utc),
        anime_title="Title",
        description="Desc",
        drive_video_url="https://drive/x",
        platforms_requested=["instagram", "tiktok"],
        platform_scheduled_at={
            "instagram": datetime(2026, 4, 27, 6, 1, tzinfo=timezone.utc),
            "tiktok": datetime(2026, 4, 27, 20, 17, tzinfo=timezone.utc),
        },
    )

    assert route.called
    sent = route.calls.last.request.content
    assert b'"platform_scheduled_at":{' in sent or b'"platform_scheduled_at": {' in sent
    assert b'"instagram":"2026-04-27T06:01:00+00:00"' in sent
    assert b'"tiktok":"2026-04-27T20:17:00+00:00"' in sent
