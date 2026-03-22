import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ...services import UploadPhaseService
from ...services.google_drive_service import GoogleDriveService
from ...services.project_service import ProjectService


router = APIRouter(prefix="/project-manager", tags=["project-manager"])


class UploadProjectRequest(BaseModel):
    account_id: str | None = None
    platforms: list[Literal["youtube", "facebook", "instagram"]] | None = None
    facebook_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    youtube_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    copyright_audio_path: str | None = None


class FacebookCheckRequest(BaseModel):
    account_id: str | None = None


class YouTubeCheckRequest(BaseModel):
    account_id: str | None = None


class CopyrightCheckRequest(BaseModel):
    account_id: str | None = None


class CopyrightBuildAudioRequest(BaseModel):
    music_key: str | None = None
    no_music_file_id: str


@router.get("/projects")
async def list_project_manager_projects():
    """List locally stored projects enriched with Drive/upload status."""
    try:
        rows = await asyncio.to_thread(UploadPhaseService.list_manager_rows)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"projects": rows}


@router.post("/projects/{project_id}/upload")
async def run_upload_phase(
    project_id: str,
    payload: UploadProjectRequest | None = Body(default=None),
):
    """Upload a ready project to configured platforms."""
    async def stream_progress():
        req = payload or UploadProjectRequest()
        yield f"data: {json.dumps({'status': 'processing', 'step': 'prepare', 'progress': 0.1, 'message': 'Preparing upload phase...'})}\n\n"
        try:
            result = await asyncio.to_thread(
                UploadPhaseService.execute_upload,
                project_id,
                account_id=req.account_id,
                platforms=req.platforms,
                facebook_strategy=req.facebook_strategy,
                youtube_strategy=req.youtube_strategy,
                copyright_audio_path=req.copyright_audio_path,
            )
            yield f"data: {json.dumps({'status': 'complete', 'step': 'complete', 'progress': 1.0, 'message': 'Upload phase complete', 'result': result})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'status': 'error', 'step': 'upload', 'progress': 0.0, 'error': str(exc), 'message': 'Upload phase failed'})}\n\n"

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.delete("/projects/{project_id}")
async def delete_managed_project(project_id: str):
    """Delete local project + linked Drive folder + webhook message cleanup."""
    try:
        return await asyncio.to_thread(UploadPhaseService.managed_delete, project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/projects/{project_id}/facebook-check")
async def facebook_duration_check(
    project_id: str,
    payload: FacebookCheckRequest | None = Body(default=None),
):
    """Check if the project video exceeds Facebook's 90s Reel limit.

    If it does, the sped-up version is pre-generated for preview.
    """
    req = payload or FacebookCheckRequest()
    try:
        result = await asyncio.to_thread(
            UploadPhaseService.check_facebook_duration,
            project_id,
            req.account_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/facebook-preview/{version}")
async def facebook_preview_video(
    project_id: str,
    version: Literal["original", "sped_up"],
):
    """Serve a cached video file for Facebook duration preview."""
    prep_dir = UploadPhaseService._facebook_prep_dir(project_id)
    if not prep_dir.exists():
        raise HTTPException(status_code=404, detail="No Facebook preview cached for this project")

    if version == "sped_up":
        video_path = prep_dir / "sped_up.mp4"
    else:
        # Original: find the first .mp4 that isn't the sped_up file
        video_path = None
        for f in sorted(prep_dir.iterdir()):
            if f.suffix.lower() == ".mp4" and f.name != "sped_up.mp4":
                video_path = f
                break

    if video_path is None or not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Preview version '{version}' not found")

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=f"{project_id}_{version}.mp4",
    )


@router.post("/projects/{project_id}/youtube-check")
async def youtube_duration_check(
    project_id: str,
    payload: YouTubeCheckRequest | None = Body(default=None),
):
    """Check if the project video exceeds YouTube's 180s limit."""
    req = payload or YouTubeCheckRequest()
    try:
        result = await asyncio.to_thread(
            UploadPhaseService.check_youtube_duration,
            project_id,
            req.account_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/youtube-preview/{version}")
async def youtube_preview_video(
    project_id: str,
    version: Literal["original", "sped_up"],
):
    """Serve a cached video file for YouTube duration preview."""
    prep_dir = UploadPhaseService._youtube_prep_dir(project_id)
    if not prep_dir.exists():
        raise HTTPException(status_code=404, detail="No YouTube preview cached for this project")

    if version == "sped_up":
        video_path = prep_dir / "sped_up.mp4"
    else:
        video_path = None
        for f in sorted(prep_dir.iterdir()):
            if f.suffix.lower() == ".mp4" and f.name != "sped_up.mp4":
                video_path = f
                break

    if video_path is None or not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Preview version '{version}' not found")

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=f"{project_id}_{version}.mp4",
    )


@router.post("/projects/{project_id}/copyright-check")
async def copyright_check(
    project_id: str,
    payload: CopyrightCheckRequest | None = Body(default=None),
):
    """Check if the project uses copyrighted music and list alternatives."""
    req = payload or CopyrightCheckRequest()
    try:
        result = await asyncio.to_thread(
            UploadPhaseService.check_copyright,
            project_id,
            req.account_id,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/projects/{project_id}/copyright-build-audio")
async def copyright_build_audio(
    project_id: str,
    payload: CopyrightBuildAudioRequest = Body(...),
):
    """Build replacement audio by mixing output_no_music.wav with a non-copyrighted music."""
    try:
        audio_path = await asyncio.to_thread(
            UploadPhaseService.build_copyright_audio,
            project_id,
            payload.music_key,
            payload.no_music_file_id,
        )
        return {"audio_path": str(audio_path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/copyright-audio")
async def copyright_audio(project_id: str):
    """Serve the most recently built copyright replacement audio."""
    prep_dir = UploadPhaseService._copyright_audio_dir(project_id)
    candidates = (
        sorted(prep_dir.glob("copyright_replacement*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        if prep_dir.exists()
        else []
    )
    if not candidates:
        raise HTTPException(status_code=404, detail="No copyright audio cached")
    return FileResponse(path=candidates[0], media_type="audio/wav")


@router.get("/projects/{project_id}/copyright-video")
async def copyright_video(project_id: str):
    """Download and cache the GDrive video for copyright preview."""
    prep_dir = UploadPhaseService._copyright_audio_dir(project_id)
    cached_videos = list(prep_dir.glob("*.mp4")) if prep_dir.exists() else []
    if cached_videos:
        return FileResponse(path=cached_videos[0], media_type="video/mp4")

    try:
        project = await asyncio.to_thread(ProjectService.load, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        readiness = await asyncio.to_thread(UploadPhaseService.compute_readiness, project)
        if not readiness.drive_video_id:
            raise HTTPException(status_code=404, detail="No drive video found")

        prep_dir.mkdir(parents=True, exist_ok=True)
        video_name = readiness.drive_video_name or "preview.mp4"
        video_path = prep_dir / video_name
        await asyncio.to_thread(GoogleDriveService.download_file, readiness.drive_video_id, video_path)
        return FileResponse(path=video_path, media_type="video/mp4")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
