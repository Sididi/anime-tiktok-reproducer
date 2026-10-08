"""Opt-in, reservation and remote lifecycle coverage for direct publishers."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes import scheduling as routes
from app.api.routes.project_manager import UploadProjectRequest
from app.models import PlatformSchedule, Project
from app.services.account_service import ADDITIONAL_PLATFORMS, AccountService
from app.services.library_state_db import LibraryStateDb
from app.services.platform_reschedule_service import PlatformRescheduleService
from app.services.project_service import ProjectService
from app.services.project_upload_service import _platforms_to_reserve
from app.services.scheduling_service import SLOT_TZ, SchedulingService
from app.services.upload_phase import UploadPhaseService

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 8, tzinfo=UTC)
NEXT = datetime(2026, 10, 2, 12, tzinfo=UTC)


def test_original_platform_defaults_remain_while_new_destinations_are_opt_in():
    configured = AccountService._parse_account("legacy", {
        "language": "fr", "slots": ["14:00"],
        "youtube": {"refresh_token": "youtube-token"},
        "meta": {
            "facebook_page_id": "page", "facebook_page_access_token": "meta-token",
            "instagram_business_account_id": "instagram",
        },
    })
    originals = ("youtube", "facebook", "instagram", "tiktok")
    assert all(configured.slots_for(p) == ["14:00"] for p in originals)
    assert configured.tiktok_mode() == "manual"
    assert _platforms_to_reserve(configured, None) == list(originals)
    assert all(configured.slots_for(p) == [] for p in ADDITIONAL_PLATFORMS)
    assert not UploadPhaseService._compute_upfront_skips(originals[:3], configured)
    assert not UploadPhaseService._compute_upfront_skips(originals[:3], None)


def account(**platforms):
    return AccountService._parse_account(
        "account",
        {
            "name": "Account",
            "language": "fr",
            "slots": ["14:00", "18:00"],
            "tiktok": {"slots": []},
            **platforms,
        },
    )


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_existing_accounts_do_not_implicitly_enable_new_platforms(platform):
    configured = account()
    assert configured.slots_for(platform) == []
    assert platform not in _platforms_to_reserve(configured, None)
    assert (
        UploadPhaseService._compute_upfront_skips((platform,), configured)[
            platform
        ].status
        == "skipped"
    )


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_explicit_manual_config_inherits_slots_and_is_uploadable(platform):
    configured = account(**{platform: {}})
    assert getattr(configured, platform).mode == "manual"
    assert configured.slots_for(platform) == ["14:00", "18:00"]
    assert _platforms_to_reserve(configured, None) == [platform]
    assert _platforms_to_reserve(configured, [platform]) == [platform]
    assert _platforms_to_reserve(configured, ["youtube"]) == []
    assert not UploadPhaseService._compute_upfront_skips((platform,), configured)


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_platform_slots_replace_defaults_including_disabled_schedule(platform):
    configured = account(**{platform: {"slots": ["21:00"]}})
    assert configured.slots_for(platform) == ["21:00"]
    disabled = account(**{platform: {"slots": []}})
    assert disabled.slots_for(platform) == []
    assert _platforms_to_reserve(disabled, [platform]) == []


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_same_explicit_identity_shares_the_platform_pool(platform):
    configured = account(
        **{platform: {"identity": "same-destination", "client_secret": "hidden"}}
    )
    assert configured.pool_key_for(platform) == f"{platform}:same-destination"
    assert "hidden" not in repr(configured)


def test_profile_and_bilibili_mid_supply_automatic_pool_identity():
    configured = account(
        snapchat={"profile_id": "snap-profile"}, bilibili={"mid": "1234"}
    )
    assert configured.pool_key_for("snapchat") == "snapchat:snap-profile"
    assert configured.pool_key_for("bilibili") == "bilibili:1234"


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_invalid_publisher_mode_is_not_silently_downgraded(platform):
    with pytest.raises(ValueError, match=platform):
        account(**{platform: {"mode": "browser-automation"}})


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_upload_and_scheduling_models_accept_additional_platforms(platform):
    request = UploadProjectRequest(platforms=[platform], immediate_platforms=[platform])
    assert request.platforms == [platform]
    manual = routes.ReserveManualRequest(
        account_id="account", at=NEXT, platforms=[platform]
    )
    assert manual.platforms == [platform]
    switch = routes.SwitchPreviewRequest(
        account_id="account", platform=platform, slot=NEXT
    )
    assert switch.platform == platform
    assert UploadPhaseService._normalize_platforms([platform.upper(), platform]) == (
        platform,
    )


def test_requests_still_reject_unknown_platforms():
    with pytest.raises(ValidationError):
        UploadProjectRequest(platforms=["unknown"])
    with pytest.raises(ValidationError):
        routes.ReserveManualRequest(
            account_id="account", at=NEXT, platforms=["unknown"]
        )


@pytest.fixture
def vps(monkeypatch):
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_url",
        "https://vps.example",
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.settings.tiktok_server_internal_token",
        "internal-token",
    )


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_url_less_pending_rows_are_rescheduled_on_vps(monkeypatch, vps, platform):
    project = Project(
        id="p1",
        upload_completed_at=NOW,
        upload_last_result={
            "platforms": [{"platform": platform, "status": "pending", "url": None}],
        },
    )
    calls = []

    def patch(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("PATCH", url))

    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.patch", patch)
    result = PlatformRescheduleService.notify(project, platform, NEXT)
    assert result.status == "ok"
    assert calls[0][0] == "https://vps.example/api/internal/jobs/p1/slot"
    assert calls[0][1]["json"] == {
        "platform_scheduled_at": {platform: NEXT.isoformat()}
    }
    assert calls[0][1]["headers"] == {"Authorization": "Bearer internal-token"}


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_url_less_pending_rows_are_cancelled_on_vps(monkeypatch, vps, platform):
    project = Project(id="p1", upload_completed_at=NOW)
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr("app.services.platform_reschedule_service.httpx.post", post)
    assert PlatformRescheduleService.cancel(project, platform).status == "ok"
    assert calls == [
        f"https://vps.example/api/internal/jobs/p1/platforms/{platform}/cancel"
    ]


@pytest.mark.parametrize(
    "status,expected",
    [(404, "skipped"), (409, "pending_retry"), (503, "pending_retry")],
)
def test_remote_cancel_failure_is_not_reported_as_success(
    monkeypatch, vps, status, expected
):
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.post",
        lambda url, **kw: httpx.Response(status, request=httpx.Request("POST", url)),
    )
    result = PlatformRescheduleService.cancel(
        Project(id="p1", upload_completed_at=NOW), "snapchat"
    )
    assert result.status == expected


def test_missing_uploaded_job_cannot_report_reschedule_success(monkeypatch, vps):
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.patch",
        lambda url, **kw: httpx.Response(404, request=httpx.Request("PATCH", url)),
    )
    assert (
        PlatformRescheduleService.notify(
            Project(id="p1", upload_completed_at=NOW), "snapchat", NEXT
        ).status
        == "pending_retry"
    )
    assert (
        PlatformRescheduleService.notify(Project(id="p1"), "snapchat", NEXT).status
        == "skipped"
    )


@pytest.fixture
def isolated_scheduler(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    configured = account(**{p: {"mode": "manual"} for p in ADDITIONAL_PLATFORMS})
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", tmp_path / "projects"
    )
    monkeypatch.setattr(
        "app.services.project_service.settings.library_state_db_path", tmp_path / "library.db"
    )
    LibraryStateDb.initialize()
    monkeypatch.setattr("app.services.scheduling_service.datetime", FixedDatetime)
    monkeypatch.setattr(AccountService, "get_account", lambda account_id: configured)
    monkeypatch.setattr(AccountService, "list_accounts", lambda: [configured])
    monkeypatch.setattr(AccountService, "all_accounts", lambda: {"account": configured})
    project = Project(id="p1", scheduled_account_id="account")
    ProjectService.get_project_dir(project.id).mkdir(parents=True)
    ProjectService.save(project)
    return configured


def test_reserves_all_opted_in_platforms_with_paris_slot_semantics(isolated_scheduler):
    reserved = SchedulingService.reserve_all_platform_slots(
        "p1", "account", list(ADDITIONAL_PLATFORMS)
    )
    assert set(reserved) == set(ADDITIONAL_PLATFORMS)
    for slot, scheduled_at in reserved.values():
        assert slot.astimezone(SLOT_TZ).hour == 14
        assert slot >= NOW
        assert scheduled_at >= NOW
    assert set(ProjectService.load("p1").platform_schedules) == set(
        ADDITIONAL_PLATFORMS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 503])
async def test_failed_cancellation_preserves_local_reservation(
    isolated_scheduler, vps, monkeypatch, status
):
    SchedulingService.reserve_all_platform_slots("p1", "account", ["snapchat"])
    before = ProjectService.load("p1").platform_schedules["snapchat"]
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.post",
        lambda url, **kw: httpx.Response(status, request=httpx.Request("POST", url)),
    )
    with pytest.raises(HTTPException) as error:
        await routes.delete_platform("p1", "snapchat")
    assert error.value.status_code == status
    assert ProjectService.load("p1").platform_schedules["snapchat"] == before


@pytest.mark.asyncio
async def test_successful_cancel_releases_slot_for_other_projects(
    isolated_scheduler, vps, monkeypatch
):
    SchedulingService.reserve_all_platform_slots("p1", "account", ["snapchat"])
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.post",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("POST", url)),
    )
    await routes.delete_platform("p1", "snapchat")
    assert ProjectService.load("p1").platform_schedules == {}


@pytest.mark.asyncio
async def test_bulk_cancel_preserves_only_failed_and_unattempted_reservations(
    isolated_scheduler, vps, monkeypatch
):
    SchedulingService.reserve_all_platform_slots(
        "p1", "account", list(ADDITIONAL_PLATFORMS)
    )
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.post",
        lambda url, **kw: httpx.Response(
            503 if "/snapchat/" in url else 200, request=httpx.Request("POST", url)
        ),
    )
    with pytest.raises(HTTPException):
        await routes.delete_all("p1")
    assert set(ProjectService.load("p1").platform_schedules) == {"snapchat", "bilibili"}


@pytest.mark.asyncio
async def test_remote_reschedule_failure_remains_visible_for_retry(
    isolated_scheduler, vps, monkeypatch
):
    SchedulingService.reserve_all_platform_slots("p1", "account", ["snapchat"])
    monkeypatch.setattr(
        "app.services.platform_reschedule_service.httpx.patch",
        lambda url, **kw: httpx.Response(503, request=httpx.Request("PATCH", url)),
    )
    result = await routes.patch_platform(
        "p1", "snapchat", routes.PatchPlatformRequest(new_slot=NEXT)
    )
    assert result["notification_status"] == "pending_retry"
    latest = ProjectService.load("p1")
    persisted_target = datetime.fromisoformat(
        latest.reschedule_pending["snapchat"]["target_scheduled_at"]
    )
    assert persisted_target == latest.platform_schedules["snapchat"].scheduled_at


@pytest.mark.parametrize("platform", ADDITIONAL_PLATFORMS)
def test_past_slot_without_upload_confirmation_stays_dispatched(monkeypatch, platform):
    project = Project(
        id="p1",
        final_upload_discord_message_id="message-1",
        platform_schedules={
            platform: PlatformSchedule(
                slot=NOW - timedelta(days=1), scheduled_at=NOW - timedelta(days=1)
            ),
        },
    )
    monkeypatch.setattr(
        "app.services.vps_status_sync_service.VpsStatusSyncService.cached_status",
        lambda *args: None,
    )
    monkeypatch.setattr(routes, "_local_job_running", lambda p: False)
    assert routes._project_event_status(project, platform) == ("dispatched", None)
