from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...config import settings as app_settings
from ...models import Project
from ...services.account_service import AccountService
from ...services.project_service import ProjectService
from ...services.scheduling_service import SchedulingService, StealSpec


def _require_v2() -> None:
    if not app_settings.scheduling_v2_enabled:
        raise HTTPException(503, "scheduling_v2_disabled")


router = APIRouter(
    prefix="/scheduling",
    tags=["scheduling"],
    dependencies=[Depends(_require_v2)],
)

Platform = Literal["youtube", "facebook", "instagram", "tiktok"]


class PlanningEvent(BaseModel):
    project_id: str
    anime_title: str
    account_id: str
    account_avatar_url: str
    account_name: str
    platform: Platform
    slot: datetime
    scheduled_at: datetime
    drive_folder_url: str | None
    # "dispatched" = handed to the VPS scheduler (IG/TT), not yet confirmed.
    status: Literal["scheduled", "dispatched", "running", "complete", "failed"]
    posted_url: str | None = None
    manual: bool = False
    timing_locked: bool = False


class FreeSlotResponse(BaseModel):
    slot: datetime
    available: bool
    taken_by_project_id: str | None = None
    taken_by_title: str | None = None


def _platform_posted_url_exists(project: Project, platform: str) -> bool:
    from ...services.scheduling_service import platform_upload_result_entry  # noqa: PLC0415

    entry = platform_upload_result_entry(project, platform)
    return bool(entry and entry.get("url"))


def _local_job_running(project: Project) -> bool:
    from ...services.project_upload_service import project_upload_queue  # noqa: PLC0415

    return any(
        j.project_id == project.id and j.status == "running"
        for j in project_upload_queue.list_jobs()
    )


def _pfm_event_status(project: Project) -> tuple[str, str | None] | None:
    """Status for a backend-owned Post for Me TikTok post (2026-08 migration).

    Returns None for legacy projects without tiktok_pfm state — those fall
    back to the VPS-status derivation."""
    state = project.tiktok_pfm
    if state is None:
        return None
    if state.stage == "published":
        return "complete", state.url
    if state.stage == "failed":
        return "failed", None
    if state.stage == "post_created":
        # Instant post or past-publish-instant scheduled post: processing.
        return "running", None
    if state.stage == "post_scheduled":
        # Post exists on PFM, fires server-side at scheduled_at.
        return "dispatched", None
    if state.stage == "media_uploaded":
        return "running", None
    return "scheduled", None


def _vps_event_status(project: Project, platform: str) -> tuple[str, str | None]:
    """Status for VPS-published platforms (instagram/tiktok).

    The VPS owns publication; locally we know: the live/synced VPS status,
    persisted terminal outcomes, and whether the job was dispatched at all.
    With no contrary information a past slot is presumed published (the VPS
    pings Discord and its failures get synced back on the next planning
    open)."""
    from ...services.scheduling_service import platform_upload_result_entry  # noqa: PLC0415
    from ...services.vps_status_sync_service import VpsStatusSyncService  # noqa: PLC0415

    source = VpsStatusSyncService.cached_status(
        project.id, platform
    ) or platform_upload_result_entry(project, platform)
    if source:
        status = source.get("status")
        url = source.get("url")
        if status == "uploaded":
            return "complete", url
        if status == "failed":
            return "failed", None
        if status == "uploading":
            return "running", None
        if status == "pending":
            return "dispatched", None
        # "skipped": locally skipped during prep — nothing will publish.
        return "scheduled", None

    if _local_job_running(project):
        return "running", None

    if project.final_upload_discord_message_id:
        # Dispatched to the VPS, no news: presumed on track / published.
        sched = (project.platform_schedules or {}).get(platform)
        if sched is not None:
            sched_at = sched.scheduled_at
            if sched_at.tzinfo is None:
                sched_at = sched_at.replace(tzinfo=timezone.utc)
            if sched_at <= datetime.now(timezone.utc):
                return "complete", None
        return "dispatched", None

    return "scheduled", None


