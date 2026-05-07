from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.models import Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


_NOW = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


@pytest.fixture
def isolated_scheduler(tmp_path: Path, monkeypatch):
    """Reset accounts cache + projects dir + freeze time."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        """\
accounts:
  acc_a:
    name: "Account A"
    language: "fr"
    device: "poco"
    slots: ["12:00", "14:00", "18:00"]
    youtube:
      refresh_token: "tok"
      channel_id: "ch_a"
    tiktok:
      slots: ["12:00", "14:00", "18:00", "21:00"]
  acc_b:
    name: "Account B"
    language: "fr"
    device: "poco"
    slots: ["14:00", "18:00"]
    youtube:
      refresh_token: "tok"
      channel_id: "ch_a"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    monkeypatch.setattr(
        "app.services.scheduling_service.datetime", _FixedDateTime
    )
    AccountService.invalidate()
    yield
    AccountService.invalidate()


def test_find_free_slots_after_returns_chronological_chips(isolated_scheduler):
    slots = SchedulingService.find_free_slots_after(
        account_id="acc_a",
        platform="tiktok",
        after=_NOW,
        limit=5,
    )
    assert len(slots) == 5
    assert all(s.available for s in slots)
    assert [s.slot.hour for s in slots[:4]] == [14, 18, 21, 12]


def test_find_free_slots_after_marks_taken_slots(isolated_scheduler):
    project = Project(id="p1", scheduled_account_id="acc_a")
    project.platform_schedules = {
        "tiktok": __import__("app").models.PlatformSchedule(
            slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            scheduled_at=datetime(2026, 5, 7, 14, 11, tzinfo=timezone.utc),
        )
    }
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)

    slots = SchedulingService.find_free_slots_after(
        account_id="acc_a",
        platform="tiktok",
        after=_NOW,
        limit=5,
    )
    taken = [s for s in slots if not s.available]
    assert len(taken) == 1
    assert taken[0].slot == datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    assert taken[0].taken_by_project_id == "p1"


def test_resolve_anchor_resolves_each_platform_to_first_free_slot(isolated_scheduler):
    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides=None,
    )
    yt = result.resolved["youtube"]
    assert yt.slot == datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    assert yt.available is True
    assert result.conflicts == []


def test_resolve_anchor_falls_back_to_next_slot_when_taken(isolated_scheduler):
    other = Project(id="other", scheduled_account_id="acc_a")
    other.platform_schedules = {
        "youtube": __import__("app").models.PlatformSchedule(
            slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
            scheduled_at=datetime(2026, 5, 7, 14, 7, tzinfo=timezone.utc),
        )
    }
    ProjectService.get_project_dir(other.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(other)

    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides=None,
    )
    yt = result.resolved["youtube"]
    assert yt.slot == datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    assert yt.available is True


def test_resolve_anchor_uses_overrides(isolated_scheduler):
    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides={"youtube": datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)},
    )
    yt = result.resolved["youtube"]
    assert yt.slot == datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)


def test_resolve_anchor_invalid_override_returns_conflict(isolated_scheduler):
    result = SchedulingService.resolve_anchor(
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        overrides={"youtube": datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc)},
    )
    assert any(c.platform == "youtube" for c in result.conflicts)


def test_reserve_anchor_persists_platform_schedules(isolated_scheduler):
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    result = SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    assert "tiktok" in result
    assert "youtube" in result
    reloaded = ProjectService.load("proj")
    assert reloaded.scheduled_account_id == "acc_a"
    assert "tiktok" in reloaded.platform_schedules


def test_reserve_anchor_idempotent_when_called_twice(isolated_scheduler):
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    first = SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    second = SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    assert first["tiktok"].slot == second["tiktok"].slot
    assert first["tiktok"].scheduled_at == second["tiktok"].scheduled_at


def test_reserve_anchor_raises_on_conflict(isolated_scheduler):
    other = Project(
        id="other",
        scheduled_account_id="acc_a",
        platform_schedules={
            "tiktok": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 7, 14, 8, tzinfo=timezone.utc),
            )
        },
    )
    ProjectService.get_project_dir(other.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(other)
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    with pytest.raises(ValueError) as exc:
        SchedulingService.reserve_anchor(
            project_id="proj",
            account_id="acc_a",
            tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
        )
    assert "tiktok" in str(exc.value)


def test_reschedule_anchor_swaps_existing_reservations(isolated_scheduler):
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    SchedulingService.reserve_anchor(
        project_id="proj",
        account_id="acc_a",
        tiktok_slot=datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc),
    )
    new_anchor = datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc)
    SchedulingService.reschedule_anchor(
        project_id="proj",
        tiktok_slot=new_anchor,
    )
    reloaded = ProjectService.load("proj")
    assert reloaded.platform_schedules["tiktok"].slot == new_anchor


def test_reschedule_platform_replaces_single_platform_slot(isolated_scheduler):
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    new_yt = datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
    sched = SchedulingService.reschedule_platform("proj", "youtube", new_yt)
    assert sched.slot == new_yt

    reloaded = ProjectService.load("proj")
    assert reloaded.platform_schedules["youtube"].slot == new_yt
    # tiktok unchanged
    assert reloaded.platform_schedules["tiktok"].slot == datetime(
        2026, 5, 7, 14, 0, tzinfo=timezone.utc
    )


def test_reschedule_platform_rejects_taken_slot(isolated_scheduler):
    other = Project(
        id="other",
        scheduled_account_id="acc_a",
        platform_schedules={
            "youtube": __import__("app").models.PlatformSchedule(
                slot=datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
                scheduled_at=datetime(2026, 5, 8, 14, 5, tzinfo=timezone.utc),
            )
        },
    )
    ProjectService.get_project_dir(other.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(other)
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    with pytest.raises(ValueError):
        SchedulingService.reschedule_platform(
            "proj", "youtube", datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc)
        )


def test_cancel_platform_slot_removes_only_one_platform(isolated_scheduler):
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    SchedulingService.cancel_platform_slot("proj", "youtube")
    reloaded = ProjectService.load("proj")
    assert "youtube" not in reloaded.platform_schedules
    assert "tiktok" in reloaded.platform_schedules


def test_cancel_all_slots_clears_everything(isolated_scheduler):
    project = Project(id="proj")
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    SchedulingService.reserve_anchor(
        "proj", "acc_a", datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    )
    SchedulingService.cancel_all_slots("proj")
    reloaded = ProjectService.load("proj")
    assert reloaded.platform_schedules == {}
    assert reloaded.scheduled_account_id is None
