"""Gap resolution routes for extending clips that hit the 75% speed floor."""

import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ...services import ProjectService
from ...services.gap_resolution import GapResolutionService
from ...utils.timing import compute_adjusted_scene_end_times

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


class AllCandidatesResponse(BaseModel):
    """Response containing candidates for all gaps in one batch."""

    candidates_by_scene: dict[int, list[dict]]


@router.get("/all-candidates")
async def get_all_candidates(project_id: str) -> AllCandidatesResponse:
    """Get AI candidates for ALL gaps in a single batch request.

    Loads matches/transcription once, calculates gaps once, then generates
    candidates sequentially to avoid subprocess storms (ffprobe, pyscenedetect).
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)

    # Load matches ONCE
    matches = ProjectService.load_matches(project_id)
    if not matches:
        return AllCandidatesResponse(candidates_by_scene={})

    # Load transcription ONCE
    transcription_path = project_dir / "gap_detection_transcription.json"
    if not transcription_path.exists():
        transcription_path = project_dir / "output" / "transcription_timing.json"
        if not transcription_path.exists():
            return AllCandidatesResponse(candidates_by_scene={})

    try:
        transcription_data = json.loads(transcription_path.read_text())
        scene_timings = transcription_data.get("scenes", [])
    except (json.JSONDecodeError, KeyError):
        return AllCandidatesResponse(candidates_by_scene={})

    # Calculate gaps ONCE
    gaps = GapResolutionService.calculate_gaps(matches.matches, scene_timings)

    try:
        candidates_by_scene_raw = await GapResolutionService.generate_candidates_batch_dedup(gaps)
    except Exception:
        candidates_by_scene_raw = {gap.scene_index: [] for gap in gaps}
    candidates_by_scene = {
        scene_index: [candidate.to_dict() for candidate in candidates]
        for scene_index, candidates in candidates_by_scene_raw.items()
    }

    return AllCandidatesResponse(candidates_by_scene=candidates_by_scene)


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


class AutoFillResult(BaseModel):
    """Result of auto-filling a single gap."""
    scene_index: int
    success: bool
    start_time: float | None = None
    end_time: float | None = None
    speed: float | None = None
    message: str = ""


class AutoFillResponse(BaseModel):
    """Response for auto-fill all gaps."""
    filled_count: int
    skipped_count: int
    results: list[AutoFillResult]


@router.post("/auto-fill")
async def auto_fill_all_gaps(project_id: str) -> AutoFillResponse:
    """Automatically fill all gaps with their best AI candidate.

    For each gap, generates candidates and applies the one closest to 100% speed.
    Gaps without valid candidates are skipped.
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

    # Calculate all gaps
    gaps = GapResolutionService.calculate_gaps(matches.matches, scene_timings)

    results = []
    filled_count = 0
    skipped_count = 0

    candidates_by_scene = await GapResolutionService.generate_candidates_batch_dedup(gaps)

    for gap in gaps:
        candidates = candidates_by_scene.get(gap.scene_index, [])

        if not candidates:
            results.append(AutoFillResult(
                scene_index=gap.scene_index,
                success=False,
                message="No valid candidates found",
            ))
            skipped_count += 1
            continue

        # Get the best candidate (first one, sorted by speed_diff)
        best = candidates[0]

        # Find and update the match
        for match in matches.matches:
            if match.scene_index == gap.scene_index:
                match.start_time = best.start_time
                match.end_time = best.end_time
                # Convert Fraction to float for Pydantic model
                match.speed_ratio = float(best.effective_speed)
                match.confirmed = True
                break

        results.append(AutoFillResult(
            scene_index=gap.scene_index,
            success=True,
            # Keep response precision aligned with /candidates payload.
            start_time=round(best.start_time, 6),
            end_time=round(best.end_time, 6),
            speed=round(float(best.effective_speed), 6),  # Convert Fraction to float
            message=f"Applied: {best.snap_description}",
        ))
        filled_count += 1

    # Save updated matches
    ProjectService.save_matches(project_id, matches)

    return AutoFillResponse(
        filled_count=filled_count,
        skipped_count=skipped_count,
        results=results,
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

                    # Compute adjusted end times to eliminate gaps between scenes
                    adjusted_ends = compute_adjusted_scene_end_times(
                        scenes=scene_timings,
                        get_scene_index=lambda s: s.get("scene_index"),
                        get_first_word_start=lambda s: s["words"][0]["start"] if s.get("words") else None,
                        get_last_word_end=lambda s: s["words"][-1]["end"] if s.get("words") else None,
                    )

                    scene_timing = next(
                        (s for s in scene_timings if s.get("scene_index") == scene_index),
                        None,
                    )
                    if scene_timing and scene_timing.get("words"):
                        words = scene_timing["words"]
                        # Use adjusted end time to eliminate gaps between scenes
                        timeline_end = adjusted_ends.get(scene_index, words[-1]["end"])
                        target_duration = timeline_end - words[0]["start"]
                        # Use the Fraction-based compute function for precision
                        speed_frac = GapResolutionService.compute_raw_speed_for_timing(
                            request.start_time,
                            request.end_time,
                            target_duration,
                        )
                        match.speed_ratio = float(speed_frac)  # Convert to float for model
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
    Uses Fraction-based arithmetic for frame-perfect precision.
    """
    effective_speed_frac = GapResolutionService.compute_speed_for_timing(
        request.start_time,
        request.end_time,
        request.target_duration,
    )

    raw_speed_frac = GapResolutionService.compute_raw_speed_for_timing(
        request.start_time,
        request.end_time,
        request.target_duration,
    )

    return ComputeSpeedResponse(
        effective_speed=float(effective_speed_frac),  # Convert Fraction to float
        raw_speed=float(raw_speed_frac),  # Convert Fraction to float
        has_gap=raw_speed_frac < GapResolutionService.MIN_SPEED,
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


@router.post("/reset")
async def reset_gaps(project_id: str):
    """Reset gap resolution state, allowing gaps to be reprocessed.

    This removes the gaps_resolved.flag and resets all matches to their
    original timings (before any gap resolution was applied).
    The original matches are stored when gap detection first runs.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)

    # Remove the gaps_resolved flag
    gaps_resolved_flag = project_dir / "gaps_resolved.flag"
    if gaps_resolved_flag.exists():
        gaps_resolved_flag.unlink()

    # Check if we have a backup of original matches (before gap resolution)
    original_matches_path = project_dir / "matches_before_gaps.json"
    matches_path = project_dir / "matches.json"

    if original_matches_path.exists():
        # Restore original matches
        import shutil
        shutil.copy(original_matches_path, matches_path)

    return {
        "status": "ok",
        "message": "Gap resolution reset. Navigate to /processing to re-detect gaps.",
    }