def _project_event_status(project: Project, platform: str) -> tuple[str, str | None]:
    """Per-platform (status, posted_url).

    YT/FB publish locally: derive from the latest upload job's incremental
    platform_results, falling back to the persisted upload_last_result.
    IG/TT publish on the VPS: see _vps_event_status."""
    from ...services.project_upload_service import project_upload_queue  # noqa: PLC0415
    from ...services.scheduling_service import platform_upload_result_entry  # noqa: PLC0415

    if platform == "tiktok":
        pfm_status = _pfm_event_status(project)
        if pfm_status is not None:
            return pfm_status
        return _vps_event_status(project, platform)
    if platform == "instagram":
        return _vps_event_status(project, platform)
    if platform == "facebook":
        # Long-range (>29d) holds live on the VPS: once its status cache has
        # a facebook entry, it is authoritative (pending hold → "dispatched",
        # converted → "complete"). Native posts keep the local derivation.
        from ...services.vps_status_sync_service import VpsStatusSyncService  # noqa: PLC0415

        if not _local_job_running(project) and (
            VpsStatusSyncService.cached_status(project.id, "facebook") is not None
        ):
            return _vps_event_status(project, platform)

    jobs = [j for j in project_upload_queue.list_jobs() if j.project_id == project.id]
    persisted = platform_upload_result_entry(project, platform)
    persisted_url = persisted.get("url") if persisted else None
    if not jobs:
        return ("complete", persisted_url) if persisted_url else ("scheduled", None)
    job = max(jobs, key=lambda j: j.updated_at)
    entry = next(
        (r for r in (job.platform_results or []) if str(r.get("platform")) == platform),
        None,
    )
    if entry is not None:
        result_status = entry.get("status")
        if result_status == "uploaded":
            return "complete", entry.get("url")
        if result_status == "failed":
            return "failed", None
        if result_status == "skipped":
            return "scheduled", None
    if job.status == "running":
        return "running", None
    if persisted_url:
        return "complete", persisted_url
    if job.status == "error":
        return "failed", None
    if job.status == "complete":
        # Legacy jobs without platform_results.
        return "complete", None
    return "scheduled", None


def _build_planning_event(
    project: Project, platform: str, accounts: dict
) -> PlanningEvent | None:
    sched = (project.platform_schedules or {}).get(platform)
    if sched is None or project.scheduled_account_id is None:
        return None
    account = accounts.get(project.scheduled_account_id)
    if account is None:
        return None
    status, posted_url = _project_event_status(project, platform)
    return PlanningEvent(
        project_id=project.id,
        anime_title=project.anime_name or project.id,
        account_id=account.id,
        account_avatar_url=f"/api/accounts/{account.id}/avatar",
        account_name=account.name,
        platform=platform,  # type: ignore[arg-type]
        slot=sched.slot,
        scheduled_at=sched.scheduled_at,
        drive_folder_url=project.drive_folder_url,
        status=status,  # type: ignore[arg-type]
        posted_url=posted_url,
        manual=sched.manual,
        timing_locked=SchedulingService.tiktok_timing_locked(project),
    )


def _platforms_visible_for_account_filter(
    selected_account_id: str | None,
    project: Project,
    platform: str,
) -> bool:
    if selected_account_id is None:
        return True
    if project.scheduled_account_id is None:
        return False
    selected = AccountService.get_account(selected_account_id)
    owner = AccountService.get_account(project.scheduled_account_id)
    if selected is None or owner is None:
        return False
    selected_pool = selected.pool_key_for(platform) or f"account:{selected.id}:{platform}"
    owner_pool = owner.pool_key_for(platform) or f"account:{owner.id}:{platform}"
    return selected_pool == owner_pool


