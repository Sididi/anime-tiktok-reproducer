from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.models.project_upload import ProjectUploadJob
from app.services.account_service import AccountService
from app.services.project_service import ProjectService
from app.services.scheduling_errors import (
    SchedulingConflictError,
    SchedulingError,
    SchedulingLockedError,
    SchedulingNotFoundError,
)
from app.services.scheduling_service import SchedulingService

PARIS = ZoneInfo("Europe/Paris")


def paris(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=PARIS).astimezone(timezone.utc)


def _setup(tmp_path, monkeypatch, slots=("18:00",)):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(exist_ok=True)
    slot_yaml = ", ".join(f'"{s}"' for s in slots)
    cfg = tmp_path / "accounts.yaml"
    cfg.write_text(
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
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr("app.services.account_service.settings.accounts_config_path", cfg)
    AccountService.invalidate()
    return "acc1"


# --------------------------------------------------------------- Paris slots


def test_slots_are_paris_wall_times_across_dst(tmp_path, monkeypatch):
    """EU DST ends Sun 2026-10-25: 18:00 Paris is 16:00Z before, 17:00Z after."""
    acc = _setup(tmp_path, monkeypatch, slots=("18:00",))
    after = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
    slots = SchedulingService.find_free_slots_after(acc, "tiktok", after, 4)
    instants = [s.slot for s in slots]
    assert instants[0] == datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)
    assert instants[1] == datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
    # DST switch day: 18:00 Paris is already UTC+1.
    assert instants[2] == datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc)
    assert instants[3] == datetime(2026, 10, 26, 17, 0, tzinfo=timezone.utc)
    # And every one is 18:00 on the Paris wall clock.
    assert all(i.astimezone(PARIS).hour == 18 for i in instants)


def test_slot_config_check_uses_paris_wall_time(tmp_path, monkeypatch):
    acc = _setup(tmp_path, monkeypatch, slots=("18:00",))
    assert SchedulingService._is_slot_in_account_config(acc, "tiktok", paris(2026, 7, 1, 18, 0))
    assert not SchedulingService._is_slot_in_account_config(
        acc, "tiktok", datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    )


# ---------------------------------------------------------- find_slots_in_range


def test_find_slots_in_range_bounds_and_occupancy(tmp_path, monkeypatch):
    acc = _setup(tmp_path, monkeypatch, slots=("12:00", "18:00"))
    projects_dir = tmp_path / "projects"
    (projects_dir / "occ").mkdir(exist_ok=True)
    occupant = Project(id="occ", anime_name="Occupant")
    occupant.scheduled_account_id = acc
    occupant.platform_schedules = {
        "tiktok": PlatformSchedule(
            slot=paris(2026, 7, 2, 18, 0), scheduled_at=paris(2026, 7, 2, 18, 0)
        )
    }
    ProjectService.save(occupant)

    start = paris(2026, 7, 1, 0, 0)
    end = paris(2026, 7, 3, 0, 0)
    slots = SchedulingService.find_slots_in_range(acc, "tiktok", start, end)

    assert [s.slot for s in slots] == [
        paris(2026, 7, 1, 12, 0),
        paris(2026, 7, 1, 18, 0),
        paris(2026, 7, 2, 12, 0),
        paris(2026, 7, 2, 18, 0),
    ]
    taken = [s for s in slots if not s.available]
    assert len(taken) == 1
    assert taken[0].slot == paris(2026, 7, 2, 18, 0)
    assert taken[0].taken_by_project_id == "occ"
    assert taken[0].taken_by_title == "Occupant"


def test_find_slots_in_range_unconfigured_platform_is_empty(tmp_path, monkeypatch):
    acc = _setup(tmp_path, monkeypatch, slots=())
    assert SchedulingService.find_slots_in_range(
        acc, "tiktok", paris(2026, 7, 1, 0, 0), paris(2026, 7, 8, 0, 0)
    ) == []


