"""Instagram thumb_offset scaling for the thumbnail feature."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.upload_phase import UploadPhaseService
from app.services.social_upload_service import PlatformUploadResult
from app.services.thumbnail_service import ThumbnailCandidate, ThumbnailService
from app.services.project_service import ProjectService


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


# ---- processing-aware thumbnail set (YouTube Shorts cover race fix) ----

class _FakeYouTubeProcessing:
    """videos().list() stub; responses are injected via _execute_google_request."""

    def videos(self):
        return self

    def list(self, **kwargs):
        return {"list_kwargs": kwargs}


def _processing_response(status: str, availability: str | None = "available") -> dict:
    details: dict = {"processingStatus": status}
    if availability is not None:
        details["thumbnailsAvailability"] = availability
    return {"items": [{"processingDetails": details}]}


def _patch_processing_polls(monkeypatch, responses):
    """Each poll pops the next response; exceptions in the list are raised."""
    queue = list(responses)

    def fake_exec(cls, youtube, request, **kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    from app.services.social_upload_service import SocialUploadService as svc
    monkeypatch.setattr(svc, "_execute_google_request", classmethod(fake_exec))


def _patch_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.services.social_upload_service.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    return sleeps


def test_wait_for_processing_returns_after_success(monkeypatch):
    from app.services.social_upload_service import SocialUploadService

    _patch_processing_polls(monkeypatch, [
        _processing_response("processing"),
        _processing_response("processing"),
        _processing_response("succeeded"),
    ])
    sleeps = _patch_sleep(monkeypatch)
    status, availability, waited = SocialUploadService._wait_for_youtube_processing(
        _FakeYouTubeProcessing(), "vid123", None,
        poll_seconds=5.0, max_wait_seconds=60.0,
    )
    assert status == "succeeded"
    assert availability == "available"
    assert waited == 10.0
    assert sleeps == [5.0, 5.0]


def test_wait_for_processing_times_out_while_processing(monkeypatch):
    from app.services.social_upload_service import SocialUploadService

    _patch_processing_polls(monkeypatch, [_processing_response("processing")])
    _patch_sleep(monkeypatch)
    status, availability, waited = SocialUploadService._wait_for_youtube_processing(
        _FakeYouTubeProcessing(), "vid123", None,
        poll_seconds=5.0, max_wait_seconds=10.0,
    )
    assert status == "processing"
    assert waited == 10.0


def test_wait_for_processing_poll_errors_never_raise(monkeypatch):
    from app.services.social_upload_service import SocialUploadService

    _patch_processing_polls(monkeypatch, [RuntimeError("boom")])
    _patch_sleep(monkeypatch)
    status, availability, waited = SocialUploadService._wait_for_youtube_processing(
        _FakeYouTubeProcessing(), "vid123", None,
        poll_seconds=5.0, max_wait_seconds=10.0,
    )
    assert status == "unknown"
    assert availability is None


def test_wait_for_processing_terminal_failure_returns_immediately(monkeypatch):
    from app.services.social_upload_service import SocialUploadService

    _patch_processing_polls(monkeypatch, [_processing_response("failed", None)])
    sleeps = _patch_sleep(monkeypatch)
    status, _availability, waited = SocialUploadService._wait_for_youtube_processing(
        _FakeYouTubeProcessing(), "vid123", None,
        poll_seconds=5.0, max_wait_seconds=60.0,
    )
    assert status == "failed"
    assert waited == 0.0
    assert sleeps == []


def test_set_after_processing_success_is_silent(monkeypatch, tmp_path):
    from app.services.social_upload_service import SocialUploadService

    monkeypatch.setattr(
        SocialUploadService, "_wait_for_youtube_processing",
        classmethod(lambda cls, yt, vid, deadline: ("succeeded", "available", 40.0)),
    )
    calls = []
    monkeypatch.setattr(
        SocialUploadService, "_set_youtube_thumbnail",
        classmethod(lambda cls, yt, vid, image, deadline: calls.append(vid) or None),
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_youtube_thumbnail_after_processing(
        _FakeYouTubeProcessing(), "vid123", image, None,
    )
    assert warning is None
    assert calls == ["vid123"]


def test_set_after_processing_unconfirmed_status_noted_in_french(monkeypatch, tmp_path):
    from app.services.social_upload_service import SocialUploadService

    monkeypatch.setattr(
        SocialUploadService, "_wait_for_youtube_processing",
        classmethod(lambda cls, yt, vid, deadline: ("processing", None, 600.0)),
    )
    monkeypatch.setattr(
        SocialUploadService, "_set_youtube_thumbnail",
        classmethod(lambda cls, yt, vid, image, deadline: None),
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_youtube_thumbnail_after_processing(
        _FakeYouTubeProcessing(), "vid123", image, None,
    )
    assert warning is not None
    assert "traitement" in warning
    assert "processing" in warning


def test_set_after_processing_set_failure_takes_priority(monkeypatch, tmp_path):
    from app.services.social_upload_service import SocialUploadService

    monkeypatch.setattr(
        SocialUploadService, "_wait_for_youtube_processing",
        classmethod(lambda cls, yt, vid, deadline: ("processing", None, 600.0)),
    )
    monkeypatch.setattr(
        SocialUploadService, "_set_youtube_thumbnail",
        classmethod(lambda cls, yt, vid, image, deadline: "Miniature YouTube non appliquée: boom"),
    )
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    warning = SocialUploadService._set_youtube_thumbnail_after_processing(
        _FakeYouTubeProcessing(), "vid123", image, None,
    )
    assert warning == "Miniature YouTube non appliquée: boom"


# ---- scheduled Facebook Reel thumbnail attempt (post-YouTube-discovery) ----

def _patch_fb_thumbnail_call(monkeypatch, warning: str | None):
    from app.services.social_upload_service import SocialUploadService

    calls: list[dict] = []

    def fake_set(cls, *, session, base, video_id, token, image_path, deadline):
        calls.append({"video_id": video_id, "token": token, "image_path": image_path})
        return warning

    class _NullSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        SocialUploadService, "_set_facebook_video_thumbnail", classmethod(fake_set)
    )
    monkeypatch.setattr(
        SocialUploadService, "_create_upload_session", classmethod(lambda cls: _NullSession())
    )
    return calls


def test_scheduled_reel_thumbnail_attempted_silent_on_success(monkeypatch, tmp_path):
    from app.services.social_upload_service import SocialUploadService

    calls = _patch_fb_thumbnail_call(monkeypatch, None)
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    result = _result("facebook", "uploaded", "Reel programmé le jeudi")
    result.resource_id = "reel123"
    SocialUploadService._apply_scheduled_reel_thumbnail(result, "tok", image, None)
    assert calls == [{"video_id": "reel123", "token": "tok", "image_path": image}]
    assert result.detail == "Reel programmé le jeudi"


def test_scheduled_reel_thumbnail_failure_appends_warning(monkeypatch, tmp_path):
    from app.services.social_upload_service import SocialUploadService

    _patch_fb_thumbnail_call(monkeypatch, "Miniature Facebook non appliquée: denied")
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    result = _result("facebook", "uploaded", "Reel programmé le jeudi")
    result.resource_id = "reel123"
    SocialUploadService._apply_scheduled_reel_thumbnail(result, "tok", image, None)
    assert result.detail == "Reel programmé le jeudi; Miniature Facebook non appliquée: denied"


def test_scheduled_reel_thumbnail_skipped_without_upload_or_id(monkeypatch, tmp_path):
    from app.services.social_upload_service import SocialUploadService

    calls = _patch_fb_thumbnail_call(monkeypatch, None)
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")

    failed = _result("facebook", "failed", "boom")
    failed.resource_id = "reel123"
    SocialUploadService._apply_scheduled_reel_thumbnail(failed, "tok", image, None)

    no_id = _result("facebook", "uploaded")
    SocialUploadService._apply_scheduled_reel_thumbnail(no_id, "tok", image, None)

    missing = _result("facebook", "uploaded")
    missing.resource_id = "reel123"
    SocialUploadService._apply_scheduled_reel_thumbnail(
        missing, "tok", tmp_path / "absent.jpg", None
    )

    assert calls == []
    assert failed.detail == "boom"


# ---- cover_image_for (candidate-index cover resolution for execute_upload) ----

def test_cover_image_for_cache_hit_copies_composed_jpg(tmp_path, monkeypatch):
    cached = tmp_path / "cand_2.jpg"
    cached.write_bytes(b"\xff\xd8\xff\xd9composed")
    monkeypatch.setattr(
        ThumbnailService, "cached_frame_path",
        classmethod(lambda cls, pid, index: cached if index == 2 else None),
    )

    dest = tmp_path / "out" / "thumbnail.jpg"
    result = ThumbnailService.cover_image_for("proj1", 2, None, dest)

    assert result == dest
    assert dest.read_bytes() == cached.read_bytes()


_FAKE_CANDIDATE = ThumbnailCandidate(
    index=0,
    label="Scène 1 · début",
    timestamp_seconds=1.0,
    scene_index=0,
    position="start",
    episode="Some Anime - 01.mkv",
    source_timestamp_seconds=1.0,
)


def test_cover_image_for_miss_falls_back_to_output_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ThumbnailService, "cached_frame_path",
        classmethod(lambda cls, pid, index: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "load_final_timeline",
        classmethod(lambda cls, pid: object()),
    )
    monkeypatch.setattr(
        ProjectService, "load_matches", classmethod(lambda cls, pid: None)
    )
    monkeypatch.setattr(
        ThumbnailService, "compute_candidates",
        classmethod(lambda cls, transcription, matches: [_FAKE_CANDIDATE]),
    )
    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, pid: None))
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, candidate, library_type: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, candidate, drive_folder_id: None),
    )

    from PIL import Image
    from app.services.anime_matcher import AnimeMatcherService

    fake_image = Image.new("RGB", (1920, 1080), color="red")
    monkeypatch.setattr(
        AnimeMatcherService, "extract_frame",
        classmethod(lambda cls, video_path, timestamp: fake_image),
    )

    dest = tmp_path / "thumbnail.jpg"
    result = ThumbnailService.cover_image_for(
        "proj1", 0, tmp_path / "output.mp4", dest
    )

    assert result == dest
    assert dest.exists()


def test_cover_image_for_returns_none_when_all_sources_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ThumbnailService, "cached_frame_path",
        classmethod(lambda cls, pid, index: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "load_final_timeline",
        classmethod(lambda cls, pid: object()),
    )
    monkeypatch.setattr(
        ProjectService, "load_matches", classmethod(lambda cls, pid: None)
    )
    monkeypatch.setattr(
        ThumbnailService, "compute_candidates",
        classmethod(lambda cls, transcription, matches: [_FAKE_CANDIDATE]),
    )
    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, pid: None))
    monkeypatch.setattr(
        ThumbnailService, "_extract_local_clean_frame",
        classmethod(lambda cls, candidate, library_type: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "_extract_drive_clean_frame",
        classmethod(lambda cls, candidate, drive_folder_id: None),
    )

    dest = tmp_path / "thumbnail.jpg"
    result = ThumbnailService.cover_image_for("proj1", 0, None, dest)

    assert result is None
    assert not dest.exists()


def test_cover_image_for_missing_timeline_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ThumbnailService, "cached_frame_path",
        classmethod(lambda cls, pid, index: None),
    )
    monkeypatch.setattr(
        ThumbnailService, "load_final_timeline", classmethod(lambda cls, pid: None)
    )
    dest = tmp_path / "thumbnail.jpg"
    result = ThumbnailService.cover_image_for("proj1", 0, None, dest)
    assert result is None
