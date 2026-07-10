from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Project
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_service import SchedulingService


def test_instagram_shared_pool_uses_canonical_slot_not_jitter(
    tmp_path: Path, monkeypatch,
):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        """\
accounts:
  anime_fr_2:
    name: "Anime v2"
    language: "fr"
    device: "poco"
    slots: ["22:00"]
    meta:
      facebook_page_id: "fb_page"
      facebook_page_access_token: "token"
      instagram_business_account_id: "ig_shared"
  anime_fr_4:
    name: "Anime v4"
    language: "fr"
    device: "poco"
    slots: ["22:00"]
    meta:
      facebook_page_id: "fb_page"
      facebook_page_access_token: "token"
      instagram_business_account_id: "ig_shared"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    monkeypatch.setattr(
        SchedulingService,
        "_earliest_allowed_publish_time",
        classmethod(lambda cls: datetime(2026, 4, 29, 0, 0, tzinfo=timezone.utc)),
    )
    monkeypatch.setattr(
        "app.services.scheduling_service.datetime",
        _FixedDateTime,
    )
    AccountService.invalidate()

    (projects_dir / "p1").mkdir()
    (projects_dir / "p2").mkdir()
    ProjectService.save(Project(id="p1", anime_name="One", output_language="fr"))
    ProjectService.save(Project(id="p2", anime_name="Two", output_language="fr"))

    first = SchedulingService.reserve_all_platform_slots(
        "p1", "anime_fr_2", ["instagram"]
    )
    second = SchedulingService.reserve_all_platform_slots(
        "p2", "anime_fr_4", ["instagram"]
    )

    assert first["instagram"][0] == datetime(2026, 4, 29, 22, 0, tzinfo=timezone.utc)
    assert second["instagram"][0] == datetime(2026, 4, 30, 22, 0, tzinfo=timezone.utc)
    assert first["instagram"][1].minute != second["instagram"][1].minute or (
        first["instagram"][1].date() != second["instagram"][1].date()
    )


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        return current if tz is None else current.astimezone(tz)


def _setup_single_account(tmp_path, monkeypatch, slots=("10:00", "14:00", "18:00")):
    """One account 'acc1' with the given top-level slots. Returns 'acc1'."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(exist_ok=True)
    slot_yaml = ", ".join(f'"{s}"' for s in slots)
    accounts_config = tmp_path / "accounts.yaml"
    accounts_config.write_text(
        f"""\
accounts:
  acc1:
    name: "Acc 1"
    language: "fr"
    device: "poco"
    slots: [{slot_yaml}]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    monkeypatch.setattr(
        "app.services.account_service.settings.accounts_config_path", accounts_config
    )
    AccountService.invalidate()
    return "acc1"


def _save_scheduled_project(pid, account_id, platform, slot_dt, manual=False, title=None):
    from app.models import PlatformSchedule
    project = Project(id=pid, anime_name=title or pid)
    project.scheduled_account_id = account_id
    project.platform_schedules = {
        platform: PlatformSchedule(slot=slot_dt, scheduled_at=slot_dt, manual=manual)
    }
    ProjectService.get_project_dir(pid).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


def test_manual_entries_do_not_block_slots(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    _save_scheduled_project("manualproj", acc, "tiktok", tomorrow, manual=True)

    slots = SchedulingService.find_free_slots_after(
        acc, "tiktok", tomorrow - timedelta(minutes=1), 1
    )
    assert slots[0].slot == tomorrow
    assert slots[0].available is True          # manual entry invisible to the pool
    assert slots[0].taken_by_project_id is None


def test_taken_slot_reports_project_and_title(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    _save_scheduled_project("slotproj", acc, "tiktok", tomorrow, title="Naruto")

    slots = SchedulingService.find_free_slots_after(
        acc, "tiktok", tomorrow - timedelta(minutes=1), 1
    )
    assert slots[0].available is False
    assert slots[0].taken_by_project_id == "slotproj"
    assert slots[0].taken_by_title == "Naruto"


def test_cascade_skips_manual_entries(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SchedulingService, "_pool_is_busy_uploading", classmethod(lambda cls, a, p: (False, None))
    )
    monkeypatch.setattr(
        SchedulingService,
        "_platforms_for_project",
        classmethod(lambda cls, pid, aid: ["tiktok"]),
    )
    # Place the manual project exactly ON the cascade anchor slot, so the
    # test fails if manual entries are ever visible to the cascade walk.
    anchor = SchedulingService._earliest_slot_at_or_after(
        acc, "tiktok", datetime.now(timezone.utc) + timedelta(minutes=30)
    )
    _save_scheduled_project("manualproj", acc, "tiktok", anchor, manual=True)

    result = SchedulingService.compute_cascade("newproj", acc)
    tt = next(p for p in result.per_platform if p.platform == "tiktok")
    assert tt.target_slot == anchor
    # the manual project is NOT displaced even though it sits on the anchor slot
    assert tt.displaced == []


def test_reserve_manual_writes_exact_time_no_jitter(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.get_project_dir("p1").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="p1", anime_name="Bleach"))
    at = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)

    schedules = SchedulingService.reserve_manual("p1", acc, at, ["tiktok", "youtube"])

    assert set(schedules) == {"tiktok", "youtube"}
    for sched in schedules.values():
        assert sched.manual is True
        assert sched.slot == at
        assert sched.scheduled_at == at          # exact, no jitter
    saved = ProjectService.load("p1")
    assert saved.scheduled_account_id == acc
    assert saved.platform_schedules["tiktok"].manual is True


def test_reserve_manual_rejects_too_close(tmp_path, monkeypatch):
    from datetime import timedelta
    import pytest
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.get_project_dir("p1").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="p1"))
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    with pytest.raises(ValueError, match="slot_too_close"):
        SchedulingService.reserve_manual("p1", acc, at, ["tiktok"])


def test_reserve_manual_overwrites_previous_manual(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.get_project_dir("p1").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="p1"))
    at1 = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)
    at2 = at1 + timedelta(hours=3)
    SchedulingService.reserve_manual("p1", acc, at1, ["tiktok"])
    SchedulingService.reserve_manual("p1", acc, at2, ["tiktok"])
    assert ProjectService.load("p1").platform_schedules["tiktok"].slot == at2


def test_reserve_manual_rejects_when_timing_locked(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    tiktok_at = now + timedelta(minutes=3)  # inside the 10-min lock window
    _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    new_at = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    with pytest.raises(ValueError, match="timing_locked"):
        SchedulingService.reserve_manual("p1", acc, new_at, ["tiktok"])


def test_reserve_manual_not_blocked_for_fresh_project(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    ProjectService.get_project_dir("p1").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="p1"))  # no platform_schedules at all
    at = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(second=0, microsecond=0)

    schedules = SchedulingService.reserve_manual("p1", acc, at, ["tiktok"])

    assert schedules["tiktok"].slot == at
    assert ProjectService.load("p1").platform_schedules["tiktok"].slot == at


# --------------------------------------------------------------- compute_switch


def _future_slot(days, hour):
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _patch_pool_not_busy(monkeypatch):
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (False, None)),
    )


def test_compute_switch_chain_and_next_free(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)   # slots 10/14/18
    _patch_pool_not_busy(monkeypatch)
    s10, s14, s18 = _future_slot(1, 10), _future_slot(1, 14), _future_slot(1, 18)
    _save_scheduled_project("projB", acc, "tiktok", s10, title="B")
    _save_scheduled_project("projC", acc, "tiktok", s14, title="C")
    # 18:00 free

    result = SchedulingService.compute_switch("newproj", acc, "tiktok", s10)

    assert result.occupant_project_id == "projB"
    assert result.occupant_title == "B"
    # cascade: B -> 14 pushes C -> 18
    assert [(d.project_id, d.from_slot, d.to_slot) for d in result.cascade.displaced] == [
        ("projB", s10, s14),
        ("projC", s14, s18),
    ]
    assert result.cascade.blockers == []
    # next_free: B jumps over taken 14 straight to 18
    assert [(d.project_id, d.to_slot) for d in result.next_free.displaced] == [
        ("projB", s18)
    ]
    assert result.next_free.blockers == []
    assert result.uploaded_count == 0


def test_compute_switch_skips_own_reservation_and_manual(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14 = _future_slot(1, 10), _future_slot(1, 14)
    _save_scheduled_project("projB", acc, "tiktok", s10, title="B")
    _save_scheduled_project("me", acc, "tiktok", s14, title="Me")          # my own old slot
    _save_scheduled_project("manualp", acc, "tiktok", _future_slot(1, 18), manual=True)

    result = SchedulingService.compute_switch("me", acc, "tiktok", s10)
    # my own 14:00 counts as free (it's released by the switch), so B lands there
    assert [(d.project_id, d.to_slot) for d in result.cascade.displaced] == [("projB", s14)]
    assert result.next_free.displaced[0].to_slot == s14


def test_compute_switch_free_slot_has_no_occupant(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    result = SchedulingService.compute_switch("newproj", acc, "tiktok", _future_slot(1, 10))
    assert result.occupant_project_id is None
    assert result.cascade.displaced == [] and result.next_free.displaced == []


def test_compute_switch_pool_busy_blocks_both_plans(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SchedulingService,
        "_pool_is_busy_uploading",
        classmethod(lambda cls, a, p: (True, "busyproj")),
    )
    _save_scheduled_project("projB", acc, "tiktok", _future_slot(1, 10))
    result = SchedulingService.compute_switch("newproj", acc, "tiktok", _future_slot(1, 10))
    assert any(b.reason == "pool_busy" for b in result.cascade.blockers)
    assert any(b.reason == "pool_busy" for b in result.next_free.blockers)


def test_apply_switch_cascade_moves_chain_and_reserves(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14, s18 = _future_slot(1, 10), _future_slot(1, 14), _future_slot(1, 18)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    _save_scheduled_project("projC", acc, "tiktok", s14)
    ProjectService.get_project_dir("me").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="me"))

    SchedulingService.apply_switch("me", acc, "tiktok", s10, "cascade", "projB")

    assert ProjectService.load("me").platform_schedules["tiktok"].slot == s10
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s14
    assert ProjectService.load("projC").platform_schedules["tiktok"].slot == s18


def test_apply_switch_next_free_moves_only_occupant(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14, s18 = _future_slot(1, 10), _future_slot(1, 14), _future_slot(1, 18)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    _save_scheduled_project("projC", acc, "tiktok", s14)
    ProjectService.get_project_dir("me").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="me"))

    SchedulingService.apply_switch("me", acc, "tiktok", s10, "next_free", "projB")

    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s18
    assert ProjectService.load("projC").platform_schedules["tiktok"].slot == s14  # untouched


def test_apply_switch_stale_occupant_raises(tmp_path, monkeypatch):
    import pytest
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10 = _future_slot(1, 10)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    ProjectService.get_project_dir("me").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="me"))
    with pytest.raises(ValueError, match="slot_state_changed"):
        SchedulingService.apply_switch("me", acc, "tiktok", s10, "cascade", "someoneelse")


def test_apply_switch_rejects_when_switcher_timing_locked(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    now = datetime.now(timezone.utc)
    locked_at = now + timedelta(minutes=3)  # inside the 10-min lock window
    _save_scheduled_project("me", acc, "tiktok", locked_at)
    target_slot = _future_slot(1, 10)  # free slot, no occupant to conflict with

    with pytest.raises(ValueError, match="timing_locked"):
        SchedulingService.apply_switch("me", acc, "tiktok", target_slot, "cascade", None)


def test_reserve_anchor_with_steal_is_atomic(tmp_path, monkeypatch):
    from app.services.scheduling_service import StealSpec
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10, s14 = _future_slot(1, 10), _future_slot(1, 14)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    ProjectService.get_project_dir("me").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="me"))

    schedules, switches = SchedulingService.reserve_anchor(
        "me", acc, s10,
        steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="projB")},
    )

    assert schedules["tiktok"].slot == s10
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s14
    assert switches["tiktok"].occupant_project_id == "projB"


def test_reserve_anchor_steal_stale_occupant_writes_nothing(tmp_path, monkeypatch):
    import pytest
    from app.services.scheduling_service import StealSpec
    acc = _setup_single_account(tmp_path, monkeypatch)
    _patch_pool_not_busy(monkeypatch)
    s10 = _future_slot(1, 10)
    _save_scheduled_project("projB", acc, "tiktok", s10)
    ProjectService.get_project_dir("me").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="me"))

    with pytest.raises(ValueError, match="slot_state_changed"):
        SchedulingService.reserve_anchor(
            "me", acc, s10,
            steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="wrong")},
        )
    # nothing moved, nothing reserved
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s10
    assert not (ProjectService.load("me").platform_schedules or {})


def test_reserve_anchor_rolls_back_steal_on_anchor_conflict(tmp_path, monkeypatch):
    import pytest
    from app.services.scheduling_service import StealSpec
    acc = _setup_single_account(tmp_path, monkeypatch)  # slots 10/14/18 all platforms
    _patch_pool_not_busy(monkeypatch)
    s10, s14 = _future_slot(1, 10), _future_slot(1, 14)

    # A valid tiktok steal: projB occupies the anchor slot, cascade moves it to 14.
    _save_scheduled_project("projB", acc, "tiktok", s10, title="B")
    # A different, NON-stolen project already holds the youtube slot we override to,
    # so resolve_anchor raises "Anchor conflicts" AFTER the tiktok steal was applied.
    _save_scheduled_project("projY", acc, "youtube", s14, title="Y")
    ProjectService.get_project_dir("me").mkdir(parents=True, exist_ok=True)
    ProjectService.save(Project(id="me"))

    with pytest.raises(ValueError, match="Anchor conflicts"):
        SchedulingService.reserve_anchor(
            "me", acc, s10,
            overrides={"youtube": s14},
            steals={"tiktok": StealSpec(mode="cascade", expected_occupant_id="projB")},
        )

    # Rollback: the tiktok occupant is back on its ORIGINAL slot...
    assert ProjectService.load("projB").platform_schedules["tiktok"].slot == s10
    # ...the colliding youtube project is untouched...
    assert ProjectService.load("projY").platform_schedules["youtube"].slot == s14
    # ...and "me" reserved nothing.
    assert not (ProjectService.load("me").platform_schedules or {})


def test_tiktok_timing_locked_inside_window(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    tiktok_at = now + timedelta(minutes=5)  # lock opened at now-10min (lock=15)
    project = _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is True


def test_tiktok_timing_not_locked_outside_window(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    tiktok_at = now + timedelta(minutes=25)  # lock opens at now+10min
    project = _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is False


def test_project_without_tiktok_never_timing_locked(tmp_path, monkeypatch):
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    project = _save_scheduled_project("p1", acc, "youtube", now)
    assert SchedulingService.tiktok_timing_locked(project, now=now) is False


def test_reschedule_platform_rejects_when_timing_locked(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    tiktok_at = now + timedelta(minutes=3)  # inside the 10-min window
    _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    with pytest.raises(ValueError, match="timing_locked"):
        SchedulingService.reschedule_platform("p1", "tiktok", tiktok_at)


def test_reschedule_anchor_rejects_when_timing_locked(tmp_path, monkeypatch):
    from datetime import timedelta
    acc = _setup_single_account(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    tiktok_at = now + timedelta(minutes=3)
    _save_scheduled_project("p1", acc, "tiktok", tiktok_at)
    with pytest.raises(ValueError, match="timing_locked"):
        SchedulingService.reschedule_anchor("p1", tiktok_at)