def test_find_slots_in_range_unknown_account_raises_not_found(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with pytest.raises(SchedulingNotFoundError):
        SchedulingService.find_slots_in_range(
            "nope", "tiktok", paris(2026, 7, 1, 0, 0), paris(2026, 7, 2, 0, 0)
        )


# ------------------------------------------------------------ typed exceptions


def test_typed_exceptions_are_valueerrors():
    for exc_cls in (
        SchedulingError,
        SchedulingConflictError,
        SchedulingLockedError,
        SchedulingNotFoundError,
    ):
        assert issubclass(exc_cls, ValueError)
    assert SchedulingError.http_status == 422
    assert SchedulingConflictError.http_status == 409
    assert SchedulingLockedError.http_status == 423
    assert SchedulingNotFoundError.http_status == 404


def test_reserve_manual_unknown_project_raises_not_found(tmp_path, monkeypatch):
    acc = _setup(tmp_path, monkeypatch)
    with pytest.raises(SchedulingNotFoundError):
        SchedulingService.reserve_manual(
            "ghost", acc, datetime.now(timezone.utc) + timedelta(days=1), ["tiktok"]
        )


def test_reserve_manual_locked_project_raises_locked(tmp_path, monkeypatch):
    acc = _setup(tmp_path, monkeypatch)
    projects_dir = tmp_path / "projects"
    (projects_dir / "p1").mkdir(exist_ok=True)
    project = Project(id="p1", anime_name="P1")
    project.scheduled_account_id = acc
    near = datetime.now(timezone.utc) + timedelta(minutes=5)
    project.platform_schedules = {
        "tiktok": PlatformSchedule(slot=near, scheduled_at=near)
    }
    ProjectService.save(project)
    with pytest.raises(SchedulingLockedError):
        SchedulingService.reserve_manual(
            "p1", acc, datetime.now(timezone.utc) + timedelta(days=1), ["tiktok"]
        )


def test_reschedule_platform_unconfigured_slot_raises_scheduling_error(
    tmp_path, monkeypatch
):
    acc = _setup(tmp_path, monkeypatch, slots=("18:00",))
    projects_dir = tmp_path / "projects"
    (projects_dir / "p1").mkdir(exist_ok=True)
    project = Project(id="p1", anime_name="P1")
    project.scheduled_account_id = acc
    far = paris(2099, 7, 1, 18, 0)
    project.platform_schedules = {
        "youtube": PlatformSchedule(slot=far, scheduled_at=far)
    }
    ProjectService.save(project)
    with pytest.raises(SchedulingError) as exc_info:
        SchedulingService.reschedule_platform("p1", "youtube", paris(2099, 7, 2, 9, 30))
    assert not isinstance(exc_info.value, SchedulingConflictError)
    assert "not configured" in str(exc_info.value)


# ------------------------------------------------------- per-platform statuses


class _FakeQueue:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self):
        return list(self._jobs)


def _event_status(monkeypatch, jobs, platform, project=None):
    from app.api.routes.scheduling import _project_event_status
    from app.services import project_upload_service
    from app.services.vps_status_sync_service import VpsStatusSyncService

    monkeypatch.setattr(project_upload_service, "project_upload_queue", _FakeQueue(jobs))
    monkeypatch.setattr(VpsStatusSyncService, "_cache", {})
    return _project_event_status(project or Project(id="p1", anime_name="P1"), platform)[0]


def test_event_status_per_platform_mixed_results(monkeypatch):
    """YT/FB derive from the local job's incremental platform_results."""
    job = ProjectUploadJob(
        project_id="p1",
        status="running",
        platform_results=[
            {"platform": "youtube", "status": "uploaded"},
            {"platform": "facebook", "status": "failed"},
        ],
    )
    assert _event_status(monkeypatch, [job], "youtube") == "complete"
    assert _event_status(monkeypatch, [job], "facebook") == "failed"
    # IG/TT are VPS platforms: a running local job means prep in progress.
    assert _event_status(monkeypatch, [job], "instagram") == "running"
    assert _event_status(monkeypatch, [job], "tiktok") == "running"


