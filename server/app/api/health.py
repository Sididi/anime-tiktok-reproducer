"""GET /healthz — uptime + counts. No auth."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    store = request.app.state.job_store
    settings = request.app.state.settings
    pending = 0
    for device_id in settings.devices:
        pending += len(await store.list_for_device(device_id, status="pending"))
    return {"status": "ok", "jobs_pending": pending}
