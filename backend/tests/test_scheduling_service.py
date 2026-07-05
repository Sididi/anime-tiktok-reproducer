from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

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
