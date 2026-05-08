from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project
from app.services.platform_reschedule_service import (
    NotificationResult,
    PlatformRescheduleService,
)


def test_notify_returns_skipped_for_unknown_platform():
    project = Project(id="p1")
    result = PlatformRescheduleService.notify(
        project, "myspace", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    assert result.status == "skipped"


def test_notify_skips_when_video_id_missing():
    project = Project(id="p1")
    result = PlatformRescheduleService.notify(
        project, "youtube", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    assert result.status == "skipped"


def test_notify_youtube_calls_videos_update_with_publish_at():
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"youtube": {"url": "https://youtu.be/abc12345"}}},
    )
    fake_youtube = MagicMock()
    update_call = MagicMock()
    fake_youtube.videos.return_value.update.return_value = update_call
    update_call.execute.return_value = {"id": "abc12345"}

    with patch(
        "app.services.platform_reschedule_service.AccountService.get_youtube_credentials"
    ), patch(
        "app.services.platform_reschedule_service.build", return_value=fake_youtube
    ):
        result = PlatformRescheduleService.notify(
            project, "youtube",
            datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
        )
    assert result.status == "ok"
    args, kwargs = fake_youtube.videos.return_value.update.call_args
    body = kwargs["body"]
    assert body["id"] == "abc12345"
    assert body["status"]["publishAt"].startswith("2026-05-08T14:00:00")
    assert body["status"]["privacyStatus"] == "private"


def test_cancel_youtube_clears_publish_at_and_sets_private():
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"youtube": {"url": "https://youtu.be/abc12345"}}},
    )
    fake_youtube = MagicMock()
    fake_youtube.videos.return_value.update.return_value.execute.return_value = {"id": "abc12345"}

    with patch(
        "app.services.platform_reschedule_service.AccountService.get_youtube_credentials"
    ), patch(
        "app.services.platform_reschedule_service.build", return_value=fake_youtube
    ):
        result = PlatformRescheduleService.cancel(project, "youtube")
    assert result.status == "ok"
    body = fake_youtube.videos.return_value.update.call_args.kwargs["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert "publishAt" not in body["status"]


def test_notify_facebook_posts_scheduled_publish_time(monkeypatch):
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"facebook": {"url": "https://www.facebook.com/page/videos/9876543210/"}}},
    )

    posted: dict = {}
    class FakeResp:
        status_code = 200
        def json(self) -> dict:
            return {"success": True}
        def raise_for_status(self) -> None:
            return None

    def fake_post(url, data=None, **kwargs):
        posted["url"] = url
        posted["data"] = data
        return FakeResp()

    monkeypatch.setattr(
        "app.services.platform_reschedule_service.AccountService.get_meta_credentials",
        lambda _id: type("C", (), {"facebook_page_access_token": "tok", "page_id": "p"})(),
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.post", fake_post
    )

    result = PlatformRescheduleService.notify(
        project, "facebook",
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
    )
    assert result.status == "ok"
    assert "9876543210" in posted["url"]
    assert posted["data"]["scheduled_publish_time"] == int(
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc).timestamp()
    )


def test_cancel_facebook_marks_unpublished(monkeypatch):
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"facebook": {"url": "https://www.facebook.com/page/videos/9876543210/"}}},
    )

    posted: dict = {}
    class FakeResp:
        status_code = 200
        def json(self): return {"success": True}
        def raise_for_status(self): return None
    def fake_post(url, data=None, **kwargs):
        posted["data"] = data
        return FakeResp()
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.AccountService.get_meta_credentials",
        lambda _id: type("C", (), {"facebook_page_access_token": "tok", "page_id": "p"})(),
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.post", fake_post)

    result = PlatformRescheduleService.cancel(project, "facebook")
    assert result.status == "ok"
    assert posted["data"]["published"] == "false"


def test_notify_instagram_patches_server_endpoint(monkeypatch):
    project = Project(
        id="p1",
        scheduled_account_id="acc_a",
        upload_last_result={"platforms": {"instagram": {"url": "https://instagram.com/p/abc"}}},
    )

    captured: dict = {}
    class FakeResp:
        status_code = 200
        def raise_for_status(self): return None
    def fake_patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://server.example.com",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "secret",
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.patch", fake_patch)

    result = PlatformRescheduleService.notify(
        project, "instagram",
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
    )
    assert result.status == "ok"
    assert captured["url"] == "https://server.example.com/api/internal/jobs/p1/slot"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    # Per-platform reschedule must NOT clobber the canonical slot_time on
    # the server — it only updates the per-platform map entry.
    assert "slot_time" not in captured["json"]
    assert captured["json"]["platform_scheduled_at"] == {
        "instagram": "2026-05-08T14:00:00+00:00",
    }


def test_notify_tiktok_patches_server_endpoint(monkeypatch):
    """TT reschedule must reach the server so the reminder fires at the new
    time — TT is manually posted but the /server/ pings the operator."""
    project = Project(id="p1", scheduled_account_id="acc_a")

    captured: dict = {}
    class FakeResp:
        status_code = 200
        def raise_for_status(self): return None
    def fake_patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://server.example.com",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "secret",
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.patch", fake_patch)

    result = PlatformRescheduleService.notify(
        project, "tiktok",
        datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
    )
    assert result.status == "ok"
    assert captured["url"] == "https://server.example.com/api/internal/jobs/p1/slot"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert "slot_time" not in captured["json"]
    assert captured["json"]["platform_scheduled_at"] == {
        "tiktok": "2026-05-08T14:00:00+00:00",
    }


def test_cancel_tiktok_marks_reminder_cancelled(monkeypatch):
    """TT cancel keeps the job on the server (so other platforms' reminders
    survive) but flips reminder_cancelled so the TT ping is skipped."""
    project = Project(id="p1", scheduled_account_id="acc_a")

    captured: dict = {}
    class FakeResp:
        status_code = 200
        def raise_for_status(self): return None
    def fake_patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://server.example.com",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "secret",
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.patch", fake_patch)

    result = PlatformRescheduleService.cancel(project, "tiktok")
    assert result.status == "ok"
    assert captured["url"] == "https://server.example.com/api/internal/jobs/p1/slot"
    assert captured["json"] == {"reminder_cancelled": True}


def test_cancel_instagram_deletes_server_job(monkeypatch):
    project = Project(id="p1", scheduled_account_id="acc_a")

    captured: dict = {}
    class FakeResp:
        status_code = 204
        def raise_for_status(self): return None
    def fake_delete(url, headers=None, timeout=None):
        captured["url"] = url
        return FakeResp()
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://server.example.com",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "secret",
    )
    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.delete", fake_delete)

    result = PlatformRescheduleService.cancel(project, "instagram")
    assert result.status == "ok"
    assert captured["url"] == "https://server.example.com/api/internal/jobs/p1"
