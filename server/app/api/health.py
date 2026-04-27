"""GET /healthz — uptime + counts. No auth."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    store = request.app.state.job_store
    pending = len(await store.list_all(status="pending"))
    return {"status": "ok", "jobs_pending": pending}
