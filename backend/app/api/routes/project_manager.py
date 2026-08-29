import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from ...services.executors import run_heavy
from ...services import UploadPhaseService
from ...services.google_drive_service import GoogleDriveService
from ...services.project_duplication_service import UploadRestrictionService
from ...services.project_upload_service import project_upload_queue
from ...services.project_service import ProjectService
from ...services.thumbnail_service import ThumbnailService
from ...services.upload_phase import (
    PendingProjectDeletionRequiresConfirmation,
    UploadPreflightUnavailableError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/project-manager", tags=["project-manager"])


class UploadProjectRequest(BaseModel):
    account_id: str | None = None
    platforms: list[Literal["youtube", "facebook", "instagram"]] | None = None
    facebook_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    instagram_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    youtube_strategy: Literal["auto", "cut", "sped_up", "skip"] | None = None
    copyright_audio_path: str | None = None
    thumbnail_timestamp_ms: int | None = None
    thumbnail_candidate_index: int | None = None
    # Urgent-immediate mode: publish right now instead of reserving slots.
    # immediate_platforms=None ⇒ every reserved platform; ["tiktok"] ⇒ the
    # TikTok-only toggle (other platforms keep/reuse their reservations).
    immediate: bool = False
    immediate_platforms: (
        list[Literal["youtube", "facebook", "instagram", "tiktok"]] | None
    ) = None


class FacebookCheckRequest(BaseModel):
    account_id: str | None = None


class InstagramCheckRequest(BaseModel):
    account_id: str | None = None


class YouTubeCheckRequest(BaseModel):
    account_id: str | None = None


class CopyrightCheckRequest(BaseModel):
    account_id: str | None = None


class CopyrightBuildAudioRequest(BaseModel):
    music_key: str | None = None
    no_music_file_id: str | None = None


@router.get("/projects")
async def list_project_manager_projects():
    """List locally stored projects enriched with Drive/upload status."""
    try:
        rows = await run_heavy(UploadPhaseService.list_manager_rows)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"projects": rows}


@router.get("/projects/{project_id}/row")
async def get_project_manager_row(project_id: str):
    """Refresh a single manager row, skipping the all-projects Drive sweep."""
    try:
        # Light pool: one row is a project read (no Drive sweep), so it must
        # not queue behind heavy work while an upload/export is running.
        row = await asyncio.to_thread(UploadPhaseService.get_manager_row, project_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"project": row}


