"""API routes for anime library management."""

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ...library_types import LibraryType
from ...services import (
    AnimeLibraryService,
    AnimeMatcherService,
    IndexationPreflightService,
    LibraryHydrationService,
    LibraryStateDb,
    StorageBoxRepository,
)
from ...services.library_hydration_service import SeriesDeleteBlockedError

router = APIRouter(prefix="/anime", tags=["anime"])

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v"}


def _preflight_error_payload(
    *,
    code: str,
    message: str,
    resolution: str,
    name: str,
    series_id: str | None = None,
    storage_release_id: str | None = None,
    conflict_details: dict | None = None,
    orphan_reason: str | None = None,
    invalid_video_files: list[str] | None = None,
) -> dict:
    return {
        "code": code,
        "message": message,
        "resolution": resolution,
        "name": name,
        "series_id": series_id,
        "storage_release_id": storage_release_id,
        "conflict_details": conflict_details,
        "orphan_reason": orphan_reason,
        "invalid_video_files": invalid_video_files,
    }


def _format_invalid_video_files_message(name: str, invalid_video_files: list[str]) -> str:
    preview = ", ".join(invalid_video_files[:5])
    if len(invalid_video_files) > 5:
        preview += f", et {len(invalid_video_files) - 5} autre(s)"
    return (
        f"'{name}' contient uniquement des fichiers vidéo illisibles ou corrompus. "
        f"Fichiers détectés: {preview}"
    )


def _raise_index_preflight_error(result: dict) -> None:
    resolution = str(result.get("resolution") or "")
    name = str(result.get("name") or "source")
    payload = None
    status_code = 409
    if resolution == "exact_match":
        payload = _preflight_error_payload(
            code="already_indexed_exact_match",
            message=f"'{name}' est déjà indexée avec le même contenu distant.",
            resolution=resolution,
            name=name,
            series_id=result.get("series_id"),
            storage_release_id=result.get("storage_release_id"),
        )
    elif resolution == "update_required":
        payload = _preflight_error_payload(
            code="already_indexed_update_required",
            message=f"'{name}' existe déjà. Utilisez la mise à jour au lieu d'une nouvelle indexation.",
            resolution=resolution,
            name=name,
            series_id=result.get("series_id"),
            storage_release_id=result.get("storage_release_id"),
            conflict_details=result.get("conflict_details"),
        )
    elif resolution == "blocked_orphan":
        payload = _preflight_error_payload(
            code="blocked_orphan",
            message=str(
                result.get("orphan_reason")
                or "Un dossier local orphelin bloque cette indexation."
            ),
            resolution=resolution,
            name=name,
            series_id=result.get("series_id"),
            storage_release_id=result.get("storage_release_id"),
            orphan_reason=result.get("orphan_reason"),
        )
    elif resolution == "needs_fix":
        status_code = 400
        invalid_video_files = [
            str(value).strip()
            for value in (result.get("invalid_video_files") or [])
            if str(value).strip()
        ]
        payload = _preflight_error_payload(
            code="invalid_video_files" if invalid_video_files else "needs_fix",
            message=(
                _format_invalid_video_files_message(name, invalid_video_files)
                if invalid_video_files
                else (
                    f"'{name}' ne contient pas directement de fichiers vidéo. "
                    "Choisissez le sous-dossier qui contient les épisodes."
                )
            ),
            resolution=resolution,
            name=name,
            invalid_video_files=invalid_video_files or None,
        )

    if payload is not None:
        raise HTTPException(status_code=status_code, detail=payload)


