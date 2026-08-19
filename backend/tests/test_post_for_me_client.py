"""Backend Post for Me client (2026-08 TikTok migration)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.post_for_me_client import (
    PfmPublishOutcome,
    PostForMeClient,
    PostForMeError,
    PostForMeNotConfiguredError,
    build_post_body,
    _derive_tiktok_video_url,
)


@pytest.fixture(autouse=True)
def _pfm_key(monkeypatch):
    monkeypatch.setattr("app.services.post_for_me_client.settings.pfm_api_key", "key")
    monkeypatch.setattr(
        "app.services.post_for_me_client.settings.pfm_base_url",
        "https://pfm.test/v1",
    )


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    def client(cls):
        return httpx.Client(transport=transport)

    monkeypatch.setattr(PostForMeClient, "_client", classmethod(client))


def test_build_post_body_schedules_utc_iso():
    at = datetime(2026, 12, 25, 18, 0, tzinfo=timezone.utc)
    body = build_post_body(
        social_account_id="spc_1",
        media_url="https://pfm/media/1",
        caption="cap",
        scheduled_at=at,
    )
    assert body["scheduled_at"] == "2026-12-25T18:00:00+00:00"
    assert body["social_accounts"] == ["spc_1"]
    assert body["platform_configurations"]["tiktok"]["privacy_status"] == "public"


def test_build_post_body_instant_omits_scheduled_at():
    body = build_post_body(
        social_account_id="spc_1", media_url="https://pfm/media/1", caption="cap"
    )
    assert "scheduled_at" not in body


def test_build_post_body_rejects_unknown_connector():
    with pytest.raises(ValueError, match="unsupported"):
        build_post_body(
            social_account_id="spc_1",
            media_url="u",
            caption="c",
            post_for_me_platform="youtube",
        )


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr("app.services.post_for_me_client.settings.pfm_api_key", None)
    with pytest.raises(PostForMeNotConfiguredError):
        PostForMeClient.create_post({"caption": "x"})


def test_create_post_returns_id(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/social-posts"
        assert request.headers["Authorization"] == "Bearer key"
        return httpx.Response(200, json={"data": {"id": "sp_42"}})

    _mock_client(monkeypatch, handler)
    assert PostForMeClient.create_post({"caption": "x"}) == "sp_42"


def test_create_post_error_carries_detail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "bad schedule"})

    _mock_client(monkeypatch, handler)
    with pytest.raises(PostForMeError, match="create_post") as exc:
        PostForMeClient.create_post({"caption": "x"})
    assert exc.value.status_code == 422


def test_delete_post_treats_404_as_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)
    PostForMeClient.delete_post("sp_1")  # no raise


def test_update_post_puts_body(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "sp_1"})

    _mock_client(monkeypatch, handler)
    PostForMeClient.update_post("sp_1", {"caption": "x"})
    assert seen == {"method": "PUT", "path": "/v1/social-posts/sp_1"}


def test_fetch_outcome_pending_returns_none(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    _mock_client(monkeypatch, handler)
    assert PostForMeClient.fetch_outcome("sp_1") is None


def test_fetch_outcome_success_derives_video_url(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "social_account_id": "spc_1",
                        "success": True,
                        "platform_data": {
                            "url": "https://www.tiktok.com/@user",
                            "id": "v_pub_url~v2-1.7659653399897655318",
                        },
                    }
                ]
            },
        )

    _mock_client(monkeypatch, handler)
    outcome = PostForMeClient.fetch_outcome("sp_1", "spc_1")
    assert outcome == PfmPublishOutcome(
        success=True, url="https://www.tiktok.com/@user/video/7659653399897655318"
    )


def test_fetch_outcome_failure_digs_platform_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "success": False,
                        "error": "Failed to post to TikTok",
                        "details": {
                            "error": {
                                "response": {
                                    "status": 403,
                                    "data": {
                                        "error": {"code": "reached_active_user_cap"}
                                    },
                                }
                            }
                        },
                    }
                ]
            },
        )

    _mock_client(monkeypatch, handler)
    outcome = PostForMeClient.fetch_outcome("sp_1")
    assert outcome.success is False
    assert "reached_active_user_cap" in (outcome.detail or "")


def test_derive_tiktok_video_url_guards():
    assert _derive_tiktok_video_url({"url": "https://www.tiktok.com/@u/video/123"}) == (
        "https://www.tiktok.com/@u/video/123"
    )
    assert (
        _derive_tiktok_video_url(
            {"url": "https://www.tiktok.com/@u", "id": "v~x.notdigits"}
        )
        is None
    )
