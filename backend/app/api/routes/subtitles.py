"""API routes for subtitle video generation."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse

from ...models.subtitle import (
    SubtitleGenerationRequest,
    SubtitlePreviewRequest,
)
from ...services import ProjectService
from ...services.subtitle_video import SubtitleVideoService
from ...services.subtitle_styles import list_styles, get_style

router = APIRouter(prefix="/projects/{project_id}/subtitles", tags=["subtitles"])


@router.get("/styles")
async def get_styles(project_id: str):
    """Get all available subtitle styles."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    styles = list_styles()
    return {
        "styles": [style.model_dump() for style in styles],
        "karaoke_count": len([s for s in styles if s.style_type.value == "karaoke"]),
        "regular_count": len([s for s in styles if s.style_type.value == "regular"]),
    }


@router.get("/styles/{style_id}")
async def get_style_by_id(project_id: str, style_id: str):
    """Get a specific subtitle style by ID."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    style = get_style(style_id)
    if not style:
        raise HTTPException(status_code=404, detail=f"Style not found: {style_id}")

    return {"style": style.model_dump()}


@router.post("/previews")
async def generate_previews(project_id: str, request: SubtitlePreviewRequest):
    """
    Generate preview videos for all 15 subtitle styles.

    Streams progress updates via SSE.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    async def stream_progress():
        async for progress in SubtitleVideoService.generate_style_previews(
            project_id, request.duration
        ):
            yield f"data: {json.dumps(progress.model_dump())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/generate")
async def generate_subtitle_video(project_id: str, request: SubtitleGenerationRequest):
    """
    Generate a subtitle video with the specified style.

    Streams progress updates via SSE.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate style
    style = get_style(request.style_id)
    if not style:
        raise HTTPException(status_code=400, detail=f"Unknown style: {request.style_id}")

    # Validate format
    if request.output_format not in ["webm", "mov"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid output format: {request.output_format}. Use 'webm' or 'mov'.",
        )

    async def stream_progress():
        async for progress in SubtitleVideoService.generate_subtitle_video(
            project_id,
            request.style_id,
            request.output_format,
            request.use_new_tts,
        ):
            yield f"data: {json.dumps(progress.model_dump())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/files")
async def list_subtitle_files(project_id: str):
    """List available subtitle video files."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    files = SubtitleVideoService.get_available_files(project_id)
    return files


@router.get("/download/{filename}")
async def download_subtitle_video(project_id: str, filename: str):
    """Download a generated subtitle video."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Security: validate filename doesn't have path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    project_dir = ProjectService.get_project_dir(project_id)
    file_path = project_dir / "subtitles" / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Determine media type
    if filename.endswith(".webm"):
        media_type = "video/webm"
    elif filename.endswith(".mov"):
        media_type = "video/quicktime"
    else:
        media_type = "application/octet-stream"

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type=media_type,
    )


@router.get("/previews/{style_id}")
async def get_preview_video(project_id: str, style_id: str):
    """Get a specific style preview video."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate style exists
    style = get_style(style_id)
    if not style:
        raise HTTPException(status_code=404, detail=f"Style not found: {style_id}")

    project_dir = ProjectService.get_project_dir(project_id)
    preview_path = project_dir / "subtitles" / "previews" / f"{style_id}.webm"

    if not preview_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Preview not generated. Call POST /subtitles/previews first.",
        )

    return FileResponse(
        path=preview_path,
        filename=f"{style_id}_preview.webm",
        media_type="video/webm",
    )
