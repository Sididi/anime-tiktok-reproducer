import asyncio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pathlib import Path
from urllib.parse import unquote

from ...config import settings
from ...services import ProjectService, AnimeLibraryService

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
            # Try direct child first
            for ext in video_extensions:
                candidate = src_path / f"{episode_name}{ext}"
                if candidate.exists():
                    return candidate
            # Then recursive search
            for ext in video_extensions:
                for match in src_path.rglob(f"*{ext}"):
                    if match.stem == episode_name:
                        return match
        elif src_path.is_file() and src_path.stem == episode_name:
            return src_path
    return None


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


@router.get("/video/source")
async def get_source_video(
    project_id: str,
    path: str = Query(..., description="Path to the source episode file"),
) -> FileResponse:
    """
    Stream a source anime episode file.

    The path can be either:
    - A full path that matches one of the source_paths or anime_library_path
    - A short episode name (stem) that will be searched for in source_paths/anime_library_path
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Decode the path
    decoded_path = unquote(path)
    source_path = Path(decoded_path)

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".webm", ".mov"}

    # Build list of allowed source directories
    # Use project source_paths if configured, otherwise fall back to anime_library_path
    source_dirs: list[Path] = []
    if project.source_paths:
        source_dirs = [Path(src) for src in project.source_paths]
    elif settings.anime_library_path and settings.anime_library_path.exists():
        source_dirs = [settings.anime_library_path]

    if not source_dirs:
        raise HTTPException(status_code=400, detail="No source paths configured")

    # First, check if this is a full path that exists and is valid
    is_valid = False
    if source_path.is_absolute() and source_path.exists():
        for src_path in source_dirs:
            if src_path.is_dir():
                try:
                    source_path.relative_to(src_path)
                    is_valid = True
                    break
                except ValueError:
                    continue
            elif source_path == src_path:
                is_valid = True
                break

    # If not a valid full path, treat as an episode name and search for it
    if not is_valid:
        episode_name = decoded_path  # e.g., "[9volt] Hanebado! - 03 [D0B8F455]"
        found_path = None

        # Fast-path through indexed manifest for anime library lookups.
        if settings.anime_library_path and settings.anime_library_path.exists():
            manifest = await AnimeLibraryService.ensure_episode_manifest()
            candidate = AnimeLibraryService.resolve_episode_path(episode_name, manifest)
            if candidate and _is_path_allowed(candidate, source_dirs):
                found_path = candidate

        # Fallback for non-library custom source directories.
        if found_path is None:
            found_path = await asyncio.to_thread(
                _search_episode_sync,
                episode_name,
                source_dirs,
                VIDEO_EXTENSIONS,
            )

        if found_path:
            source_path = found_path
            is_valid = True

    if not is_valid:
        raise HTTPException(
            status_code=404,
            detail=f"Source file not found: {decoded_path}"
        )

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Source file not found")

    # Serve a browser-safe preview proxy when the source codec/container is not
    # directly playable in the frontend video element.
    stream_path = await AnimeLibraryService.resolve_source_preview_path(
        source_path,
        allow_generate=False,
    )
    if not stream_path.exists():
        stream_path = source_path

    # If we had to fall back to the original file for an unsupported source,
    # trigger proxy generation in background so future requests are compatible.
    if stream_path == source_path:
        compatible = await asyncio.to_thread(
            AnimeLibraryService.is_browser_preview_compatible,
            source_path,
        )
        if not compatible:
            await AnimeLibraryService.trigger_preview_proxy_generation(source_path)
            ready_proxy = await AnimeLibraryService.wait_for_preview_proxy(
                source_path,
                timeout_seconds=1.5,
            )
            if ready_proxy is not None:
                stream_path = ready_proxy

    # Determine media type based on extension
    suffix = stream_path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }
    media_type = media_types.get(suffix, "video/mp4")

    return FileResponse(
        stream_path,
        media_type=media_type,
        headers={"Accept-Ranges": "bytes"},
    )
