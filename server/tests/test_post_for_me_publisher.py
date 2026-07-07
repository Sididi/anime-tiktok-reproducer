"""Tests for the Post for Me TikTok publisher (httpx.MockTransport-based)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.models.job import TikTokPublishState
from app.services import post_for_me_publisher as pfm
from app.services.post_for_me_publisher import (
    _derive_tiktok_video_url,
    publish_to_tiktok,
)

BASE = "https://api.postforme.dev/v1"


class FakePfm:
    """Programmable fake of the PFM API + the video download host."""

    def __init__(self) -> None:
        self.video_bytes = b"\x00" * 1024
        self.upload_puts: list[bytes] = []
        self.created_posts: list[dict] = []
        # list of result-payloads returned per successive results poll
        self.results_sequence: list[list[dict]] = []
        self._results_calls = 0
        self.fail_create_post = False
        self.fail_upload = False

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        url = str(request.url)
        if url == "https://drive.example/video.mp4":
            return httpx.Response(200, content=self.video_bytes)
        if url == f"{BASE}/media/create-upload-url":
            return httpx.Response(200, json={
                "upload_url": "https://storage.example/signed-put",
                "media_url": "https://media.example/abc.mp4",
            })
        if url == "https://storage.example/signed-put":
            if self.fail_upload:
                return httpx.Response(500, text="storage error")
            self.upload_puts.append(request.read())
            return httpx.Response(200)
        if url == f"{BASE}/social-posts" and request.method == "POST":
            if self.fail_create_post:
                return httpx.Response(400, json={"error": "bad payload"})
            body = json.loads(request.read())
            self.created_posts.append(body)
            return httpx.Response(200, json={"id": "post_1", "status": "processing"})
        if url.startswith(f"{BASE}/social-post-results"):
            idx = min(self._results_calls, len(self.results_sequence) - 1)
            data = self.results_sequence[idx] if self.results_sequence else []
            self._results_calls += 1
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404, text=f"unexpected: {request.method} {url}")


@pytest.fixture
def fake(monkeypatch) -> FakePfm:
    fake = FakePfm()
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(pfm.httpx, "AsyncClient", client_factory)
    # the binary PUT goes through a sync httpx call in a thread; patch it too
    def fake_put_sync(*, upload_url, video_path, timeout_seconds):
        with httpx.Client(transport=transport) as c:
            with open(video_path, "rb") as f:
                r = c.put(upload_url, content=f.read())
            return r.status_code, r.text

    monkeypatch.setattr(pfm, "_put_file_sync", fake_put_sync)
    return fake


async def _publish(fake, tmp_path, **overrides):
    kwargs = dict(
        api_key="key",
        social_account_id="spc_1",
        caption="my caption",
        download_url="https://drive.example/video.mp4",
        poll_interval=0.0,
        poll_timeout=1.0,
        temp_dir=tmp_path,
    )
    kwargs.update(overrides)
    return await publish_to_tiktok(**kwargs)


async def test_happy_path(fake, tmp_path):
    fake.results_sequence = [
        [],
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"id": "tt1", "url": "https://tiktok.com/@a/video/1"},
          "error": None}],
    ]
    result = await _publish(fake, tmp_path)
    assert result.success is True
    assert result.url == "https://tiktok.com/@a/video/1"
    assert result.publish_state.stage == "published"
    # exactly one post created, with the tiktok configuration
    assert len(fake.created_posts) == 1
    body = fake.created_posts[0]
    assert body["social_accounts"] == ["spc_1"]
    assert body["caption"] == "my caption"
    assert body["media"] == [{"url": "https://media.example/abc.mp4"}]
    assert body["platform_configurations"]["tiktok"]["privacy_status"] == "public"
    # binary was uploaded once
    assert fake.upload_puts == [fake.video_bytes]


async def test_platform_options_forwarded(fake, tmp_path):
    fake.results_sequence = [[{"social_account_id": "spc_1", "success": True,
                               "platform_data": {"url": "u"}, "error": None}]]
    await _publish(
        fake, tmp_path,
        privacy_status="private", allow_comment=False,
        allow_duet=False, allow_stitch=False,
    )
    tiktok = fake.created_posts[0]["platform_configurations"]["tiktok"]
    assert tiktok == {
        "privacy_status": "private",
        "allow_comment": False,
        "allow_duet": False,
        "allow_stitch": False,
    }


async def test_failed_result_reports_error(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": False,
          "platform_data": {}, "error": {"message": "tiktok rejected"}}],
    ]
    result = await _publish(fake, tmp_path)
    assert result.success is False
    assert "tiktok rejected" in result.detail
    assert result.publish_state.stage == "failed"


async def test_poll_timeout_keeps_resumable_state(fake, tmp_path):
    fake.results_sequence = [[]]  # never a result
    result = await _publish(fake, tmp_path, poll_timeout=0.0)
    assert result.success is False
    assert "timeout" in result.detail
    assert result.publish_state.post_id == "post_1"
    assert result.publish_state.stage == "post_created"


async def test_resume_polls_existing_post_without_new_post(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "https://tiktok.com/v/9"}, "error": None}],
    ]
    state = TikTokPublishState(
        post_id="post_1", media_url="https://media.example/abc.mp4",
        stage="post_created", created_at=datetime.now(tz=UTC),
    )
    result = await _publish(fake, tmp_path, publish_state=state)
    assert result.success is True
    assert fake.created_posts == []      # double-post guard
    assert fake.upload_puts == []        # no re-download / re-upload


async def test_already_published_short_circuits(fake, tmp_path):
    state = TikTokPublishState(post_id="post_1", stage="published", url="https://t/v")
    result = await _publish(fake, tmp_path, publish_state=state)
    assert result.success is True
    assert result.url == "https://t/v"
    assert fake.created_posts == []


async def test_retry_after_failed_reuses_media_and_creates_new_post(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "https://t/v2"}, "error": None}],
    ]
    state = TikTokPublishState(
        post_id="post_old", media_url="https://media.example/abc.mp4",
        stage="failed", last_error="tiktok rejected",
    )
    result = await _publish(fake, tmp_path, publish_state=state)
    assert result.success is True
    assert len(fake.created_posts) == 1   # new post created
    assert fake.upload_puts == []         # media reused, no re-upload
    assert fake.created_posts[0]["media"] == [{"url": "https://media.example/abc.mp4"}]


async def test_create_post_http_error_is_failure(fake, tmp_path):
    fake.fail_create_post = True
    result = await _publish(fake, tmp_path)
    assert result.success is False
    assert "create_post" in result.detail


async def test_upload_failure_is_failure(fake, tmp_path):
    fake.fail_upload = True
    result = await _publish(fake, tmp_path)
    assert result.success is False
    assert "upload" in result.detail


async def test_progress_callback_receives_states(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {"url": "u"}, "error": None}],
    ]
    seen: list[str] = []

    async def cb(state):
        seen.append(state.stage)

    await _publish(fake, tmp_path, progress_callback=cb)
    assert "media_uploaded" in seen
    assert "post_created" in seen
    assert seen[-1] == "published"


def test_derive_url_constructs_permalink_from_embedded_id():
    pd = {
        "id": "v_pub_url~v2-1.7659653399897655318",
        "url": "https://www.tiktok.com/@animespm2002",
    }
    assert _derive_tiktok_video_url(pd) == (
        "https://www.tiktok.com/@animespm2002/video/7659653399897655318"
    )


def test_derive_url_passes_through_existing_video_url():
    pd = {"id": "anything", "url": "https://www.tiktok.com/@a/video/12345"}
    assert _derive_tiktok_video_url(pd) == "https://www.tiktok.com/@a/video/12345"


def test_derive_url_none_when_id_not_video_id():
    # trailing segment is not an 18-19 digit id
    pd = {"id": "v_pub_url~v2-1.abc", "url": "https://www.tiktok.com/@a"}
    assert _derive_tiktok_video_url(pd) is None


def test_derive_url_none_when_id_wrong_length():
    pd = {"id": "v_pub_url~v2-1.123", "url": "https://www.tiktok.com/@a"}
    assert _derive_tiktok_video_url(pd) is None


def test_derive_url_none_when_url_missing_username():
    pd = {"id": "v_pub_url~v2-1.7659653399897655318", "url": ""}
    assert _derive_tiktok_video_url(pd) is None


def test_derive_url_accepts_20_digit_upper_bound():
    # 19 digits is the accepted upper bound; 20 digits must be rejected.
    pd = {"id": "v_pub_url~v2-1.76596533998976553180", "url": "https://www.tiktok.com/@a"}
    assert _derive_tiktok_video_url(pd) is None


def test_derive_url_none_when_id_is_non_ascii_digits():
    # Fullwidth digits satisfy str.isdigit() but are not ASCII; must be rejected.
    non_ascii_digits = "１２３４５６７８９０１２３４５６７８"
    assert len(non_ascii_digits) == 18
    pd = {"id": f"v_pub_url~v2-1.{non_ascii_digits}", "url": "https://www.tiktok.com/@a"}
    assert _derive_tiktok_video_url(pd) is None


async def test_publish_returns_constructed_video_url(fake, tmp_path):
    fake.results_sequence = [
        [{"social_account_id": "spc_1", "success": True,
          "platform_data": {
              "id": "v_pub_url~v2-1.7659653399897655318",
              "url": "https://www.tiktok.com/@animespm2002",
          },
          "error": None}],
    ]
    result = await _publish(fake, tmp_path)
    assert result.success is True
    assert result.url == (
        "https://www.tiktok.com/@animespm2002/video/7659653399897655318"
    )
    assert result.publish_state.url == result.url
