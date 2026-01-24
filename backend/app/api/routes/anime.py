"""API routes for anime library management."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pathlib import Path
import json

from ...services import AnimeLibraryService

router = APIRouter(prefix="/anime", tags=["anime"])


@router.get("/list")
async def list_indexed_anime():
    """List all indexed anime series in the library."""
    try:
        series = await AnimeLibraryService.list_indexed_anime()
        return {"series": series, "count": len(series)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class IndexAnimeRequest(BaseModel):
    source_path: str
    anime_name: str | None = None
    fps: float = 2.0


@router.post("/index")
async def index_anime(request: IndexAnimeRequest):
    """Index a new anime folder into the library with SSE progress."""
    source_folder = Path(request.source_path)

    if not source_folder.exists():
        raise HTTPException(status_code=400, detail=f"Source folder not found: {request.source_path}")

    if not source_folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Source path is not a directory: {request.source_path}")

    async def stream_progress():
        async for progress in AnimeLibraryService.index_anime(
            source_folder=source_folder,
            anime_name=request.anime_name,
            fps=request.fps,
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


class CheckFoldersRequest(BaseModel):
    path: str


@router.post("/check-folders")
async def check_folders(request: CheckFoldersRequest):
    """Check available folders in a path that could be indexed."""
    source_path = Path(request.path)

    if not source_path.exists():
        raise HTTPException(status_code=400, detail=f"Path not found: {request.path}")

    if not source_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {request.path}")

    folders = await AnimeLibraryService.get_available_folders(source_path)
    return {"path": request.path, "folders": folders}
