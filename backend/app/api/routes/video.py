import asyncio
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ...config import settings
from ...services import (
    AnimeLibraryService,
    ProjectService,
    SourceChunkStreamingService,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["video"])


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_path_allowed(path: Path, source_dirs: list[Path]) -> bool:
    for src_path in source_dirs:
        if src_path.is_dir():
            if _is_under(path, src_path):
                return True
        elif path.resolve() == src_path.resolve():
            return True
    return False


def _search_episode_sync(episode_name: str, source_dirs: list[Path], video_extensions: set[str]) -> Path | None:
    for src_path in source_dirs:
        if src_path.is_dir():
            for ext in video_extensions:
                candidate = src_path / f"{episode_name}{ext}"
                if candidate.exists():
                    return candidate
            for ext in video_extensions:
                for match in src_path.rglob(f"*{ext}"):
                    if match.stem == episode_name:
                        return match
        elif src_path.is_file() and src_path.stem == episode_name:
            return src_path
    return None


def _build_source_dirs(project) -> list[Path]:
    if project.source_paths:
        return [Path(src) for src in project.source_paths]
    library_root = AnimeLibraryService.get_library_path(project.library_type)
    if library_root.exists():
        return [library_root]
    return []


async def _resolve_source_path(project, raw_path: str) -> Path:
    decoded_path = unquote(raw_path)
    source_path = Path(decoded_path)

    video_extensions = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v"}
    source_dirs = _build_source_dirs(project)

    if not source_dirs:
        raise HTTPException(status_code=400, detail="No source paths configured")

    if source_path.is_absolute() and source_path.exists() and _is_path_allowed(source_path, source_dirs):
        return source_path

    found_path: Path | None = None

    library_root = AnimeLibraryService.get_library_path(project.library_type)
    if library_root.exists():
        manifest = await AnimeLibraryService.ensure_episode_manifest(
            library_type=project.library_type,
        )
        candidate = AnimeLibraryService.resolve_episode_path(
            decoded_path,
            manifest,
            library_type=project.library_type,
        )
        if candidate and _is_path_allowed(candidate, source_dirs):
            found_path = candidate

    if found_path is None:
        found_path = await asyncio.to_thread(
            _search_episode_sync,
            decoded_path,
            source_dirs,
            video_extensions,
        )

    if found_path is None or not found_path.exists():
        raise HTTPException(status_code=404, detail=f"Source file not found: {decoded_path}")

    return found_path


def _media_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/mp4",
    }
    return media_types.get(suffix, "video/mp4")


@router.get("/video")
async def get_video(project_id: str) -> FileResponse:
    """Stream the project's video file."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if not project.video_path:
        raise HTTPException(status_code=404, detail="Video not yet downloaded")

    video_path = Path(project.video_path)
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    return FileResponse(
        video_path,
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/video/info")
async def get_video_info(project_id: str) -> dict:
    """Get video metadata."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {
        "duration": project.video_duration,
        "fps": project.video_fps,
        "width": project.video_width,
        "height": project.video_height,
        "path": project.video_path,
    }


@router.get("/video/source/descriptor")
async def get_source_video_descriptor(
    project_id: str,
    path: str = Query(..., description="Path to the source episode file"),
) -> dict[str, object]:
    """Return source streaming descriptor for manual preview workflows."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    source_path = await _resolve_source_path(project, path)
    return await SourceChunkStreamingService.get_descriptor(source_path)


@router.get("/video/source/chunk")
async def get_source_video_chunk(
    project_id: str,
    path: str = Query(..., description="Path to the source episode file"),
    chunk_start: float = Query(..., ge=0.0, description="Chunk start time in seconds"),
    chunk_duration: float | None = Query(
        default=None,
        gt=0.0,
        description="Optional chunk duration in seconds",
    ),
) -> FileResponse:
    """Serve one browser-safe source chunk for preview streaming."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    source_path = await _resolve_source_path(project, path)
    descriptor = await SourceChunkStreamingService.get_descriptor(source_path)

    if descriptor.get("mode") == "passthrough":
        raise HTTPException(
            status_code=400,
            detail="Source is browser-compatible. Use /video/source for direct passthrough.",
        )

    try:
        chunk_path = await SourceChunkStreamingService.get_chunk(
            source_path=source_path,
            chunk_start=chunk_start,
            chunk_duration=chunk_duration,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FileResponse(
        chunk_path,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


@router.get("/video/source")
async def get_source_video(
    project_id: str,
    path: str = Query(..., description="Path to the source episode file"),
) -> FileResponse:
    """
    Stream a source anime episode file when browser-compatible.

    For non-compatible sources, clients must use the descriptor/chunk endpoints.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    source_path = await _resolve_source_path(project, path)

    compatible = await asyncio.to_thread(
        AnimeLibraryService.is_browser_preview_compatible,
        source_path,
    )
    if not compatible:
        raise HTTPException(
            status_code=415,
            detail=(
                "Source video is not browser-compatible. "
                "Use /video/source/descriptor and /video/source/chunk for chunked streaming."
            ),
        )

    return FileResponse(
        source_path,
        media_type=_media_type_for_path(source_path),
        headers={"Accept-Ranges": "bytes"},
    )
