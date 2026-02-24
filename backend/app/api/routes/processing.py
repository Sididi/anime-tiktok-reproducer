"""Processing routes for script restructure and final export."""

import asyncio
import mimetypes
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse
from typing import Optional
import json
import tempfile
from pathlib import Path
from pydantic import BaseModel

from pydub import AudioSegment

from ...config import settings
from ...models import ProjectPhase
from ...services import (
    ProjectService,
    ProcessingService,
    MetadataService,
    ExportService,
    DiscordService,
    GeminiService,
    ElevenLabsService,
    VoiceConfigService,
    ScriptAutomationService,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["processing"])


UPLOAD_CHUNK_SIZE = 1024 * 1024


class MetadataPromptRequest(BaseModel):
    script: str
    target_language: str = "fr"


class ScriptAutomateRequest(BaseModel):
    target_language: str = "fr"
    voice_key: str
    existing_script_json: dict | None = None
    skip_metadata: bool = False
    skip_tts: bool = False


async def _write_upload_to_path(upload: UploadFile, destination: Path) -> None:
    """Stream uploaded file to disk in chunks."""
    with destination.open("wb") as out:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)


@router.get("/script/automation/config")
async def get_script_automation_config(project_id: str):
    """Return automation config and integration readiness for /script page."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    voice_error: str | None = None
    voices: list[dict] = []
    default_voice_key: str | None = None

    try:
        config = VoiceConfigService.get_config()
        preview_url_map: dict[str, str | None] = {}
        if ElevenLabsService.is_configured():
            preview_url_map = await asyncio.to_thread(ElevenLabsService.get_preview_url_map)
        voices = [
            {
                "key": entry.key,
                "display_name": entry.display_name,
                "preview_url": preview_url_map.get(entry.elevenlabs_voice_id),
            }
            for entry in config.voices.values()
        ]
        default_voice_key = config.default_voice_key
    except Exception as exc:
        voice_error = str(exc)

    return {
        "enabled": settings.script_automate_enabled,
        "gemini": {
            "configured": GeminiService.is_configured(),
            "model": settings.gemini_model,
        },
        "elevenlabs": {
            "configured": ElevenLabsService.is_configured(),
            "model_id": settings.elevenlabs_model_id,
            "output_format": settings.elevenlabs_output_format,
        },
        "voices": voices,
        "default_voice_key": default_voice_key,
        "voice_config_error": voice_error,
    }


@router.post("/script/automate")
async def automate_script(project_id: str, request: ScriptAutomateRequest):
    """Automate /script generation: Gemini script -> optional metadata -> ElevenLabs parts."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if not settings.script_automate_enabled:
        raise HTTPException(status_code=503, detail="Script automation is disabled")

    async def stream_progress():
        async for event in ScriptAutomationService.stream_automation(
            project_id=project_id,
            target_language=request.target_language,
            voice_key=request.voice_key,
            existing_script_json=request.existing_script_json,
            skip_metadata=request.skip_metadata,
            skip_tts=request.skip_tts,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/script/automate/runs/{run_id}/parts/{part_id}")
async def download_automation_part(project_id: str, run_id: str, part_id: str):
    """Download one generated TTS part from an automation run."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        part_path = ScriptAutomationService.get_part_path(project_id, run_id, part_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    media_type = mimetypes.guess_type(part_path.name)[0] or "application/octet-stream"
    return FileResponse(
        path=part_path,
        filename=part_path.name,
        media_type=media_type,
    )


@router.post("/script/restructured")
async def upload_restructured_script(
    project_id: str,
    script: str = Form(...),
    audio: Optional[UploadFile] = File(None),
    audio_parts: Optional[list[UploadFile]] = File(None),
    metadata_json: Optional[str] = Form(None),
):
    """Upload the restructured script JSON and new TTS audio file(s).

    Accepts either:
    - A single 'audio' file
    - Multiple 'audio_parts' files (will be concatenated in order)
    """
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate that we have at least one audio source
    if audio is None and (audio_parts is None or len(audio_parts) == 0):
        raise HTTPException(
            status_code=400,
            detail="Either 'audio' or 'audio_parts' must be provided"
        )

    # Validate JSON
    try:
        script_data = json.loads(script)
        if "scenes" not in script_data:
            raise ValueError("Script must contain 'scenes' array")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Save script
    project_dir = ProjectService.get_project_dir(project_id)
    script_path = project_dir / "new_script.json"
    script_path.write_text(json.dumps(script_data, indent=2))

    # Handle audio file(s)
    audio_path = project_dir / "new_tts.wav"

    if audio is not None:
        # Single file upload
        await _write_upload_to_path(audio, audio_path)
    else:
        # Multiple files - concatenate them
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            part_paths: list[Path] = []

            for i, part in enumerate(audio_parts):
                # Save each part temporarily
                part_path = tmp_path / f"part_{i}{Path(part.filename or '.mp3').suffix}"
                await _write_upload_to_path(part, part_path)
                part_paths.append(part_path)

            def _concat_parts_to_wav() -> None:
                combined_audio: Optional[AudioSegment] = None
                for part_path in part_paths:
                    segment = AudioSegment.from_file(str(part_path))
                    if combined_audio is None:
                        combined_audio = segment
                    else:
                        combined_audio = combined_audio + segment
                if combined_audio is None:
                    raise ValueError("Failed to process audio parts")
                combined_audio.export(str(audio_path), format="wav")

            try:
                await asyncio.to_thread(_concat_parts_to_wav)
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="Failed to process audio parts"
                )

    metadata_saved = False
    if metadata_json and metadata_json.strip():
        try:
            payload = MetadataService.validate_json_string(metadata_json)
            MetadataService.save(project_id, payload)
            metadata_saved = True
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if isinstance(script_data.get("language"), str):
        project.output_language = script_data["language"]

    # Update phase
    project.phase = ProjectPhase.PROCESSING
    ProjectService.save(project)

    return {
        "status": "ok",
        "script_path": str(script_path),
        "audio_path": str(audio_path),
        "metadata_saved": metadata_saved,
    }


@router.get("/metadata")
async def get_metadata(project_id: str):
    """Get persisted metadata for a project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    path = ProjectService.get_metadata_file(project_id)
    if not path.exists():
        return {"exists": False, "metadata": None}

    try:
        payload = MetadataService.load(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "exists": payload is not None,
        "metadata": payload.model_dump() if payload else None,
    }


@router.post("/metadata/prompt")
async def build_metadata_prompt(project_id: str, request: MetadataPromptRequest):
    """Build a metadata-generation prompt from script JSON."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        prompt = MetadataService.build_prompt_from_script_json(
            anime_name=project.anime_name or "Inconnu",
            script_json=request.script,
            target_language=request.target_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"prompt": prompt}


@router.post("/process")
async def process_project(project_id: str):
    """Run the full processing pipeline (auto-editor, JSX generation, subtitles)."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)

    # Load required files
    script_path = project_dir / "new_script.json"
    audio_path = project_dir / "new_tts.wav"

    if not script_path.exists():
        raise HTTPException(status_code=400, detail="New script not uploaded")
    if not audio_path.exists():
        raise HTTPException(status_code=400, detail="New TTS audio not uploaded")

    new_script = json.loads(script_path.read_text())

    # Load matches
    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")

    async def stream_progress():
        async for progress in ProcessingService.process(
            project,
            new_script,
            audio_path,
            matches.matches,
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

            if progress.status == "complete":
                project.phase = ProjectPhase.COMPLETE
                ProjectService.save(project)
            elif progress.status == "error":
                # Keep phase as processing so user can retry
                pass

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.get("/download/bundle")
async def download_bundle(project_id: str):
    """Download the generated project bundle."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    bundle_path = ProjectService.get_project_dir(project_id) / "project_bundle.zip"

    if not bundle_path.exists():
        raise HTTPException(status_code=404, detail="Bundle not found. Run processing first.")

    return FileResponse(
        path=bundle_path,
        filename=f"atr_project_{project_id}.zip",
        media_type="application/zip",
    )


@router.post("/exports/bundle")
async def create_bundle(project_id: str):
    """Create a project bundle on demand and stream progress updates."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")

    async def stream_progress():
        yield f"data: {json.dumps({'status': 'processing', 'step': 'bundle', 'progress': 0.1, 'message': 'Building ZIP bundle...'})}\n\n"
        try:
            await asyncio.to_thread(ExportService.build_bundle, project, matches.matches)
            yield f"data: {json.dumps({'status': 'complete', 'step': 'bundle', 'progress': 1.0, 'message': 'Bundle ready', 'download_url': f'/api/projects/{project_id}/download/bundle'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'step': 'bundle', 'progress': 0.0, 'error': str(exc), 'message': 'Bundle generation failed'})}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/exports/gdrive")