def _raise_update_preflight_error(result: dict) -> None:
    resolution = str(result.get("resolution") or "")
    name = str(result.get("name") or "source")
    payload = None
    status_code = 409
    if resolution == "new":
        payload = _preflight_error_payload(
            code="update_target_missing",
            message=f"'{name}' n'existe pas encore. Utilisez une nouvelle indexation.",
            resolution=resolution,
            name=name,
        )
    elif resolution == "exact_match":
        payload = _preflight_error_payload(
            code="update_no_changes",
            message=f"'{name}' ne contient aucun changement à publier.",
            resolution=resolution,
            name=name,
            series_id=result.get("series_id"),
            storage_release_id=result.get("storage_release_id"),
        )
    elif resolution == "blocked_orphan":
        payload = _preflight_error_payload(
            code="blocked_orphan",
            message=str(
                result.get("orphan_reason")
                or "Un dossier local orphelin bloque cette mise à jour."
            ),
            resolution=resolution,
            name=name,
            series_id=result.get("series_id"),
            storage_release_id=result.get("storage_release_id"),
            orphan_reason=result.get("orphan_reason"),
        )
    elif resolution == "needs_fix":
        status_code = 400
        invalid_video_files = [
            str(value).strip()
            for value in (result.get("invalid_video_files") or [])
            if str(value).strip()
        ]
        payload = _preflight_error_payload(
            code="invalid_video_files" if invalid_video_files else "needs_fix",
            message=(
                _format_invalid_video_files_message(name, invalid_video_files)
                if invalid_video_files
                else (
                    f"'{name}' ne contient pas directement de fichiers vidéo. "
                    "Choisissez le sous-dossier qui contient les épisodes."
                )
            ),
            resolution=resolution,
            name=name,
            invalid_video_files=invalid_video_files or None,
        )

    if payload is not None:
        raise HTTPException(status_code=status_code, detail=payload)


def _json_write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _series_dir_size_bytes(series_dir: Path) -> int:
    return sum(path.stat().st_size for path in series_dir.rglob("*") if path.is_file())


def _purge_local_orphan_series_sync(
    library_type: LibraryType,
    source_dir: Path,
) -> None:
    display_name = source_dir.name
    library_root = AnimeLibraryService.get_library_path(library_type)
    if source_dir.exists():
        shutil.rmtree(source_dir, ignore_errors=True)

    index_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME
    manifest_path = index_dir / AnimeLibraryService.MANIFEST_FILE
    state_path = index_dir / AnimeLibraryService.STATE_FILE
    if not manifest_path.exists() or not state_path.exists():
        return

    try:
        local_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        local_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    shard_key = None
    raw_series = local_manifest.get("series", {})
    if isinstance(raw_series, dict):
        series_entry = raw_series.pop(display_name, None)
        if isinstance(series_entry, dict):
            shard_key = str(series_entry.get("key") or "").strip() or None
        _json_write_atomic(manifest_path, local_manifest)

    raw_files = local_state.get("files", {})
    if isinstance(raw_files, dict):
        prefix = f"{display_name}/"
        local_state["files"] = {
            path: value
            for path, value in raw_files.items()
            if not (path == display_name or str(path).startswith(prefix))
        }
        _json_write_atomic(state_path, local_state)

    if shard_key:
        shutil.rmtree(index_dir / "series" / shard_key, ignore_errors=True)


