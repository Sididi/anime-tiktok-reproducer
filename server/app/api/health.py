"""GET /healthz — uptime + counts. No auth."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    store = request.app.state.job_store
    pending = sum(
        1 for j in await store.list_all()
        if any(ps.status == "pending" for ps in j.platform_statuses.values())
    )
    return {"status": "ok", "jobs_pending": pending}
