"""Tests for app.services.instagram_publisher using respx-mocked Graph API."""
from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from app.models.job import InstagramPublishState
from app.services import instagram_publisher
from app.services.instagram_publisher import (
    _preflight_instagram_account,
    _upload_headers,
    _upload_resumable_binary_sync,
    _UploadResponse,
    publish_to_instagram,
)

BASE = "https://graph.facebook.com/v25.0"
IG_USER_ID = "ig_user_123"
ACCESS_TOKEN = "access_token_abc"
CONTAINER_ID = "container_42"
VIDEO_URL_CONTAINER_ID = "container_video_url"
MEDIA_ID = "media_99"
PERMALINK_URL = "https://www.instagram.com/reel/Cxxx/"
UPLOAD_URI = "https://rupload.facebook.com/ig-api-upload/v25.0/container_42"
ORIGINAL_VALIDATE_VIDEO = instagram_publisher._validate_video
ORIGINAL_PREPARE_VIDEO = instagram_publisher._prepare_video_for_instagram_upload

# Common kwargs shared by all tests
_COMMON = dict(
    ig_user_id=IG_USER_ID,
    ig_access_token=ACCESS_TOKEN,
    caption="Test caption #anime",
    video_url="https://cdn.example.com/video.mp4",
)


def test_stream_validation_uses_configured_account_duration_limit():
    payload = {
        "format": {"format_name": "mov,mp4", "duration": "901"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 540,
                "height": 960,
                "avg_frame_rate": "24/1",
            }
        ],
    }
    assert "outside 3-900s" in instagram_publisher._validate_video_streams(payload)
    assert (
        instagram_publisher._validate_video_streams(
            payload, max_duration_seconds=1200
        )
        is None
    )


def _mock_video_download():
    return respx.get(_COMMON["video_url"]).mock(
        return_value=httpx.Response(
            200,
            content=b"fake mp4 bytes",
            headers={"content-type": "video/mp4"},
        )
    )


def _mock_resumable_create():
    return respx.post(f"{BASE}/{IG_USER_ID}/media").mock(
        return_value=httpx.Response(200, json={"id": CONTAINER_ID, "uri": UPLOAD_URI})
    )


def _form_call(route, index: int = -1) -> dict[str, list[str]]:
    content = route.calls[index].request.content.decode()
    return parse_qs(content)


@pytest.fixture(autouse=True)
def mock_meta_side_effects(monkeypatch):
    async def preflight(*args, **kwargs):
        return None

    async def validate(path):
        return None

    async def prepare(path):
        return path

    async def upload(**kwargs):
        return _UploadResponse(status_code=200, body='{"success": true}')

    monkeypatch.setattr(instagram_publisher, "_preflight_instagram_account", preflight)
    monkeypatch.setattr(instagram_publisher, "_prepare_video_for_instagram_upload", prepare)
    monkeypatch.setattr(instagram_publisher, "_validate_video", validate)
    monkeypatch.setattr(instagram_publisher, "_upload_resumable_binary", upload)


@respx.mock
async def test_happy_path():
    """Full happy path: container → IN_PROGRESS → FINISHED → publish → permalink."""
    download_route = _mock_video_download()
    create_route = _mock_resumable_create()
    status_responses = [
        httpx.Response(200, json={"status_code": "IN_PROGRESS", "id": CONTAINER_ID}),
        httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID}),
    ]
    status_route = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        side_effect=status_responses
    )
    publish_route = respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    permalink_route = respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is True
    assert result.permalink == PERMALINK_URL
    assert result.detail is None
    assert download_route.called
    assert create_route.called
    assert status_route.call_count == 2
    assert publish_route.called
    assert permalink_route.called


@respx.mock
async def test_polling_waits_multiple_ticks():
    """Status returns IN_PROGRESS 3 times before FINISHED — verifies multiple polls."""
    _mock_video_download()
    _mock_resumable_create()
    status_responses = [
        httpx.Response(200, json={"status_code": "IN_PROGRESS", "id": CONTAINER_ID}),
        httpx.Response(200, json={"status_code": "IN_PROGRESS", "id": CONTAINER_ID}),
        httpx.Response(200, json={"status_code": "IN_PROGRESS", "id": CONTAINER_ID}),
        httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID}),
    ]
    status_route = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        side_effect=status_responses
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is True
    assert status_route.call_count == 4


