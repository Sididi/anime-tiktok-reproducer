import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from ...services import UploadPhaseService


router = APIRouter(prefix="/project-manager", tags=["project-manager"])


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
    account_id: Annotated[str | None, Body(embed=True)] = None,
):
    """Upload a ready project to configured platforms."""
    async def stream_progress():
        yield f"data: {json.dumps({'status': 'processing', 'step': 'prepare', 'progress': 0.1, 'message': 'Preparing upload phase...'})}\n\n"
        try:
            result = await asyncio.to_thread(UploadPhaseService.execute_upload, project_id, account_id)
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
