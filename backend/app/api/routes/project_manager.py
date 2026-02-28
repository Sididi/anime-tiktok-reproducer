import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...services import UploadPhaseService


router = APIRouter(prefix="/project-manager", tags=["project-manager"])


class UploadProjectRequest(BaseModel):
    account_id: str | None = None
    platforms: list[Literal["youtube", "facebook", "instagram"]] | None = None
    facebook_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None


class FacebookCheckRequest(BaseModel):
    account_id: str | None = None


@router.get("/projects")
async def list_project_manager_projects():
    """List locally stored projects enriched with Drive/upload status."""
    try:
        rows = await asyncio.to_thread(UploadPhaseService.list_manager_rows)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"projects": rows}


@router.post("/projects/{project_id}/upload")
async def run_upload_phase(
    project_id: str,
    payload: UploadProjectRequest | None = Body(default=None),
):
    """Upload a ready project to configured platforms."""
    async def stream_progress():
        req = payload or UploadProjectRequest()
        yield f"data: {json.dumps({'status': 'processing', 'step': 'prepare', 'progress': 0.1, 'message': 'Preparing upload phase...'})}\n\n"
        try:
            result = await asyncio.to_thread(
                UploadPhaseService.execute_upload,
                project_id,
                req.account_id,
                req.platforms,
            )
            yield f"data: {json.dumps({'status': 'complete', 'step': 'complete', 'progress': 1.0, 'message': 'Upload phase complete', 'result': result})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'step': 'upload', 'progress': 0.0, 'error': str(exc), 'message': 'Upload phase failed'})}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.delete("/projects/{project_id}")
async def delete_managed_project(project_id: str):
    """Delete local project + linked Drive folder + webhook message cleanup."""
    try:
        return await asyncio.to_thread(UploadPhaseService.managed_delete, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/projects/{project_id}/facebook-check")
async def facebook_duration_check(
    project_id: str,
    payload: FacebookCheckRequest | None = Body(default=None),
):
    """Check if the project video exceeds Facebook's 90s Reel limit.

    If it does, the sped-up version is pre-generated for preview.
    """
    req = payload or FacebookCheckRequest()
    try:
        result = await asyncio.to_thread(
            UploadPhaseService.check_facebook_duration,
            project_id,
            req.account_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/facebook-preview/{version}")
async def facebook_preview_video(
    project_id: str,
    version: Literal["original", "sped_up"],
):
    """Serve a cached video file for Facebook duration preview."""
    prep_dir = UploadPhaseService._facebook_prep_dir(project_id)
    if not prep_dir.exists():
        raise HTTPException(status_code=404, detail="No Facebook preview cached for this project")

    if version == "sped_up":
        video_path = prep_dir / "sped_up.mp4"
    else:
        # Original: find the first .mp4 that isn't the sped_up file
        video_path = None
        for f in sorted(prep_dir.iterdir()):
            if f.suffix.lower() == ".mp4" and f.name != "sped_up.mp4":
                video_path = f
                break

    if video_path is None or not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Preview version '{version}' not found")

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=f"{project_id}_{version}.mp4",
    )
