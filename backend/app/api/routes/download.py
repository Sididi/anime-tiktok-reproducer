from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json

from ...models import ProjectPhase
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

    # Update project with URL
    project.tiktok_url = request.url
    project.phase = ProjectPhase.DOWNLOADING
    ProjectService.save(project)

    async def stream_progress():
        async for progress in DownloaderService.download(request.url, project_id):
            if progress.status == "complete":
                # Update project with video info BEFORE yielding complete
                video_path = DownloaderService.get_output_path(project_id)
                video_info = await DownloaderService.get_video_info(video_path)

                project.video_path = str(video_path)
                project.video_duration = video_info.get("duration")
                project.video_fps = video_info.get("fps")
                project.video_width = video_info.get("width")
                project.video_height = video_info.get("height")
                project.phase = ProjectPhase.SCENE_DETECTION
                ProjectService.save(project)
                
                yield f"data: {json.dumps(progress.to_dict())}\n\n"

            elif progress.status == "error":
                project.phase = ProjectPhase.SETUP
                ProjectService.save(project)
                yield f"data: {json.dumps(progress.to_dict())}\n\n"
            else:
                yield f"data: {json.dumps(progress.to_dict())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
