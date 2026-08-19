"""Urgent-immediate flow: collision preview + deferred apply (2026-08)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.models.project import TikTokPfmState
from app.services.account_service import AccountService
from app.services.platform_reschedule_service import (
    NotificationResult,
    PlatformRescheduleService,
)
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService
from app.services.urgent_upload_service import UrgentUploadService

PARIS = ZoneInfo("Europe/Paris")


def paris(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=PARIS).astimezone(timezone.utc)


@pytest.fixture()
def acc(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(exist_ok=True)
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        """\
accounts:
  acc1:
    name: "Acc 1"
    language: "fr"
    device: "poco"
    slots: ["10:00", "14:00", "18:00"]
    youtube:
      refresh_token: "tok"
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
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (False, None)),
    )
    # No real platform notifications in tests.
    monkeypatch.setattr(
        PlatformRescheduleService,
        "notify",
        classmethod(lambda cls, project, platform, at: NotificationResult(status="skipped")),
    )
    AccountService.invalidate()
    return "acc1"


def _save_project(pid, account_id, schedules, title=None, **extra):
    project = Project(id=pid, anime_name=title or pid, **extra)
    project.scheduled_account_id = account_id
    project.platform_schedules = {
        p: PlatformSchedule(slot=dt, scheduled_at=dt, manual=manual)
        for p, (dt, manual) in schedules.items()
    }
    ProjectService.get_project_dir(pid).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


def _in(minutes: int) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(
        second=0, microsecond=0
    )


def _tomorrow(h):
    d = (datetime.now(PARIS) + timedelta(days=1)).date()
    return paris(d.year, d.month, d.day, h)


def test_preview_two_phases_and_window(acc):
    _save_project("near_tt", acc, {"tiktok": (_in(30), False)})
    _save_project("near_yt", acc, {"youtube": (_in(45), False)})
    _save_project("far_tt", acc, {"tiktok": (_in(120), False)})
    _save_project("urgent", acc, {})

    preview = UrgentUploadService.compute_preview("urgent", acc)
    assert preview["window_minutes"] == 60
    p1_ids = [e["project_id"] for e in preview["phase1"]]
    p2_ids = [e["project_id"] for e in preview["phase2"]]
    assert p1_ids == ["near_tt"]
    assert p2_ids == ["near_yt"]
    item = preview["phase1"][0]["items"][0]
    assert item["platform"] == "tiktok"
    assert item["movable"] is True
    # A TikTok inside the 15-min edit-lock window is movable best-effort.
    assert item["best_effort"] is False


def test_preview_tiktok_only_skips_phase2(acc):
    _save_project("near_yt", acc, {"youtube": (_in(30), False)})
    _save_project("near_tt", acc, {"tiktok": (_in(30), False)})
    _save_project("urgent", acc, {})

    preview = UrgentUploadService.compute_preview("urgent", acc, tiktok_only=True)
    assert [e["project_id"] for e in preview["phase1"]] == ["near_tt"]
    assert preview["phase2"] == []


def test_preview_classifies_unmovable_states(acc):
    _save_project(
        "published",
        acc,
        {"tiktok": (_in(20), False)},
        tiktok_pfm=TikTokPfmState(post_id="sp1", stage="published", url="https://t"),
    )
    _save_project(
        "processing",
        acc,
        {"tiktok": (_in(25), False)},
        tiktok_pfm=TikTokPfmState(post_id="sp2", stage="post_created"),
    )
    _save_project(
        "failed",
        acc,
        {"tiktok": (_in(30), False)},
        tiktok_pfm=TikTokPfmState(post_id="sp3", stage="failed"),
    )
    _save_project(
        "locked_but_movable",
        acc,
        {"tiktok": (_in(10), False)},
        tiktok_pfm=TikTokPfmState(post_id="sp4", stage="post_scheduled"),
    )
    _save_project("urgent", acc, {})

    preview = UrgentUploadService.compute_preview("urgent", acc)
    items = {
        e["project_id"]: e["items"][0] for e in preview["phase1"]
    }
    assert set(items) == {"published", "processing", "locked_but_movable"}
    assert items["published"]["reason"] == "unmovable_published"
    assert items["processing"]["reason"] == "unmovable_processing"
    assert items["locked_but_movable"]["movable"] is True
    assert items["locked_but_movable"]["best_effort"] is True


def test_preview_overdue_unpublished_is_window_passed(acc):
    _save_project("overdue", acc, {"youtube": (_in(-10), False)})
    _save_project("urgent", acc, {})

    preview = UrgentUploadService.compute_preview("urgent", acc)
    item = preview["phase2"][0]["items"][0]
    assert item["movable"] is False
    assert item["reason"] == "unmovable_window_passed"