@respx.mock
async def test_status_poll_rate_limit_retries():
    _mock_video_download()
    _mock_resumable_create()
    status_route = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        side_effect=[
            httpx.Response(
                400,
                json={
                    "error": {
                        "message": "(#4) Application request limit reached",
                        "code": 4,
                    }
                },
            ),
            httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID}),
        ]
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is True
    assert status_route.call_count == 2


@respx.mock
async def test_resumable_upload_processing_failed_error_polls_container(monkeypatch):
    """Meta can return an indeterminate rupload error while the container proceeds."""
    async def upload(**kwargs):
        return _UploadResponse(
            status_code=400,
            body=(
                '{"debug_info":{"retriable":false,"type":"ProcessingFailedError",'
                '"message":"Request processing failed"}}'
            ),
        )

    monkeypatch.setattr(instagram_publisher, "_upload_resumable_binary", upload)
    _mock_video_download()
    _mock_resumable_create()
    status_route = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED"})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is True
    assert result.permalink == PERMALINK_URL
    assert status_route.called


@respx.mock
async def test_resumable_upload_2xx_without_success_true_fails(monkeypatch):
    async def upload(**kwargs):
        return _UploadResponse(status_code=200, body='{"success": false, "message": "nope"}')

    monkeypatch.setattr(instagram_publisher, "_upload_resumable_binary", upload)
    _mock_video_download()
    _mock_resumable_create()
    status_route = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED"})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail is not None
    assert result.detail.startswith("rupload:")
    assert status_route.called is False


@respx.mock
async def test_resumable_upload_2xx_non_json_body_fails(monkeypatch):
    async def upload(**kwargs):
        return _UploadResponse(status_code=200, body="OK")

    monkeypatch.setattr(instagram_publisher, "_upload_resumable_binary", upload)
    _mock_video_download()
    _mock_resumable_create()

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail == "rupload: rupload returned non-JSON success body: OK"


def test_upload_headers_match_instagram_rupload_contract():
    headers = _upload_headers(ACCESS_TOKEN, len(b"fake mp4 bytes"))

    assert headers["Content-Length"] == str(len(b"fake mp4 bytes"))
    assert headers["file_size"] == str(len(b"fake mp4 bytes"))
    assert headers["offset"] == "0"
    assert headers["Authorization"] == f"OAuth {ACCESS_TOKEN}"
    assert "X-Entity-Length" not in headers
    assert "Content-Type" not in headers
    assert "Transfer-Encoding" not in headers


@respx.mock
async def test_container_creation_fails_http_400():
    """Container creation returns 400 → success=False with failure detail."""
    _mock_video_download()
    respx.post(f"{BASE}/{IG_USER_ID}/media").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Invalid video URL"}})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail is not None
    assert "create_container:" in result.detail


@respx.mock
async def test_polling_sees_error_status():
    """Container status returns ERROR → success=False."""
    _mock_video_download()
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status_code": "ERROR",
                "status": "The video URL is not reachable.",
                "id": CONTAINER_ID,
            },
        )
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail == (
        "status_poll: container status_code = ERROR; "
        "status = The video URL is not reachable."
    )


@respx.mock
async def test_polling_sees_expired_status():
    _mock_video_download()
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200,
            json={"status_code": "EXPIRED", "status": "Container expired."},
        )
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail == (
        "status_poll: container status_code = EXPIRED; status = Container expired."
    )


@respx.mock
async def test_polling_sees_published_status_as_success():
    _mock_video_download()
    _mock_resumable_create()
    publish_route = respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(500, text="should not publish again")
    )
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "PUBLISHED"})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is True
    assert publish_route.called is False


@respx.mock
async def test_polling_timeout():
    """Status stuck IN_PROGRESS forever → returns success=False with poll timeout."""
    _mock_video_download()
    _mock_resumable_create()
    # Always return IN_PROGRESS
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "IN_PROGRESS", "id": CONTAINER_ID})
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=0.05
    )

    assert result.success is False
    assert result.detail is not None
    assert result.detail.startswith("status_poll: poll timeout after ")
    assert f"container={CONTAINER_ID}" in result.detail
    assert "last_status=IN_PROGRESS" in result.detail
    assert "resumable=true" in result.detail
    assert result.publish_state is not None
    assert result.publish_state.container_id == CONTAINER_ID
    assert result.publish_state.last_status_code == "IN_PROGRESS"


