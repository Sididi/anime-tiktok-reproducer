"""Processing routes for script restructure and final export."""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse
import json

from ...models import ProjectPhase
from ...services import ProjectService, ProcessingService

router = APIRouter(prefix="/projects/{project_id}", tags=["processing"])


@router.post("/script/restructured")
async def upload_restructured_script(
    project_id: str,
    script: str = Form(...),
    audio: UploadFile = File(...),
):
    """Upload the restructured script JSON and new TTS audio file."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

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

    # Save audio file
    audio_path = project_dir / "new_tts.wav"
    content = await audio.read()
    audio_path.write_bytes(content)

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
