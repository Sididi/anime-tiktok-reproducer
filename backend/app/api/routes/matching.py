from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
from pathlib import Path
import re

from ...config import settings
from ...models import ProjectPhase, MatchList, SceneMatch, Scene, SceneList
from ...services import ProjectService, AnimeMatcherService, SceneMergerService

router = APIRouter(prefix="/projects/{project_id}", tags=["matching"])


class SetSourcesRequest(BaseModel):
    paths: list[str]


class FindMatchesRequest(BaseModel):
    source_path: str | None = None  # Optional, defaults to anime_library_path
    merge_continuous: bool = True  # Auto-merge continuous anime scenes


def _normalize_name(value: str) -> str:
    """Normalize folder/anime names for robust matching."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _resolve_anime_source_dir(library_root: Path, anime_name: str) -> Path | None:
    """Resolve the anime directory in the library using exact or normalized name match."""
    if not library_root.exists() or not library_root.is_dir():
        return None

    direct = library_root / anime_name
    if direct.exists() and direct.is_dir():
        return direct

    target = _normalize_name(anime_name)
    for child in library_root.iterdir():
        if child.is_dir() and _normalize_name(child.name) == target:
            return child

    return None


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

    # Use project source_paths if configured. Otherwise, scope to project anime in
    # the library so manual editors only offer episodes for this anime.
    source_dirs: list[Path] = []
    if project.source_paths:
        source_dirs = [Path(src) for src in project.source_paths]
    elif settings.anime_library_path and settings.anime_library_path.exists():
        if project.anime_name:
            scoped_dir = _resolve_anime_source_dir(
                settings.anime_library_path,
                project.anime_name,
            )
            source_dirs = [scoped_dir] if scoped_dir else []
        else:
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
    """Find anime source matches for all scenes with optional continuous scene merging."""
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
    merge_continuous = request.merge_continuous

    async def stream_progress():
        # === PASS 1: Match all scenes ===
        first_pass_label = "Pass 1: " if merge_continuous else ""
        first_pass_matches: MatchList | None = None

        async for progress in AnimeMatcherService.match_scenes(
            video_path, scenes, source_path,
            anime_name=anime_name,
            pass_label=first_pass_label,
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

            if progress.status == "complete" and progress.matches:
                first_pass_matches = progress.matches
            elif progress.status == "error":
                project.phase = ProjectPhase.SCENE_VALIDATION
                ProjectService.save(project)
                return

        if not first_pass_matches:
            return

        # If merge disabled or no matches, save and finish
        if not merge_continuous:
            ProjectService.save_matches(project_id, first_pass_matches)
            project.phase = ProjectPhase.MATCH_VALIDATION
            ProjectService.save(project)
            return

        # === CONTINUITY DETECTION ===
        yield f"data: {json.dumps({'status': 'matching', 'progress': 0.5, 'message': 'Detecting continuous scenes...', 'current_scene': 0, 'total_scenes': len(scenes.scenes), 'error': None})}\n\n"

        pairs = SceneMergerService.detect_continuous_pairs(scenes, first_pass_matches)

        if not pairs:
            # No continuous scenes found, save first pass results
            ProjectService.save_matches(project_id, first_pass_matches)
            project.phase = ProjectPhase.MATCH_VALIDATION
            ProjectService.save(project)
            yield f"data: {json.dumps({'status': 'complete', 'progress': 1.0, 'message': f'Matched {len(first_pass_matches.matches)} scenes (no continuous scenes found)', 'current_scene': len(scenes.scenes), 'total_scenes': len(scenes.scenes), 'error': None, 'matches': first_pass_matches.model_dump()})}\n\n"
            return

        chains = SceneMergerService.build_merge_chains(pairs, scenes, first_pass_matches)

        if not chains:
            ProjectService.save_matches(project_id, first_pass_matches)
            project.phase = ProjectPhase.MATCH_VALIDATION
            ProjectService.save(project)
            yield f"data: {json.dumps({'status': 'complete', 'progress': 1.0, 'message': f'Matched {len(first_pass_matches.matches)} scenes', 'current_scene': len(scenes.scenes), 'total_scenes': len(scenes.scenes), 'error': None, 'matches': first_pass_matches.model_dump()})}\n\n"
            return

        # === MERGE ===
        merged_count = sum(len(c) for c in chains)
        group_count = len(chains)
        merged_scenes, merged_matches, backup = SceneMergerService.merge_scenes_and_matches(
            scenes, first_pass_matches, chains,
        )

        SceneMergerService.save_pre_merge_backup(project_id, backup)
        ProjectService.save_scenes(project_id, merged_scenes)

        yield f"data: {json.dumps({'status': 'matching', 'progress': 0.6, 'message': f'Merged {merged_count} scenes into {group_count} groups. Re-matching...', 'current_scene': 0, 'total_scenes': len(merged_scenes.scenes), 'error': None})}\n\n"

        # === PASS 2: Re-match only merged scenes ===
        merged_indices = [
            i for i, m in enumerate(merged_matches.matches)
            if m.merged_from is not None
        ]

        async for progress in AnimeMatcherService.match_scenes(
            video_path, merged_scenes, source_path,
            anime_name=anime_name,
            scene_indices_to_match=merged_indices,
            existing_matches=merged_matches,
            pass_label="Pass 2: ",
        ):
            if progress.status == "complete" and progress.matches:
                # Preserve merged_from metadata on re-matched scenes
                for i in merged_indices:
                    if i < len(progress.matches.matches) and i < len(merged_matches.matches):
                        progress.matches.matches[i].merged_from = merged_matches.matches[i].merged_from

                ProjectService.save_matches(project_id, progress.matches)
                project.phase = ProjectPhase.MATCH_VALIDATION
                ProjectService.save(project)

            yield f"data: {json.dumps(progress.to_dict())}\n\n"

            if progress.status == "error":
                # On pass 2 error, still save what we have from pass 1
                ProjectService.save_matches(project_id, merged_matches)
                project.phase = ProjectPhase.MATCH_VALIDATION
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
    # Also preserve was_no_match flag if it was initially true
    if match.confidence == 0 and request.confirmed:
        match.confidence = 1.0
        # was_no_match should already be set, but ensure it's preserved

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


@router.post("/matches/undo-merge/{scene_index}")
async def undo_merge(project_id: str, scene_index: int):
    """Undo a merge for a specific scene, restoring original sub-scenes."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = SceneMergerService.undo_merge(project_id, scene_index)
    if not result:
        raise HTTPException(
            status_code=400,
            detail="Cannot undo: scene is not a merged scene or no backup found",
        )

    restored_scenes, restored_matches = result
    return {
        "scenes": [s.model_dump() for s in restored_scenes.scenes],
        "matches": [m.model_dump() for m in restored_matches.matches],
    }