@respx.mock
async def test_prepared_media_video_url_is_primary(tmp_path):
    prepared_dir = tmp_path / "prepared"
    _mock_video_download()
    create_route = respx.post(f"{BASE}/{IG_USER_ID}/media").mock(
        return_value=httpx.Response(200, json={"id": VIDEO_URL_CONTAINER_ID})
    )
    video_url_status = respx.get(f"{BASE}/{VIDEO_URL_CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200, json={"status_code": "FINISHED", "id": VIDEO_URL_CONTAINER_ID}
        )
    )
    publish_route = respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=60,
        project_id="ig-job",
        prepared_media_dir=prepared_dir,
        public_base_url="https://tiktok.sididi.tv",
    )

    assert result.success is True
    assert create_route.call_count == 1
    create_form = _form_call(create_route)
    assert create_form["video_url"][0].startswith(
        "https://tiktok.sididi.tv/api/instagram/prepared/ig-job/"
    )
    assert create_form["video_url"][0].endswith(".mp4")
    assert video_url_status.called
    assert publish_route.called
    assert result.publish_state is not None
    assert result.publish_state.container_id == VIDEO_URL_CONTAINER_ID
    assert result.publish_state.upload_method == "video_url"
    assert result.publish_state.prepared_media_filename is None
    assert not prepared_dir.exists() or not list(prepared_dir.glob("*.mp4"))


@respx.mock
async def test_prepared_media_video_url_create_failure_falls_back_to_rupload(tmp_path):
    prepared_dir = tmp_path / "prepared"
    _mock_video_download()
    create_route = respx.post(f"{BASE}/{IG_USER_ID}/media").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "URL fetch failed"}}),
            httpx.Response(200, json={"id": CONTAINER_ID, "uri": UPLOAD_URI}),
        ]
    )
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        project_id="ig-job",
        prepared_media_dir=prepared_dir,
        public_base_url="https://tiktok.sididi.tv",
    )

    assert result.success is True
    assert create_route.call_count == 2
    assert _form_call(create_route, 0)["video_url"][0].startswith(
        "https://tiktok.sididi.tv/api/instagram/prepared/ig-job/"
    )
    assert _form_call(create_route, 1)["upload_type"][0] == "resumable"
    assert result.publish_state is not None
    assert result.publish_state.upload_method == "rupload"
    assert result.publish_state.fallback_reason is not None
    assert "URL fetch failed" in result.publish_state.fallback_reason


@respx.mock
async def test_in_progress_with_phase_error_falls_back_to_video_url():
    _mock_video_download()
    create_route = respx.post(f"{BASE}/{IG_USER_ID}/media").mock(
        side_effect=[
            httpx.Response(200, json={"id": CONTAINER_ID, "uri": UPLOAD_URI}),
            httpx.Response(200, json={"id": VIDEO_URL_CONTAINER_ID}),
        ]
    )
    rupload_status = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status_code": "IN_PROGRESS",
                "status": "In Progress: Media is still being processed.",
                "id": CONTAINER_ID,
                "video_status": {
                    "uploading_phase": {
                        "status": "error",
                        "bytes_transferred": 0,
                    },
                    "processing_phase": {"status": "error"},
                },
            },
        )
    )
    video_url_status = respx.get(f"{BASE}/{VIDEO_URL_CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200, json={"status_code": "FINISHED", "id": VIDEO_URL_CONTAINER_ID}
        )
    )
    publish_route = respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=60,
    )

    assert result.success is True
    assert create_route.call_count == 2
    assert _form_call(create_route, 1)["video_url"][0] == _COMMON["video_url"]
    assert rupload_status.called
    assert video_url_status.called
    assert publish_route.called
    assert result.publish_state is not None
    assert result.publish_state.container_id == VIDEO_URL_CONTAINER_ID
    assert result.publish_state.upload_method == "video_url"
    assert result.publish_state.fallback_reason is not None
    assert "uploading_phase=error" in result.publish_state.fallback_reason


