"""Facecam APIs. Detached jobs survive navigation; writes use document revisions."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ...models.facecam import FacecamAutoEditorSettings, FindingRange, RecordingCut, ScriptBlock
from ...services.facecam_service import FacecamService, StaleFacecamRevision
from ...services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["facecam"])
_jobs: dict[str, asyncio.Task] = {}


class DraftRequest(BaseModel):
    revision: int
    blocks: list[ScriptBlock]
    selected_hook: str = ""
    selected_opinion: str = ""
    finding_ranges: list[FindingRange] | None = None


class ActionRequest(BaseModel):
    revision: int
    hook: str = ""
    opinion: str = ""


class StoryRequest(BaseModel):
    revision: int
    story_id: str


class PreviewRequest(BaseModel):
    revision: int
    auto_editor: FacecamAutoEditorSettings


class RecordingCutsRequest(BaseModel):
    revision: int
    edit_id: str
    version: int
    cuts: list[RecordingCut] = Field(max_length=200)


class RecordingReviewRequest(BaseModel):
    revision: int
    review_id: str
    version: int


class RecordingReviewDraftRequest(RecordingReviewRequest):
    blocks: list[ScriptBlock]
    resolved_ids: list[str] = Field(default_factory=list)


def _require_idle(project_id):
    if project_id in FacecamService._processing or (project_id in _jobs and not _jobs[project_id].done()):
        raise HTTPException(409, "Attendez la fin de l'opération Facecam.")


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
        values = {"job": {"status": "error", "message": "Opération interrompue. Vous pouvez réessayer."}}
        if state.placement and state.placement.status == "running":
            values["placement"] = state.placement.model_copy(update={"status": "error", "error": "Placement interrompu. Reprenez les étapes restantes."})
        if state.recording_edit and state.recording_edit.analysis_status == "analyzing":
            values["recording_edit"] = state.recording_edit.model_copy(update={"analysis_status": "error",
                "analysis_error": "Analyse interrompue. Vous pouvez couper manuellement ou réessayer."})
        state = FacecamService.update(project_id, values)
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
        return await asyncio.to_thread(FacecamService.save_draft, project_id, request.revision, request.blocks, request.selected_hook, request.selected_opinion, request.finding_ranges)
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


@router.post("/{project_id}/facecam/generate-original")
async def generate_original(project_id: str, request: ActionRequest):
    from ...services.facecam_original_service import FacecamOriginalService

    def generate_and_check(pid, revision):
        FacecamOriginalService.generate(pid, revision)
        FacecamService.update(pid, {"job": {"status": "processing", "step": "Vérification des faits", "progress": 0.8}})
        FacecamService.fact_check(pid)

    return await _job(project_id, "Création d’un script original · recherche sur l’anime",
                      generate_and_check, request.revision)


@router.get("/{project_id}/facecam/fact-evidence/{source_id}")
async def fact_evidence(project_id: str, source_id: str):
    state = _state(project_id)
    for source in (state.fact_context or {}).get("sources", []):
        if source.get("id") == source_id:
            return PlainTextResponse(f"{source['title']}\nLangue : {source['language']}\n\n{source['text']}")
    raise HTTPException(404, "Cet extrait n'est plus disponible dans la vérification actuelle.")


@router.post("/{project_id}/facecam/approve")
async def approve(project_id: str, request: ActionRequest):
    _require_idle(project_id)
    try:
        return await asyncio.to_thread(FacecamService.approve, project_id, request.revision)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, StaleFacecamRevision) else 400, str(exc)) from exc


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


@router.get("/{project_id}/facecam/recording/review")
async def get_recording_review(project_id: str):
    from ...services.facecam_recording_review import FacecamRecordingReview
    try:
        return FacecamRecordingReview.ensure(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/{project_id}/facecam/recording/review")
async def save_recording_review(project_id: str, request: RecordingReviewDraftRequest):
    from ...services.facecam_recording_review import FacecamRecordingReview
    _require_idle(project_id)
    try:
        return FacecamRecordingReview.save(project_id, request.revision, request.review_id, request.version,
                                           request.blocks, request.resolved_ids)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/facecam/recording/review")
async def review_recording(project_id: str, request: RecordingReviewRequest):
    from ...services.facecam_recording_review import FacecamRecordingReview
    from ...services.facecam_placement_service import FacecamPlacementService
    state = _state(project_id)
    review = state.recording_review
    same_validation = (review and review.id == request.review_id and review.version == request.version
                       and review.validated_revision == state.revision and state.placement and state.recording
                       and state.recording.get("reviewed_revision") == state.revision
                       and state.placement.fingerprint == state.recording["fingerprint"])
    if same_validation and state.placement.status in {"complete", "pending", "running"}:
        job = _jobs.get(project_id)
        if state.placement.status == "complete" or (job and not job.done()):
            return state
    _require_idle(project_id)
    try:
        state = FacecamRecordingReview.validate(project_id, request.revision, request.review_id, request.version)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await _job(project_id, "Placement des scènes", FacecamPlacementService.run, state.placement.key, heavy=True)


@router.get("/{project_id}/facecam/recording")
async def recording(project_id: str):
    state = _state(project_id)
    if not state.recording:
        raise HTTPException(404, "Enregistrement manquant.")
    root = Path(state.recording["directory"])
    for name in ("review.mp4", "source_preview.mp4", state.recording["original_name"]):
        if (root / name).is_file():
            return FileResponse(root / name)
    raise HTTPException(404, "Enregistrement indisponible.")


@router.get("/{project_id}/facecam/recording/source")
async def recording_source(project_id: str, edit_id: str):
    edit = _state(project_id).recording_edit
    if not edit or edit.id != edit_id:
        raise HTTPException(409, "L'enregistrement a changé. Rouvrez le montage.")
    root = Path(edit.source_directory)
    path = root / "source_preview.mp4"
    return FileResponse(path if path.is_file() else root / edit.original_name)


@router.get("/{project_id}/facecam/recording/cuts")
async def recording_cuts(project_id: str, edit_id: str):
    from ...services.facecam_cut_service import FacecamCutService
    _state(project_id)
    try:
        return await asyncio.to_thread(FacecamCutService.data, project_id, edit_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{project_id}/facecam/recording/cuts/analyze")
async def analyze_recording_cuts(project_id: str):
    from ...services.facecam_cut_service import FacecamCutService
    state = _state(project_id)
    if not state.recording:
        raise HTTPException(400, "Importez votre enregistrement.")
    return await _job(project_id, "Analyse des reprises", FacecamCutService.analyze, heavy=True)


@router.post("/{project_id}/facecam/recording/cuts/apply")
async def apply_recording_cuts(project_id: str, request: RecordingCutsRequest):
    from ...services.facecam_cut_service import FacecamCutService
    _require_idle(project_id)
    try:
        FacecamCutService.validate_request(project_id, request.revision, request.edit_id, request.version, request.cuts)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, StaleFacecamRevision) else 400, str(exc)) from exc
    return await _job(project_id, "Application des coupes", FacecamCutService.apply,
        request.revision, request.edit_id, request.version, request.cuts, heavy=True)


@router.post("/{project_id}/facecam/preview")
async def preview(project_id: str, request: PreviewRequest | None = None):
    from ...services.facecam_media_service import FacecamMediaService
    # Save the exact settings used by the job, after checking the editor revision
    # and before returning its state to the browser. Repeated clicks cannot race.
    with FacecamService.lock(project_id):
        state = _state(project_id)
        if project_id in FacecamService._processing or (project_id in _jobs and not _jobs[project_id].done()):
            raise HTTPException(409, "Attendez la fin de l'opération Facecam.")
        if state.recording_edit and not state.recording_edit.applied:
            raise HTTPException(409, "Validez d'abord les coupes de l'enregistrement.")
        if not state.placement or state.placement.status != "complete":
            raise HTTPException(409, "Validez la relecture et attendez le placement des scènes.")
        if request:
            if request.revision != state.revision:
                raise HTTPException(409, "Le script a changé. Rechargez la version actuelle.")
            if request.auto_editor != state.auto_editor:
                FacecamService.update(project_id, {"auto_editor": request.auto_editor, "processed_signature": None}, request.revision)
                FacecamService.invalidate_output(project_id)
        FacecamService.update(project_id, {"preview_enabled": True})
    # Cached audio edits need no GPU and should not wait behind an indexation.
    return await _job(project_id, "Préparation de la voix et de l'aperçu", FacecamMediaService.preview,
                      heavy=FacecamMediaService.needs_speech_analysis(project_id))


@router.get("/{project_id}/facecam/preview")
async def preview_audio(project_id: str):
    from ...services.facecam_auto_editor import prepared_is_current
    state = _state(project_id)
    if not prepared_is_current(state) or not state.prepared.get("preview_version"):
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
        "auto_editor_defaults": FacecamAutoEditorSettings().model_dump(),
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
