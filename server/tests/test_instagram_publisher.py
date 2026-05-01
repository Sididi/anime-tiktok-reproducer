"""Tests for app.services.instagram_publisher using respx-mocked Graph API."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.services.instagram_publisher import publish_to_instagram

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


def _mock_resumable_upload():
    return respx.post(UPLOAD_URI).mock(return_value=httpx.Response(200, json={"success": True}))


@respx.mock
async def test_happy_path():
    """Full happy path: container → IN_PROGRESS → FINISHED → publish → permalink."""
    download_route = _mock_video_download()
    create_route = _mock_resumable_create()
    upload_route = _mock_resumable_upload()
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
    assert upload_route.called
    assert status_route.call_count == 2
    assert publish_route.called
    assert permalink_route.called


@respx.mock
async def test_polling_waits_multiple_ticks():
    """Status returns IN_PROGRESS 3 times before FINISHED — verifies multiple polls."""
    _mock_video_download()
    _mock_resumable_create()
    _mock_resumable_upload()
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
    _mock_resumable_upload()
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
    _mock_resumable_upload()
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
    _mock_resumable_upload()
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
    _mock_resumable_upload()
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
