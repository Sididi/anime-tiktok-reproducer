from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...library_types import DEFAULT_LIBRARY_TYPE, LibraryType
from ...services.project_startup_service import project_startup_queue


router = APIRouter(prefix="/projects", tags=["startup"])


class StartProjectAsyncRequest(BaseModel):
    tiktok_url: str
    anime_name: str | None = None
    series_id: str | None = None
    library_type: LibraryType = DEFAULT_LIBRARY_TYPE


@router.post("/start-async")
async def start_project_async(request: StartProjectAsyncRequest):
    """Create a project and launch startup work in the background."""
    try:
        job = await project_startup_queue.start_project(
            tiktok_url=request.tiktok_url,
            anime_name=request.anime_name,
            series_id=request.series_id,
            library_type=request.library_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return job.model_dump(mode="json")


@router.post("/{project_id}/startup/retry")
async def retry_project_startup(project_id: str):
    """Retry a failed or interrupted project startup."""
    try:
        job = await project_startup_queue.retry_project(project_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return job.model_dump(mode="json")


@router.get("/startup/jobs")
async def list_project_startup_jobs():
    """List persisted project startup jobs.

    Live updates flow over the shared ``/api/events/stream`` (topic
    ``startup_jobs``); this REST snapshot remains for tooling and tests.
    """
    return {
        "jobs": [job.model_dump(mode="json") for job in project_startup_queue.list_jobs()],
    }
