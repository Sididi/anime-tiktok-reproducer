"""TikTok-first scheduling: no platform ever posts before TikTok.

Covers the 2026-07 rework: shared per-project jitter, TikTok-anchored
reserve_all_platform_slots / cascade, loud failure on expired manual
schedules, and the tiktok_precedence guard on single-platform edits.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.astimezone(tz)


@pytest.fixture
def scheduler(tmp_path: Path, monkeypatch):
    """acc1: youtube slots 14:00, tiktok slots 20:00 — youtube's nearest slot
    falls BEFORE tiktok's, which is exactly the inversion the rework forbids."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        """\
accounts:
  acc1:
    name: "Acc 1"
    language: "fr"
    device: "poco"
    slots: ["14:00"]
    youtube:
      refresh_token: "tok"
      channel_id: "ch"
      slots: ["14:00"]
    tiktok:
      slots: ["20:00"]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    monkeypatch.setattr("app.services.scheduling_service.datetime", _FixedDateTime)
    AccountService.invalidate()
    yield "acc1"
    AccountService.invalidate()


def _make_project(pid: str, **kwargs) -> Project:
    project = Project(id=pid, **kwargs)
    ProjectService.get_project_dir(pid).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


TT_SLOT = datetime(2026, 7, 21, 20, 0, tzinfo=timezone.utc)
YT_SLOT_TODAY = datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)
YT_SLOT_TOMORROW = datetime(2026, 7, 22, 14, 0, tzinfo=timezone.utc)


# ------------------------------------------------- reserve_all_platform_slots

def test_reserve_all_platform_slots_is_tiktok_first(scheduler):
    _make_project("p1")
    results = SchedulingService.reserve_all_platform_slots(
        "p1", scheduler, ["youtube", "tiktok"]
    )
    assert results["tiktok"][0] == TT_SLOT
    # youtube's nearest free slot (today 14:00) is before tiktok — it must
    # jump to the first slot at or after the tiktok anchor.
    assert results["youtube"][0] == YT_SLOT_TOMORROW
    assert results["youtube"][0] >= results["tiktok"][0]
    assert results["youtube"][1] >= results["tiktok"][1]


def test_reserve_without_tiktok_keeps_nearest_slot(scheduler):
    _make_project("p1")
    results = SchedulingService.reserve_all_platform_slots("p1", scheduler, ["youtube"])
    assert results["youtube"][0] == YT_SLOT_TODAY


def test_shared_jitter_same_offset_for_all_platforms(scheduler):
    _make_project("p1")
    results = SchedulingService.reserve_all_platform_slots(
        "p1", scheduler, ["youtube", "tiktok"]
    )
    tt_offset = results["tiktok"][1] - results["tiktok"][0]
    yt_offset = results["youtube"][1] - results["youtube"][0]
    assert tt_offset == yt_offset
    assert abs(tt_offset) <= timedelta(minutes=30)


def test_stale_pre_tiktok_reservation_is_realigned(scheduler):
    # An old-logic reservation left youtube before tiktok; re-reserving must
    # pull youtube back at/after the tiktok anchor instead of reusing it.
    project = _make_project("p1", scheduled_account_id=scheduler)
    project.platform_schedules = {
        "youtube": PlatformSchedule(slot=YT_SLOT_TODAY, scheduled_at=YT_SLOT_TODAY),
        "tiktok": PlatformSchedule(slot=TT_SLOT, scheduled_at=TT_SLOT),
    }
    ProjectService.save(project)

    results = SchedulingService.reserve_all_platform_slots(
        "p1", scheduler, ["youtube", "tiktok"]
    )
    assert results["tiktok"][0] == TT_SLOT
    assert results["youtube"][0] == YT_SLOT_TOMORROW


# ----------------------------------------------------------- manual schedules

