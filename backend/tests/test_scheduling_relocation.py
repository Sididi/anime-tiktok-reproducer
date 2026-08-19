"""Relocate steal mode: single push of the occupant to its nearest free
slots, TikTok-first (2026-08 upload flows redesign)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService

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
    AccountService.invalidate()
    return "acc1"


def _tomorrow(h, mi=0):
    d = (datetime.now(PARIS) + timedelta(days=1)).date()
    return paris(d.year, d.month, d.day, h, mi)


def _save_project(pid, account_id, schedules, title=None):
    project = Project(id=pid, anime_name=title or pid)
    project.scheduled_account_id = account_id
    project.platform_schedules = {
        p: PlatformSchedule(slot=dt, scheduled_at=dt, manual=manual)
        for p, (dt, manual) in schedules.items()
    }
    ProjectService.get_project_dir(pid).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


def test_relocate_tiktok_steal_moves_occupant_and_follows_other_platforms(acc):
    tt_slot = _tomorrow(10)
    _save_project(
        "victim",
        acc,
        {"tiktok": (tt_slot, False), "youtube": (tt_slot, False)},
        title="Victim",
    )
    _save_project("switcher", acc, {})

    result = SchedulingService.compute_switch("switcher", acc, "tiktok", tt_slot)
    plan = result.relocate

    assert not plan.blockers
    moves = {(d.platform or "tiktok"): d for d in plan.displaced}
    assert set(moves) == {"tiktok", "youtube"}
    # Occupant TT pushed to the next free TT slot; YT follows to the nearest
    # free slot >= the new TT slot (same instant allowed).
    assert moves["tiktok"].to_slot == _tomorrow(14)
    assert moves["youtube"].from_slot == tt_slot
    assert moves["youtube"].to_slot >= moves["tiktok"].to_slot
    # Precedence-safe by construction — no displaced-TT warning.
    assert plan.precedence_warnings == []
    # next_free (single move) would have warned about the same steal.
    assert result.next_free.precedence_warnings


def test_relocate_does_not_move_platforms_already_after_new_tiktok(acc):
    tt_slot = _tomorrow(10)
    yt_slot = _tomorrow(18)
    _save_project(
        "victim", acc, {"tiktok": (tt_slot, False), "youtube": (yt_slot, False)}
    )
    _save_project("switcher", acc, {})

    plan = SchedulingService.compute_switch(
        "switcher", acc, "tiktok", tt_slot
    ).relocate
    platforms = [d.platform or "tiktok" for d in plan.displaced]
    assert platforms == ["tiktok"]  # YT at 18:00 already >= new TT (14:00)


def test_relocate_ignores_manual_sibling_platforms(acc):
    tt_slot = _tomorrow(10)
    _save_project(
        "victim",
        acc,
        {"tiktok": (tt_slot, False), "youtube": (tt_slot, True)},  # manual YT
    )
    _save_project("switcher", acc, {})

    plan = SchedulingService.compute_switch(
        "switcher", acc, "tiktok", tt_slot
    ).relocate
    platforms = [d.platform or "tiktok" for d in plan.displaced]
    assert platforms == ["tiktok"]
    assert plan.precedence_warnings == []


def test_relocate_non_tiktok_steal_is_single_move(acc):
    yt_slot = _tomorrow(10)
    _save_project(
        "victim", acc, {"youtube": (yt_slot, False), "tiktok": (_tomorrow(14), False)}
    )
    _save_project("switcher", acc, {})

    plan = SchedulingService.compute_switch(
        "switcher", acc, "youtube", yt_slot
    ).relocate
    assert len(plan.displaced) == 1
    assert (plan.displaced[0].platform or "youtube") == "youtube"


def test_apply_switch_relocate_persists_all_moves(acc):
    tt_slot = _tomorrow(10)
    _save_project(
        "victim", acc, {"tiktok": (tt_slot, False), "youtube": (tt_slot, False)}
    )
    _save_project("switcher", acc, {})

    SchedulingService.apply_switch(
        "switcher", acc, "tiktok", tt_slot,
        mode="relocate", expected_occupant_id="victim",
    )

    victim = ProjectService.load("victim")
    switcher = ProjectService.load("switcher")
    assert switcher.platform_schedules["tiktok"].slot == tt_slot
    assert victim.platform_schedules["tiktok"].slot == _tomorrow(14)
    assert (
        victim.platform_schedules["youtube"].slot
        >= victim.platform_schedules["tiktok"].slot
    )


def test_reserve_anchor_steal_relocate(acc):
    from app.services.scheduling_service import StealSpec

    tt_slot = _tomorrow(10)
    _save_project(
        "victim", acc, {"tiktok": (tt_slot, False), "youtube": (tt_slot, False)}
    )
    _save_project("switcher", acc, {})

    schedules, switches = SchedulingService.reserve_anchor(
        "switcher",
        acc,
        tt_slot,
        steals={"tiktok": StealSpec(mode="relocate", expected_occupant_id="victim")},
    )
    assert schedules["tiktok"].slot == tt_slot
    victim = ProjectService.load("victim")
    assert victim.platform_schedules["tiktok"].slot == _tomorrow(14)
    assert (
        victim.platform_schedules["youtube"].slot
        >= victim.platform_schedules["tiktok"].slot
    )
    assert "tiktok" in switches