@respx.mock
async def test_rupload_phase_error_and_video_url_fallback_failure_reports_both():
    _mock_video_download()
    create_route = respx.post(f"{BASE}/{IG_USER_ID}/media").mock(
        side_effect=[
            httpx.Response(200, json={"id": CONTAINER_ID, "uri": UPLOAD_URI}),
            httpx.Response(200, json={"id": VIDEO_URL_CONTAINER_ID}),
        ]
    )
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status_code": "IN_PROGRESS",
                "status": "In Progress: Media is still being processed.",
                "video_status": {
                    "uploading_phase": {
                        "status": "error",
                        "bytes_transferred": 0,
                    },
                    "processing_phase": {"status": "error"},
                },
            },
        )
    )
    respx.get(f"{BASE}/{VIDEO_URL_CONTAINER_ID}").mock(
        return_value=httpx.Response(
            200,
            json={
                "status_code": "ERROR",
                "status": "The video URL is not reachable.",
            },
        )
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
    )

    assert result.success is False
    assert result.detail is not None
    assert result.detail.startswith("status_poll: container status_code = IN_PROGRESS")
    assert "fallback_video_url: status_poll: container status_code = ERROR" in result.detail
    assert _form_call(create_route, 1)["video_url"][0] == _COMMON["video_url"]
    assert result.publish_state is not None
    assert result.publish_state.container_id == VIDEO_URL_CONTAINER_ID
    assert result.publish_state.upload_method == "video_url"
    assert result.publish_state.prepared_media_filename is None


@respx.mock
async def test_timeout_retry_reuses_uploaded_container_without_new_create():
    state = InstagramPublishState(
        container_id=CONTAINER_ID,
        upload_uri=UPLOAD_URI,
        stage="uploaded",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=5),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=23),
        upload_completed_at=datetime.now(tz=UTC) - timedelta(minutes=4),
        last_status_code="IN_PROGRESS",
    )
    status_route = respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    publish_route = respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        publish_state=state,
    )

    assert result.success is True
    assert result.permalink == PERMALINK_URL
    assert status_route.called
    assert publish_route.called
    assert result.publish_state is not None
    assert result.publish_state.container_id == CONTAINER_ID
    assert result.publish_state.media_id == MEDIA_ID


@respx.mock
async def test_resumed_published_container_returns_success_without_publish():
    state = InstagramPublishState(
        container_id=CONTAINER_ID,
        upload_uri=UPLOAD_URI,
        stage="uploaded",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=5),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=23),
        upload_completed_at=datetime.now(tz=UTC) - timedelta(minutes=4),
        last_status_code="IN_PROGRESS",
    )
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "PUBLISHED", "id": CONTAINER_ID})
    )
    publish_route = respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(500, text="should not publish again")
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        publish_state=state,
    )

    assert result.success is True
    assert publish_route.called is False
    assert result.publish_state is not None
    assert result.publish_state.stage == "published"


@respx.mock
async def test_expired_state_creates_fresh_container():
    expired_state = InstagramPublishState(
        container_id="old_container",
        upload_uri="https://rupload.facebook.com/old",
        stage="uploaded",
        created_at=datetime.now(tz=UTC) - timedelta(days=2),
        expires_at=datetime.now(tz=UTC) - timedelta(days=1),
        upload_completed_at=datetime.now(tz=UTC) - timedelta(days=2),
        last_status_code="IN_PROGRESS",
    )
    _mock_video_download()
    create_route = _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        publish_state=expired_state,
    )

    assert result.success is True
    assert create_route.called
    assert result.publish_state is not None
    assert result.publish_state.container_id == CONTAINER_ID


@respx.mock
async def test_error_state_creates_fresh_container():
    error_state = InstagramPublishState(
        container_id="old_container",
        upload_uri="https://rupload.facebook.com/old",
        stage="polling",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=5),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=23),
        upload_completed_at=datetime.now(tz=UTC) - timedelta(minutes=4),
        last_status_code="ERROR",
    )
    _mock_video_download()
    create_route = _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        publish_state=error_state,
    )

    assert result.success is True
    assert create_route.called
    assert result.publish_state is not None
    assert result.publish_state.container_id == CONTAINER_ID


@respx.mock
async def test_phase_error_state_creates_fresh_container():
    phase_error_state = InstagramPublishState(
        container_id="old_container",
        upload_uri="https://rupload.facebook.com/old",
        stage="polling",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=5),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=23),
        upload_completed_at=datetime.now(tz=UTC) - timedelta(minutes=4),
        last_status_code="IN_PROGRESS",
        last_status_detail=(
            "container status_code = IN_PROGRESS; status = In Progress "
            "(uploading_phase=error, bytes_transferred=0, processing_phase=error)"
        ),
        last_status_payload_summary={
            "status_code": "IN_PROGRESS",
            "video_status": {
                "uploading_phase": {
                    "status": "error",
                    "bytes_transferred": 0,
                },
                "processing_phase": {"status": "error"},
            },
        },
    )
    _mock_video_download()
    create_route = _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        publish_state=phase_error_state,
    )

    assert result.success is True
    assert create_route.called
    assert result.publish_state is not None
    assert result.publish_state.container_id == CONTAINER_ID