def test_valid_manual_schedule_is_reused_exactly(scheduler):
    manual_at = _NOW + timedelta(hours=3)
    project = _make_project("p1", scheduled_account_id=scheduler)
    project.platform_schedules = {
        "youtube": PlatformSchedule(slot=manual_at, scheduled_at=manual_at, manual=True),
        "tiktok": PlatformSchedule(slot=manual_at, scheduled_at=manual_at, manual=True),
    }
    ProjectService.save(project)

    results = SchedulingService.reserve_all_platform_slots(
        "p1", scheduler, ["youtube", "tiktok"]
    )
    assert results["tiktok"] == (manual_at, manual_at)
    assert results["youtube"] == (manual_at, manual_at)


def test_expired_manual_schedule_fails_loudly(scheduler):
    manual_at = _NOW + timedelta(minutes=5)  # inside the 30-min lead window
    project = _make_project("p1", scheduled_account_id=scheduler)
    project.platform_schedules = {
        "tiktok": PlatformSchedule(slot=manual_at, scheduled_at=manual_at, manual=True),
    }
    ProjectService.save(project)

    with pytest.raises(ValueError, match="manual_schedule_expired"):
        SchedulingService.reserve_all_platform_slots("p1", scheduler, ["tiktok"])


# ------------------------------------------------------------------- cascade

def test_compute_cascade_is_tiktok_first(scheduler, monkeypatch):
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (False, None)),
    )
    _make_project("urgent")

    result = SchedulingService.compute_cascade("urgent", scheduler)
    targets = {p.platform: p.target_slot for p in result.per_platform}
    assert targets["tiktok"] == TT_SLOT
    # youtube would anchor at today 14:00 on its own; tiktok-first pushes it
    # to the first slot at or after tiktok's anchor.
    assert targets["youtube"] == YT_SLOT_TOMORROW
    assert result.blockers == []


# ------------------------------------------------- single-platform edit guard

def _reserved_project(scheduler) -> Project:
    project = _make_project("p1", scheduled_account_id=scheduler)
    project.platform_schedules = {
        "youtube": PlatformSchedule(slot=YT_SLOT_TOMORROW, scheduled_at=YT_SLOT_TOMORROW),
        "tiktok": PlatformSchedule(slot=TT_SLOT, scheduled_at=TT_SLOT),
    }
    ProjectService.save(project)
    return project


def test_reschedule_platform_warns_when_moving_before_tiktok(scheduler):
    _reserved_project(scheduler)
    with pytest.raises(ValueError, match="tiktok_precedence"):
        SchedulingService.reschedule_platform("p1", "youtube", YT_SLOT_TODAY)
    # untouched on refusal
    saved = ProjectService.load("p1")
    assert saved.platform_schedules["youtube"].slot == YT_SLOT_TOMORROW


def test_reschedule_platform_allows_with_user_confirmation(scheduler):
    _reserved_project(scheduler)
    sched = SchedulingService.reschedule_platform(
        "p1", "youtube", YT_SLOT_TODAY, allow_before_tiktok=True
    )
    assert sched.slot == YT_SLOT_TODAY


def test_reschedule_tiktok_after_other_platform_warns(scheduler):
    _reserved_project(scheduler)
    late_tt = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)  # after youtube
    with pytest.raises(ValueError, match="tiktok_precedence"):
        SchedulingService.reschedule_platform("p1", "tiktok", late_tt)


def test_apply_switch_warns_when_moving_before_tiktok(scheduler, monkeypatch):
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (False, None)),
    )
    _reserved_project(scheduler)
    with pytest.raises(ValueError, match="tiktok_precedence"):
        SchedulingService.apply_switch(
            "p1", scheduler, "youtube", YT_SLOT_TODAY, "cascade", None
        )
    sched = SchedulingService.apply_switch(
        "p1", scheduler, "youtube", YT_SLOT_TODAY, "cascade", None,
        allow_before_tiktok=True,
    )
    assert sched.slot == YT_SLOT_TODAY
    assert ProjectService.load("p1").platform_schedules["youtube"].slot == YT_SLOT_TODAY


# ------------------------------------------- displaced projects (steal/cascade)

TT_SLOT_TOMORROW = datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc)


