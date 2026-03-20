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

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v"}


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
        try:
            raw_entries = list(browse_path.iterdir())
        except PermissionError:
            return [], []

        def _safe_mtime(e: Path) -> float:
            try:
                return e.stat().st_mtime
            except (OSError, ValueError):
                return 0.0

        raw_entries.sort(key=_safe_mtime, reverse=True)

        for entry in raw_entries:
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except OSError:
                continue
            if is_dir:
                try:
                    has_videos = any(
                        child.suffix.lower() in VIDEO_EXTENSIONS
                        for child in entry.iterdir()
                        if child.is_file()
                    )
                except (PermissionError, OSError):
                    has_videos = False
                dirs.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": True,
                    "has_videos": has_videos,
                    "mtime": _safe_mtime(entry),
                })
            elif is_file and entry.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "is_dir": False,
                    "has_videos": False,
                    "mtime": _safe_mtime(entry),
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


# ---------------------------------------------------------------------------
# Batch folder validation
# ---------------------------------------------------------------------------


class ValidateBatchFoldersRequest(BaseModel):
    paths: list[str]
    library_type: LibraryType


class ConflictDetails(BaseModel):
    new_episodes: list[str]
    removed_episodes: list[str]
    existing_episode_count: int
    existing_torrent_count: int


class FolderValidationResult(BaseModel):
    path: str
    name: str
    has_videos: bool
    suggested_path: str | None = None
    index_status: str = "new"  # "new" | "exact_match" | "conflict"
    conflict_details: ConflictDetails | None = None


def _find_first_video_dir(root: Path) -> str | None:
    """BFS for the first subdirectory that directly contains video files."""
    from collections import deque

    queue = deque([root])
    while queue:
        current = queue.popleft()
        try:
            children = sorted(current.iterdir())
        except PermissionError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                has_vids = any(
                    f.suffix.lower() in VIDEO_EXTENSIONS
                    for f in child.iterdir()
                    if f.is_file()
                )
            except (PermissionError, OSError):
                continue
            if has_vids:
                return str(child)
            queue.append(child)
    return None


def _validate_batch_folders_sync(
    paths: list[str],
    library_type: LibraryType,
) -> list[dict]:
    library_path = AnimeLibraryService.get_library_path(library_type=library_type)

    # Load state.json once for indexed episode comparison
    index_dir = library_path / ".index"
    state_files: dict = {}
    try:
        state_payload = json.loads(
            (index_dir / "state.json").read_text(encoding="utf-8")
        )
        if isinstance(state_payload, dict):
            files = state_payload.get("files", {})
            if isinstance(files, dict):
                state_files = files
    except (OSError, json.JSONDecodeError):
        pass

    results = []
    for folder_path in paths:
        p = Path(folder_path)
        if not p.exists() or not p.is_dir():
            continue

        name = p.name

        # Check direct video files in the source folder
        try:
            source_videos = sorted(
                f.name
                for f in p.iterdir()
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
            )
        except (PermissionError, OSError):
            source_videos = []

        has_videos = len(source_videos) > 0

        suggested = None
        if not has_videos:
            suggested = _find_first_video_dir(p)

        # Check if already indexed
        source_dir = library_path / name
        index_status = "new"
        conflict_details = None

        if source_dir.exists() and source_dir.is_dir():
            # Get video files on disk in the library directory
            try:
                library_videos = sorted(
                    f.name
                    for f in source_dir.iterdir()
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
                )
            except (PermissionError, OSError):
                library_videos = []

            # Get torrent count
            torrent_count = 0
            torrents_path = source_dir / ".atr_torrents.json"
            try:
                t_payload = json.loads(torrents_path.read_text(encoding="utf-8"))
                if isinstance(t_payload, dict):
                    torrent_count = len(t_payload.get("torrents", []))
            except (OSError, json.JSONDecodeError):
                pass

            # Compare source folder videos with library videos
            source_set = set(source_videos) if has_videos else set()
            library_set = set(library_videos)

            new_episodes = sorted(source_set - library_set)
            removed_episodes = sorted(library_set - source_set)

            if not new_episodes and not removed_episodes:
                index_status = "exact_match"
            else:
                index_status = "conflict"
                conflict_details = {
                    "new_episodes": new_episodes,
                    "removed_episodes": removed_episodes,
                    "existing_episode_count": len(library_videos),
                    "existing_torrent_count": torrent_count,
                }

        results.append({
            "path": folder_path,
            "name": name,
            "has_videos": has_videos,
            "suggested_path": suggested,
            "index_status": index_status,
            "conflict_details": conflict_details,
        })

    return results


@router.post("/validate-batch-folders")
async def validate_batch_folders(request: ValidateBatchFoldersRequest):
    """Validate a batch of folders for indexation, detecting conflicts."""
    results = await asyncio.to_thread(
        _validate_batch_folders_sync,
        request.paths,
        request.library_type,
    )
    return {"results": results}


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


# ---------------------------------------------------------------------------
# Async indexation queue
# ---------------------------------------------------------------------------

class IndexAnimeAsyncRequest(BaseModel):
    source_path: str
    library_type: LibraryType
    anime_name: str | None = None
    fps: float = 2.0


@router.post("/index-async")
async def index_anime_async(request: IndexAnimeAsyncRequest):
    """Enqueue an async indexation job."""
    from ...services.indexation_queue import indexation_queue

    source_folder = Path(request.source_path)
    if not source_folder.exists() or not source_folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Invalid source: {request.source_path}")
    job_id = await indexation_queue.enqueue(
        source_path=request.source_path,
        library_type=request.library_type,
        anime_name=request.anime_name,
        fps=request.fps,
    )
    return {"job_id": job_id}


