"""captions().insert must never sink an already-live YouTube upload.

Regression for 2026-08-18: a video was uploaded, processed and scheduled, but
the caption insert answered `videoNotFound` (the new id had not propagated to
the captions endpoint yet). The error escaped to the outer handler, so the
whole upload was reported failed with no id — hiding a live video and inviting
a duplicate retry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.social_upload_service import (  # noqa: E402
    SocialUploadService,
    _is_youtube_caption_retryable_error,
)


def _http_error(status: int, reason: str, message: str = "boom") -> HttpError:
    content = json.dumps(
        {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}
    ).encode("utf-8")
    return HttpError(httplib2.Response({"status": status}), content)


class _FakeYouTube:
    """Minimal stand-in: only captions().insert() is exercised."""

    def captions(self):
        return self

    def insert(self, **kwargs):
        return kwargs


@pytest.fixture
def srt(tmp_path: Path) -> Path:
    path = tmp_path / "subs.fr_FR.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nbonjour\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.services.social_upload_service.time.sleep", lambda _s: None)


def test_video_not_found_is_retryable():
    assert _is_youtube_caption_retryable_error(_http_error(404, "videoNotFound"))


def test_server_errors_are_retryable():
    assert _is_youtube_caption_retryable_error(_http_error(503, "backendError"))


def test_permission_error_is_not_retryable():
    assert not _is_youtube_caption_retryable_error(_http_error(403, "forbidden"))


def test_caption_retried_until_the_video_id_propagates(monkeypatch, srt):
    calls = {"n": 0}

    def fake_execute(cls, service, request, *, deadline, platform, operation):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(404, "videoNotFound")
        return {"id": "caption-id"}

    monkeypatch.setattr(
        SocialUploadService,
        "_execute_google_request",
        classmethod(fake_execute),
    )

    warning = SocialUploadService._insert_youtube_caption(
        _FakeYouTube(), "vid123", srt, "fr", None
    )

    assert warning is None
    assert calls["n"] == 3


def test_caption_failure_degrades_to_a_warning(monkeypatch, srt):
    def always_fail(cls, service, request, *, deadline, platform, operation):
        raise _http_error(
            404,
            "videoNotFound",
            "The video identified by the <code>videoId</code> parameter could not be found.",
        )

    monkeypatch.setattr(
        SocialUploadService,
        "_execute_google_request",
        classmethod(always_fail),
    )

    warning = SocialUploadService._insert_youtube_caption(
        _FakeYouTube(), "vid123", srt, "fr", None
    )

    assert warning is not None
    assert "Sous-titres YouTube non ajoutés" in warning
    assert "videoNotFound" in warning


def test_non_retryable_caption_error_stops_immediately(monkeypatch, srt):
    calls = {"n": 0}

    def forbidden(cls, service, request, *, deadline, platform, operation):
        calls["n"] += 1
        raise _http_error(403, "forbidden")

    monkeypatch.setattr(
        SocialUploadService,
        "_execute_google_request",
        classmethod(forbidden),
    )

    warning = SocialUploadService._insert_youtube_caption(
        _FakeYouTube(), "vid123", srt, "fr", None
    )

    assert calls["n"] == 1
    assert warning is not None


def test_orphan_hint_names_the_live_video():
    detail = SocialUploadService._with_orphan_video_hint("boom", "ageBtiDdtEc")
    assert "https://youtu.be/ageBtiDdtEc" in detail
    assert "ne pas relancer" in detail


def test_orphan_hint_is_a_no_op_without_a_video_id():
    assert SocialUploadService._with_orphan_video_hint("boom", None) == "boom"