@router.get("/events")
async def list_events(
    account_id: str | None = None,
    platforms: str | None = None,  # CSV
    range_start: datetime | None = None,
    range_end: datetime | None = None,
):
    from ...services.pfm_status_sync_service import PfmStatusSyncService  # noqa: PLC0415
    from ...services.vps_status_sync_service import VpsStatusSyncService  # noqa: PLC0415

    # Fire-and-forget: pull IG (and legacy TT) publish outcomes from the VPS
    # job store, and TikTok outcomes from Post for Me (both throttled
    # internally; results land in cache + project.json for the next refetch).
    VpsStatusSyncService.request_sync()
    PfmStatusSyncService.request_sync()

    accounts = AccountService.all_accounts()
    wanted_platforms = (
        [p.strip() for p in platforms.split(",") if p.strip()]
        if platforms else ["youtube", "facebook", "instagram", "tiktok"]
    )
    events: list[dict] = []
    now = datetime.now(tz=range_start.tzinfo if range_start else timezone.utc)

    for project in await asyncio.to_thread(ProjectService.list_all):
        for platform in wanted_platforms:
            if not _platforms_visible_for_account_filter(account_id, project, platform):
                continue
            ev = _build_planning_event(project, platform, accounts)
            if ev is None:
                continue
            if ev.slot < (range_start or now):
                continue
            if range_end and ev.slot > range_end:
                continue
            events.append(ev.model_dump(mode="json"))
    return {"events": events}


@router.get("/free-slots")
async def free_slots(
    account_id: str,
    platform: Platform,
    after: datetime,
    limit: int = Query(default=20, ge=1, le=200),
):
    slots = await asyncio.to_thread(
        SchedulingService.find_free_slots_after,
        account_id, platform, after, limit,
    )
    return {
        "slots": [
            FreeSlotResponse(
                slot=s.slot,
                available=s.available,
                taken_by_project_id=s.taken_by_project_id,
                taken_by_title=s.taken_by_title,
            ).model_dump(mode="json")
            for s in slots
        ]
    }


_FREE_SLOTS_RANGE_MAX_DAYS = 62


@router.get("/free-slots-range")
async def free_slots_range(
    account_id: str,
    range_start: datetime,
    range_end: datetime,
    platforms: str | None = None,  # CSV; defaults to all four
):
    """Configured slots (free or taken) per platform inside a date range.

    Powers the planning board's ghost slots. Platforms without configured
    slots return an empty list."""
    if range_end <= range_start:
        raise HTTPException(422, "invalid_range")
    if range_end - range_start > timedelta(days=_FREE_SLOTS_RANGE_MAX_DAYS):
        raise HTTPException(422, "range_too_large")
    wanted_platforms = (
        [p.strip() for p in platforms.split(",") if p.strip()]
        if platforms else ["youtube", "facebook", "instagram", "tiktok"]
    )

    def _collect() -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for platform in wanted_platforms:
            slots = SchedulingService.find_slots_in_range(
                account_id, platform, range_start, range_end
            )
            out[platform] = [
                FreeSlotResponse(
                    slot=s.slot,
                    available=s.available,
                    taken_by_project_id=s.taken_by_project_id,
                    taken_by_title=s.taken_by_title,
                ).model_dump(mode="json")
                for s in slots
            ]
        return out

    return {"slots": await asyncio.to_thread(_collect)}


class ResolveAnchorRequest(BaseModel):
    project_id: str
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


class StealSpecModel(BaseModel):
    # "relocate" = single push of the occupant to its nearest free slots,
    # TikTok-first (the UI's only offered mode since 2026-08; the older modes
    # stay accepted).
    mode: Literal["cascade", "next_free", "relocate"]
    expected_occupant_id: str | None = None


class ReserveAnchorRequest(BaseModel):
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None
    steals: dict[str, StealSpecModel] | None = None
    # User acknowledged the "a displaced project's TikTok would land after
    # its other platforms" warning.
    confirm_before_tiktok: bool = False


class PatchPlatformRequest(BaseModel):
    new_slot: datetime
    # User acknowledged the "another platform would post before TikTok" warning.
    confirm_before_tiktok: bool = False