@respx.mock
async def test_created_state_reuploads_without_new_create(monkeypatch):
    state = InstagramPublishState(
        container_id=CONTAINER_ID,
        upload_uri=UPLOAD_URI,
        stage="created",
        created_at=datetime.now(tz=UTC) - timedelta(minutes=5),
        expires_at=datetime.now(tz=UTC) + timedelta(hours=23),
    )
    uploaded = {}

    async def upload(**kwargs):
        uploaded.update(kwargs)
        return _UploadResponse(status_code=200, body='{"success": true}')

    monkeypatch.setattr(instagram_publisher, "_upload_resumable_binary", upload)
    _mock_video_download()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        publish_state=state,
    )

    assert result.success is True
    assert uploaded["upload_uri"] == UPLOAD_URI
    assert result.publish_state is not None
    assert result.publish_state.upload_completed_at is not None
    assert result.publish_state.upload_method == "rupload"


@respx.mock
async def test_publish_api_returns_5xx():
    """media_publish returns 500 → success=False mentioning publish failure."""
    _mock_video_download()
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail is not None
    assert "publish:" in result.detail


@respx.mock
async def test_permalink_fetch_fails_after_publish():
    """Permalink fetch fails → still success=True, permalink=None (best-effort)."""
    _mock_video_download()
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED", "id": CONTAINER_ID})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is True
    assert result.permalink is None


@respx.mock
async def test_preflight_checks_account_and_quota():
    account_route = respx.get(f"{BASE}/{IG_USER_ID}").mock(
        return_value=httpx.Response(200, json={"id": IG_USER_ID})
    )
    quota_route = respx.get(f"{BASE}/{IG_USER_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(
            200, json={"quota_usage": 12, "config": {"quota_total": 100}}
        )
    )

    async with httpx.AsyncClient() as client:
        await _preflight_instagram_account(
            client,
            base=BASE,
            ig_user_id=IG_USER_ID,
            ig_access_token=ACCESS_TOKEN,
        )

    assert account_route.called
    assert quota_route.called


@respx.mock
async def test_preflight_accepts_quota_usage_without_config_total():
    respx.get(f"{BASE}/{IG_USER_ID}").mock(
        return_value=httpx.Response(200, json={"id": IG_USER_ID})
    )
    respx.get(f"{BASE}/{IG_USER_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(200, json={"quota_usage": 12})
    )

    async with httpx.AsyncClient() as client:
        await _preflight_instagram_account(
            client,
            base=BASE,
            ig_user_id=IG_USER_ID,
            ig_access_token=ACCESS_TOKEN,
        )


@respx.mock
async def test_preflight_fails_when_quota_exhausted():
    respx.get(f"{BASE}/{IG_USER_ID}").mock(
        return_value=httpx.Response(200, json={"id": IG_USER_ID})
    )
    respx.get(f"{BASE}/{IG_USER_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(
            200, json={"quota_usage": 100, "config": {"quota_total": 100}}
        )
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="quota exhausted"):
            await _preflight_instagram_account(
                client,
                base=BASE,
                ig_user_id=IG_USER_ID,
                ig_access_token=ACCESS_TOKEN,
            )


@respx.mock
async def test_preflight_fails_when_default_quota_exhausted_without_config_total():
    respx.get(f"{BASE}/{IG_USER_ID}").mock(
        return_value=httpx.Response(200, json={"id": IG_USER_ID})
    )
    respx.get(f"{BASE}/{IG_USER_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(200, json={"quota_usage": 50})
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="50/50"):
            await _preflight_instagram_account(
                client,
                base=BASE,
                ig_user_id=IG_USER_ID,
                ig_access_token=ACCESS_TOKEN,
            )


async def test_caption_limit_failure_happens_before_download():
    result = await publish_to_instagram(
        **{**_COMMON, "caption": "x" * 2201},
        poll_interval=0.01,
        poll_timeout=1.0,
    )

    assert result.success is False
    assert result.detail == "preflight: caption is 2201 chars; max is 2200"


@respx.mock
async def test_validation_failure_is_stage_prefixed(monkeypatch):
    async def validate(path):
        return "duration 1.00s outside 3-900s"

    monkeypatch.setattr(instagram_publisher, "_validate_video", validate)
    _mock_video_download()

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail == "validate: duration 1.00s outside 3-900s"


