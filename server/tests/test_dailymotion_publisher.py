"""Dailymotion's private staging, durable transitions and retry boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.services.dailymotion_publisher import (
    API_BASE,
    TOKEN_URL,
    DailymotionError,
    DailymotionPublisher,
)

UPLOAD_URL = "https://upload-us1.dailymotion.com/upload?seal=private-upload-capability"
CREDS = {"client_id": "client-id", "client_secret": "private-secret", "profile_id": "profile1"}


@pytest.fixture
def video(tmp_path: Path) -> Path:
    result = tmp_path / "video.mp4"
    result.write_bytes(b"test video bytes")
    return result


def publisher(monkeypatch, handler, **credentials):
    result = DailymotionPublisher({**CREDS, **credentials})
    monkeypatch.setattr(
        result,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return result


def token_response():
    return httpx.Response(200, json={"access_token": "secret-access", "expires_in": 1800})


def staging_response(request: httpx.Request) -> httpx.Response:
    if str(request.url) == TOKEN_URL:
        form = parse_qs(request.content.decode())
        assert form == {
            "grant_type": ["client_credentials"],
            "client_id": ["client-id"],
            "client_secret": ["private-secret"],
            "scope": ["video.manage video.read"],
        }
        return token_response()
    assert request.headers["Authorization"] == "Bearer secret-access"
    if request.url.path == "/v2/files/upload_sessions":
        return httpx.Response(201, json={"upload_url": UPLOAD_URL})
    if str(request.url) == UPLOAD_URL:
        assert b"test video bytes" in request.content
        assert "multipart/form-data" in request.headers["content-type"]
        return httpx.Response(200, json={"url": "https://upload-us1.dailymotion.com/files/f1"})
    if request.url.path == "/v2/profiles/profile1/videos":
        body = json.loads(request.content)
        assert body["visibility"] == "private"
        assert body["is_for_kids"] is False
        assert body["title"] == "A test video"
        assert body["category"] == "shortfilms"
        assert "published_at" not in body
        return httpx.Response(201, json={"video_id": "x123"})
    raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")


async def test_persisted_stages_precede_private_create_and_public_publish(monkeypatch, video):
    states = []
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/v2/profiles/profile1/videos":
            assert states[-1] == {"stage": "creating"}
        if request.method == "PATCH":
            assert states[-1]["stage"] == "submitting"
            assert states[-1]["resource_id"] == "x123"
            assert json.loads(request.content) == {"visibility": "public"}
            return httpx.Response(204)
        return staging_response(request)

    result = await publisher(monkeypatch, handler).publish(
        video,
        "A test video",
        on_state=states.append,
    )
    assert result.success and not result.uncertain
    assert result.resource_id == "x123"
    assert result.url == "https://www.dailymotion.com/video/x123"
    assert [state["stage"] for state in states] == [
        "creating",
        "private",
        "submitting",
        "submitted",
    ]
    assert requests.count(("POST", "/v2/token")) == 1


async def test_ambiguous_create_is_not_repeated_on_retry(monkeypatch, video):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v2/profiles/profile1/videos":
            raise httpx.ReadTimeout("response lost")
        return staging_response(request)

    client = publisher(monkeypatch, handler)
    first = await client.publish(video, "A test video")
    assert not first.success and first.uncertain
    assert first.state == {"stage": "creating"}
    count = len(calls)
    retried = await client.publish(video, "A test video", state=first.state)
    assert not retried.success and retried.uncertain
    assert len(calls) == count


async def test_retry_reconciles_lost_publish_response_without_recreating(monkeypatch, video):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if str(request.url) == TOKEN_URL:
            return token_response()
        assert request.method == "GET"
        assert request.url.path == "/v2/videos/x123"
        return httpx.Response(
            200, json={"visibility": "public", "video_url": "https://dai.ly/x123"}
        )

    result = await publisher(monkeypatch, handler).publish(
        video,
        state={"stage": "submitting", "resource_id": "x123"},
    )
    assert result.success
    assert result.url == "https://dai.ly/x123"
    assert result.state["stage"] == "submitted"
    assert seen == [("POST", "/v2/token"), ("GET", "/v2/videos/x123")]


async def test_existing_private_video_is_published_without_reupload(monkeypatch, video):
    seen = []

    def handler(request):
        seen.append(request.method)
        if str(request.url) == TOKEN_URL:
            return token_response()
        assert request.url.path == "/v2/videos/x123"
        if request.method == "GET":
            return httpx.Response(200, json={"visibility": "private"})
        assert request.method == "PATCH"
        return httpx.Response(204)

    result = await publisher(monkeypatch, handler).publish(
        video,
        state={"stage": "submitting", "resource_id": "x123"},
    )
    assert result.success
    assert seen == ["POST", "GET", "PATCH"]


async def test_failed_reconciliation_keeps_publication_uncertain(monkeypatch, video):
    def handler(request):
        if str(request.url) == TOKEN_URL:
            return token_response()
        return httpx.Response(503)

    result = await publisher(monkeypatch, handler).publish(
        video,
        state={"stage": "submitting", "resource_id": "x123"},
    )
    assert not result.success and result.uncertain
    assert result.state["stage"] == "submitting"


@pytest.mark.parametrize(
    "status,uncertain,stage", [(403, False, "private"), (503, True, "submitting")]
)
async def test_publish_rejection_differs_from_unknown_server_outcome(
    monkeypatch,
    video,
    status,
    uncertain,
    stage,
):
    def handler(request):
        if request.method == "PATCH":
            return httpx.Response(status, json={"error": {"code": "MISSING_PERMISSIONS"}})
        return staging_response(request)

    result = await publisher(monkeypatch, handler).publish(video, "A test video")
    assert not result.success and result.uncertain is uncertain
    assert result.resource_id == "x123"
    assert result.state["stage"] == stage


async def test_creation_rejected_by_api_can_retry_without_uncertainty(monkeypatch, video):
    def handler(request):
        if request.url.path == "/v2/profiles/profile1/videos":
            return httpx.Response(422)
        return staging_response(request)

    result = await publisher(monkeypatch, handler).publish(video, "A test video")
    assert not result.success and not result.uncertain
    assert result.state["stage"] == "pending"


async def test_state_storage_failure_prevents_publication(monkeypatch, video):
    seen = []

    def handler(request):
        seen.append(request.method)
        assert request.method != "PATCH"
        return staging_response(request)

    async def persist(state):
        if state["stage"] == "submitting":
            raise RuntimeError("state store unavailable")

    with pytest.raises(RuntimeError, match="state store unavailable"):
        await publisher(monkeypatch, handler).publish(video, "A test video", on_state=persist)
    assert "PATCH" not in seen


@pytest.mark.parametrize(
    "upload_url",
    [
        "https://attacker.test/upload",
        "http://upload-us1.dailymotion.com/upload",
        "https://upload-us1.dailymotion.com.attacker.test/upload",
        "https://upload-us1.dailymotion.com:444/upload",
    ],
)
async def test_upload_endpoint_cannot_exfiltrate_file_or_token(monkeypatch, video, upload_url):
    def handler(request):
        if str(request.url) == TOKEN_URL:
            return token_response()
        assert request.url.path == "/v2/files/upload_sessions"
        return httpx.Response(201, json={"upload_url": upload_url})

    result = await publisher(monkeypatch, handler).publish(video, "A test video")
    assert not result.success
    assert "invalid upload URL" in result.detail


async def test_expired_token_is_replaced_after_401(monkeypatch):
    tokens = 0
    attempts = 0

    def handler(request):
        nonlocal tokens, attempts
        if str(request.url) == TOKEN_URL:
            tokens += 1
            return httpx.Response(200, json={"access_token": f"token{tokens}", "expires_in": 1800})
        attempts += 1
        assert request.method == "DELETE"
        assert request.headers["Authorization"] == f"Bearer token{attempts}"
        return httpx.Response(401 if attempts == 1 else 204)

    await publisher(monkeypatch, handler).delete("x123")
    assert tokens == attempts == 2


@pytest.mark.parametrize("status", [204, 404])
async def test_delete_is_idempotent(monkeypatch, status):
    def handler(request):
        if str(request.url) == TOKEN_URL:
            return token_response()
        assert str(request.url) == f"{API_BASE}/videos/x123"
        assert request.method == "DELETE"
        return httpx.Response(status)

    await publisher(monkeypatch, handler).delete("x123")


async def test_delete_failure_raises_sanitized_error(monkeypatch):
    def handler(request):
        if str(request.url) == TOKEN_URL:
            return token_response()
        return httpx.Response(403, json={"error": {"message": "private-secret secret-access"}})

    with pytest.raises(DailymotionError) as exc:
        await publisher(monkeypatch, handler).delete("x123")
    assert "HTTP 403" in str(exc.value)
    assert "private-secret" not in str(exc.value)
    assert "secret-access" not in str(exc.value)


async def test_submitted_state_never_uploads_again(monkeypatch, video):
    def handler(request):
        raise AssertionError("Already submitted job must not make network requests")

    result = await publisher(monkeypatch, handler).publish(
        video,
        state={"stage": "submitted", "resource_id": "x123"},
    )
    assert result.success


async def test_missing_credentials_fail_before_file_upload(monkeypatch, video):
    def handler(request):
        raise AssertionError("No network call expected")

    result = await publisher(monkeypatch, handler, client_secret="").publish(video, "Title")
    assert not result.success and not result.uncertain
    assert "client_secret is not configured" in result.detail
