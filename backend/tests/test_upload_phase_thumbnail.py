"""Instagram thumb_offset scaling for the thumbnail feature."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.upload_phase import UploadPhaseService
from app.services.social_upload_service import PlatformUploadResult
from app.services.thumbnail_service import ThumbnailService


def test_cleanup_stale_thumbnail_cache_removes_old_keeps_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ThumbnailService, "_THUMBS_CACHE_DIR", tmp_path)

    old_project = tmp_path / "old-project"
    old_project.mkdir()
    (old_project / "marker.txt").write_text("x")
    old_time = time.time() - UploadPhaseService._SOURCE_CACHE_MAX_AGE_SECONDS - 60
    os.utime(old_project, (old_time, old_time))

    fresh_project = tmp_path / "fresh-project"
    fresh_project.mkdir()
    (fresh_project / "marker.txt").write_text("y")

    UploadPhaseService.cleanup_stale_thumbnail_cache()

    assert not old_project.exists()
    assert fresh_project.exists()


def _result(platform: str, status: str, detail: str | None = None) -> PlatformUploadResult:
    return PlatformUploadResult(platform=platform, status=status, detail=detail)


def test_extraction_warning_appended_to_uploaded_youtube_no_prior_detail():
    result = _result("youtube", "uploaded")
    UploadPhaseService._apply_thumbnail_extraction_warning(result)
    assert result.detail == "Miniature non appliquée: extraction de l'image impossible"


def test_extraction_warning_appended_to_uploaded_facebook_with_prior_detail():
    result = _result("facebook", "uploaded", detail="Publié avec succès")
    UploadPhaseService._apply_thumbnail_extraction_warning(result)
    assert result.detail == (
        "Publié avec succès; Miniature non appliquée: extraction de l'image impossible"
    )


def test_extraction_warning_does_not_touch_skipped_or_failed_results():
    skipped = _result("youtube", "skipped", detail="not configured")
    failed = _result("facebook", "failed", detail="boom")
    UploadPhaseService._apply_thumbnail_extraction_warning(skipped)
    UploadPhaseService._apply_thumbnail_extraction_warning(failed)
    assert skipped.detail == "not configured"
    assert failed.detail == "boom"


def test_extraction_warning_does_not_touch_other_platforms():
    tiktok = _result("tiktok", "uploaded", detail=None)
    instagram = _result("instagram", "uploaded", detail="ok")
    UploadPhaseService._apply_thumbnail_extraction_warning(tiktok)
    UploadPhaseService._apply_thumbnail_extraction_warning(instagram)
    assert tiktok.detail is None
    assert instagram.detail == "ok"


def test_thumb_offset_passthrough_no_speed():
    assert UploadPhaseService._instagram_thumb_offset(2350, None, 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "1.0", 90.0) == 2350


def test_thumb_offset_scaled_when_sped_up():
    # 10s into the original maps to 8s in a 1.25x sped-up artifact
    assert UploadPhaseService._instagram_thumb_offset(10_000, "1.25", 90.0) == 8000


def test_thumb_offset_clamped_to_max_duration():
    # cut video: offset can never exceed the prepared duration (minus margin)
    assert UploadPhaseService._instagram_thumb_offset(95_000, None, 90.0) == 89_500


def test_thumb_offset_garbage_speed_treated_as_one():
    assert UploadPhaseService._instagram_thumb_offset(2350, "abc", 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "0.0", 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "-3", 90.0) == 2350


def test_thumb_offset_never_negative():
    assert UploadPhaseService._instagram_thumb_offset(0, None, 90.0) == 0


from app.services.social_upload_service import SocialUploadService


class _FakeThumbnails:
    def __init__(self, fail: bool):
        self._fail = fail
        self.calls: list[dict] = []

    def set(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("boom")
        return object()  # request object; execution is monkeypatched


class _FakeYouTube:
    def __init__(self, fail: bool = False):
        self._thumbnails = _FakeThumbnails(fail)

    def thumbnails(self):
        return self._thumbnails


def test_set_youtube_thumbnail_success_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        SocialUploadService, "_execute_google_request",
        classmethod(lambda cls, youtube, request, **kw: {}),
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    yt = _FakeYouTube()
    warning = SocialUploadService._set_youtube_thumbnail(yt, "vid123", image, None)
    assert warning is None
    assert yt.thumbnails().calls[0]["videoId"] == "vid123"


def test_set_youtube_thumbnail_failure_returns_warning(tmp_path, monkeypatch):
    def raise_it(cls, youtube, request, **kw):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(
        SocialUploadService, "_execute_google_request", classmethod(raise_it)
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_youtube_thumbnail(
        _FakeYouTube(), "vid123", image, None
    )
    assert warning is not None
    assert "Miniature YouTube" in warning


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.posts: list[dict] = []

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return _FakeResponse(self.status_code, {"error": {"message": "denied"}})


def test_set_facebook_thumbnail_success(tmp_path):
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    session = _FakeSession(200)
    warning = SocialUploadService._set_facebook_video_thumbnail(
        session=session,
        base="https://graph.facebook.com/v25.0",
        video_id="v1",
        token="tok",
        image_path=image,
        deadline=None,
    )
    assert warning is None
    assert session.posts[0]["url"] == "https://graph.facebook.com/v25.0/v1/thumbnails"
    assert session.posts[0]["data"]["is_preferred"] == "true"


def test_set_facebook_thumbnail_failure_returns_warning(tmp_path):
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_facebook_video_thumbnail(
        session=_FakeSession(400),
        base="https://graph.facebook.com/v25.0",
        video_id="v1",
        token="tok",
        image_path=image,
        deadline=None,
    )
    assert warning is not None
    assert "Miniature Facebook" in warning