async def test_validation_failure_deletes_downloaded_video(tmp_path, monkeypatch):
    video = tmp_path / "downloaded.mp4"
    video.write_bytes(b"fake mp4 bytes")

    async def download(client, video_url, *, temp_dir=None):
        return video

    async def validate(path):
        return "duration 1.00s outside 3-900s"

    monkeypatch.setattr(instagram_publisher, "_download_video", download)
    monkeypatch.setattr(instagram_publisher, "_validate_video", validate)

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail == "validate: duration 1.00s outside 3-900s"
    assert not video.exists()


def _patch_downloaded_video(monkeypatch, tmp_path: Path):
    video = tmp_path / "downloaded.mp4"
    video.write_bytes(b"fake mp4 bytes")
    seen: dict[str, Path | str | None] = {}

    async def download(client, video_url, *, temp_dir=None):
        seen["video_url"] = video_url
        seen["temp_dir"] = temp_dir
        return video

    monkeypatch.setattr(instagram_publisher, "_download_video", download)
    return video, seen


@respx.mock
async def test_success_deletes_downloaded_video_and_uses_temp_dir(tmp_path, monkeypatch):
    video, seen = _patch_downloaded_video(monkeypatch, tmp_path)
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED"})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )
    respx.get(f"{BASE}/{MEDIA_ID}").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID, "permalink": PERMALINK_URL})
    )
    temp_dir = tmp_path / "ig-tmp"

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
        temp_dir=temp_dir,
    )

    assert result.success is True
    assert seen["temp_dir"] == temp_dir
    assert not video.exists()


@respx.mock
async def test_upload_failure_deletes_downloaded_video(tmp_path, monkeypatch):
    video, _seen = _patch_downloaded_video(monkeypatch, tmp_path)

    async def upload(**kwargs):
        return _UploadResponse(status_code=500, body="upload failed")

    monkeypatch.setattr(instagram_publisher, "_upload_resumable_binary", upload)
    _mock_resumable_create()

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
    )

    assert result.success is False
    assert result.detail == "rupload: upload failed"
    assert not video.exists()


@respx.mock
async def test_poll_failure_deletes_downloaded_video(tmp_path, monkeypatch):
    video, _seen = _patch_downloaded_video(monkeypatch, tmp_path)
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "ERROR"})
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
    )

    assert result.success is False
    assert result.detail == (
        "status_poll: container status_code = ERROR; no status detail returned"
    )
    assert not video.exists()


@respx.mock
async def test_publish_failure_deletes_downloaded_video(tmp_path, monkeypatch):
    video, _seen = _patch_downloaded_video(monkeypatch, tmp_path)
    _mock_resumable_create()
    respx.get(f"{BASE}/{CONTAINER_ID}").mock(
        return_value=httpx.Response(200, json={"status_code": "FINISHED"})
    )
    respx.post(f"{BASE}/{IG_USER_ID}/media_publish").mock(
        return_value=httpx.Response(500, text="publish failed")
    )

    result = await publish_to_instagram(
        **_COMMON,
        poll_interval=0.01,
        poll_timeout=1.0,
    )

    assert result.success is False
    assert result.detail == "publish: publish failed"
    assert not video.exists()


@respx.mock
async def test_download_failure_detail_includes_host_status_and_body():
    respx.get(_COMMON["video_url"]).mock(
        return_value=httpx.Response(403, text="forbidden by upstream")
    )

    result = await publish_to_instagram(
        **_COMMON, poll_interval=0.01, poll_timeout=1.0
    )

    assert result.success is False
    assert result.detail is not None
    assert result.detail.startswith("download: GET cdn.example.com failed HTTP 403")
    assert "forbidden by upstream" in result.detail


async def test_ffprobe_unavailable_validation_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram_publisher.shutil, "which", lambda name: None)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake mp4 bytes")

    assert await ORIGINAL_VALIDATE_VIDEO(video) is None


