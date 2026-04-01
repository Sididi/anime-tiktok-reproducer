from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json

from ...services import ProjectService, DownloaderService

router = APIRouter(prefix="/projects/{project_id}", tags=["download"])


class DownloadRequest(BaseModel):
    url: str


@router.post("/download")
async def download_video(project_id: str, request: DownloadRequest):
    """Download a TikTok video and stream progress updates."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    async def stream_progress():
        async for progress in DownloaderService.download_project_video(
            request.url,
            project_id,
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
