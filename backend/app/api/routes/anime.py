"""API routes for anime library management."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pathlib import Path
import json
import os

from ...services import AnimeLibraryService, AnimeMatcherService

router = APIRouter(prefix="/anime", tags=["anime"])

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm"}


@router.get("/browse")
async def browse_directories(path: str | None = Query(default=None)):
    """Browse directories on the server filesystem."""
    browse_path = Path(path) if path else Path.home()

    if not browse_path.exists() or not browse_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a valid directory: {browse_path}")

    parent_path = str(browse_path.parent) if browse_path != browse_path.parent else None

    dirs = []
    files = []
    try:
        for entry in sorted(browse_path.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                try:
                    has_videos = any(
                        child.suffix.lower() in VIDEO_EXTENSIONS
                        for child in entry.iterdir()
                        if child.is_file()
                    )
                except PermissionError:
                    has_videos = False
                dirs.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": True,
                    "has_videos": has_videos,
                })
            elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": False,
                    "has_videos": False,
                })
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {browse_path}")

    return {
        "current_path": str(browse_path),
        "parent_path": parent_path,
        "entries": dirs + files,
    }


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

    # If this series already exists in the index, this operation is an update.
    target_anime_name = request.anime_name or source_folder.name
    indexed_series = await AnimeLibraryService.list_indexed_anime()
    is_update = target_anime_name in indexed_series

    async def stream_progress():
        async for progress in AnimeLibraryService.index_anime(
            source_folder=source_folder,
            anime_name=request.anime_name,
            fps=request.fps,
        ):
            if progress.status == "complete" and is_update:
                # Mark only this series as stale so matcher reloads cache lazily
                # when this exact anime is matched next time.
                AnimeMatcherService.mark_series_updated(target_anime_name)
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