async def test_ffprobe_invalid_codec_validation_fails(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake mp4 bytes")

    class Completed:
        returncode = 0
        stderr = ""
        stdout = (
            '{"format":{"format_name":"mov,mp4,m4a,3gp,3g2,mj2","duration":"10"},'
            '"streams":[{"codec_type":"video","codec_name":"vp9","width":1080,'
            '"avg_frame_rate":"30/1"}]}'
        )

    monkeypatch.setattr(instagram_publisher.shutil, "which", lambda name: "/usr/bin/ffprobe")
    monkeypatch.setattr(instagram_publisher.subprocess, "run", lambda *a, **k: Completed())

    assert await ORIGINAL_VALIDATE_VIDEO(video) == (
        "video codec 'vp9' is not H.264/HEVC"
    )


def test_upload_streams_file_without_read_bytes(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake mp4 bytes")
    captured = {}

    def post(url, *, headers, content, timeout, follow_redirects):
        captured["url"] = url
        captured["headers"] = headers
        captured["content_has_read"] = hasattr(content, "read")
        return httpx.Response(200, json={"success": True})

    def fail_read_bytes(self):
        raise AssertionError("read_bytes should not be used for rupload")

    monkeypatch.setattr(instagram_publisher.httpx, "post", post)
    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    result = _upload_resumable_binary_sync(
        upload_uri=UPLOAD_URI,
        ig_access_token=ACCESS_TOKEN,
        video_path=video,
        timeout_seconds=30,
    )

    assert result.status_code == 200
    assert captured["url"] == UPLOAD_URI
    assert captured["headers"]["file_size"] == str(len(b"fake mp4 bytes"))
    assert captured["content_has_read"] is True


def test_upload_transport_sends_exact_body_length_without_chunking(tmp_path):
    video = tmp_path / "video.mp4"
    payload = b"0123456789abcdef" * 1024
    video.write_bytes(payload)
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            content_length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(content_length)
            captured["headers"] = dict(self.headers)
            captured["body"] = body
            response_body = b'{"success": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, *args):
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        result = _upload_resumable_binary_sync(
            upload_uri=f"http://127.0.0.1:{server.server_port}/upload",
            ig_access_token=ACCESS_TOKEN,
            video_path=video,
            timeout_seconds=30,
        )
    finally:
        thread.join(timeout=5)
        server.server_close()

    assert result.status_code == 200
    assert captured["headers"]["Content-Length"] == str(len(payload))
    assert captured["headers"]["file_size"] == str(len(payload))
    assert captured["headers"]["offset"] == "0"
    assert "Transfer-Encoding" not in captured["headers"]
    assert captured["body"] == payload


async def test_prepare_video_uses_valid_original_when_under_target(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 1024)

    def fail_run(*args, **kwargs):
        raise AssertionError("valid small video should not invoke ffmpeg")

    monkeypatch.setattr(instagram_publisher.subprocess, "run", fail_run)

    result = await ORIGINAL_PREPARE_VIDEO(video)

    assert result == video
    assert result.exists()


async def test_prepare_video_outputs_vertical_reels_safe_file(tmp_path, monkeypatch):
    ffmpeg = instagram_publisher.shutil.which("ffmpeg")
    ffprobe = instagram_publisher.shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg/ffprobe unavailable")

    source = tmp_path / "landscape.mp4"
    create = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=150x108:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert create.returncode == 0, create.stderr

    async def validate(path):
        if path == source:
            return "source requires normalization"
        return await ORIGINAL_VALIDATE_VIDEO(path)

    monkeypatch.setattr(instagram_publisher, "_validate_video", validate)
    prepared = await ORIGINAL_PREPARE_VIDEO(source)
    try:
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                str(prepared),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.returncode == 0, probe.stderr
        payload = json.loads(probe.stdout)
        video = next(s for s in payload["streams"] if s["codec_type"] == "video")
        audio = next(s for s in payload["streams"] if s["codec_type"] == "audio")
        assert video["codec_name"] == "h264"
        assert video["width"] == 1080
        assert video["height"] == 1920
        assert video["pix_fmt"] == "yuv420p"
        assert audio["codec_name"] == "aac"
        assert audio["sample_rate"] == "48000"
    finally:
        prepared.unlink(missing_ok=True)


async def test_prepare_video_fails_when_ffmpeg_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram_publisher, "_MAX_REEL_BYTES", 1024)
    monkeypatch.setattr(instagram_publisher, "_TARGET_REEL_BYTES", 900)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * (instagram_publisher._MAX_REEL_BYTES + 1))
    monkeypatch.setattr(instagram_publisher.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="ffmpeg is unavailable"):
        await ORIGINAL_PREPARE_VIDEO(video)

    assert video.exists()


