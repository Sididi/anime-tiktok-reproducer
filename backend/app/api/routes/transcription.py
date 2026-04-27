from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json

from ...config import settings
from ...models import ProjectPhase, Transcription, SceneTranscription
from ...services import ProjectService, TranscriberService
from ...services.match_playback_service import MatchPlaybackService

router = APIRouter(prefix="/projects/{project_id}/transcription", tags=["transcription"])


class StartTranscriptionRequest(BaseModel):
    language: str = "auto"


class UpdateTranscriptionRequest(BaseModel):
    scenes: list[dict]  # scene_index, text pairs


@router.get("/config")
async def get_transcription_config(project_id: str):
    """Get transcription page feature flags."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"full_auto_enabled": settings.transcription_full_auto_enabled}


@router.post("/start")
async def start_transcription(project_id: str, request: StartTranscriptionRequest):
    """Start transcription with WhisperX."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if MatchPlaybackService.is_prepare_running(project_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Playback preparation is still running for this project. "
                "Wait for /matches warmup to complete before starting transcription."
            ),
        )

    # Update phase
    project.phase = ProjectPhase.TRANSCRIPTION
    ProjectService.save(project)

    async def stream_progress():
        async for progress in TranscriberService.transcribe(project_id, request.language):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

            if progress.status == "complete":
                # Check if raw scenes were detected
                detection_file = ProjectService.get_project_dir(project_id) / "raw_scene_detection.json"
                if detection_file.exists():
                    from ...models.raw_scene import RawSceneDetectionResult
                    detection = RawSceneDetectionResult.model_validate_json(
                        detection_file.read_text()
                    )
                    if detection.has_raw_scenes:
                        project.phase = ProjectPhase.RAW_SCENE_VALIDATION
                        ProjectService.save(project)
                        continue

                project.phase = ProjectPhase.SCRIPT_RESTRUCTURE
                ProjectService.save(project)

            elif progress.status == "error":
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


@router.get("")
async def get_transcription(project_id: str):
    """Get transcription for a project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    transcription = ProjectService.load_transcription(project_id)
    if not transcription:
        return {"transcription": None}

    return {"transcription": transcription.model_dump()}


@router.put("")
async def update_transcription(project_id: str, request: UpdateTranscriptionRequest):
    """Update transcription text (user edits)."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    transcription = ProjectService.load_transcription(project_id)
    if not transcription:
        raise HTTPException(status_code=404, detail="No transcription found")

    # Update scene texts
    for update in request.scenes:
        scene_index = update.get("scene_index")
        new_text = update.get("text")
        if scene_index is None or new_text is None:
            continue

        for scene in transcription.scenes:
            if scene.scene_index == scene_index:
                # Raw scenes must keep empty text by invariant.
                if scene.is_raw:
                    scene.text = ""
                else:
                    scene.text = new_text
                break

    ProjectService.save_transcription(project_id, transcription)
    return {"status": "ok", "transcription": transcription.model_dump()}


@router.post("/confirm")
async def confirm_transcription(project_id: str):
    """Confirm transcription is valid and proceed."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Check if raw scenes were detected — route to validation if so
    detection_file = ProjectService.get_project_dir(project_id) / "raw_scene_detection.json"
    if detection_file.exists():
        from ...models.raw_scene import RawSceneDetectionResult
        detection = RawSceneDetectionResult.model_validate_json(detection_file.read_text())
        if detection.has_raw_scenes:
            project.phase = ProjectPhase.RAW_SCENE_VALIDATION
            ProjectService.save(project)
            return {"status": "ok", "next_phase": "raw_scene_validation"}

    project.phase = ProjectPhase.SCRIPT_RESTRUCTURE
    ProjectService.save(project)

    return {"status": "ok", "next_phase": "script_restructure"}
