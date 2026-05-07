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