async def upload_to_gdrive(project_id: str):
    """Upload the project export tree to Google Drive and notify via Discord webhook."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")

    async def stream_progress():
        yield f"data: {json.dumps({'status': 'processing', 'step': 'gdrive', 'progress': 0.1, 'message': 'Uploading project to Google Drive...'})}\n\n"
        try:
            result = await asyncio.to_thread(ExportService.upload_manifest_to_drive, project, matches.matches)
            project.drive_folder_id = result["folder_id"]
            project.drive_folder_url = result["folder_url"]
            ProjectService.save(project)

            try:
                if project.generation_discord_message_id:
                    DiscordService.delete_message(project.generation_discord_message_id)
                    project.generation_discord_message_id = None

                anime_title = project.anime_name or "Inconnu"
                discord_message = DiscordService.post_message(
                    "\n".join(
                        [
                            f"**{anime_title}**: Génération terminée pour le projet `{project.id}`.",
                            f"Dossier Google Drive: <{result['folder_url']}>",
                        ]
                    )
                )
                if discord_message:
                    project.generation_discord_message_id = discord_message.id
                ProjectService.save(project)
            except Exception:
                pass

            yield f"data: {json.dumps({'status': 'complete', 'step': 'gdrive', 'progress': 1.0, 'message': 'Upload complete', 'folder_url': result['folder_url'], 'folder_id': result['folder_id']})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'step': 'gdrive', 'progress': 0.0, 'error': str(exc), 'message': 'Drive upload failed'})}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
