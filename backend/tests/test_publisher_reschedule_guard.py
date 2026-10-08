"""Definite VPS edit conflicts preserve local schedules, including bulk moves."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.services.account_service import ADDITIONAL_PLATFORMS, AccountService
from app.services.platform_reschedule_service import NotificationResult, PlatformRescheduleService
from app.services.project_service import ProjectService
from app.services.reschedule_retry_service import RescheduleRetryService
from app.services.scheduling_errors import SchedulingConflictError
from app.services.scheduling_service import SchedulingService, StealSpec

NOW = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
OLD = NOW.replace(hour=12)
NEW = NOW.replace(hour=16)


@pytest.fixture
def scheduled(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    account = AccountService._parse_account("account", {
        "name": "Account", "language": "fr", "slots": ["10:00", "14:00", "18:00"],
        **{platform: {"mode": "manual"} for platform in ADDITIONAL_PLATFORMS},
    })
    projects = {}
    monkeypatch.setattr("app.services.scheduling_service.datetime", Clock)
    monkeypatch.setattr(AccountService, "get_account", lambda _: account)
    monkeypatch.setattr(AccountService, "all_accounts", lambda: {"account": account})
    monkeypatch.setattr(ProjectService, "load", lambda key: projects[key].model_copy(deep=True)
                        if key in projects else None)
    monkeypatch.setattr(ProjectService, "list_all", lambda: [p.model_copy(deep=True) for p in projects.values()])
    monkeypatch.setattr(ProjectService, "list_with_reschedule_pending", lambda: [
        p.model_copy(deep=True) for p in projects.values() if p.reschedule_pending
    ])
    monkeypatch.setattr(ProjectService, "save", lambda p: projects.__setitem__(p.id, p.model_copy(deep=True)))
    monkeypatch.setattr(SchedulingService, "_validate_duplication_restrictions", lambda *args: None)
    monkeypatch.setattr(SchedulingService, "_pool_is_busy_uploading", lambda *args: (False, None))
    monkeypatch.setattr(PlatformRescheduleService, "notify", lambda *args: NotificationResult(
        "pending_retry", "VPS publisher has started; inspect the remote submission",
        conflict=True,
    ))

    def save(project_id, **slots):
        project = Project(id=project_id, scheduled_account_id="account", upload_completed_at=NOW,
            platform_schedules={p: PlatformSchedule(slot=t, scheduled_at=t) for p, t in slots.items()})
        SchedulingService._recompute_aggregates(project)
        ProjectService.save(project)
        return project

    return projects, save


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_definite_conflict_restores_single_platform_and_aggregate(scheduled, platform):
    projects, save = scheduled
    original = save("p1", **{platform: OLD})
    with pytest.raises(SchedulingConflictError, match="has started"):
        SchedulingService.reschedule_platform("p1", platform, NEW)
    assert projects["p1"].platform_schedules == original.platform_schedules
    assert projects["p1"].scheduled_at == original.scheduled_at
    assert not projects["p1"].reschedule_pending


def test_transient_failure_keeps_route_retry_semantics(scheduled, monkeypatch):
    projects, save = scheduled
    save("p1", dailymotion=OLD)
    monkeypatch.setattr(PlatformRescheduleService, "notify", lambda *args: NotificationResult(
        "pending_retry", "VPS publisher update failed (ConnectError)",
    ))
    result = SchedulingService.reschedule_platform("p1", "dailymotion", NEW)
    assert result.slot == NEW
    assert projects["p1"].platform_schedules["dailymotion"].slot == NEW


def test_manual_batch_conflict_compensates_prior_remote_move(scheduled, monkeypatch):
    projects, save = scheduled
    original = save("p1", dailymotion=OLD, snapchat=OLD)
    calls = []

    def notify(project, platform, at):
        calls.append((platform, at))
        return NotificationResult("pending_retry", "VPS publisher has started", conflict=True) if platform == "snapchat" else NotificationResult("ok")

    monkeypatch.setattr(PlatformRescheduleService, "notify", notify)
    with pytest.raises(SchedulingConflictError):
        SchedulingService.reserve_manual("p1", "account", NEW, ["dailymotion", "snapchat"])
    assert projects["p1"].platform_schedules == original.platform_schedules
    assert calls == [("dailymotion", NEW), ("snapchat", NEW), ("dailymotion", OLD)]


def test_uncertain_compensation_is_queued_to_original_time(scheduled, monkeypatch):
    projects, save = scheduled
    original = save("p1", dailymotion=OLD, snapchat=OLD)

    def notify(project, platform, at):
        if platform == "snapchat":
            return NotificationResult("pending_retry", "VPS publisher has started", conflict=True)
        return NotificationResult("pending_retry", "VPS publisher update failed (ConnectError)")

    monkeypatch.setattr(PlatformRescheduleService, "notify", notify)
    with pytest.raises(SchedulingConflictError):
        SchedulingService.reserve_manual("p1", "account", NEW, ["dailymotion", "snapchat"])
    assert projects["p1"].platform_schedules == original.platform_schedules
    assert projects["p1"].reschedule_pending["dailymotion"]["target_scheduled_at"] == OLD


def test_anchor_conflict_restores_all_original_reservations(scheduled):
    projects, save = scheduled
    original = save("p1", tiktok=OLD, dailymotion=OLD)
    with pytest.raises(SchedulingConflictError, match="has started"):
        SchedulingService.reschedule_anchor("p1", NEW)
    assert projects["p1"].platform_schedules == original.platform_schedules
    assert projects["p1"].scheduled_at == original.scheduled_at


def test_anchor_steal_conflict_restores_displaced_and_own_schedules(scheduled):
    projects, save = scheduled
    own = save("p1", tiktok=OLD)
    other = save("other", dailymotion=NEW)
    with pytest.raises(SchedulingConflictError, match="has started"):
        SchedulingService.reschedule_anchor("p1", NEW,
            overrides={"dailymotion": NEW},
            steals={"dailymotion": StealSpec(mode="relocate", expected_occupant_id="other")})
    assert projects["p1"].platform_schedules == own.platform_schedules
    assert projects["other"].platform_schedules == other.platform_schedules


def test_switch_conflict_leaves_all_projects_unmoved(scheduled):
    projects, save = scheduled
    own = save("p1")
    other = save("other", dailymotion=OLD)
    with pytest.raises(SchedulingConflictError, match="has started"):
        SchedulingService.apply_switch("p1", "account", "dailymotion", OLD, "relocate", "other")
    assert projects["p1"].platform_schedules == own.platform_schedules
    assert projects["other"].platform_schedules == other.platform_schedules


def test_cascade_conflict_leaves_all_projects_unmoved(scheduled):
    projects, save = scheduled
    own = save("p1")
    other = save("other", dailymotion=OLD)
    with pytest.raises(SchedulingConflictError, match="has started"):
        SchedulingService.apply_cascade("p1", "account")
    assert projects["p1"].platform_schedules == own.platform_schedules
    assert projects["other"].platform_schedules == other.platform_schedules


def _pending(target):
    return {
        "target_scheduled_at": target,
        "retries": 0,
        "last_error": "previous failure",
        "last_attempt_at": datetime.now(timezone.utc) - timedelta(minutes=10),
    }


@pytest.fixture(params=["route", "urgent"])
def track_notification(request):
    if request.param == "route":
        from app.api.routes.scheduling import _notify_displaced

        return _notify_displaced
    from app.services.urgent_upload_service import UrgentUploadService

    return UrgentUploadService._notify_and_track


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_success_clears_older_pending_only_for_current_target(
    scheduled, monkeypatch, track_notification, platform,
):
    projects, save = scheduled
    save("p1", **{platform: NEW})
    other_pending = _pending(OLD)
    projects["p1"].reschedule_pending = {
        platform: _pending(OLD), "youtube": other_pending,
    }
    monkeypatch.setattr(PlatformRescheduleService, "notify", lambda *args: NotificationResult("ok"))

    assert track_notification("p1", platform, NEW) == "ok"
    assert projects["p1"].reschedule_pending == {"youtube": other_pending}


@pytest.mark.parametrize("status", ["ok", "pending_retry"])
def test_stale_notification_preserves_newer_target_and_pending(
    scheduled, monkeypatch, track_notification, status,
):
    projects, save = scheduled
    save("p1", dailymotion=OLD)
    newer_pending = _pending(NEW)

    def notify_and_reschedule(*args):
        projects["p1"].platform_schedules["dailymotion"] = PlatformSchedule(slot=NEW, scheduled_at=NEW)
        projects["p1"].reschedule_pending = {"dailymotion": newer_pending}
        projects["p1"].anime_name = "Concurrent edit"
        return NotificationResult(status, "old response")

    monkeypatch.setattr(PlatformRescheduleService, "notify", notify_and_reschedule)
    assert track_notification("p1", "dailymotion", OLD) == status
    assert projects["p1"].reschedule_pending == {"dailymotion": newer_pending}
    assert projects["p1"].platform_schedules["dailymotion"].scheduled_at == NEW
    assert projects["p1"].anime_name == "Concurrent edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
async def test_retry_discards_superseded_target_without_notifying(scheduled, monkeypatch, platform):
    projects, save = scheduled
    save("p1", **{platform: NEW})
    projects["p1"].reschedule_pending = {platform: _pending(OLD)}

    def unexpected_notify(*args):
        pytest.fail("A superseded target must not reach the VPS")

    monkeypatch.setattr(PlatformRescheduleService, "notify", unexpected_notify)
    await RescheduleRetryService.run_once()
    assert projects["p1"].reschedule_pending == {}
    assert projects["p1"].platform_schedules[platform].scheduled_at == NEW


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "pending_retry"])
async def test_retry_preserves_replaced_and_added_pending_entries(scheduled, monkeypatch, status):
    projects, save = scheduled
    save("p1", dailymotion=OLD)
    projects["p1"].reschedule_pending = {"dailymotion": _pending(OLD)}
    newer_pending = _pending(NEW)
    added_pending = _pending(NEW)

    def notify_and_reschedule(*args):
        projects["p1"].platform_schedules["dailymotion"] = PlatformSchedule(slot=NEW, scheduled_at=NEW)
        projects["p1"].reschedule_pending = {
            "dailymotion": newer_pending, "snapchat": added_pending,
        }
        return NotificationResult(status, "old response")

    monkeypatch.setattr(PlatformRescheduleService, "notify", notify_and_reschedule)
    await RescheduleRetryService.run_once()
    assert projects["p1"].reschedule_pending == {
        "dailymotion": newer_pending, "snapchat": added_pending,
    }


@pytest.mark.asyncio
async def test_current_retry_failure_keeps_target_and_increments_attempt(scheduled, monkeypatch):
    projects, save = scheduled
    save("p1", bilibili=NEW)
    projects["p1"].reschedule_pending = {"bilibili": _pending(NEW)}
    monkeypatch.setattr(PlatformRescheduleService, "notify", lambda *args: NotificationResult(
        "pending_retry", "VPS unavailable",
    ))
    await RescheduleRetryService.run_once()
    entry = projects["p1"].reschedule_pending["bilibili"]
    assert entry["target_scheduled_at"] == NEW
    assert entry["retries"] == 1
    assert entry["last_error"] == "VPS unavailable"
