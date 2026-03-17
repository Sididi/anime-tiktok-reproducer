"""Processing routes for script restructure and final export."""

import asyncio
import mimetypes
import shutil
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse
from typing import Optional, Any
import json
import tempfile
from pathlib import Path
from threading import Lock
import wave
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
    ScriptPayloadService,
    ScriptPhasePromptService,
    MusicConfigService,
    AudioSpeedService,
)
from ...services.forced_alignment import ForcedAlignmentService

router = APIRouter(prefix="/projects/{project_id}", tags=["processing"])


UPLOAD_CHUNK_SIZE = 1024 * 1024
_gdrive_upload_locks_guard = Lock()
_gdrive_upload_locks: dict[str, Lock] = {}


class DriveUploadInProgressError(RuntimeError):
    """Raised when an upload is already active for the same project."""


class MetadataPromptRequest(BaseModel):
    script: str
    target_language: str = "fr"


class ScriptAutomateRequest(BaseModel):
    target_language: str = "fr"
    voice_key: str
    existing_script_json: dict | None = None
    skip_metadata: bool = False
    skip_tts: bool = False
    pause_after_script: bool = False
    skip_overlay: bool = False


class ScriptTtsPrepareRequest(BaseModel):
    script_json: dict[str, Any]
    target_language: str | None = None


class ScriptSettingsRequest(BaseModel):
    tts_speed: float | None = None
    music_key: str | None = None
    video_overlay: dict | None = None
    voice_key: str | None = None


class OverlayGenerateRequest(BaseModel):
    script_json: dict
    target_language: str = "fr"


class PreviewBuildRequest(BaseModel):
    run_id: str | None = None
    tts_speed: float = 1.0
    music_key: str | None = None


def _load_transcription_for_script(project_id: str):
    transcription = ProjectService.load_transcription(project_id)
    if not transcription or not transcription.scenes:
        raise HTTPException(status_code=400, detail="No transcription found for this project")
    return transcription


def _normalize_script_payload_or_400(
    project_id: str,
    payload: dict[str, Any],
    *,
    target_language: str | None = None,
):
    transcription = _load_transcription_for_script(project_id)
    try:
        return ScriptPayloadService.normalize(
            payload=payload,
            transcription=transcription,
            target_language=target_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _notify_drive_upload_complete(project_id: str, _folder_url: str) -> None:
    """
    Best-effort Discord notification after Drive export.

    This must never raise.
    """
    try:
        project = ProjectService.load(project_id)
        if not project:
            return

        if project.generation_discord_message_id:
            try:
                DiscordService.delete_message(project.generation_discord_message_id)
            except Exception:
                pass
            project.generation_discord_message_id = None

        anime_title = project.anime_name or "Inconnu"
        trigger_url = settings.cep_trigger_url_template.format(project_id=project.id)
        discord_message = DiscordService.post_message(
            "\n".join(
                [
                    f"**{anime_title}**: Génération terminée pour le projet `{project.id}`.",
                    f"Lien de génération: <{trigger_url}>",
                ]
            )
        )
        if discord_message:
            project.generation_discord_message_id = discord_message.id
        ProjectService.save(project)
    except Exception:
        # Notification must not impact API completion semantics.
        pass


def _get_gdrive_upload_lock(project_id: str) -> Lock:
    with _gdrive_upload_locks_guard:
        existing = _gdrive_upload_locks.get(project_id)
        if existing is not None:
            return existing
        lock = Lock()
        _gdrive_upload_locks[project_id] = lock
        return lock


def _upload_manifest_and_persist(
    project_id: str,
    project,
    matches,
    progress_callback=None,
) -> dict[str, Any]:
    """Run upload + persistence under a project lock."""
    lock = _get_gdrive_upload_lock(project_id)
    if not lock.acquire(blocking=False):
        raise DriveUploadInProgressError("Upload already in progress for this project")
    try:
        result = ExportService.upload_manifest_to_drive(
            project,
            matches,
            progress_callback=progress_callback,
        )
        project.drive_folder_id = result["folder_id"]
        project.drive_folder_url = result["folder_url"]
        project.drive_export_uploaded_once = True
        ProjectService.save(project)
        return result
    finally:
        lock.release()


async def _write_upload_to_path(upload: UploadFile, destination: Path) -> None:
    """Stream uploaded file to disk in chunks."""
    with destination.open("wb") as out:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)


