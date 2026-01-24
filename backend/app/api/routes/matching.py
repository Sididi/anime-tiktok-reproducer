from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
from pathlib import Path

from ...config import settings
from ...models import ProjectPhase, MatchList
from ...services import ProjectService, AnimeMatcherService

router = APIRouter(prefix="/projects/{project_id}", tags=["matching"])


class SetSourcesRequest(BaseModel):
    paths: list[str]


class FindMatchesRequest(BaseModel):
    source_path: str | None = None  # Optional, defaults to anime_library_path


@router.post("/sources")
async def set_sources(project_id: str, request: SetSourcesRequest):
    """Set source episode paths for the project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate paths exist
    for path in request.paths:
        if not Path(path).exists():
            raise HTTPException(status_code=400, detail=f"Path not found: {path}")

    project.source_paths = request.paths
    ProjectService.save(project)

    return {"status": "ok", "source_paths": project.source_paths}


@router.get("/sources")
async def get_sources(project_id: str):
    """Get source episode paths."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"source_paths": project.source_paths}


@router.get("/sources/episodes")
async def list_episodes(project_id: str):
    """List all video files in the source paths or anime library."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".webm", ".mov"}
    episodes: list[str] = []

    # Use project source_paths if configured, otherwise fall back to anime_library_path
    source_dirs: list[Path] = []
    if project.source_paths:
        source_dirs = [Path(src) for src in project.source_paths]
    elif settings.anime_library_path and settings.anime_library_path.exists():
        source_dirs = [settings.anime_library_path]

    for src_path in source_dirs:
        if src_path.is_dir():
            # Collect all video files in directory
            for ext in VIDEO_EXTENSIONS:
                episodes.extend(str(f) for f in src_path.glob(f"*{ext}"))
                episodes.extend(str(f) for f in src_path.glob(f"**/*{ext}"))
        elif src_path.is_file() and src_path.suffix.lower() in VIDEO_EXTENSIONS:
            episodes.append(str(src_path))

    # Remove duplicates and sort
    episodes = sorted(set(episodes))

    return {"episodes": episodes}


@router.post("/matches/find")
async def find_matches(project_id: str, request: FindMatchesRequest):
    """Find anime source matches for all scenes."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Use provided source_path or default to anime_library_path
    source_path = Path(request.source_path) if request.source_path else settings.anime_library_path
    if not source_path.exists():
        raise HTTPException(status_code=400, detail="Source path not found")

    # Load scenes
    scenes = ProjectService.load_scenes(project_id)
    if not scenes or not scenes.scenes:
        raise HTTPException(status_code=400, detail="No scenes detected yet")

    video_path = Path(project.video_path) if project.video_path else None
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=400, detail="Video not found")

    # Update phase
    project.phase = ProjectPhase.MATCHING
    ProjectService.save(project)

    # Get anime name for filtering (if set on project)
    anime_name = project.anime_name

    async def stream_progress():
        async for progress in AnimeMatcherService.match_scenes(
            video_path, scenes, source_path, anime_name=anime_name
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

            if progress.status == "complete" and progress.matches:
                # Save matches
                ProjectService.save_matches(project_id, progress.matches)

                # Update phase
                project.phase = ProjectPhase.MATCH_VALIDATION
                ProjectService.save(project)

            elif progress.status == "error":
                project.phase = ProjectPhase.SCENE_VALIDATION
                ProjectService.save(project)

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/matches")
async def get_matches(project_id: str):
    """Get all matches for a project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        return {"matches": []}

    return {"matches": [m.model_dump() for m in matches.matches]}


class UpdateMatchRequest(BaseModel):
    episode: str
    start_time: float
    end_time: float
    confirmed: bool = True


@router.put("/matches/{scene_index}")
async def update_match(project_id: str, scene_index: int, request: UpdateMatchRequest):
    """Update or confirm a match for a scene."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=404, detail="No matches found")

    # Find the match for this scene
    match = next((m for m in matches.matches if m.scene_index == scene_index), None)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found for scene")

    # Update match
    match.episode = request.episode
    match.start_time = request.start_time
    match.end_time = request.end_time
    match.confirmed = request.confirmed

    # Set confidence to 1.0 for manually confirmed matches (if it was 0)
    if match.confidence == 0 and request.confirmed:
        match.confidence = 1.0

    # Recalculate speed ratio
    scenes = ProjectService.load_scenes(project_id)
    if scenes and scene_index < len(scenes.scenes):
        scene = scenes.scenes[scene_index]
        scene_duration = scene.end_time - scene.start_time
        source_duration = match.end_time - match.start_time
        if source_duration > 0:
            match.speed_ratio = scene_duration / source_duration

    ProjectService.save_matches(project_id, matches)

    return {"status": "ok", "match": match.model_dump()}