def test_event_status_uses_latest_job(monkeypatch):
    now = datetime.now(timezone.utc)
    stale = ProjectUploadJob(
        project_id="p1",
        status="error",
        updated_at=now - timedelta(hours=2),
    )
    fresh = ProjectUploadJob(
        project_id="p1",
        status="complete",
        updated_at=now,
        platform_results=[{"platform": "youtube", "status": "uploaded"}],
    )
    assert _event_status(monkeypatch, [stale, fresh], "youtube") == "complete"


def test_event_status_no_jobs_is_scheduled(monkeypatch):
    assert _event_status(monkeypatch, [], "youtube") == "scheduled"
    assert _event_status(monkeypatch, [], "tiktok") == "scheduled"


def test_event_status_posted_url_fallback_without_jobs(monkeypatch):
    project = Project(id="p1", anime_name="P1")
    # Real uploader shape: a LIST of per-platform entries.
    project.upload_last_result = {
        "platforms": [
            {"platform": "tiktok", "status": "uploaded", "url": "https://tiktok.com/@x/video/1"},
            {"platform": "youtube", "status": "uploaded", "url": "https://youtu.be/x"},
        ]
    }
    assert _event_status(monkeypatch, [], "tiktok", project) == "complete"
    assert _event_status(monkeypatch, [], "youtube", project) == "complete"
    assert _event_status(monkeypatch, [], "instagram", project) == "scheduled"

    # Historical dict shape keeps working too.
    project.upload_last_result = {
        "platforms": {"youtube": {"url": "https://youtu.be/x"}, "facebook": {}}
    }
    assert _event_status(monkeypatch, [], "youtube", project) == "complete"
    assert _event_status(monkeypatch, [], "facebook", project) == "scheduled"


# ------------------------------------------------------- VPS-published statuses


def _vps_project(sched_offset_minutes: int, dispatched: bool = True) -> Project:
    from app.models import PlatformSchedule

    project = Project(id="p1", anime_name="P1")
    if dispatched:
        project.final_upload_discord_message_id = "msg1"
    at = datetime.now(timezone.utc) + timedelta(minutes=sched_offset_minutes)
    project.platform_schedules = {
        "instagram": PlatformSchedule(slot=at, scheduled_at=at),
        "tiktok": PlatformSchedule(slot=at, scheduled_at=at),
    }
    return project


def test_vps_status_from_sync_cache(monkeypatch):
    from app.api.routes.scheduling import _project_event_status
    from app.services import project_upload_service
    from app.services.vps_status_sync_service import VpsStatusSyncService

    monkeypatch.setattr(project_upload_service, "project_upload_queue", _FakeQueue([]))
    monkeypatch.setattr(
        VpsStatusSyncService,
        "_cache",
        {
            "p1": {
                "instagram": {"status": "uploaded", "url": "https://instagram.com/p/x"},
                "tiktok": {"status": "failed", "detail": "boom"},
            }
        },
    )
    project = _vps_project(sched_offset_minutes=-60)
    assert _project_event_status(project, "instagram") == (
        "complete",
        "https://instagram.com/p/x",
    )
    assert _project_event_status(project, "tiktok") == ("failed", None)


def test_vps_status_pending_and_uploading(monkeypatch):
    from app.api.routes.scheduling import _project_event_status
    from app.services import project_upload_service
    from app.services.vps_status_sync_service import VpsStatusSyncService

    monkeypatch.setattr(project_upload_service, "project_upload_queue", _FakeQueue([]))
    monkeypatch.setattr(
        VpsStatusSyncService,
        "_cache",
        {
            "p1": {
                "instagram": {"status": "pending"},
                "tiktok": {"status": "uploading"},
            }
        },
    )
    project = _vps_project(sched_offset_minutes=120)
    assert _project_event_status(project, "instagram")[0] == "dispatched"
    assert _project_event_status(project, "tiktok")[0] == "running"


def test_vps_status_presumed_success_after_slot(monkeypatch):
    """No VPS info + dispatched job: future slot = dispatched, past = presumed
    published (success until proven otherwise)."""
    assert (
        _event_status(monkeypatch, [], "instagram", _vps_project(120)) == "dispatched"
    )
    assert (
        _event_status(monkeypatch, [], "instagram", _vps_project(-120)) == "complete"
    )
    # Never dispatched at all: plain scheduled.
    assert (
        _event_status(monkeypatch, [], "instagram", _vps_project(-120, dispatched=False))
        == "scheduled"
    )