def _is_wave_file(path: Path) -> bool:
    """Return True when the file is already a valid WAV container."""
    try:
        with wave.open(str(path), "rb") as wav_file:
            wav_file.getparams()
        return True
    except (wave.Error, EOFError):
        return False


def _normalize_audio_file_to_wav(input_path: Path, output_path: Path) -> None:
    """Copy WAV uploads as-is or transcode other audio formats to a real WAV file."""
    if _is_wave_file(input_path):
        shutil.copy2(input_path, output_path)
        return

    audio = AudioSegment.from_file(str(input_path))
    audio.export(str(output_path), format="wav")


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
                "languages": list(entry.languages) if entry.languages else None,
            }
            for entry in config.voices.values()
        ]
        default_voice_key = config.default_voice_key
    except Exception as exc:
        voice_error = str(exc)

    music_error: str | None = None
    musics: list[dict] = []
    default_music_key: str | None = None
    try:
        music_config = MusicConfigService.get_config()
        musics = [
            {"key": entry.key, "display_name": entry.display_name}
            for entry in music_config.musics.values()
        ]
        default_music_key = music_config.default_music_key
    except Exception as exc:
        music_error = str(exc)

    return {
        "enabled": settings.script_automate_enabled,
        "overlay_title_selection_enabled": settings.automate_overlay_title_selection_enabled,
        "gemini": {
            "configured": GeminiService.is_configured(),
            "model": settings.gemini_model,
        },
        "gemini_light": {
            "configured": GeminiService.is_configured(),
            "model": settings.gemini_light_model,
        },
        "elevenlabs": {
            "configured": ElevenLabsService.is_configured(),
            "model_id": settings.elevenlabs_model_id,
            "output_format": settings.elevenlabs_output_format,
        },
        "voices": voices,
        "default_voice_key": default_voice_key,
        "voice_config_error": voice_error,
        "musics": musics,
        "default_music_key": default_music_key,
        "music_config_error": music_error,
    }


@router.get("/script/latest-generation")
async def get_latest_generation(project_id: str):
    """Return the latest script generation (automation run or project root fallback)."""
    project_dir = ProjectService.get_project_dir(project_id)
    if not project_dir.exists():
        raise HTTPException(status_code=404, detail="Project not found")

    latest = ScriptAutomationService.get_latest_run(project_id)
    if latest and latest.get("script_json"):
        return {
            "exists": True,
            "source": "automation_run",
            "run_id": latest["run_id"],
            "script_json": latest["script_json"],
            "parts": latest["parts"],
        }

    fallback_path = project_dir / "new_script.json"
    if fallback_path.exists():
        try:
            script_json = json.loads(fallback_path.read_text(encoding="utf-8"))
            return {
                "exists": True,
                "source": "project_root",
                "run_id": None,
                "script_json": script_json,
                "parts": [],
            }
        except Exception:
            pass

    return {
        "exists": False,
        "source": None,
        "run_id": None,
        "script_json": None,
        "parts": [],
    }


@router.get("/script/prompt")
async def get_script_prompt(project_id: str, target_language: str = "fr"):
    """Build the canonical script prompt from the project's transcription."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    transcription = _load_transcription_for_script(project_id)
    prompt = ScriptPhasePromptService.build_script_prompt(
        project=project,
        transcription=transcription,
        target_language=target_language,
    )
    return {"prompt": prompt}


@router.get("/config")
async def get_processing_config(project_id: str):
    """Get processing page feature flags."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"gdrive_full_auto_enabled": settings.processing_gdrive_full_auto_enabled}


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
            pause_after_script=request.pause_after_script,
            skip_overlay=request.skip_overlay,
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