@router.get("/jobs")
async def list_jobs():
    """List all indexation jobs."""
    from ...services.indexation_queue import indexation_queue

    return {"jobs": [j.model_dump(mode="json") for j in indexation_queue.list_jobs()]}


@router.get("/jobs/stream")
async def stream_jobs():
    """Stream indexation job updates via SSE."""
    from ...services.indexation_queue import indexation_queue

    async def generate():
        async for data in indexation_queue.stream_all_jobs():
            yield f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# Purge system
# ---------------------------------------------------------------------------

class PurgeRequest(BaseModel):
    library_type: LibraryType
    all_types: bool = False


@router.post("/purge")
async def purge_library(request: PurgeRequest):
    """Delete video files from library, preserving indexes and metadata."""
    types_to_purge = list(LibraryType) if request.all_types else [request.library_type]
    return await AnimeLibraryService.purge_library(types_to_purge)


@router.get("/purge/estimate")
async def estimate_purge(
    library_type: LibraryType = Query(...),
    all_types: bool = Query(False),
):
    """Estimate how much space would be freed by purging."""
    types_to_check = list(LibraryType) if all_types else [library_type]
    return await AnimeLibraryService.estimate_purge_size(types_to_check)


# ---------------------------------------------------------------------------
# Purge protection toggle
# ---------------------------------------------------------------------------

@router.patch("/{source_name}/protection")
async def toggle_protection(
    source_name: str,
    library_type: LibraryType = Query(...),
):
    """Toggle purge protection for a source."""
    from ...services.torrent_linker import TorrentLinkerService
    from ...models.torrent import SourceTorrentMetadata

    library_root = AnimeLibraryService.get_library_path(library_type=library_type)
    source_dir = library_root / source_name
    if not source_dir.exists():
        raise HTTPException(status_code=404, detail=f"Source not found: {source_name}")

    metadata = TorrentLinkerService.load_metadata(source_dir) or SourceTorrentMetadata()
    metadata.purge_protection = not metadata.purge_protection
    TorrentLinkerService.save_metadata(source_dir, metadata)
    return {"purge_protected": metadata.purge_protection}


# ---------------------------------------------------------------------------
# Torrent management
# ---------------------------------------------------------------------------


@router.get("/{source_name}/torrents")
async def get_source_torrents(
    source_name: str,
    library_type: LibraryType = Query(...),
):
    """Get torrent metadata for a source."""
    from ...services.torrent_linker import TorrentLinkerService
    from ...models.torrent import SourceTorrentMetadata

    library_root = AnimeLibraryService.get_library_path(library_type=library_type)
    source_dir = library_root / source_name
    if not source_dir.exists():
        raise HTTPException(status_code=404, detail=f"Source not found: {source_name}")

    metadata = TorrentLinkerService.load_metadata(source_dir)
    if not metadata:
        return SourceTorrentMetadata().model_dump(mode="json")
    return metadata.model_dump(mode="json")


@router.post("/{source_name}/torrents/replace")
async def replace_torrents(
    source_name: str,
    request: "ReplaceTorrentsBody",
):
    """Start torrent replacement pipeline. Returns SSE progress stream."""
    from ...models.torrent import ReplaceTorrentsRequest
    from ...services.torrent_replacer import TorrentReplacerService
    from ...services.qbittorrent import QBittorrentClient

    lock = TorrentReplacerService._get_lock(source_name)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Un remplacement est déjà en cours pour cette source.",
        )

    full_request = ReplaceTorrentsRequest(
        source_name=source_name,
        library_type=request.library_type,
        replacements=request.replacements,
    )

    async def stream_progress():
        qbt = QBittorrentClient()
        try:
            async with lock:
                async for progress in TorrentReplacerService.replace_torrents(
                    full_request, qbt
                ):
                    yield f"data: {json.dumps(progress.model_dump(mode='json'))}\n\n"
        finally:
            await qbt.close()

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/{source_name}/torrents/replace/confirm-reindex")
async def confirm_reindex(
    source_name: str,
    request: "ConfirmReindexBody",
):
    """Confirm reindex for WARN torrents after replacement verification."""
    from ...models.torrent import ConfirmReindexRequest
    from ...services.torrent_replacer import TorrentReplacerService
    from ...services.qbittorrent import QBittorrentClient

    lock = TorrentReplacerService._get_lock(source_name)
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Un remplacement est déjà en cours pour cette source.",
        )

    full_request = ConfirmReindexRequest(
        source_name=source_name,
        library_type=request.library_type,
        torrent_ids=request.torrent_ids,
    )

    async def stream_progress():
        qbt = QBittorrentClient()
        try:
            async with lock:
                async for progress in TorrentReplacerService.execute_reindex(
                    full_request, qbt
                ):
                    yield f"data: {json.dumps(progress.model_dump(mode='json'))}\n\n"
        finally:
            await qbt.close()

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# Request bodies for torrent replacement endpoints (avoid circular import issues)

class _TorrentReplacementItem(BaseModel):
    torrent_id: str
    new_magnet_uri: str


class ReplaceTorrentsBody(BaseModel):
    library_type: LibraryType
    replacements: list[_TorrentReplacementItem]


class ConfirmReindexBody(BaseModel):
    library_type: LibraryType
    torrent_ids: list[str]