class PatchAnchorRequest(BaseModel):
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None
    steals: dict[str, StealSpecModel] | None = None
    confirm_before_tiktok: bool = False


def _platform_schedules_to_dict(schedules):
    return {
        p: {
            "slot": s.slot.isoformat(),
            "scheduled_at": s.scheduled_at.isoformat(),
            "manual": s.manual,
        }
        for p, s in schedules.items()
    }


def _notify_displaced(project_id: str, platform: str, new_scheduled_at: datetime) -> str:
    """Trigger platform notification, return 'ok' / 'pending_retry' / 'skipped'.

    Mutates project.reschedule_pending on pending_retry to feed the retry loop.
    """
    from ...services.platform_reschedule_service import PlatformRescheduleService  # noqa: PLC0415
    project = ProjectService.load(project_id)
    if project is None:
        return "skipped"
    result = PlatformRescheduleService.notify(project, platform, new_scheduled_at)
    if result.status == "pending_retry":
        pending = dict(project.reschedule_pending or {})
        pending[platform] = {
            "target_scheduled_at": new_scheduled_at,
            "retries": 0,
            "last_error": result.error,
            "last_attempt_at": datetime.now(tz=new_scheduled_at.tzinfo),
        }
        project.reschedule_pending = pending
        ProjectService.save(project)
    return result.status


@router.post("/resolve-anchor")
async def resolve_anchor(req: ResolveAnchorRequest):
    result = await asyncio.to_thread(
        SchedulingService.resolve_anchor,
        req.account_id, req.tiktok_slot, req.overrides, req.project_id,
    )
    return {
        "resolved": {
            p: {"slot": r.slot.isoformat(), "scheduled_at": r.scheduled_at.isoformat(),
                "available": r.available}
            for p, r in result.resolved.items()
        },
        "conflicts": [{"platform": c.platform, "reason": c.reason} for c in result.conflicts],
    }


def _steals_to_specs(steals):
    return (
        {p: StealSpec(mode=s.mode, expected_occupant_id=s.expected_occupant_id)
         for p, s in steals.items()}
        if steals else None
    )


async def _notify_switch_displacements(switches, steals) -> dict[str, dict[str, str]]:
    notification_status: dict[str, dict[str, str]] = {}
    for platform, result in switches.items():
        spec = steals[platform]
        plan = result.plan_for(spec.mode)
        notification_status[platform] = {}
        for displaced in plan.displaced:
            # Relocate follow moves carry their own platform.
            item_platform = displaced.platform or platform
            moved = await ProjectService.aload(displaced.project_id)
            sched = (moved.platform_schedules or {}).get(item_platform) if moved else None
            if sched is None:
                continue
            key = (
                displaced.project_id
                if item_platform == platform
                else f"{displaced.project_id}:{item_platform}"
            )
            notification_status[platform][key] = await asyncio.to_thread(
                _notify_displaced, displaced.project_id, item_platform, sched.scheduled_at
            )
    return notification_status


@router.post("/projects/{project_id}/reserve-anchor")
async def reserve_anchor(project_id: str, req: ReserveAnchorRequest):
    steals = _steals_to_specs(req.steals)
    schedules, switches = await asyncio.to_thread(
        SchedulingService.reserve_anchor,
        project_id, req.account_id, req.tiktok_slot, req.overrides, steals,
        req.confirm_before_tiktok,
    )

    notification_status = await _notify_switch_displacements(switches, req.steals or {})
    return {
        "platform_schedules": _platform_schedules_to_dict(schedules),
        "notification_status": notification_status,
    }


class ReserveManualRequest(BaseModel):
    account_id: str
    at: datetime
    platforms: list[Platform] | None = None