async def test_prepare_video_runs_one_pass_and_replaces_file(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram_publisher, "_MAX_REEL_BYTES", 1024)
    monkeypatch.setattr(instagram_publisher, "_TARGET_REEL_BYTES", 900)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * (instagram_publisher._MAX_REEL_BYTES + 1))
    output_holder: dict[str, Path] = {}
    commands: list[list[str]] = []

    monkeypatch.setattr(
        instagram_publisher.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    async def fake_duration(path):
        return 600.0

    monkeypatch.setattr(
        instagram_publisher, "_probe_duration_seconds", fake_duration
    )

    class Completed:
        def __init__(self, returncode=0, stderr=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        out = Path(cmd[-1])
        out.write_bytes(b"transcoded")
        output_holder["out"] = out
        return Completed()

    monkeypatch.setattr(instagram_publisher.subprocess, "run", fake_run)

    result = await ORIGINAL_PREPARE_VIDEO(video)

    assert len(commands) == 1
    assert "-pass" not in commands[0]
    assert "-hide_banner" in commands[0]
    assert result != video
    assert result.exists()
    assert result == output_holder["out"]
    assert not video.exists()
    result.unlink(missing_ok=True)


async def test_prepare_video_retries_when_output_still_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram_publisher, "_MAX_REEL_BYTES", 1024)
    monkeypatch.setattr(instagram_publisher, "_TARGET_REEL_BYTES", 20)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * (instagram_publisher._MAX_REEL_BYTES + 1))
    commands: list[list[str]] = []
    outputs = 0

    monkeypatch.setattr(
        instagram_publisher.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    async def fake_duration(path):
        return 600.0

    monkeypatch.setattr(
        instagram_publisher, "_probe_duration_seconds", fake_duration
    )

    class Completed:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kwargs):
        nonlocal outputs
        commands.append(cmd)
        out = Path(cmd[-1])
        outputs += 1
        if outputs == 1:
            out.write_bytes(b"x" * 25)
        else:
            out.write_bytes(b"transcoded")
        return Completed()

    monkeypatch.setattr(instagram_publisher.subprocess, "run", fake_run)

    result = await ORIGINAL_PREPARE_VIDEO(video)

    assert len(commands) == 2
    assert result != video
    assert result.exists()
    assert result.stat().st_size <= instagram_publisher._TARGET_REEL_BYTES
    assert not video.exists()
    result.unlink(missing_ok=True)


async def test_prepare_video_falls_back_to_valid_original_when_normalization_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(instagram_publisher, "_MAX_REEL_BYTES", 100)
    monkeypatch.setattr(instagram_publisher, "_TARGET_REEL_BYTES", 50)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 80)

    monkeypatch.setattr(
        instagram_publisher.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    async def fake_duration(path):
        return 600.0

    monkeypatch.setattr(
        instagram_publisher, "_probe_duration_seconds", fake_duration
    )

    class Completed:
        returncode = 1
        stderr = "encoder exploded"
        stdout = ""

    monkeypatch.setattr(
        instagram_publisher.subprocess, "run", lambda *a, **k: Completed()
    )

    result = await ORIGINAL_PREPARE_VIDEO(video)

    assert result == video
    assert video.exists()


async def test_prepare_video_fails_when_ffmpeg_fails_for_invalid_source(
    tmp_path, monkeypatch
):
    video = tmp_path / "video.mp4"
    original_size = 80
    video.write_bytes(b"x" * original_size)

    async def invalid_source(path):
        return "video codec 'vp9' is not H.264/HEVC"

    monkeypatch.setattr(instagram_publisher, "_validate_video", invalid_source)
    monkeypatch.setattr(
        instagram_publisher.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    async def fake_duration(path):
        return 600.0

    monkeypatch.setattr(
        instagram_publisher, "_probe_duration_seconds", fake_duration
    )

    class Completed:
        returncode = 1
        stderr = (
            "ffmpeg version 7.1.4\n"
            "configuration: --lots-of-noise\n"
            "Input #0, mov,mp4,m4a,3gp,3g2,mj2\n"
            "encoder exploded"
        )
        stdout = ""

    monkeypatch.setattr(
        instagram_publisher.subprocess, "run", lambda *a, **k: Completed()
    )

    with pytest.raises(RuntimeError) as exc:
        await ORIGINAL_PREPARE_VIDEO(video)

    assert "original invalid" in str(exc.value)
    assert "ffmpeg failed" in str(exc.value)
    assert "encoder exploded" in str(exc.value)
    assert "configuration:" not in str(exc.value)
    assert video.exists()
    assert video.stat().st_size == original_size


def test_upload_error_detail_includes_meta_codes():
    detail = instagram_publisher._upload_response_detail(
        _UploadResponse(
            status_code=400,
            body=(
                '{"error":{"message":"Bad media","code":9004,'
                '"error_subcode":2207052,"fbtrace_id":"abc"}}'
            ),
        )
    )

    assert detail == "Bad media code=9004 subcode=2207052 fbtrace_id=abc"
