from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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
