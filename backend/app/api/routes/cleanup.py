"""Pure-mode cleanup routes: zones, preview, full inpainting job."""

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...library_types import LibraryType
from ...models import ProjectPhase
from ...models.cleanup import CleanupState, CleanupZone
from ...services import ProjectService
from ...services.video_cleanup_service import VideoCleanupService

logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/projects/{project_id}/cleanup", tags=["cleanup"])


def _require_pure(project_id: str):
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.library_type != LibraryType.PURE:
        raise HTTPException(
            status_code=400, detail="Cleanup is only available for Pure projects"
        )
    return project


class SaveZonesRequest(BaseModel):
    zones: list[CleanupZone]


class PreviewRequest(BaseModel):
    timestamp: float = 0.0


@router.get("")
async def get_cleanup_state(project_id: str) -> CleanupState:
    _require_pure(project_id)
    return VideoCleanupService.get_state(project_id)


@router.put("/zones")
async def save_zones(project_id: str, request: SaveZonesRequest) -> CleanupState:
    _require_pure(project_id)
    try:
        return VideoCleanupService.save_zones(project_id, request.zones)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/preview")
async def render_preview(project_id: str, request: PreviewRequest) -> dict:
    _require_pure(project_id)
    try:
        return await VideoCleanupService.render_preview(
            project_id, timestamp=request.timestamp
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/preview/{which}")
async def get_preview(project_id: str, which: str):
    _require_pure(project_id)
    if which not in ("before", "after"):
        raise HTTPException(status_code=404, detail="Unknown preview")
    path = VideoCleanupService.preview_path(project_id, which)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Preview not rendered yet")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/run", status_code=202)
async def run_cleanup(project_id: str) -> dict:
    _require_pure(project_id)
    try:
        await VideoCleanupService.start_full_cleanup(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "started"}


@router.get("/stream")
async def stream_cleanup(project_id: str):
    _require_pure(project_id)

    async def stream():
        async for state in VideoCleanupService.stream_state(project_id):
            yield "data: " + state.model_dump_json() + "\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/cancel")
async def cancel_cleanup(project_id: str) -> dict:
    _require_pure(project_id)
    await VideoCleanupService.cancel(project_id)
    return {"status": "cancelling"}


@router.post("/skip")
async def skip_cleanup(project_id: str) -> dict:
    """Continue without cleaning: the raw download stays the project video."""
    project = _require_pure(project_id)
    state = project.cleanup
    if state is not None and state.status == "running":
        raise HTTPException(
            status_code=400, detail="Cleanup is running; cancel it first"
        )
    project.phase = ProjectPhase.SCENE_DETECTION
    ProjectService.save(project)
    return {"status": "skipped"}