# ------------------------------------------------------------- VPS status sync


def test_vps_sync_persists_terminal_outcomes(tmp_path, monkeypatch):
    from app.services.vps_status_sync_service import VpsStatusSyncService

    _setup(tmp_path, monkeypatch)
    projects_dir = tmp_path / "projects"
    (projects_dir / "p1").mkdir(exist_ok=True)
    project = Project(id="p1", anime_name="P1")
    project.upload_last_result = {
        "platforms": [
            {"platform": "youtube", "status": "uploaded", "url": "https://youtu.be/x"}
        ]
    }
    ProjectService.save(project)

    cache = {
        "p1": {
            "instagram": {
                "status": "uploaded",
                "url": "https://instagram.com/p/x",
                "detail": None,
                "completed_at": "2026-08-14T12:00:00+00:00",
                "attempts": 1,
            },
            "tiktok": {"status": "pending"},
        }
    }
    VpsStatusSyncService._persist_terminal_outcomes(cache)

    saved = ProjectService.load("p1")
    entries = saved.upload_last_result["platforms"]
    by_platform = {e["platform"]: e for e in entries}
    # YouTube untouched, Instagram appended with source marker, pending TikTok
    # NOT persisted.
    assert by_platform["youtube"]["url"] == "https://youtu.be/x"
    assert by_platform["instagram"]["status"] == "uploaded"
    assert by_platform["instagram"]["source"] == "vps"
    assert "tiktok" not in by_platform

    # Re-running with identical data must not rewrite the file.
    mtime = (projects_dir / "p1" / "project.json").stat().st_mtime_ns
    VpsStatusSyncService._persist_terminal_outcomes(cache)
    assert (projects_dir / "p1" / "project.json").stat().st_mtime_ns == mtime


def test_vps_sync_request_is_throttled(monkeypatch):
    import time as time_module

    from app.services import vps_status_sync_service as mod
    from app.services.discord_service import DiscordService
    from app.services.vps_status_sync_service import VpsStatusSyncService

    monkeypatch.setattr(DiscordService, "is_configured", classmethod(lambda cls: True))
    spawned: list[str] = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            spawned.append(name)
            self._target = target

        def start(self):
            # Run inline; _sync will fetch (None) and release the lock.
            self._target()

    monkeypatch.setattr(mod.threading, "Thread", _FakeThread)
    monkeypatch.setattr(
        DiscordService, "fetch_job_statuses", classmethod(lambda cls: None)
    )

    VpsStatusSyncService._last_sync_monotonic = 0.0
    VpsStatusSyncService._sync_running = False
    VpsStatusSyncService.request_sync()
    assert len(spawned) == 1
    # Second call inside the interval: no new sync.
    VpsStatusSyncService.request_sync()
    assert len(spawned) == 1
    # After the interval elapses, a sync runs again.
    VpsStatusSyncService._last_sync_monotonic = (
        time_module.monotonic() - mod._MIN_SYNC_INTERVAL_SECONDS - 1
    )
    VpsStatusSyncService.request_sync()
    assert len(spawned) == 2


def test_manager_uploaded_fields_publish_error():
    from app.services.upload_phase import _uploaded_fields

    project = Project(id="p1", anime_name="P1")
    project.final_upload_discord_message_id = "msg1"
    project.scheduled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    project.upload_last_result = {
        "platforms": [
            {"platform": "youtube", "status": "uploaded", "url": "https://youtu.be/x"},
            {
                "platform": "tiktok",
                "status": "failed",
                "detail": "PFM gave up",
                "source": "vps",
            },
        ]
    }
    fields = _uploaded_fields(project)
    assert fields["uploaded_status"] == "publish_error"
    assert "PFM gave up" in fields["publish_error_detail"]

    # Without a VPS failure the clock-based status is preserved.
    project.upload_last_result = {"platforms": []}
    assert _uploaded_fields(project)["uploaded_status"] == "green"
