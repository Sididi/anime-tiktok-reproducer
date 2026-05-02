"""Tests for app.services.instagram_publisher using respx-mocked Graph API."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.services import instagram_publisher
from app.services.instagram_publisher import _UploadResponse, _upload_headers, publish_to_instagram

BASE = "https://graph.facebook.com/v25.0"
IG_USER_ID = "ig_user_123"
ACCESS_TOKEN = "access_token_abc"
CONTAINER_ID = "container_42"
MEDIA_ID = "media_99"
PERMALINK_URL = "https://www.instagram.com/reel/Cxxx/"
UPLOAD_URI = "https://rupload.facebook.com/ig-api-upload/v25.0/container_42"

# Common kwargs shared by all tests
_COMMON = dict(
    ig_user_id=IG_USER_ID,
    ig_access_token=ACCESS_TOKEN,
    caption="Test caption #anime",
    video_url="https://cdn.example.com/video.mp4",
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


@pytest.fixture(autouse=True)
def mock_resumable_upload(monkeypatch):
    async def upload(**kwargs):
        return _UploadResponse(status_code=200, body='{"success": true}')

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


def test_upload_headers_include_entity_length_without_transfer_encoding():
    headers = _upload_headers(ACCESS_TOKEN, len(b"fake mp4 bytes"))

    assert headers["Content-Length"] == str(len(b"fake mp4 bytes"))
    assert headers["X-Entity-Length"] == str(len(b"fake mp4 bytes"))
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
    assert "create container failed" in result.detail


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
        "container status_code = ERROR; status = The video URL is not reachable."
    )


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
    assert result.detail == "poll timeout"


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
    assert "publish failed" in result.detail


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