@router.post("/projects/{project_id}/reserve-manual")
async def reserve_manual(project_id: str, req: ReserveManualRequest):
    platforms = list(req.platforms) if req.platforms else None
    if not platforms:
        from ...services.project_upload_service import _platforms_to_reserve  # noqa: PLC0415
        account = AccountService.get_account(req.account_id)
        if account is None:
            raise HTTPException(404, "Account not found")
        platforms = _platforms_to_reserve(account, requested_platforms=None)
    schedules = await asyncio.to_thread(
        SchedulingService.reserve_manual,
        project_id, req.account_id, req.at, platforms,
    )

    # Editing an already-uploaded manual schedule must reach the platforms
    # (YT publishAt, FB scheduled_publish_time, VPS reminder). For not-yet
    # uploaded projects every notify is a cheap skip.
    statuses: dict[str, str] = {}
    for platform, sched in schedules.items():
        statuses[platform] = await asyncio.to_thread(
            _notify_displaced, project_id, platform, sched.scheduled_at
        )
    return {
        "platform_schedules": _platform_schedules_to_dict(schedules),
        "notification_status": statuses,
    }


@router.patch("/projects/{project_id}/platforms/{platform}")
async def patch_platform(project_id: str, platform: str, req: PatchPlatformRequest):
    sched = await asyncio.to_thread(
        SchedulingService.reschedule_platform,
        project_id, platform, req.new_slot, req.confirm_before_tiktok,
    )
    notif_status = await asyncio.to_thread(
        _notify_displaced, project_id, platform, sched.scheduled_at
    )
    return {
        "slot": sched.slot.isoformat(),
        "scheduled_at": sched.scheduled_at.isoformat(),
        "notification_status": notif_status,
    }


@router.patch("/projects/{project_id}/anchor")
async def patch_anchor(project_id: str, req: PatchAnchorRequest):
    steals = _steals_to_specs(req.steals)
    schedules, switches = await asyncio.to_thread(
        SchedulingService.reschedule_anchor,
        project_id, req.tiktok_slot, req.overrides, steals,
        req.confirm_before_tiktok,
    )

    # Notify the rescheduled project's own platforms (its slots moved).
    statuses: dict[str, str] = {}
    for platform, sched in schedules.items():
        statuses[platform] = await asyncio.to_thread(
            _notify_displaced, project_id, platform, sched.scheduled_at
        )
    # Notify projects displaced by any steal.
    displaced_status = await _notify_switch_displacements(switches, req.steals or {})
    return {
        "platform_schedules": _platform_schedules_to_dict(schedules),
        "notification_status": statuses,
        "displaced_notification_status": displaced_status,
    }


def _notify_cancellation(project_id: str, platform: str) -> str:
    from ...services.platform_reschedule_service import PlatformRescheduleService  # noqa: PLC0415
    project = ProjectService.load(project_id)
    if project is None:
        return "skipped"
    return PlatformRescheduleService.cancel(project, platform).status


@router.delete("/projects/{project_id}/platforms/{platform}", status_code=204)
async def delete_platform(project_id: str, platform: str):
    await asyncio.to_thread(_notify_cancellation, project_id, platform)
    await asyncio.to_thread(SchedulingService.cancel_platform_slot, project_id, platform)