def _scan_local_purge_candidates(
    library_type: LibraryType,
    details_by_series: dict[str, dict],
) -> list[dict]:
    library_root = AnimeLibraryService.get_library_path(library_type)
    if not library_root.exists():
        return []

    state_by_series = LibraryStateDb.list_series_states(library_type)
    candidates: list[dict] = []
    for source_dir in library_root.iterdir():
        if not source_dir.is_dir() or source_dir.name.startswith("."):
            continue

        metadata = StorageBoxRepository.read_local_series_metadata(source_dir)
        series_id = None
        if isinstance(metadata, dict):
            raw_series_id = str(metadata.get("series_id") or "").strip()
            if raw_series_id:
                series_id = raw_series_id

        detail = details_by_series.get(series_id) if series_id else None
        state = state_by_series.get(series_id) if series_id else None
        project_pin_count = (
            LibraryStateDb.count_project_pins(series_id) if series_id else 0
        )
        permanent_pin = bool(detail["permanent_pin"]) if detail else bool(
            state.permanent_pin
        ) if state else False
        size_bytes = _series_dir_size_bytes(source_dir)
        candidates.append(
            {
                "name": str(detail["name"]) if detail else source_dir.name,
                "series_id": series_id,
                "project_pin_count": int(detail["project_pin_count"]) if detail else project_pin_count,
                "permanent_pin": bool(detail["permanent_pin"]) if detail else permanent_pin,
                "size_bytes": size_bytes,
                "path": str(source_dir),
            }
        )
    return candidates


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
        details = await LibraryHydrationService.list_source_details(library_type=library_type)
        series = [item["name"] for item in details]
        return {"series": series, "count": len(series)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class IndexAnimeRequest(BaseModel):
    source_path: str
    library_type: LibraryType
    anime_name: str | None = None
    fps: float = 2.0
    batch_size: int = 64
    prefetch_batches: int = 2
    transform_workers: int = 4
    decode_backend: str = "auto"
    precision: str = "fp16"
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
    preflight = await IndexationPreflightService.preflight_source(
        source_path=source_folder,
        library_type=request.library_type,
        anime_name=target_anime_name,
    )
    _raise_index_preflight_error(preflight)

    async def stream_progress():
        final_complete: dict | None = None
        saw_error = False
        async for progress in AnimeLibraryService.index_anime(
            source_folder=source_folder,
            library_type=request.library_type,
            anime_name=request.anime_name,
            fps=request.fps,
            batch_size=request.batch_size,
            prefetch_batches=request.prefetch_batches,
            transform_workers=request.transform_workers,
            decode_backend=request.decode_backend,
            precision=request.precision,
            require_gpu=request.require_gpu,
        ):
            if progress.status == "error":
                saw_error = True
                yield f"data: {json.dumps(progress.to_dict())}\n\n"
                return
            if progress.status == "complete":
                final_complete = progress.to_dict()
                break
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

        if saw_error:
            return
        yield f"data: {json.dumps({'status': 'indexing', 'progress': 0.97, 'message': 'Packaging release...', 'anime_name': target_anime_name})}\n\n"
        try:
            publish_result = await LibraryHydrationService.publish_series_release(
                library_type=request.library_type,
                display_name=target_anime_name,
            )
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'progress': 0.0, 'message': str(exc), 'error': str(exc), 'anime_name': target_anime_name})}\n\n"
            return
        AnimeMatcherService.mark_series_updated(
            request.library_type,
            target_anime_name,
        )
        if final_complete is None:
            final_complete = {
                "status": "complete",
                "progress": 1.0,
                "message": f"Successfully indexed {target_anime_name}",
                "anime_name": target_anime_name,
            }
        final_complete["series_id"] = str(publish_result["series_id"])
        final_complete["storage_release_id"] = str(publish_result["release_id"])
        yield f"data: {json.dumps(final_complete)}\n\n"

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
    prefetch_batches: int = 2
    transform_workers: int = 4
    decode_backend: str = "auto"
    precision: str = "fp16"
    require_gpu: bool = True


@router.post("/update")
async def update_anime(request: UpdateAnimeRequest):
    """Incrementally update an already indexed anime with a precise file list."""
    if not request.source_paths:
        raise HTTPException(status_code=400, detail="No source_paths provided.")

    source_files = [Path(path) for path in request.source_paths]
    series_id = await StorageBoxRepository.find_remote_series_id_by_name(
        request.library_type,
        request.anime_name,
    )
    if series_id:
        await LibraryHydrationService.ensure_series_index_hydrated(
            library_type=request.library_type,
            series_id=series_id,
        )

    async def stream_progress():
        final_complete: dict | None = None
        saw_error = False
        async for progress in AnimeLibraryService.update_anime(
            library_type=request.library_type,
            anime_name=request.anime_name,
            source_paths=source_files,
            batch_size=request.batch_size,
            prefetch_batches=request.prefetch_batches,
            transform_workers=request.transform_workers,
            decode_backend=request.decode_backend,
            precision=request.precision,
            require_gpu=request.require_gpu,
        ):
            if progress.status == "error":
                saw_error = True
                yield f"data: {json.dumps(progress.to_dict())}\n\n"
                return
            if progress.status == "complete":
                final_complete = progress.to_dict()
                break
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

        if saw_error:
            return
        yield f"data: {json.dumps({'status': 'indexing', 'progress': 0.97, 'message': 'Publishing updated release...', 'anime_name': request.anime_name})}\n\n"
        try:
            publish_result = await LibraryHydrationService.publish_series_release(
                library_type=request.library_type,
                display_name=request.anime_name,
                series_id=series_id,
            )
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'progress': 0.0, 'message': str(exc), 'error': str(exc), 'anime_name': request.anime_name})}\n\n"
            return
        AnimeMatcherService.mark_series_updated(
            request.library_type,
            request.anime_name,
        )
        if final_complete is None:
            final_complete = {
                "status": "complete",
                "progress": 1.0,
                "message": f"Successfully updated {request.anime_name}",
                "anime_name": request.anime_name,
            }
        final_complete["series_id"] = str(publish_result["series_id"])
        final_complete["storage_release_id"] = str(publish_result["release_id"])
        yield f"data: {json.dumps(final_complete)}\n\n"

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
    paths: list[str] = []
    items: list[dict] = []
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
    resolution: str = "new"
    series_id: str | None = None
    storage_release_id: str | None = None
    conflict_details: ConflictDetails | None = None
    orphan_reason: str | None = None


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


