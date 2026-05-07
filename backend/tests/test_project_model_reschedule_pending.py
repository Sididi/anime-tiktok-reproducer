from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project


def test_reschedule_pending_defaults_to_empty_dict():
    project = Project(id="p1")
    assert project.reschedule_pending == {}


def test_reschedule_pending_round_trips_through_json():
    payload = {
        "youtube": {
            "target_scheduled_at": datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            "retries": 2,
            "last_error": "503 Service Unavailable",
            "last_attempt_at": datetime(2026, 5, 7, 14, 5, tzinfo=timezone.utc),
        }
    }
    project = Project(id="p1", reschedule_pending=payload)
    dumped = project.model_dump(mode="json")
    assert "reschedule_pending" in dumped
    restored = Project.model_validate(dumped)
    assert restored.reschedule_pending["youtube"]["retries"] == 2
    assert restored.reschedule_pending["youtube"]["last_error"] == "503 Service Unavailable"


def test_legacy_project_json_without_field_loads():
    project = Project.model_validate({"id": "p1"})
    assert project.reschedule_pending == {}