def test_apply_anchor_shift_bypasses_edit_lock(acc):
    tt_at = _in(10)  # inside the 15-min edit lock: normal reschedule refuses
    victim = _save_project("victim", acc, {"tiktok": (tt_at, False)})
    _save_project("urgent", acc, {})
    new_slot = _tomorrow(10)

    result = UrgentUploadService.apply_plan(
        "urgent",
        acc,
        shifts=[
            {
                "project_id": "victim",
                "kind": "anchor",
                "tiktok_slot": new_slot.isoformat(),
                "expected_scheduled_at": {
                    "tiktok": victim.platform_schedules["tiktok"].scheduled_at.isoformat()
                },
            }
        ],
        own_reservations=None,
        tiktok_only=False,
        confirm_before_tiktok=True,
    )
    assert result["shifts"][0]["status"] == "ok"
    moved = ProjectService.load("victim")
    assert moved.platform_schedules["tiktok"].slot == new_slot


def test_apply_degrades_on_state_change(acc):
    victim = _save_project("victim", acc, {"tiktok": (_in(30), False)})
    _save_project("urgent", acc, {})
    stale = (
        victim.platform_schedules["tiktok"].scheduled_at - timedelta(minutes=5)
    ).isoformat()

    result = UrgentUploadService.apply_plan(
        "urgent",
        acc,
        shifts=[
            {
                "project_id": "victim",
                "kind": "anchor",
                "tiktok_slot": _tomorrow(10).isoformat(),
                "expected_scheduled_at": {"tiktok": stale},
            }
        ],
        own_reservations=None,
        tiktok_only=False,
        confirm_before_tiktok=True,
    )
    assert result["shifts"][0]["status"] == "unmovable"
    assert result["shifts"][0]["reason"] == "state_changed"
    # Nothing moved.
    assert ProjectService.load("victim").platform_schedules[
        "tiktok"
    ].scheduled_at == victim.platform_schedules["tiktok"].scheduled_at


def test_apply_degrades_on_published_between_preview_and_apply(acc):
    victim = _save_project(
        "victim",
        acc,
        {"tiktok": (_in(30), False)},
        tiktok_pfm=TikTokPfmState(post_id="sp1", stage="published", url="https://t"),
    )
    _save_project("urgent", acc, {})

    result = UrgentUploadService.apply_plan(
        "urgent",
        acc,
        shifts=[
            {
                "project_id": "victim",
                "kind": "anchor",
                "tiktok_slot": _tomorrow(10).isoformat(),
                "expected_scheduled_at": {
                    "tiktok": victim.platform_schedules["tiktok"].scheduled_at.isoformat()
                },
            }
        ],
        own_reservations=None,
        tiktok_only=False,
        confirm_before_tiktok=True,
    )
    assert result["shifts"][0]["status"] == "unmovable"
    assert result["shifts"][0]["reason"] == "unmovable_published"


def test_apply_platform_shift(acc):
    victim = _save_project("victim", acc, {"youtube": (_in(30), False)})
    _save_project("urgent", acc, {})
    new_slot = _tomorrow(14)

    result = UrgentUploadService.apply_plan(
        "urgent",
        acc,
        shifts=[
            {
                "project_id": "victim",
                "kind": "platform",
                "platform": "youtube",
                "slot": new_slot.isoformat(),
                "expected_scheduled_at": {
                    "youtube": victim.platform_schedules["youtube"].scheduled_at.isoformat()
                },
            }
        ],
        own_reservations=None,
        tiktok_only=False,
        confirm_before_tiktok=True,
    )
    assert result["shifts"][0]["status"] == "ok"
    assert ProjectService.load("victim").platform_schedules["youtube"].slot == new_slot


def test_apply_own_reservations_tiktok_only(acc):
    _save_project("urgent", acc, {})
    first = _tomorrow(10)

    result = UrgentUploadService.apply_plan(
        "urgent",
        acc,
        shifts=[],
        own_reservations={"first_slot": first.isoformat()},
        tiktok_only=True,
        confirm_before_tiktok=True,
    )
    own = result["own_schedules"]
    assert "youtube" in own
    assert datetime.fromisoformat(own["youtube"]["slot"]) >= first
    # TikTok is NOT reserved (it publishes immediately).
    assert "tiktok" not in own
    saved = ProjectService.load("urgent")
    assert "tiktok" not in saved.platform_schedules
    assert "youtube" in saved.platform_schedules


def test_record_immediate_schedules_writes_manual_now(acc):
    _save_project("urgent", acc, {})
    schedules = SchedulingService.record_immediate_schedules(
        "urgent", acc, ["tiktok", "youtube"]
    )
    assert schedules["tiktok"].manual is True
    assert schedules["tiktok"].slot <= datetime.now(timezone.utc)
    saved = ProjectService.load("urgent")
    assert saved.scheduled_account_id == acc
    # Manual entries are invisible to the pool.
    slots = SchedulingService._collect_pool_reservations(
        SchedulingService._resolve_pool_key(acc, "tiktok"), "tiktok"
    )
    assert not slots
