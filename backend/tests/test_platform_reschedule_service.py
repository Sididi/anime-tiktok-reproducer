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


def test_notify_returns_skipped_for_unsupported_platform():
    project = Project(id="p1")
    result = PlatformRescheduleService.notify(
        project, "tiktok", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
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
