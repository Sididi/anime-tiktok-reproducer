"""Facecam APIs. Detached jobs survive navigation; writes use document revisions."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from ...models.facecam import ScriptBlock
from ...services.facecam_service import FacecamService, StaleFacecamRevision
from ...services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["facecam"])
_jobs: dict[str, asyncio.Task] = {}


class DraftRequest(BaseModel):
    revision: int
    blocks: list[ScriptBlock]
    selected_hook: str = ""
    selected_opinion: str = ""


class ActionRequest(BaseModel):
    revision: int
    hook: str = ""
    opinion: str = ""


class StoryRequest(BaseModel):
    revision: int
    story_id: str


def _state(project_id):
    try:
        return FacecamService.load(project_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


async def _job(project_id, label, function, *args, heavy=False):
    _state(project_id)
    if project_id in FacecamService._processing:
        raise HTTPException(409, "Attendez la fin du traitement Facecam.")
    if project_id in _jobs and not _jobs[project_id].done():
        raise HTTPException(409, "Une opération Facecam est déjà en cours.")
    FacecamService.update(project_id, {"job": {"status": "processing", "step": label, "progress": 0, "message": label}})
    async def run():
        try:
            if heavy:
                from ...services.indexation_queue import indexation_queue
                async with indexation_queue.heavy_slot("facecam"):
                    await asyncio.to_thread(function, project_id, *args)
            else:
                await asyncio.to_thread(function, project_id, *args)
            FacecamService.update(project_id, {"job": {"status": "complete", "step": label, "progress": 1, "message": "Terminé"}})
        except Exception as exc:
            FacecamService.update(project_id, {"job": {"status": "error", "step": label, "progress": 0, "error": str(exc), "message": str(exc)}})
    _jobs[project_id] = asyncio.create_task(run(), name=f"facecam:{project_id}:{label}")
    return _state(project_id)


@router.post("/{project_id}/facecam")
async def create_facecam(project_id: str):
    from .projects import ProjectResponse
    try:
        project = await asyncio.to_thread(FacecamService.create, project_id)
        return ProjectResponse.from_project(project)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{project_id}/facecam")
async def get_facecam(project_id: str):
    state = _state(project_id)
    if state.job and state.job.get("status") == "processing" and (project_id not in _jobs or _jobs[project_id].done()):
        state = FacecamService.update(project_id, {"job": {"status": "error", "message": "Opération interrompue. Vous pouvez réessayer."}})
    return state


@router.get("/{project_id}/facecam/events")
async def events(project_id: str):
    _state(project_id)
    async def stream():
        while True:
            state = await get_facecam(project_id)
            yield "data: " + state.model_dump_json() + "\n\n"
            if not state.job or state.job.get("status") != "processing":
                break
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")


@router.put("/{project_id}/facecam/draft")
async def save_draft(project_id: str, request: DraftRequest):
    try:
        return await asyncio.to_thread(FacecamService.save_draft, project_id, request.revision, request.blocks, request.selected_hook, request.selected_opinion)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, StaleFacecamRevision) else 400, str(exc)) from exc


@router.post("/{project_id}/facecam/candidates/{kind}")
async def candidates(project_id: str, kind: Literal["hooks", "opinions"]):
    return await _job(project_id, "Génération des propositions", FacecamService.candidates, kind)


@router.post("/{project_id}/facecam/stories/analyze")
async def analyze_stories(project_id: str, request: ActionRequest):
    return await _job(project_id, "Détection des histoires", FacecamService.analyze_stories, request.revision)


@router.put("/{project_id}/facecam/story")
async def select_story(project_id: str, request: StoryRequest):
    if project_id in _jobs and not _jobs[project_id].done():
        raise HTTPException(409, "Attendez la fin de l'opération Facecam.")
    try:
        return await asyncio.to_thread(FacecamService.select_story, project_id, request.revision, request.story_id)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, StaleFacecamRevision) else 400, str(exc)) from exc


@router.post("/{project_id}/facecam/generate")
async def generate(project_id: str, request: ActionRequest):
    def generate_and_check(pid, hook, opinion, revision):
        FacecamService.generate(pid, hook, opinion, revision)
        FacecamService.update(pid, {"job": {"status": "processing", "step": "Vérification des faits", "progress": 0.6}})
        FacecamService.fact_check(pid)
    return await _job(project_id, "Rédaction", generate_and_check, request.hook, request.opinion, request.revision)


@router.post("/{project_id}/facecam/fact-check")
async def fact_check(project_id: str):
    return await _job(project_id, "Vérification des faits", FacecamService.fact_check)


@router.get("/{project_id}/facecam/fact-evidence/{source_id}")
async def fact_evidence(project_id: str, source_id: str):
    state = _state(project_id)
    for source in (state.fact_context or {}).get("sources", []):
        if source.get("id") == source_id:
            return PlainTextResponse(f"{source['title']}\nLangue : {source['language']}\n\n{source['text']}")
    raise HTTPException(404, "Cet extrait n'est plus disponible dans la vérification actuelle.")


@router.post("/{project_id}/facecam/approve")
async def approve(project_id: str, request: ActionRequest):
    return await _job(project_id, "Répartition sur les scènes", FacecamService.approve, request.revision)


@router.post("/{project_id}/facecam/recording")
async def upload_recording(project_id: str, file: UploadFile = File(...)):
    _state(project_id)
    if project_id in _jobs and not _jobs[project_id].done():
        raise HTTPException(409, "Attendez la fin de l'opération en cours.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov"}:
        raise HTTPException(400, "Importez une vidéo MP4 ou MOV.")
    root = FacecamService.directory(project_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"upload-{uuid.uuid4().hex}{suffix}"
    import aiofiles
    try:
        async with aiofiles.open(path, "wb") as stream:
            while chunk := await file.read(1024 * 1024):
                await stream.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    from ...services.facecam_media_service import FacecamMediaService
    def ingest_and_cleanup(pid, upload):
        try:
            return FacecamMediaService.ingest(pid, upload)
        finally:
            upload.unlink(missing_ok=True)
    try:
        return await _job(project_id, "Analyse de l'enregistrement", ingest_and_cleanup, path, heavy=True)
    except Exception:
        path.unlink(missing_ok=True)
        raise


@router.post("/{project_id}/facecam/recording/review")
async def review_recording(project_id: str, request: ActionRequest):
    from ...services.facecam_media_service import FacecamMediaService
    try:
        return FacecamMediaService.review(project_id, request.revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{project_id}/facecam/recording")
async def recording(project_id: str):
    state = _state(project_id)
    if not state.recording:
        raise HTTPException(404, "Enregistrement manquant.")
    root = Path(state.recording["directory"])
    # Original recording includes its voice for the user's recording player.
    return FileResponse(root / state.recording["original_name"])


@router.post("/{project_id}/facecam/preview")
async def preview(project_id: str):
    from ...services.facecam_media_service import FacecamMediaService
    return await _job(project_id, "Préparation de la voix et de l'aperçu", FacecamMediaService.preview, heavy=True)


@router.get("/{project_id}/facecam/preview")
async def preview_audio(project_id: str):
    state = _state(project_id)
    if not state.prepared:
        raise HTTPException(404, "Préparez l'aperçu.")
    path = Path(state.prepared["directory"]) / "preview.wav"
    if not path.exists():
        raise HTTPException(404, "Préparez l'aperçu.")
    return FileResponse(path, media_type="audio/wav")


@router.get("/{project_id}/facecam/covers")
async def search_covers(project_id: str):
    from ...services.facecam_cover_service import FacecamCoverService
    _state(project_id)
    try:
        return await asyncio.to_thread(FacecamCoverService.search, project_id)
    except Exception as exc:
        raise HTTPException(503, "Recherche AniList indisponible. Vous pouvez importer une couverture manuellement.") from exc


@router.post("/{project_id}/facecam/covers/{media_id}")
async def select_cover(project_id: str, media_id: int):
    from ...services.facecam_cover_service import FacecamCoverService
    return await _job(project_id, "Téléchargement de la couverture", FacecamCoverService.select, media_id)


@router.post("/{project_id}/facecam/cover")
async def upload_cover(project_id: str, file: UploadFile = File(...)):
    from ...services.facecam_cover_service import FacecamCoverService
    _state(project_id)
    content = await file.read(20 * 1024 * 1024 + 1)
    await file.close()
    try:
        await asyncio.to_thread(FacecamCoverService.store, project_id, content, {"manual": True})
        return _state(project_id)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{project_id}/facecam/cover")
async def cover(project_id: str):
    state = _state(project_id)
    if not state.cover:
        raise HTTPException(404, "Couverture manquante.")
    return FileResponse(FacecamService.directory(project_id) / "anime_reveal.png")


@router.post("/{project_id}/facecam/metadata/generate")
async def generate_metadata(project_id: str):
    def build(pid):
        from ...services.metadata import MetadataService
        state, project = _state(pid), FacecamService.project(pid)
        prompt = MetadataService.build_prompt(project.anime_name or "Anime", state.spoken_text(), "fr")
        result = FacecamService.llm(pid, prompt)
        if "title_candidates" in result:
            payload = MetadataService.resolve_candidate_payload(result, selected_title=result["title_candidates"][0])
        else:
            payload = MetadataService.validate_payload(result)
        with FacecamService.lock(pid):
            if FacecamService.load(pid).revision != state.revision:
                raise StaleFacecamRevision("Le script a changé; régénérez les métadonnées.")
            MetadataService.save(pid, payload)
    return await _job(project_id, "Métadonnées de publication", build)


@router.put("/{project_id}/facecam/metadata")
async def save_metadata(project_id: str, payload: dict[str, Any]):
    from ...services.metadata import MetadataService
    _state(project_id)
    try:
        MetadataService.save(project_id, MetadataService.validate_payload(payload))
        return {"saved": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{project_id}/facecam/delivery")
async def delivery(project_id: str):
    from ...services.facecam_delivery_service import FacecamDeliveryService
    try:
        return await asyncio.to_thread(FacecamDeliveryService.sync, project_id)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/{project_id}/facecam/delivery/reopen")
async def reopen(project_id: str):
    from ...services.facecam_delivery_service import FacecamDeliveryService
    try:
        return await asyncio.to_thread(FacecamDeliveryService.reopen, project_id)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@router.put("/{project_id}/facecam/selection")
async def save_selection(project_id: str, request: ActionRequest):
    try:
        return FacecamService.update(project_id, {"selected_hook": request.hook, "selected_opinion": request.opinion}, request.revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{project_id}/facecam/settings")
async def settings(project_id: str):
    from ...services.llm_config_service import LLMConfigService
    from ...services.music_config_service import MusicConfigService
    project = FacecamService.project(project_id)
    llm = LLMConfigService.get_config()
    music = MusicConfigService.get_config()
    return {"llm_preset": project.resolved_llm_preset_key(), "music_key": project.resolved_music_key(),
        "models": [{"key": k, "label": v.label} for k, v in llm.presets.items()],
        "music": [{"key": k, "label": v.display_name} for k, v in music.musics.items()]}


class SettingsRequest(BaseModel):
    llm_preset: str | None = None
    music_key: str | None = None


@router.put('/{project_id}/facecam/settings')
async def update_settings(project_id: str, request: SettingsRequest):
    from ...services.llm_config_service import LLMConfigService
    from ...services.music_config_service import MusicConfigService
    _state(project_id)
    if project_id in FacecamService._processing or (project_id in _jobs and not _jobs[project_id].done()):
        raise HTTPException(409, 'Attendez la fin de l’opération en cours.')
    try:
        if request.llm_preset is not None:
            LLMConfigService.get_preset(request.llm_preset)
        if request.music_key is not None:
            MusicConfigService.get_music(request.music_key)
        async with ProjectService.edit_lock(project_id):
            project = await ProjectService.aload(project_id)
            for key, value in request.model_dump(exclude_none=True).items():
                setattr(project, key, value)
            await ProjectService.asave(project)
        return await settings(project_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