def _batch_episode_stems(video_names: list[str]) -> set[str]:
    """Normalize batch comparison to logical episode names without extensions."""
    return {Path(video_name).stem for video_name in video_names}


def _validate_batch_folders_sync(
    paths: list[str],
    library_type: LibraryType,
) -> list[dict]:
    items = [{"path": path} for path in paths]
    return asyncio.run(
        IndexationPreflightService.validate_batch_items(
            items=items,
            library_type=library_type,
        )
    )


@router.post("/validate-batch-folders")
async def validate_batch_folders(request: ValidateBatchFoldersRequest):
    """Validate a batch of folders for indexation, detecting conflicts."""
    raw_items = request.items or [{"path": path} for path in request.paths]
    results = await IndexationPreflightService.validate_batch_items(
        items=raw_items,
        library_type=request.library_type,
    )
    return {"results": results}


class SourceDetails(BaseModel):
    name: str
    series_id: str
    episode_count: int
    local_episode_count: int
    total_size_bytes: int
    fps: float
    is_fully_local: bool
    project_pin_count: int
    permanent_pin: bool
    storage_release_id: str
    torrent_count: int
    hydration_status: str
    updated_at: str


@router.get("/source-details")
async def get_source_details(
    library_type: LibraryType = Query(...),
) -> list[SourceDetails]:
    """Get detailed metadata for all sources in a library type."""
    return await LibraryHydrationService.list_source_details(library_type=library_type)


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
    target_anime_name = request.anime_name or source_folder.name
    preflight = await IndexationPreflightService.preflight_source(
        source_path=source_folder,
        library_type=request.library_type,
        anime_name=target_anime_name,
    )
    _raise_index_preflight_error(preflight)
    job_id = await indexation_queue.enqueue(
        source_path=request.source_path,
        library_type=request.library_type,
        anime_name=target_anime_name,
        fps=request.fps,
        job_type="index",
        series_id=None,
    )
    return {"job_id": job_id}


class UpdateAnimeAsyncRequest(BaseModel):
    source_path: str
    library_type: LibraryType
    anime_name: str