@router.post("/script/tts/prepare")
async def prepare_script_tts(project_id: str, request: ScriptTtsPrepareRequest):
    """Prepare normalized TTS text segments from a script JSON payload."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    normalized = _normalize_script_payload_or_400(
        project_id,
        request.script_json,
        target_language=request.target_language,
    )

    try:
        prepared_payload = await asyncio.to_thread(
            ScriptAutomationService.prepare_tts_payload,
            script_payload=normalized.public_payload,
            target_language=request.target_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return prepared_payload


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
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    normalized_script = _normalize_script_payload_or_400(project_id, script_data)
    try:
        prepared_tts = ScriptAutomationService.prepare_tts_payload(
            script_payload=normalized_script.public_payload,
            target_language=normalized_script.language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Save script
    project_dir = ProjectService.get_project_dir(project_id)
    script_path = project_dir / "new_script.json"
    script_path.write_text(
        json.dumps(normalized_script.public_payload, ensure_ascii=False, indent=2)
    )

    # Handle audio file(s)
    audio_path = project_dir / "new_tts.wav"

    if audio is not None:
        ForcedAlignmentService.clear_upload_artifacts(project_id)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            suffix = Path(audio.filename or "").suffix or ".bin"
            uploaded_audio_path = tmp_path / f"upload{suffix}"
            await _write_upload_to_path(audio, uploaded_audio_path)
            try:
                await asyncio.to_thread(
                    _normalize_audio_file_to_wav,
                    uploaded_audio_path,
                    audio_path,
                )
            except Exception:
                raise HTTPException(status_code=400, detail="Failed to process audio file")
        ForcedAlignmentService.save_upload_manifest(
            project_id,
            script_payload=normalized_script.public_payload,
            mode="single_audio",
        )
    else:
        expected_segment_count = len(prepared_tts.get("segments") or [])
        actual_segment_count = len(audio_parts or [])
        if actual_segment_count != expected_segment_count:
            raise HTTPException(
                status_code=400,
                detail=(
                    "audio_parts count does not match expected TTS segments "
                    f"({actual_segment_count} != {expected_segment_count})"
                ),
            )

        ForcedAlignmentService.clear_upload_artifacts(project_id)
        parts_dir = ForcedAlignmentService.parts_dir(project_id)
        parts_dir.mkdir(parents=True, exist_ok=True)

        # Multiple files - concatenate them
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stored_part_paths: list[Path] = []

            for i, part in enumerate(audio_parts):
                uploaded_path = tmp_path / f"part_{i}{Path(part.filename or '.mp3').suffix}"
                await _write_upload_to_path(part, uploaded_path)
                normalized_part_path = parts_dir / f"part_{i + 1:04d}.wav"
                try:
                    await asyncio.to_thread(
                        _normalize_audio_file_to_wav,
                        uploaded_path,
                        normalized_part_path,
                    )
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to process audio part {i + 1}",
                    )
                stored_part_paths.append(normalized_part_path)

            def _concat_parts_to_wav() -> None:
                combined_audio: Optional[AudioSegment] = None
                for part_path in stored_part_paths:
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

        ForcedAlignmentService.save_upload_manifest(
            project_id,
            script_payload=normalized_script.public_payload,
            mode="audio_parts",
            stored_part_paths=[
                str(path.relative_to(project_dir))
                for path in stored_part_paths
            ],
        )

    # Apply TTS speed if configured
    tts_speed = project.tts_speed
    if tts_speed is not None and tts_speed != 1.0:
        tmp_audio = audio_path.with_suffix(".tmp.wav")
        try:
            await AudioSpeedService.apply_speed(audio_path, tmp_audio, tts_speed)
            tmp_audio.rename(audio_path)
        except Exception as exc:
            tmp_audio.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Failed to apply TTS speed: {exc}")

    metadata_saved = False
    if metadata_json and metadata_json.strip():
        try:
            payload = MetadataService.validate_json_string(metadata_json)
            MetadataService.save(project_id, payload)
            metadata_saved = True
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Save video overlay if present
    if project.video_overlay:
        overlay_path = project_dir / "video_overlay.json"
        overlay_path.write_text(json.dumps(project.video_overlay, ensure_ascii=False, indent=2))

    if isinstance(normalized_script.public_payload.get("language"), str):
        project.output_language = normalized_script.public_payload["language"]

    # Update phase
    project.phase = ProjectPhase.PROCESSING
    ProjectService.save(project)

    return {
        "status": "ok",
        "script_path": str(script_path),
        "audio_path": str(audio_path),
        "metadata_saved": metadata_saved,
    }


@router.get("/music/{music_key}/preview")
async def preview_music(music_key: str):
    """Serve a music file for preview playback."""
    try:
        music = MusicConfigService.get_music(music_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    file_path = Path(music.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Music file not found on disk")

    media_type = mimetypes.guess_type(file_path.name)[0] or "audio/mpeg"
    return FileResponse(path=file_path, filename=file_path.name, media_type=media_type)


@router.patch("/script/settings")
async def update_script_settings(project_id: str, request: ScriptSettingsRequest):
    """Update script phase settings (TTS speed, music key)."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if request.tts_speed is not None:
        if request.tts_speed < AudioSpeedService.SPEED_MIN or request.tts_speed > AudioSpeedService.SPEED_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"tts_speed must be between {AudioSpeedService.SPEED_MIN} and {AudioSpeedService.SPEED_MAX}",
            )
        project.tts_speed = request.tts_speed

    provided = request.model_dump(exclude_unset=True)
    if "music_key" in provided:
        if request.music_key is not None:
            try:
                MusicConfigService.get_music(request.music_key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        project.music_key = request.music_key

    if "video_overlay" in provided:
        project.video_overlay = request.video_overlay

    if "voice_key" in provided:
        if request.voice_key is not None:
            try:
                VoiceConfigService.get_voice(request.voice_key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        project.voice_key = request.voice_key

    ProjectService.save(project)
    return {"status": "ok", "tts_speed": project.tts_speed, "music_key": project.music_key}


@router.get("/script/settings")
async def get_script_settings(project_id: str):
    """Get script phase settings (TTS speed, music key, video overlay)."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {
        "tts_speed": project.tts_speed,
        "music_key": project.music_key,
        "video_overlay": project.video_overlay,
        "voice_key": project.voice_key,
    }


@router.post("/script/overlay/generate")
async def generate_overlay(project_id: str, request: OverlayGenerateRequest):
    """Generate a video overlay (title + category) via Gemini light model."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if not GeminiService.is_configured():
        raise HTTPException(status_code=503, detail="Gemini API key is missing")

    normalized = _normalize_script_payload_or_400(
        project_id,
        request.script_json,
        target_language=request.target_language,
    )

    try:
        overlay = await asyncio.to_thread(
            ScriptAutomationService.generate_video_overlay,
            project=project,
            script_payload=normalized.public_payload,
            target_language=request.target_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project.video_overlay = overlay
    ProjectService.save(project)
    return {"status": "ok", "overlay": overlay}


@router.post("/script/preview/stage")
async def stage_preview_audio(
    project_id: str,
    audio: Optional[UploadFile] = File(None),
    audio_parts: Optional[list[UploadFile]] = File(None),
):
    """Stage uploaded audio files for preview playback before final submission."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if audio is None and (audio_parts is None or len(audio_parts) == 0):
        raise HTTPException(status_code=400, detail="Either 'audio' or 'audio_parts' must be provided")

    project_dir = ProjectService.get_project_dir(project_id)
    staged_path = project_dir / "preview_staged.wav"

    if audio is not None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            suffix = Path(audio.filename or "").suffix or ".bin"
            uploaded_audio_path = tmp_path / f"preview_upload{suffix}"
            await _write_upload_to_path(audio, uploaded_audio_path)
            try:
                await asyncio.to_thread(
                    _normalize_audio_file_to_wav,
                    uploaded_audio_path,
                    staged_path,
                )
            except Exception:
                raise HTTPException(status_code=400, detail="Failed to process audio file")
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            part_paths: list[Path] = []
            for i, part in enumerate(audio_parts):
                part_path = tmp_path / f"part_{i}{Path(part.filename or '.mp3').suffix}"
                await _write_upload_to_path(part, part_path)
                part_paths.append(part_path)

            def _concat():
                combined = AudioSegment.empty()
                for p in part_paths:
                    combined += AudioSegment.from_file(str(p))
                combined.export(str(staged_path), format="wav")

            try:
                await asyncio.to_thread(_concat)
            except Exception:
                raise HTTPException(status_code=400, detail="Failed to process audio parts")

    return {"staged": True}


@router.post("/script/preview/build")
async def build_preview(project_id: str, request: PreviewBuildRequest):
    """Build a preview audio file with optional speed + music mixing."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)

    # Find source TTS audio
    source_audio: Path | None = None
    if request.run_id:
        run_dir = project_dir / ScriptAutomationService.RUNS_DIR_NAME / request.run_id
        merged = run_dir / "merged.wav"
        if merged.exists():
            source_audio = merged
    if source_audio is None:
        staged_path = project_dir / "preview_staged.wav"
        if staged_path.exists():
            source_audio = staged_path
    if source_audio is None:
        tts_path = project_dir / "new_tts.wav"
        if tts_path.exists():
            source_audio = tts_path
    if source_audio is None:
        raise HTTPException(status_code=400, detail="No TTS audio available for preview")

    preview_path = project_dir / "preview.wav"

    # Step 1: Apply speed
    speed = request.tts_speed
    if speed < AudioSpeedService.SPEED_MIN or speed > AudioSpeedService.SPEED_MAX:
        speed = 1.0

    if speed != 1.0:
        speed_tmp = project_dir / "preview_speed_tmp.wav"
        try:
            await AudioSpeedService.apply_speed(source_audio, speed_tmp, speed)
            tts_audio = AudioSegment.from_file(str(speed_tmp))
        finally:
            speed_tmp.unlink(missing_ok=True)
    else:
        tts_audio = AudioSegment.from_file(str(source_audio))

    # Step 2: Mix music if requested
    if request.music_key:
        try:
            music = MusicConfigService.get_music(request.music_key)
            music_file = Path(music.file_path)
            if music_file.exists():
                music_audio = AudioSegment.from_file(str(music_file))
                tts_len = len(tts_audio)
                # Loop music to match TTS length
                if len(music_audio) < tts_len:
                    repeats = (tts_len // len(music_audio)) + 1
                    music_audio = music_audio * repeats
                music_audio = music_audio[:tts_len]
                # Apply volume and fade
                music_audio = music_audio + music.volume_db
                music_audio = music_audio.fade_out(2000)
                tts_audio = tts_audio.overlay(music_audio)
        except ValueError:
            pass  # Unknown music key, skip

    def _export():
        tts_audio.export(str(preview_path), format="wav")

    await asyncio.to_thread(_export)
    duration = len(tts_audio) / 1000.0

    return {
        "preview_url": f"/api/projects/{project_id}/script/preview/audio",
        "duration_seconds": duration,
    }


@router.get("/script/preview/audio")
async def get_preview_audio(project_id: str):
    """Serve the built preview audio file."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    preview_path = ProjectService.get_project_dir(project_id) / "preview.wav"
    if not preview_path.exists():
        raise HTTPException(status_code=404, detail="Preview not built yet")

    return FileResponse(path=preview_path, filename="preview.wav", media_type="audio/wav")


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
        script_payload = json.loads(request.script)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}")

    normalized = _normalize_script_payload_or_400(
        project_id,
        script_payload,
        target_language=request.target_language,
    )

    try:
        prompt = MetadataService.build_prompt_from_script_payload(
            anime_name=project.anime_name or "Inconnu",
            script_payload=normalized.public_payload,
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
    normalized_script = _normalize_script_payload_or_400(project_id, new_script)
    reference_transcription = _load_transcription_for_script(project_id)

    # Load matches
    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")

    async def stream_progress():
        async for progress in ProcessingService.process(
            project,
            normalized_script.internal_payload,
            audio_path,
            matches.matches,
            reference_transcription=reference_transcription,
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


@router.post("/duration-warning/acknowledge")
async def acknowledge_duration_warning(project_id: str):
    """Acknowledge the duration warning, allowing processing to continue."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)
    (project_dir / "duration_warning_acknowledged.flag").touch()

    return {"status": "ok"}


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
async def upload_to_gdrive(project_id: str, auto: bool = False):
    """Upload the project export tree to Google Drive and notify via Discord webhook."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if auto and project.drive_export_uploaded_once:
        folder_id = project.drive_folder_id
        folder_url = project.drive_folder_url
        if not folder_url and folder_id:
            folder_url = f"https://drive.google.com/drive/folders/{folder_id}"

        async def stream_skipped_auto():
            yield f"data: {json.dumps({'status': 'complete', 'step': 'gdrive', 'progress': 1.0, 'message': 'Auto-upload skipped: project already uploaded once.', 'folder_url': folder_url, 'folder_id': folder_id, 'skipped_auto': True})}\n\n"

        return StreamingResponse(
            stream_skipped_auto(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    matches = ProjectService.load_matches(project_id)
    if not matches:
        raise HTTPException(status_code=400, detail="No matches found")

    def _gdrive_progress_to_fraction(payload: dict[str, Any]) -> float:
        phase = str(payload.get("phase") or "")
        if phase == "manifest":
            return 0.12
        if phase == "clear":
            total = int(payload.get("clear_item_count") or 0)
            completed = int(payload.get("clear_items_completed") or 0)
            if total <= 0:
                return 0.25
            return min(0.3, 0.15 + (completed / total) * 0.15)
        if phase == "upload":
            total_bytes = int(payload.get("total_bytes") or 0)
            uploaded_bytes = int(payload.get("uploaded_bytes") or 0)
            if total_bytes <= 0:
                return 0.35
            return min(0.96, 0.3 + (uploaded_bytes / total_bytes) * 0.65)
        if phase == "persist":
            return 0.98
        return 0.1

    def _gdrive_progress_to_sse_payload(payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message") or "Uploading project to Google Drive...")
        return {
            "status": "processing",
            "step": "gdrive",
            "progress": _gdrive_progress_to_fraction(payload),
            "message": message,
            "phase": payload.get("phase"),
            "file_count": payload.get("file_count"),
            "files_completed": payload.get("files_completed"),
            "total_bytes": payload.get("total_bytes"),
            "uploaded_bytes": payload.get("uploaded_bytes"),
            "current_file": payload.get("current_file"),
            "clear_item_count": payload.get("clear_item_count"),
            "clear_items_completed": payload.get("clear_items_completed"),
            "elapsed_ms": payload.get("elapsed_ms"),
            "throughput_mb_per_sec": payload.get("throughput_mb_per_sec"),
        }

    async def stream_progress():
        yield (
            "data: "
            + json.dumps(
                {
                    "status": "processing",
                    "step": "gdrive",
                    "progress": 0.1,
                    "message": "Preparing Drive upload...",
                    "phase": "manifest",
                }
            )
            + "\n\n"
        )
        try:
            loop = asyncio.get_running_loop()
            progress_queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
            sentinel = object()

            def _progress_callback(payload: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(progress_queue.put_nowait, payload)

            def _run_upload():
                try:
                    return _upload_manifest_and_persist(
                        project_id,
                        project,
                        matches.matches,
                        _progress_callback,
                    )
                finally:
                    loop.call_soon_threadsafe(progress_queue.put_nowait, sentinel)

            worker = asyncio.create_task(asyncio.to_thread(_run_upload))
            while True:
                payload = await progress_queue.get()
                if payload is sentinel:
                    break
                yield f"data: {json.dumps(_gdrive_progress_to_sse_payload(payload))}\n\n"

            result = await worker
            asyncio.create_task(
                asyncio.to_thread(
                    _notify_drive_upload_complete,
                    project_id,
                    result["folder_url"],
                )
            )

            yield (
                "data: "
                + json.dumps(
                    {
                        "status": "complete",
                        "step": "gdrive",
                        "progress": 1.0,
                        "message": "Upload complete",
                        "phase": "complete",
                        "folder_url": result["folder_url"],
                        "folder_id": result["folder_id"],
                        "file_count": result.get("file_count"),
                        "files_completed": result.get("file_count"),
                        "total_bytes": result.get("total_bytes"),
                        "uploaded_bytes": result.get("total_bytes"),
                    }
                )
                + "\n\n"
            )
        except DriveUploadInProgressError as exc:
            yield f"data: {json.dumps({'status': 'error', 'step': 'gdrive', 'progress': 0.0, 'error': str(exc), 'error_code': 'upload_in_progress', 'message': 'Drive upload already running for this project'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'step': 'gdrive', 'progress': 0.0, 'error': str(exc), 'message': 'Drive upload failed'})}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