@router.delete("/projects/{project_id}/all", status_code=204)
async def delete_all(project_id: str):
    project = await asyncio.to_thread(ProjectService.load, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    for platform in list(project.platform_schedules.keys()):
        await asyncio.to_thread(_notify_cancellation, project_id, platform)
    await asyncio.to_thread(SchedulingService.cancel_all_slots, project_id)


class CascadeRequest(BaseModel):
    account_id: str
    confirm_before_tiktok: bool = False


def _precedence_warnings_payload(warnings) -> list[dict]:
    return [
        {
            "project_id": w.project_id,
            "anime_title": w.anime_title,
            "platforms": list(w.platforms),
        }
        for w in warnings
    ]


def _cascade_to_payload(result) -> dict:
    return {
        "per_platform": [
            {
                "platform": p.platform,
                "target_slot": p.target_slot.isoformat(),
                "target_scheduled_at": p.target_scheduled_at.isoformat(),
                "displaced": [
                    {
                        "project_id": d.project_id,
                        "anime_title": d.anime_title,
                        "from_slot": d.from_slot.isoformat(),
                        "to_slot": d.to_slot.isoformat(),
                        "requires_platform_notification": d.requires_platform_notification,
                    }
                    for d in p.displaced
                ],
                "precedence_warnings": _precedence_warnings_payload(p.precedence_warnings),
            }
            for p in result.per_platform
        ],
        "blockers": [{"platform": b.platform, "reason": b.reason} for b in result.blockers],
    }


@router.post("/projects/{project_id}/cascade-preview")
async def cascade_preview(project_id: str, req: CascadeRequest):
    result = await asyncio.to_thread(
        SchedulingService.compute_cascade, project_id, req.account_id
    )
    return _cascade_to_payload(result)


@router.post("/projects/{project_id}/cascade-apply")
async def cascade_apply(project_id: str, req: CascadeRequest):
    result = await asyncio.to_thread(
        SchedulingService.apply_cascade,
        project_id, req.account_id, req.confirm_before_tiktok,
    )

    notification_status: dict[str, dict[str, str]] = {}
    for plat in result.per_platform:
        notification_status[plat.platform] = {}
        for displaced in plat.displaced:
            ts = await asyncio.to_thread(
                _notify_displaced,
                displaced.project_id,
                plat.platform,
                # use the recomputed scheduled_at written by apply_cascade
                (await ProjectService.aload(displaced.project_id))
                    .platform_schedules[plat.platform].scheduled_at,
            )
            notification_status[plat.platform][displaced.project_id] = ts

    payload = _cascade_to_payload(result)
    payload["notification_status"] = notification_status
    return payload


class SwitchPreviewRequest(BaseModel):
    account_id: str
    platform: Platform
    slot: datetime


class SwitchApplyRequest(SwitchPreviewRequest):
    mode: Literal["cascade", "next_free", "relocate"]
    expected_occupant_id: str | None = None
    # User acknowledged the "another platform would post before TikTok" warning.
    confirm_before_tiktok: bool = False


def _switch_plan_payload(plan) -> dict:
    return {
        "displaced": [
            {
                "project_id": d.project_id,
                "anime_title": d.anime_title,
                "from_slot": d.from_slot.isoformat(),
                "to_slot": d.to_slot.isoformat(),
                "requires_platform_notification": d.requires_platform_notification,
                # None = the stolen slot's platform; set on relocate follow moves.
                "platform": d.platform,
            }
            for d in plan.displaced
        ],
        "blockers": [{"platform": b.platform, "reason": b.reason} for b in plan.blockers],
        "precedence_warnings": _precedence_warnings_payload(plan.precedence_warnings),
    }


def _switch_to_payload(result) -> dict:
    return {
        "platform": result.platform,
        "slot": result.slot.isoformat(),
        "occupant_project_id": result.occupant_project_id,
        "occupant_title": result.occupant_title,
        "uploaded_count": result.uploaded_count,
        "cascade": _switch_plan_payload(result.cascade),
        "next_free": _switch_plan_payload(result.next_free),
        "relocate": _switch_plan_payload(result.relocate),
    }


@router.post("/projects/{project_id}/switch-preview")
async def switch_preview(project_id: str, req: SwitchPreviewRequest):
    result = await asyncio.to_thread(
        SchedulingService.compute_switch,
        project_id, req.account_id, req.platform, req.slot,
    )
    return _switch_to_payload(result)


@router.post("/projects/{project_id}/switch-apply")
async def switch_apply(project_id: str, req: SwitchApplyRequest):
    result = await asyncio.to_thread(
        SchedulingService.apply_switch,
        project_id, req.account_id, req.platform, req.slot,
        req.mode, req.expected_occupant_id, req.confirm_before_tiktok,
    )

    plan = result.plan_for(req.mode)
    notification_status: dict[str, str] = {}
    for displaced in plan.displaced:
        item_platform = displaced.platform or req.platform
        moved = await ProjectService.aload(displaced.project_id)
        if moved is None:
            continue
        sched = (moved.platform_schedules or {}).get(item_platform)
        if sched is None:
            continue
        key = (
            displaced.project_id
            if item_platform == req.platform
            else f"{displaced.project_id}:{item_platform}"
        )
        notification_status[key] = await asyncio.to_thread(
            _notify_displaced, displaced.project_id, item_platform, sched.scheduled_at
        )
    payload = _switch_to_payload(result)
    payload["notification_status"] = notification_status
    return payload


class UrgentPreviewRequest(BaseModel):
    account_id: str
    tiktok_only: bool = False


class UrgentShiftModel(BaseModel):
    """One colliding project's deferred re-timing, applied at final confirm."""

    project_id: str
    kind: Literal["anchor", "platform"] = "anchor"
    platform: Platform | None = None      # kind = "platform"
    tiktok_slot: datetime | None = None   # kind = "anchor"
    slot: datetime | None = None          # kind = "platform"
    manual_at: datetime | None = None
    overrides: dict[str, datetime] | None = None
    steals: dict[str, StealSpecModel] | None = None
    # Race guard: {platform: scheduled_at ISO seen at preview time}. A post
    # that changed/published meanwhile degrades to an "unmovable" item result.
    expected_scheduled_at: dict[str, str] = {}


class UrgentOwnReservations(BaseModel):
    """TikTok-only mode: when the urgent project's other platforms are
    scheduled instead of published immediately."""

    first_slot: datetime | None = None
    manual_at: datetime | None = None


class UrgentApplyRequest(BaseModel):
    account_id: str
    tiktok_only: bool = False
    shifts: list[UrgentShiftModel] = []
    own_reservations: UrgentOwnReservations | None = None
    confirm_before_tiktok: bool = True


@router.post("/projects/{project_id}/urgent-preview")
async def urgent_preview(project_id: str, req: UrgentPreviewRequest):
    """Two-phase (<60 min, same pool) collision scan for urgent-immediate.

    Read-only: the whole urgent flow defers every mutation to urgent-apply."""
    from ...services.urgent_upload_service import UrgentUploadService  # noqa: PLC0415

    return await asyncio.to_thread(
        UrgentUploadService.compute_preview, project_id, req.account_id, req.tiktok_only
    )


@router.post("/projects/{project_id}/urgent-apply")
async def urgent_apply(project_id: str, req: UrgentApplyRequest):
    """Apply the urgent plan: shift the colliding posts (per-item degradation)
    and, in TikTok-only mode, reserve the urgent project's other platforms.
    The immediate upload itself is then triggered via the normal upload
    endpoint with immediate=true."""
    from ...services.urgent_upload_service import UrgentUploadService  # noqa: PLC0415

    shifts = [s.model_dump(mode="json") for s in req.shifts]
    own = req.own_reservations.model_dump(mode="json") if req.own_reservations else None
    return await asyncio.to_thread(
        lambda: UrgentUploadService.apply_plan(
            project_id,
            req.account_id,
            shifts=shifts,
            own_reservations=own,
            tiktok_only=req.tiktok_only,
            confirm_before_tiktok=req.confirm_before_tiktok,
        )
    )


@router.get("/reschedule-pending")
async def reschedule_pending():
    items: list[dict] = []
    for project in await asyncio.to_thread(ProjectService.list_all):
        for platform, entry in (project.reschedule_pending or {}).items():
            items.append({
                "project_id": project.id,
                "platform": platform,
                "target_scheduled_at": entry.get("target_scheduled_at"),
                "retries": entry.get("retries", 0),
                "last_error": entry.get("last_error"),
                "last_attempt_at": entry.get("last_attempt_at"),
            })
    return {"items": items}
