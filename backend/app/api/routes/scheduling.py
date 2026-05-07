from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ...models import Project
from ...services.account_service import AccountService
from ...services.project_service import ProjectService
from ...services.scheduling_service import SchedulingService

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

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
    status: Literal["scheduled", "running", "complete"]


class FreeSlotResponse(BaseModel):
    slot: datetime
    available: bool
    taken_by_project_id: str | None = None


def _project_event_status(project: Project, platform: str) -> str:
    from ...services.project_upload_service import project_upload_queue  # noqa: PLC0415
    for job in project_upload_queue.list_jobs():
        if job.project_id == project.id:
            if job.status == "running":
                return "running"
            if job.status == "complete":
                return "complete"
    return "scheduled"


def _build_planning_event(
    project: Project, platform: str, accounts: dict
) -> PlanningEvent | None:
    sched = (project.platform_schedules or {}).get(platform)
    if sched is None or project.scheduled_account_id is None:
        return None
    account = accounts.get(project.scheduled_account_id)
    if account is None:
        return None
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
        status=_project_event_status(project, platform),  # type: ignore[arg-type]
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
    try:
        slots = await asyncio.to_thread(
            SchedulingService.find_free_slots_after,
            account_id, platform, after, limit,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {
        "slots": [
            FreeSlotResponse(
                slot=s.slot,
                available=s.available,
                taken_by_project_id=s.taken_by_project_id,
            ).model_dump(mode="json")
            for s in slots
        ]
    }


class ResolveAnchorRequest(BaseModel):
    project_id: str
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


class ReserveAnchorRequest(BaseModel):
    account_id: str
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


class PatchPlatformRequest(BaseModel):
    new_slot: datetime


class PatchAnchorRequest(BaseModel):
    tiktok_slot: datetime
    overrides: dict[str, datetime] | None = None


def _platform_schedules_to_dict(schedules):
    return {
        p: {"slot": s.slot.isoformat(), "scheduled_at": s.scheduled_at.isoformat()}
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
        req.account_id, req.tiktok_slot, req.overrides,
    )
    return {
        "resolved": {
            p: {"slot": r.slot.isoformat(), "scheduled_at": r.scheduled_at.isoformat(),
                "available": r.available}
            for p, r in result.resolved.items()
        },
        "conflicts": [{"platform": c.platform, "reason": c.reason} for c in result.conflicts],
    }


@router.post("/projects/{project_id}/reserve-anchor")
async def reserve_anchor(project_id: str, req: ReserveAnchorRequest):
    try:
        schedules = await asyncio.to_thread(
            SchedulingService.reserve_anchor,
            project_id, req.account_id, req.tiktok_slot, req.overrides,
        )
    except ValueError as exc:
        msg = str(exc)
        if "tiktok" in msg and "slot_taken" in msg:
            raise HTTPException(409, "tiktok_slot_taken")
        if "slot_not_configured" in msg:
            raise HTTPException(422, "invalid_slot")
        if "Project not found" in msg:
            raise HTTPException(404, msg)
        raise HTTPException(422, msg)
    return {"platform_schedules": _platform_schedules_to_dict(schedules)}


@router.patch("/projects/{project_id}/platforms/{platform}")
async def patch_platform(project_id: str, platform: str, req: PatchPlatformRequest):
    try:
        sched = await asyncio.to_thread(
            SchedulingService.reschedule_platform, project_id, platform, req.new_slot
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
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
    try:
        schedules = await asyncio.to_thread(
            SchedulingService.reschedule_anchor,
            project_id, req.tiktok_slot, req.overrides,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    statuses: dict[str, str] = {}
    for platform, sched in schedules.items():
        statuses[platform] = await asyncio.to_thread(
            _notify_displaced, project_id, platform, sched.scheduled_at
        )
    return {
        "platform_schedules": _platform_schedules_to_dict(schedules),
        "notification_status": statuses,
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
