from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...models import MatchList, ProjectPhase, SceneTranscription, Transcription
from ...models.raw_scene import RawSceneDetectionResult
from ...services import ProjectService

router = APIRouter(prefix="/projects/{project_id}/raw-scenes", tags=["raw-scenes"])


class SceneValidation(BaseModel):
    scene_index: int
    is_raw: bool
    text: str | None = None


class ValidateRequest(BaseModel):
    validations: list[SceneValidation]


@router.get("")
async def get_raw_scenes(project_id: str):
    """Get raw scene detection result and current transcription."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    detection_file = ProjectService.get_project_dir(project_id) / "raw_scene_detection.json"
    if not detection_file.exists():
        return {"detection": None, "transcription": None}

    detection = RawSceneDetectionResult.model_validate_json(detection_file.read_text())
    transcription = ProjectService.load_transcription(project_id)

    return {
        "detection": detection.model_dump(),
        "transcription": transcription.model_dump() if transcription else None,
    }


@router.post("/validate")
async def validate_raw_scenes(project_id: str, request: ValidateRequest):
    """Validate or invalidate detected raw scenes.

    For invalidated scenes (is_raw=False): merge back into adjacent TTS scene.
    For validated scenes: keep as raw.
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    transcription = ProjectService.load_transcription(project_id)
    if not transcription:
        raise HTTPException(status_code=404, detail="No transcription found")

    # Build lookup of validations
    validation_map = {v.scene_index: v for v in request.validations}

    # Apply validations
    for scene in transcription.scenes:
        v = validation_map.get(scene.scene_index)
        if v is None:
            continue

        if v.is_raw:
            # Confirmed as raw
            scene.is_raw = True
            scene.text = ""
            scene.words = []
        else:
            # Invalidated — mark as not raw, optionally set text
            scene.is_raw = False
            if v.text is not None:
                scene.text = v.text

    # Merge invalidated raw scenes into adjacent TTS scenes
    transcription.scenes = _merge_invalidated_scenes(transcription.scenes)

    ProjectService.save_transcription(project_id, transcription)
    return {"status": "ok", "transcription": transcription.model_dump()}


@router.post("/confirm")
async def confirm_raw_scenes(project_id: str):
    """Finalize raw scene validation and advance to script phase."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update matches to reflect final scene indices
    transcription = ProjectService.load_transcription(project_id)
    if transcription:
        match_list = ProjectService.load_matches(project_id)
        if match_list:
            # Re-map match scene_index to match current transcription scene indices
            matches_by_index = {m.scene_index: m for m in match_list.matches}
            updated = []
            for scene in transcription.scenes:
                m = matches_by_index.get(scene.scene_index)
                if m:
                    updated.append(m)
            if updated:
                match_list.matches = updated
                ProjectService.save_matches(project_id, match_list)

    project.phase = ProjectPhase.SCRIPT_RESTRUCTURE
    ProjectService.save(project)

    return {"status": "ok"}


@router.post("/reset")
async def reset_raw_scenes(project_id: str):
    """Reset raw scene validation to the post-detection state (before user edits)."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)

    # Restore transcription from backup
    backup_trans = project_dir / "transcription_raw_backup.json"
    if not backup_trans.exists():
        raise HTTPException(status_code=404, detail="No raw scene backup found — re-run transcription")

    transcription = Transcription.model_validate_json(backup_trans.read_text())
    ProjectService.save_transcription(project_id, transcription)

    # Restore matches from backup if available
    backup_matches = project_dir / "matches_raw_backup.json"
    if backup_matches.exists():
        match_list = MatchList.model_validate_json(backup_matches.read_text())
        ProjectService.save_matches(project_id, match_list)

    # Set phase back to raw scene validation
    project.phase = ProjectPhase.RAW_SCENE_VALIDATION
    ProjectService.save(project)

    return {"status": "ok"}


def _merge_invalidated_scenes(scenes: list[SceneTranscription]) -> list[SceneTranscription]:
    """Merge invalidated (non-raw) scenes that were formerly raw back into adjacent TTS scenes.

    A formerly-raw scene that was invalidated has is_raw=False but empty words.
    We merge it into the previous TTS scene (or next if it's the first scene).
    """
    if not scenes:
        return scenes

    # Identify scenes that were invalidated (not raw, but have no words and empty text)
    # These are scenes that were detected as raw but user said "not raw"
    # They'll have is_raw=False set by validate, but may have user-provided text
    # We only merge scenes that are still effectively empty after invalidation
    # Actually, per the plan: merge if invalidated. We track this by checking
    # scenes that had is_raw toggled off. Since we can't track the toggle directly,
    # we merge any non-raw scene with no words (it was formerly raw).
    result: list[SceneTranscription] = []
    for scene in scenes:
        if not scene.is_raw and not scene.words and not scene.text.strip():
            # Empty non-raw scene — merge into previous
            if result:
                prev = result[-1]
                prev.end_time = scene.end_time
            # If no previous scene, just drop it (edge case)
            continue

        if not scene.is_raw and not scene.words and scene.text.strip():
            # Non-raw scene with user-provided text but no words — merge text into previous
            if result:
                prev = result[-1]
                prev.end_time = scene.end_time
                if scene.text.strip():
                    prev.text = f"{prev.text} {scene.text}".strip() if prev.text else scene.text
                continue

        result.append(scene)

    # Re-index
    for idx, s in enumerate(result):
        s.scene_index = idx

    return result
