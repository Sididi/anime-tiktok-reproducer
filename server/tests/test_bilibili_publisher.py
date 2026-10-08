"""Contract and duplicate-prevention tests for official Bilibili publication."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from app.services import bilibili_publisher as module
from app.services.bilibili_publisher import BilibiliPublisher

BASE = "https://member.bilibili.com/arcopen/fn/archive"
UPLOAD = "https://openupos.bilivideo.com/video/v2/part/upload"
TOKEN = "https://api.bilibili.com/x/account-oauth2/v1/token"
IDENTITY = "https://member.bilibili.com/arcopen/fn/user/account/info"


@pytest.fixture
def credentials():
    return {
        "client_id": "client", "client_secret": "secret", "access_token": "access",
        "expires_at": int(time.time()) + 86400, "category_id": 21, "tags": ["anime", "review"],
        "title": "A video", "copyright": 1, "openid": "creator-openid",
    }


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"1234567890")
    return path


def mock_upload():
    mock_identity()
    init = respx.post(f"{BASE}/video/init").respond(
        200, json={"code": 0, "data": {"upload_token": "upload-secret"}}
    )
    parts = respx.post(UPLOAD).respond(200, json={"code": 0})
    merge = respx.post(f"{BASE}/video/complete").respond(200, json={"code": 0})
    return init, parts, merge


def mock_identity(openid="creator-openid"):
    return respx.get(IDENTITY).respond(200, json={"code": 0, "data": {"openid": openid}})


@respx.mock
async def test_raw_chunks_and_submission_boundary(credentials, video, monkeypatch):
    monkeypatch.setattr(module, "_CHUNK_BYTES", 4)
    init, parts, merge = mock_upload()
    saved = []

    async def checkpoint(state):
        saved.append(state)

    def submit(request):
        assert saved[-1]["submission_started"] is True
        assert saved[-1]["upload_completed"] is True
        payload = json.loads(request.content)
        assert payload == {
            "title": "A video", "tid": 21, "tag": "anime,review", "desc": "Test café",
            "copyright": 1,
        }
        assert request.url.params["upload_token"] == "upload-secret"
        assert request.headers["x-bili-content-md5"] == hashlib.md5(request.content).hexdigest()
        signed = sorted((k, v) for k, v in request.headers.items() if k.startswith("x-bili-"))
        canonical = "\n".join(f"{key}:{value}" for key, value in signed)
        assert request.headers["Authorization"] == hmac.new(
            b"secret", canonical.encode(), hashlib.sha256
        ).hexdigest()
        assert request.headers["Access-Token"] == "access"
        assert "access_token" not in request.url.params
        return httpx.Response(200, json={"code": 0, "data": {"resource_id": "BV1test"}})

    publication = respx.post(f"{BASE}/add-by-utoken").mock(side_effect=submit)
    result = await BilibiliPublisher(credentials).publish(video, "Test café", on_state=checkpoint)
    assert result.success is True
    assert result.url == "https://www.bilibili.com/video/BV1test"
    assert result.state["resource_id"] == "BV1test"
    assert "review" in result.detail
    assert init.call_count == merge.call_count == publication.call_count == 1
    assert [call.request.content for call in parts.calls] == [b"1234", b"5678", b"90"]
    assert [call.request.url.params["part_number"] for call in parts.calls] == ["1", "2", "3"]
    for call in parts.calls:
        assert call.request.headers["Content-Type"] == "application/octet-stream"
        assert "Access-Token" not in call.request.headers


@pytest.mark.parametrize("failure", [
    "timeout", "invalid_json", "missing_id", "server_error", "proxy_timeout",
])
@respx.mock
async def test_ambiguous_submit_is_never_repeated(credentials, video, failure):
    mock_upload()
    if failure == "timeout":
        route = respx.post(f"{BASE}/add-by-utoken").mock(side_effect=httpx.ReadTimeout("lost"))
    elif failure == "invalid_json":
        route = respx.post(f"{BASE}/add-by-utoken").respond(200, text="unparseable")
    elif failure == "missing_id":
        route = respx.post(f"{BASE}/add-by-utoken").respond(200, json={"code": 0})
    elif failure == "server_error":
        route = respx.post(f"{BASE}/add-by-utoken").respond(503)
    else:
        route = respx.post(f"{BASE}/add-by-utoken").respond(408)
    publisher = BilibiliPublisher(credentials)
    first = await publisher.publish(video)
    assert not first.success and first.uncertain
    assert first.state["submission_started"]
    second = await publisher.publish(video, state=first.state)
    assert second.uncertain
    assert "Creator Studio" in second.detail
    assert route.call_count == 1


@respx.mock
async def test_explicit_rejection_allows_retry_without_reupload(credentials, video):
    init, parts, merge = mock_upload()
    route = respx.post(f"{BASE}/add-by-utoken").respond(200, json={"code": 123013})
    publisher = BilibiliPublisher(credentials)
    first = await publisher.publish(video)
    assert not first.success and not first.uncertain
    assert "123013" in first.detail
    assert not first.state["submission_started"]
    route.respond(200, json={"code": 0, "data": {"resource_id": "BV1ok"}})
    second = await publisher.publish(video, state=first.state)
    assert second.success
    assert init.call_count == parts.call_count == merge.call_count == 1


@respx.mock
async def test_resume_upload_from_last_acknowledged_chunk(credentials, video, monkeypatch):
    monkeypatch.setattr(module, "_CHUNK_BYTES", 4)
    mock_identity()
    parts = respx.post(UPLOAD).respond(200, json={"code": 0})
    respx.post(f"{BASE}/video/complete").respond(200, json={"code": 0})
    respx.post(f"{BASE}/add-by-utoken").respond(
        200, json={"code": 0, "data": {"resource_id": "BV1ok"}}
    )
    result = await BilibiliPublisher(credentials).publish(video, state={
        "upload_token": "upload-secret", "uploaded_parts": 1,
        "source_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
    })
    assert result.success
    assert [call.request.content for call in parts.calls] == [b"5678", b"90"]


@respx.mock
async def test_unknown_merge_starts_new_unpublished_upload(credentials, video):
    init, parts, _merge = mock_upload()
    respx.post(f"{BASE}/add-by-utoken").respond(
        200, json={"code": 0, "data": {"resource_id": "BV1ok"}}
    )
    result = await BilibiliPublisher(credentials).publish(video, state={
        "upload_token": "old-merged", "uploaded_parts": 1, "merge_started": True,
    })
    assert result.success and init.call_count == 1
    assert parts.calls[0].request.url.params["upload_token"] == "upload-secret"


@respx.mock
async def test_checkpoint_failure_prevents_publication(credentials, video):
    mock_upload()

    async def fail_at_boundary(state):
        if state.get("submission_started"):
            raise OSError("state store unavailable")

    result = await BilibiliPublisher(credentials).publish(video, on_state=fail_at_boundary)
    assert not result.success
    assert "state store unavailable" in result.detail
    assert all("add-by-utoken" not in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_failure_saving_success_keeps_uncertain_submission(credentials, video):
    mock_upload()
    respx.post(f"{BASE}/add-by-utoken").respond(
        200, json={"code": 0, "data": {"resource_id": "BV1ok"}}
    )

    async def fail_saving_id(state):
        if state.get("resource_id"):
            raise OSError("database unavailable")

    result = await BilibiliPublisher(credentials).publish(video, on_state=fail_saving_id)
    assert not result.success and result.uncertain
    assert result.state["submission_started"]


@pytest.mark.parametrize("override", [
    {"access_token": ""}, {"expires_at": 0}, {"expires_at": 1234567890},
    {"title": ""}, {"title": "x" * 81}, {"category_id": 0}, {"copyright": 0},
    {"copyright": 2, "source": ""}, {"tags": []},
    {"openid": ""},
])
@respx.mock
async def test_invalid_configuration_never_contacts_provider(credentials, video, override):
    result = await BilibiliPublisher({**credentials, **override}).publish(video)
    assert not result.success and not result.uncertain
    assert not respx.calls


@respx.mock
async def test_changed_source_cannot_resume_old_upload(credentials, video):
    identity = mock_identity()
    result = await BilibiliPublisher(credentials).publish(video, state={
        "upload_token": "secret", "source_sha256": "different",
    })
    assert not result.success and "different video" in result.detail
    assert len(respx.calls) == identity.call_count == 1


@respx.mock
async def test_token_for_another_creator_never_uploads(credentials, video):
    identity = mock_identity("different-creator")
    result = await BilibiliPublisher(credentials).publish(video)
    assert not result.success and "different creator" in result.detail
    assert len(respx.calls) == identity.call_count == 1


@respx.mock
async def test_completed_result_survives_expired_credentials_and_missing_file():
    result = await BilibiliPublisher({}).publish(
        Path("missing.mp4"), state={"resource_id": "BV1done", "submission_started": True}
    )
    assert result.success and result.resource_id == "BV1done"
    assert not respx.calls


@respx.mock
async def test_oauth_code_exchange_is_form_encoded_and_expiry_is_preserved():
    mock_identity()
    expiry = int(time.time()) + 86400
    route = respx.post(TOKEN).respond(200, json={"code": 0, "data": {
        "access_token": "access", "refresh_token": "refresh", "expires_in": expiry,
    }})
    result = await BilibiliPublisher.exchange_authorization_code(
        client_id="client", client_secret="secret", code="authorization-code",
    )
    assert result == {
        "access_token": "access", "refresh_token": "refresh", "expires_at": expiry,
        "openid": "creator-openid",
    }
    request = route.calls[0].request
    assert parse_qs(request.content.decode()) == {
        "client_id": ["client"], "client_secret": ["secret"],
        "grant_type": ["authorization_code"], "code": ["authorization-code"],
    }
    assert not request.url.query