@router.post("/projects/{project_id}/upload")
async def run_upload_phase(
    project_id: str,
    payload: UploadProjectRequest | None = Body(default=None),
):
    """Queue a project upload and return the persisted background job."""
    req = payload or UploadProjectRequest()
    try:
        job = await project_upload_queue.enqueue_upload(
            project_id=project_id,
            account_id=req.account_id,
            platforms=req.platforms,
            facebook_strategy=req.facebook_strategy,
            instagram_strategy=req.instagram_strategy,
            youtube_strategy=req.youtube_strategy,
            copyright_audio_path=req.copyright_audio_path,
            thumbnail_timestamp_ms=req.thumbnail_timestamp_ms,
            thumbnail_candidate_index=req.thumbnail_candidate_index,
            immediate=req.immediate,
            immediate_platforms=req.immediate_platforms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return job.model_dump(mode="json")


@router.get("/projects/{project_id}/upload-restrictions")
async def get_upload_restrictions(project_id: str):
    """Restrictions from linked duplicated projects (blocked accounts/dates)."""
    project = await ProjectService.aload(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await asyncio.to_thread(UploadRestrictionService.describe, project)


@router.get("/upload-jobs")
async def list_project_upload_jobs():
    """List persisted Project Manager upload jobs.

    Live updates flow over the shared ``/api/events/stream`` (topic
    ``upload_jobs``); this REST snapshot remains for tooling and tests.
    """
    return {
        "jobs": [job.model_dump(mode="json") for job in project_upload_queue.list_jobs()],
    }


@router.delete("/projects/{project_id}")
async def delete_managed_project(project_id: str, confirmed: bool = False):
    """Archive and delete a project, requiring confirmation while scheduled."""
    try:
        return await asyncio.to_thread(
            UploadPhaseService.managed_delete,
            project_id,
            confirmed=confirmed,
        )
    except PendingProjectDeletionRequiresConfirmation as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scheduled_project_confirmation_required",
                "message": str(exc),
                "project_id": exc.project_id,
                "platforms": exc.platforms,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/projects/{project_id}/facebook-check")
async def facebook_duration_check(
    project_id: str,
    payload: FacebookCheckRequest | None = Body(default=None),
):
    """Check if the video exceeds the selected account's verified Facebook limit.

    Uses Drive metadata / local probe only — no video download. When a
    choice is needed, the shared preview cache is warmed in the background.
    """
    req = payload or FacebookCheckRequest()
    try:
        result = await asyncio.to_thread(
            UploadPhaseService.check_facebook_duration,
            project_id,
            req.account_id,
        )
        return result
    except UploadPreflightUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/projects/{project_id}/instagram-check")
async def instagram_duration_check(
    project_id: str,
    payload: InstagramCheckRequest | None = Body(default=None),
):
    """Check the selected account's hard Instagram limit and discovery warning."""
    req = payload or InstagramCheckRequest()
    try:
        return await asyncio.to_thread(
            UploadPhaseService.check_instagram_duration,
            project_id,
            req.account_id,
        )
    except UploadPreflightUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/upload-source-status")
async def upload_source_status(project_id: str):
    """State of the shared final-video preview cache; warms it when missing."""
    try:
        return await asyncio.to_thread(
            UploadPhaseService.start_source_video_download, project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/projects/{project_id}/upload-source-preview")
async def upload_source_preview(project_id: str):
    """Serve the cached final video used by the duration-choice modals."""
    video_path = UploadPhaseService.cached_source_video(project_id)
    if video_path is not None and video_path.exists():
        return FileResponse(
            path=video_path,
            media_type="video/mp4",
            headers={"Cache-Control": "private, no-cache"},
        )
    status = UploadPhaseService.source_video_status(project_id)
    if status["state"] == "in_progress":
        return JSONResponse(status_code=202, content=status)
    raise HTTPException(status_code=404, detail=status.get("detail") or "Preview not cached")


@router.get("/projects/{project_id}/thumbnail-candidates")
async def thumbnail_candidates(project_id: str):
    """Progressive thumbnail candidates; warms the output cache for fallbacks."""
    # THUMBNAIL FEATURE DISABLED (2026-08-16, owner request): no candidate
    # extraction or preview preparation runs. To re-enable, delete the return
    # below and uncomment the original body.
    return {"state": "error", "detail": "Fonctionnalité miniatures désactivée"}
    # try:
    #     await asyncio.to_thread(
    #         UploadPhaseService.start_source_video_download, project_id
    #     )
    # except ValueError as exc:
    #     raise HTTPException(status_code=404, detail=str(exc)) from exc
    # except Exception:
    #     # cache warming is best-effort here; clean tiles don't need it
    #     logger.warning("Source warm failed for %s", project_id, exc_info=True)
    # return await asyncio.to_thread(ThumbnailService.start_candidates_build, project_id)


@router.get("/projects/{project_id}/thumbnail-frame/{index}")
async def thumbnail_frame(project_id: str, index: int, v: str | None = None):
    """Serve a cached thumbnail candidate JPEG."""
    path = ThumbnailService.cached_frame_path(project_id, index)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail frame not cached")
    return FileResponse(
        path=path,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
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
    except UploadPreflightUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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
    """Serve the final video for copyright preview: the cached Drive download
    first, then a fresh Drive download."""
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
        await run_heavy(GoogleDriveService.download_file, readiness.drive_video_id, video_path)
        return FileResponse(path=video_path, media_type="video/mp4")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

