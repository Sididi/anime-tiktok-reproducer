"""Gap resolution routes for extending clips that hit the 75% speed floor."""

import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ...services import ProjectService
from ...services.gap_resolution import GapResolutionService

router = APIRouter(prefix="/projects/{project_id}/gaps", tags=["gap-resolution"])


class GapsResponse(BaseModel):
    """Response containing gaps that need resolution."""
    
    has_gaps: bool
    gaps: list[dict]
    total_gap_duration: float


class GapCandidatesResponse(BaseModel):
    """Response containing AI candidates for a gap."""
    
    scene_index: int
    candidates: list[dict]


class UpdateGapTimingRequest(BaseModel):
    """Request to update a gap's timing."""
    
    start_time: float
    end_time: float
    skipped: bool = False  # If True, user chose to keep the gap


class ComputeSpeedRequest(BaseModel):
    """Request to compute speed for given timing."""
    
    start_time: float
    end_time: float
    target_duration: float


class ComputeSpeedResponse(BaseModel):
    """Response with computed speed."""
    
    effective_speed: float
    raw_speed: float
    has_gap: bool  # True if raw_speed < 0.75


@router.get("")
async def get_gaps(project_id: str) -> GapsResponse:
    """Get all gaps that need resolution for this project.
    
    This should be called after the transcription step in processing
    to detect which scenes have gaps.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    project_dir = ProjectService.get_project_dir(project_id)
    
    # Load matches
    matches = ProjectService.load_matches(project_id)
    if not matches:
        return GapsResponse(has_gaps=False, gaps=[], total_gap_duration=0.0)
    
    # Load transcription (from the gap detection step - should have been saved)
    transcription_path = project_dir / "gap_detection_transcription.json"
    if not transcription_path.exists():
        # Try regular transcription
        transcription_path = project_dir / "output" / "transcription_timing.json"
        if not transcription_path.exists():
            return GapsResponse(has_gaps=False, gaps=[], total_gap_duration=0.0)
    
    try:
        transcription_data = json.loads(transcription_path.read_text())
        scene_timings = transcription_data.get("scenes", [])
    except (json.JSONDecodeError, KeyError):
        return GapsResponse(has_gaps=False, gaps=[], total_gap_duration=0.0)
    
    # Calculate gaps
    gaps = GapResolutionService.calculate_gaps(matches.matches, scene_timings)
    
    total_gap_duration = sum(g.gap_duration for g in gaps)
    
    return GapsResponse(
        has_gaps=len(gaps) > 0,
        gaps=[g.to_dict() for g in gaps],
        total_gap_duration=total_gap_duration,
    )


@router.get("/{scene_index}/candidates")
async def get_gap_candidates(project_id: str, scene_index: int) -> GapCandidatesResponse:
    """Get AI candidates for resolving a specific gap.
    
    Runs pyscenedetect on the source episode (with caching) and generates
    up to 4 candidates ranked by closeness to 100% speed.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    project_dir = ProjectService.get_project_dir(project_id)
    
    # Load matches
    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")
    
    # Load transcription
    transcription_path = project_dir / "gap_detection_transcription.json"
    if not transcription_path.exists():
        transcription_path = project_dir / "output" / "transcription_timing.json"
        if not transcription_path.exists():
            raise HTTPException(status_code=400, detail="No transcription found")
    
    try:
        transcription_data = json.loads(transcription_path.read_text())
        scene_timings = transcription_data.get("scenes", [])
    except (json.JSONDecodeError, KeyError):
        raise HTTPException(status_code=400, detail="Invalid transcription data")
    
    # Calculate gaps to find this specific scene
    gaps = GapResolutionService.calculate_gaps(matches.matches, scene_timings)
    
    gap = next((g for g in gaps if g.scene_index == scene_index), None)
    if not gap:
        raise HTTPException(status_code=404, detail=f"Scene {scene_index} does not have a gap")
    
    # Generate candidates
    candidates = await GapResolutionService.generate_candidates(gap)
    
    return GapCandidatesResponse(
        scene_index=scene_index,
        candidates=[c.to_dict() for c in candidates],
    )


@router.put("/{scene_index}")
async def update_gap_timing(
    project_id: str,
    scene_index: int,
    request: UpdateGapTimingRequest,
):
    """Update the timing for a gap scene.
    
    This updates the match data with the new extended timing.
    If skipped=True, we mark the scene as having an intentional gap.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Load matches
    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")
    
    # Find and update the match
    match_found = False
    for match in matches.matches:
        if match.scene_index == scene_index:
            match.start_time = request.start_time
            match.end_time = request.end_time
            
            # Recalculate speed ratio based on scene duration
            # We need to load transcription to get target duration
            project_dir = ProjectService.get_project_dir(project_id)
            transcription_path = project_dir / "gap_detection_transcription.json"
            if transcription_path.exists():
                try:
                    transcription_data = json.loads(transcription_path.read_text())
                    scene_timings = transcription_data.get("scenes", [])
                    scene_timing = next(
                        (s for s in scene_timings if s.get("scene_index") == scene_index),
                        None,
                    )
                    if scene_timing and scene_timing.get("words"):
                        words = scene_timing["words"]
                        target_duration = words[-1]["end"] - words[0]["start"]
                        source_duration = request.end_time - request.start_time
                        match.speed_ratio = source_duration / target_duration if target_duration > 0 else 1.0
                except (json.JSONDecodeError, KeyError):
                    pass
            
            match.confirmed = True
            match_found = True
            break
    
    if not match_found:
        raise HTTPException(status_code=404, detail=f"Match for scene {scene_index} not found")
    
    # Save updated matches
    ProjectService.save_matches(project_id, matches)
    
    # If all gaps are resolved/skipped, we can mark ready to continue processing
    # This will be checked by the frontend
    
    return {
        "status": "ok",
        "scene_index": scene_index,
        "start_time": request.start_time,
        "end_time": request.end_time,
        "skipped": request.skipped,
    }


@router.post("/compute-speed")
async def compute_speed(project_id: str, request: ComputeSpeedRequest) -> ComputeSpeedResponse:
    """Compute the speed for given timing parameters.
    
    Useful for showing live speed feedback when manually adjusting timings.
    """
    effective_speed = GapResolutionService.compute_speed_for_timing(
        request.start_time,
        request.end_time,
        request.target_duration,
    )
    
    raw_speed = GapResolutionService.compute_raw_speed_for_timing(
        request.start_time,
        request.end_time,
        request.target_duration,
    )
    
    return ComputeSpeedResponse(
        effective_speed=effective_speed,
        raw_speed=raw_speed,
        has_gap=raw_speed < GapResolutionService.MIN_SPEED,
    )


@router.post("/mark-resolved")
async def mark_gaps_resolved(project_id: str):
    """Mark gap resolution as complete, allowing processing to continue.
    
    This updates the project phase and creates a flag file.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    project_dir = ProjectService.get_project_dir(project_id)
    
    # Create a flag file indicating gaps have been resolved
    (project_dir / "gaps_resolved.flag").touch()
    
    return {"status": "ok", "message": "Gap resolution complete"}