def _victim_project(scheduler) -> Project:
    """Occupies today's TikTok slot; its youtube (today 14:00) sits safely
    before TikTok until a displacement pushes TikTok to tomorrow."""
    project = _make_project(
        "victim", scheduled_account_id=scheduler, anime_name="Victim Anime"
    )
    project.platform_schedules = {
        "youtube": PlatformSchedule(slot=YT_SLOT_TODAY, scheduled_at=YT_SLOT_TODAY),
        "tiktok": PlatformSchedule(slot=TT_SLOT, scheduled_at=TT_SLOT),
    }
    ProjectService.save(project)
    return project


def _not_busy(monkeypatch):
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (False, None)),
    )


def test_compute_switch_reports_displaced_precedence_warning(scheduler, monkeypatch):
    _not_busy(monkeypatch)
    _victim_project(scheduler)
    _make_project("me")

    result = SchedulingService.compute_switch("me", scheduler, "tiktok", TT_SLOT)

    for plan in (result.cascade, result.next_free):
        assert [w.project_id for w in plan.precedence_warnings] == ["victim"]
        assert plan.precedence_warnings[0].platforms == ["youtube"]


def test_apply_switch_blocks_displaced_precedence_without_confirm(scheduler, monkeypatch):
    _not_busy(monkeypatch)
    _victim_project(scheduler)
    _make_project("me")

    with pytest.raises(ValueError, match="tiktok_precedence_displaced:Victim Anime"):
        SchedulingService.apply_switch("me", scheduler, "tiktok", TT_SLOT, "cascade", "victim")
    # nothing moved on refusal
    assert ProjectService.load("victim").platform_schedules["tiktok"].slot == TT_SLOT

    SchedulingService.apply_switch(
        "me", scheduler, "tiktok", TT_SLOT, "cascade", "victim",
        allow_before_tiktok=True,
    )
    assert ProjectService.load("me").platform_schedules["tiktok"].slot == TT_SLOT
    assert ProjectService.load("victim").platform_schedules["tiktok"].slot == TT_SLOT_TOMORROW


def test_apply_cascade_blocks_displaced_precedence_without_confirm(scheduler, monkeypatch):
    _not_busy(monkeypatch)
    _victim_project(scheduler)
    _make_project("urgent")

    preview = SchedulingService.compute_cascade("urgent", scheduler)
    tt = next(p for p in preview.per_platform if p.platform == "tiktok")
    assert [w.project_id for w in tt.precedence_warnings] == ["victim"]

    with pytest.raises(ValueError, match="tiktok_precedence_displaced:Victim Anime"):
        SchedulingService.apply_cascade("urgent", scheduler)
    assert ProjectService.load("victim").platform_schedules["tiktok"].slot == TT_SLOT

    SchedulingService.apply_cascade("urgent", scheduler, allow_before_tiktok=True)
    assert ProjectService.load("urgent").platform_schedules["tiktok"].slot == TT_SLOT
    assert ProjectService.load("victim").platform_schedules["tiktok"].slot == TT_SLOT_TOMORROW


def test_reserve_anchor_steal_blocks_displaced_precedence(scheduler, monkeypatch):
    from app.services.scheduling_service import StealSpec

    _not_busy(monkeypatch)
    _victim_project(scheduler)
    _make_project("me")

    with pytest.raises(ValueError, match="tiktok_precedence_displaced:Victim Anime"):
        SchedulingService.reserve_anchor(
            "me", scheduler, TT_SLOT,
            steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="victim")},
        )
    # zero side effects on refusal
    assert ProjectService.load("victim").platform_schedules["tiktok"].slot == TT_SLOT
    assert not (ProjectService.load("me").platform_schedules or {})

    schedules, _ = SchedulingService.reserve_anchor(
        "me", scheduler, TT_SLOT,
        steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="victim")},
        allow_before_tiktok=True,
    )
    assert schedules["tiktok"].slot == TT_SLOT
    assert ProjectService.load("victim").platform_schedules["tiktok"].slot == TT_SLOT_TOMORROW
