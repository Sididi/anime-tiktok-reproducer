from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.models import PlatformSchedule, Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


_NOW = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    cfg = tmp_path / "accounts.yaml"
    cfg.write_text("""\
accounts:
  acc_a:
    name: "A"
    language: "fr"
    device: "poco"
    slots: ["14:00", "18:00"]
    youtube:
      refresh_token: "tok"
    tiktok:
      slots: ["12:00", "14:00", "18:00"]
""", encoding="utf-8")
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr("app.services.account_service.settings.accounts_config_path", cfg)
    monkeypatch.setattr("app.services.scheduling_service.datetime", _FixedDateTime)
    AccountService.invalidate()

    from app.main import app  # noqa: PLC0415
    with TestClient(app) as c:
        yield c
    AccountService.invalidate()


def test_list_events_returns_filtered_events(client):
    project = Project(id="p1", anime_name="Show",
        scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": PlatformSchedule(
                slot=datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 18, 5, tzinfo=timezone.utc),
            )
        }
    )
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    r = client.get("/api/scheduling/events")
    assert r.status_code == 200
    events = r.json()["events"]
    assert any(e["project_id"] == "p1" and e["platform"] == "tiktok" for e in events)


def test_free_slots_endpoint(client):
    r = client.get("/api/scheduling/free-slots", params={
        "account_id": "acc_a", "platform": "tiktok",
        "after": _NOW.isoformat(), "limit": 4,
    })
    assert r.status_code == 200
    slots = r.json()["slots"]
    assert len(slots) == 4
    assert all("slot" in s and "available" in s for s in slots)
