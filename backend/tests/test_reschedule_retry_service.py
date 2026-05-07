from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.models import Project
from app.services.platform_reschedule_service import NotificationResult
from app.services.project_service import ProjectService
from app.services.reschedule_retry_service import (
    RescheduleRetryService,
    _BACKOFF_STEPS,
)


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch):
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", pdir
    )
    return pdir


def _seed(pid: str, target: datetime, retries: int = 0, last_attempt: datetime | None = None):
    project = Project(id=pid)
    project.reschedule_pending = {
        "youtube": {
            "target_scheduled_at": target,
            "retries": retries,
            "last_error": "boom",
            "last_attempt_at": last_attempt or datetime.now(timezone.utc),
        }
    }
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)


def test_retry_clears_entry_on_success(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=0,
          last_attempt=datetime.now(timezone.utc) - timedelta(minutes=10))

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="ok"),
    ):
        asyncio.run(RescheduleRetryService.run_once())

    project = ProjectService.load("p1")
    assert project.reschedule_pending == {}


def test_retry_increments_retries_on_failure(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=0,
          last_attempt=datetime.now(timezone.utc) - timedelta(minutes=10))

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="pending_retry", error="503"),
    ):
        asyncio.run(RescheduleRetryService.run_once())

    project = ProjectService.load("p1")
    assert project.reschedule_pending["youtube"]["retries"] == 1
    assert project.reschedule_pending["youtube"]["last_error"] == "503"


def test_retry_alerts_after_5_failures(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=4,
          last_attempt=datetime.now(timezone.utc) - timedelta(hours=2))
    alerts: list = []

    async def fake_alert(text: str) -> None:
        alerts.append(text)

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="pending_retry", error="boom"),
    ), patch(
        "app.services.reschedule_retry_service._post_discord_alert", new=fake_alert
    ):
        asyncio.run(RescheduleRetryService.run_once())

    assert any("p1" in a and "youtube" in a for a in alerts)
    project = ProjectService.load("p1")
    # Entry retained for ops review
    assert "youtube" in project.reschedule_pending


def test_retry_skips_when_backoff_not_elapsed(project_dir):
    target = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    _seed("p1", target, retries=0,
          last_attempt=datetime.now(timezone.utc) - timedelta(seconds=10))

    with patch(
        "app.services.reschedule_retry_service.PlatformRescheduleService.notify",
        return_value=NotificationResult(status="ok"),
    ) as notify_mock:
        asyncio.run(RescheduleRetryService.run_once())
    notify_mock.assert_not_called()
