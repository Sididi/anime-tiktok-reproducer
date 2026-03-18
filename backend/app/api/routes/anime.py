"""API routes for anime library management."""

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...library_types import LibraryType
from ...services import AnimeLibraryService, AnimeMatcherService

router = APIRouter(prefix="/anime", tags=["anime"])

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov"}


@router.get("/browse")
async def browse_directories(path: str | None = Query(default=None)):
    """Browse directories on the server filesystem."""
    browse_path = Path(path) if path else Path.home()

    if not browse_path.exists() or not browse_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a valid directory: {browse_path}")

    parent_path = str(browse_path.parent) if browse_path != browse_path.parent else None

    def _scan_path() -> tuple[list[dict], list[dict]]:
        dirs = []
        files = []
        for entry in sorted(browse_path.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
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
                    "mtime": entry.stat().st_mtime,
                })
            elif entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": False,
                    "has_videos": False,
                    "mtime": entry.stat().st_mtime,
                })
        return dirs, files

    try:
        dirs, files = await asyncio.to_thread(_scan_path)
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {browse_path}")

    return {
        "current_path": str(browse_path),
        "parent_path": parent_path,
        "entries": dirs + files,
    }


@router.get("/list")
async def list_indexed_anime(library_type: LibraryType = Query(...)):
    """List all indexed anime series in the library."""
    try:
        series = await AnimeLibraryService.list_indexed_anime(library_type=library_type)
        return {"series": series, "count": len(series)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class IndexAnimeRequest(BaseModel):
    source_path: str
    library_type: LibraryType
    anime_name: str | None = None
    fps: float = 2.0
    batch_size: int = 64
    prefetch_batches: int = 3
    transform_workers: int = 4
    require_gpu: bool = True


@router.post("/index")
async def index_anime(request: IndexAnimeRequest):
    """Index a new anime folder into the library with SSE progress."""
    source_folder = Path(request.source_path)

    if not source_folder.exists():
        raise HTTPException(status_code=400, detail=f"Source folder not found: {request.source_path}")

    if not source_folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Source path is not a directory: {request.source_path}")

    target_anime_name = request.anime_name or source_folder.name

    async def stream_progress():
        async for progress in AnimeLibraryService.index_anime(
            source_folder=source_folder,
            library_type=request.library_type,
            anime_name=request.anime_name,
            fps=request.fps,
            batch_size=request.batch_size,
            prefetch_batches=request.prefetch_batches,
            transform_workers=request.transform_workers,
            require_gpu=request.require_gpu,
        ):
            if progress.status == "complete":
                AnimeMatcherService.mark_series_updated(
                    request.library_type,
                    progress.anime_name or target_anime_name,
                )
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


class UpdateAnimeRequest(BaseModel):
    library_type: LibraryType
    anime_name: str
    source_paths: list[str]
    batch_size: int = 64
    prefetch_batches: int = 3
    transform_workers: int = 4
    require_gpu: bool = True


@router.post("/update")
async def update_anime(request: UpdateAnimeRequest):
    """Incrementally update an already indexed anime with a precise file list."""
    if not request.source_paths:
        raise HTTPException(status_code=400, detail="No source_paths provided.")

    source_files = [Path(path) for path in request.source_paths]

    async def stream_progress():
        async for progress in AnimeLibraryService.update_anime(
            library_type=request.library_type,
            anime_name=request.anime_name,
            source_paths=source_files,
            batch_size=request.batch_size,
            prefetch_batches=request.prefetch_batches,
            transform_workers=request.transform_workers,
            require_gpu=request.require_gpu,
        ):
            if progress.status == "complete":
                AnimeMatcherService.mark_series_updated(
                    request.library_type,
                    progress.anime_name or request.anime_name,
                )
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


class RemoveAnimeFilesRequest(BaseModel):
    library_type: LibraryType
    anime_name: str
    library_paths: list[str]


@router.post("/remove")
async def remove_anime_files(request: RemoveAnimeFilesRequest):
    """Remove explicit library files from an indexed anime series."""
    if not request.library_paths:
        raise HTTPException(status_code=400, detail="No library_paths provided.")

    target_paths = [Path(path) for path in request.library_paths]

    async def stream_progress():
        async for progress in AnimeLibraryService.remove_anime_files(
            library_type=request.library_type,
            anime_name=request.anime_name,
            library_paths=target_paths,
        ):
            if progress.status == "complete":
                AnimeMatcherService.mark_series_updated(
                    request.library_type,
                    progress.anime_name or request.anime_name,
                )
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


class SourceDetails(BaseModel):
    name: str
    episode_count: int
    total_size_bytes: int
    fps: float
    missing_episodes: int
    purge_protected: bool
    original_index_path: str | None


@router.get("/source-details")
async def get_source_details(
    library_type: LibraryType = Query(...),
) -> list[SourceDetails]:
    """Get detailed metadata for all sources in a library type."""
    return await AnimeLibraryService.get_source_details(library_type=library_type)
