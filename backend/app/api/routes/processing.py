"""Processing routes for script restructure and final export."""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse
from typing import Optional
import json
import tempfile
from pathlib import Path

from pydub import AudioSegment

from ...models import ProjectPhase
from ...services import ProjectService, ProcessingService

router = APIRouter(prefix="/projects/{project_id}", tags=["processing"])


@router.post("/script/restructured")
async def upload_restructured_script(
    project_id: str,
    script: str = Form(...),
    audio: Optional[UploadFile] = File(None),
    audio_parts: Optional[list[UploadFile]] = File(None),
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
        content = await audio.read()
        audio_path.write_bytes(content)
    else:
        # Multiple files - concatenate them
        combined_audio: Optional[AudioSegment] = None

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            for i, part in enumerate(audio_parts):
                # Save each part temporarily
                part_content = await part.read()
                part_path = tmp_path / f"part_{i}{Path(part.filename or '.mp3').suffix}"
                part_path.write_bytes(part_content)

                # Load and concatenate
                segment = AudioSegment.from_file(str(part_path))
                if combined_audio is None:
                    combined_audio = segment
                else:
                    combined_audio = combined_audio + segment

            # Export combined audio
            if combined_audio is not None:
                combined_audio.export(str(audio_path), format="wav")
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Failed to process audio parts"
                )

    # Update phase
    project.phase = ProjectPhase.PROCESSING
    ProjectService.save(project)

    return {
        "status": "ok",
        "script_path": str(script_path),
        "audio_path": str(audio_path),
    }


@router.post("/process")
async def process_project(project_id: str):
    """Run the full processing pipeline (auto-editor, JSX generation, bundling)."""
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