@router.post("/update-async")
async def update_anime_async(request: UpdateAnimeAsyncRequest):
    """Enqueue an async update job for an existing remote series."""
    from ...services.indexation_queue import indexation_queue

    source_folder = Path(request.source_path)
    if not source_folder.exists() or not source_folder.is_dir():
        raise HTTPException(status_code=400, detail=f"Invalid source: {request.source_path}")

    preflight = await IndexationPreflightService.preflight_source(
        source_path=source_folder,
        library_type=request.library_type,
        anime_name=request.anime_name,
    )
    _raise_update_preflight_error(preflight)
    job_id = await indexation_queue.enqueue(
        source_path=request.source_path,
        library_type=request.library_type,
        anime_name=request.anime_name,
        fps=2.0,
        job_type="update",
        series_id=str(preflight.get("series_id") or ""),
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
    """Evict local unpinned library data while keeping remote releases intact."""
    types_to_purge = list(LibraryType) if request.all_types else [request.library_type]
    purged_sources: list[str] = []
    skipped_protected: list[str] = []
    freed_bytes = 0

    for library_type in types_to_purge:
        details = await LibraryHydrationService.list_source_details(library_type=library_type)
        details_by_series = {
            str(entry["series_id"]): entry
            for entry in details
            if str(entry.get("series_id") or "").strip()
        }
        candidates = await asyncio.to_thread(
            _scan_local_purge_candidates,
            library_type,
            details_by_series,
        )
        for candidate in candidates:
            if candidate["permanent_pin"] or int(candidate["project_pin_count"]) > 0:
                skipped_protected.append(candidate["name"])
                continue
            freed_bytes += int(candidate["size_bytes"])
            try:
                if candidate["series_id"]:
                    await LibraryHydrationService.evict_series(
                        library_type=library_type,
                        series_id=str(candidate["series_id"]),
                    )
                else:
                    await asyncio.to_thread(
                        _purge_local_orphan_series_sync,
                        library_type,
                        Path(candidate["path"]),
                    )
                purged_sources.append(candidate["name"])
            except Exception as exc:
                skipped_protected.append(f"{candidate['name']} ({exc})")

    return {
        "purged_sources": purged_sources,
        "freed_bytes": freed_bytes,
        "skipped_protected": skipped_protected,
    }


@router.get("/purge/estimate")
async def estimate_purge(
    library_type: LibraryType = Query(...),
    all_types: bool = Query(False),
):
    """Estimate how much local space would be freed by evicting unpinned series."""
    types_to_check = list(LibraryType) if all_types else [library_type]
    estimated_bytes = 0
    source_count = 0
    for current_type in types_to_check:
        details = await LibraryHydrationService.list_source_details(library_type=current_type)
        details_by_series = {
            str(entry["series_id"]): entry
            for entry in details
            if str(entry.get("series_id") or "").strip()
        }
        candidates = await asyncio.to_thread(
            _scan_local_purge_candidates,
            current_type,
            details_by_series,
        )
        for candidate in candidates:
            if candidate["permanent_pin"] or int(candidate["project_pin_count"]) > 0:
                continue
            size_bytes = int(candidate["size_bytes"])
            if size_bytes <= 0:
                continue
            source_count += 1
            estimated_bytes += size_bytes
    return {"estimated_bytes": estimated_bytes, "source_count": source_count}


# ---------------------------------------------------------------------------
# Purge protection toggle
# ---------------------------------------------------------------------------

@router.patch("/{series_id}/pin")
async def toggle_pin(
    series_id: str,
    library_type: LibraryType = Query(...),
):
    """Toggle permanent pin for a series."""
    try:
        state = await LibraryHydrationService.describe_series(
            library_type=library_type,
            series_id=series_id,
        )
        return await LibraryHydrationService.toggle_permanent_pin(
            library_type=library_type,
            series_id=series_id,
            enabled=not bool(state.get("permanent_pin")),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class HydrateSeriesRequest(BaseModel):
    library_type: LibraryType
    episode_keys: list[str] = []
    full_series: bool = False


@router.post("/{series_id}/hydrate")
async def hydrate_series(
    series_id: str,
    request: HydrateSeriesRequest,
):
    """Hydrate episodes locally from the active Storage Box release."""
    try:
        return await LibraryHydrationService.hydrate_series(
            library_type=request.library_type,
            series_id=series_id,
            episode_keys=request.episode_keys,
            full_series=request.full_series,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class EvictSeriesRequest(BaseModel):
    library_type: LibraryType


@router.post("/{series_id}/evict")
async def evict_series(
    series_id: str,
    request: EvictSeriesRequest,
):
    """Evict local hydrated data for one series if it is not pinned."""
    try:
        return await LibraryHydrationService.evict_series(
            library_type=request.library_type,
            series_id=series_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/{series_id}")
async def delete_series(
    series_id: str,
    library_type: LibraryType = Query(...),
):
    """Permanently delete a series from local storage and the Storage Box."""
    try:
        result = await LibraryHydrationService.delete_series(
            library_type=library_type,
            series_id=series_id,
        )
        AnimeMatcherService.mark_series_updated(
            library_type,
            result.get("display_name"),
        )
        return {
            "status": "deleted",
            "series_id": series_id,
            "library_type": library_type.value,
        }
    except SeriesDeleteBlockedError as exc:
        raise HTTPException(status_code=409, detail=exc.to_payload()) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Torrent management
# ---------------------------------------------------------------------------


@router.get("/{series_id}/episodes")
async def get_episode_sources(
    series_id: str,
    library_type: LibraryType = Query(...),
):
    """Return Storage Box primary episodes plus torrent fallback metadata."""
    try:
        return await LibraryHydrationService.get_episode_sources(
            library_type=library_type,
            series_id=series_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
