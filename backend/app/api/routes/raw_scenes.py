from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from ...models import MatchList, ProjectPhase, Scene, SceneList, SceneMatch, SceneTranscription, Transcription
from ...models.raw_scene import RawSceneDetectionResult
from ...services import ProjectService
from ...services.drive_prewarm_service import DrivePrewarmService
from ...services.project_locks import project_edit_locked
from ...services.narrator_service import NarratorService
from ...services.atomic_files import write_text_atomic

router = APIRouter(prefix="/projects/{project_id}/raw-scenes", tags=["raw-scenes"])


class SceneValidation(BaseModel):
    scene_index: int
    is_raw: bool
    text: str | None = None


class ValidateRequest(BaseModel):
    validations: list[SceneValidation]
    expected_revision: str | None = None


class NarratorRequest(BaseModel):
    speaker_id: str | None = None
    speaker_ids: list[str] | None = Field(default=None, min_length=1)
    expected_revision: str

    @model_validator(mode="after")
    def one_selection(self):
        if {"speaker_id", "speaker_ids"} <= self.model_fields_set:
            raise ValueError("Supply speaker_ids or the legacy speaker_id, not both")
        return self


class UndoNarratorRequest(BaseModel):
    expected_revision: str


@router.get("")
@project_edit_locked
async def get_raw_scenes(project_id: str):
    """Get raw scene detection result and current transcription."""
    project = await ProjectService.aload(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    detection_file = ProjectService.get_project_dir(project_id) / "raw_scene_detection.json"
    if not detection_file.exists():
        return {"detection": None, "transcription": None, "narrator": NarratorService.metadata(project_id)}

    detection = RawSceneDetectionResult.model_validate_json(detection_file.read_text())
    transcription = await ProjectService.aload_transcription(project_id)

    return {
        "detection": detection.model_dump(),
        "transcription": transcription.model_dump() if transcription else None,
        "narrator": NarratorService.metadata(project_id),
    }


def _narrator_response(project_id: str) -> dict:
    path = ProjectService.get_project_dir(project_id) / "raw_scene_detection.json"
    detection = RawSceneDetectionResult.model_validate_json(path.read_text())
    transcription = ProjectService.load_transcription(project_id)
    return {"detection": detection.model_dump(), "transcription": transcription.model_dump(),
            "narrator": NarratorService.metadata(project_id)}


@router.put("/narrator")
@project_edit_locked
async def change_narrator(project_id: str, request: NarratorRequest):
    selection = request.speaker_ids if "speaker_ids" in request.model_fields_set else request.speaker_id
    NarratorService.apply(project_id, selection, request.expected_revision)
    return _narrator_response(project_id)


@router.post("/narrator/undo")
@project_edit_locked
async def undo_narrator(project_id: str, request: UndoNarratorRequest):
    NarratorService.undo(project_id, request.expected_revision)
    return _narrator_response(project_id)


@router.post("/validate")
@project_edit_locked
async def validate_raw_scenes(project_id: str, request: ValidateRequest):
    """Validate or invalidate detected raw scenes.

    For invalidated scenes (is_raw=False): merge back into adjacent TTS scene.
    For validated scenes: keep as raw.
    """
    NarratorService.ensure_idle(project_id)
    if request.expected_revision is not None and request.expected_revision != NarratorService.revision(project_id):
        raise HTTPException(409, "This project changed. Reload before validating scenes.")
    project = await ProjectService.aload(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    transcription = await ProjectService.aload_transcription(project_id)
    if not transcription:
        raise HTTPException(status_code=404, detail="No transcription found")
    original_scenes = [scene.model_copy(deep=True) for scene in transcription.scenes]
    match_list = await ProjectService.aload_matches(project_id)
    source = NarratorService.load_source(project_id)

    # Build lookup of validations
    validation_map = {v.scene_index: v for v in request.validations}
    invalidated_raw_indices = {
        v.scene_index
        for v in request.validations
        if not v.is_raw
    }

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
                scene.words = []
            elif source is not None:
                scene.words = NarratorService.words_in_span(source, scene.start_time, scene.end_time)
                scene.text = " ".join(word.text for word in scene.words)

    # Merge invalidated raw scenes into adjacent TTS scenes
    # Recoverable projects keep their scene boundaries, including explicit
    # empty text edits and intervals with an unresolved ASR gap.
    if source is None:
        transcription.scenes = _merge_invalidated_scenes(transcription.scenes)

    if match_list and _scene_structure_changed(original_scenes, transcription.scenes):
        match_list.matches = _remap_matches_after_scene_structure_change(
            before_scenes=original_scenes,
            after_scenes=transcription.scenes,
            matches=match_list.matches,
        )
        await ProjectService.asave_matches(project_id, match_list)

    _enforce_raw_scene_invariants(transcription.scenes)

    await ProjectService.asave_transcription(project_id, transcription)
    _persist_detection_after_validation(
        project_id=project_id,
        invalidated_raw_indices=invalidated_raw_indices,
        updated_scenes=transcription.scenes,
    )

    # Sync scenes.json with current scene structure
    await ProjectService.asave_scenes(project_id, SceneList(scenes=[
        Scene(index=s.scene_index, start_time=s.start_time, end_time=s.end_time)
        for s in transcription.scenes
    ]))

    return {"status": "ok", "transcription": transcription.model_dump()}


@router.post("/confirm")
@project_edit_locked
async def confirm_raw_scenes(project_id: str):
    """Finalize raw scene validation and advance to script phase."""
    NarratorService.ensure_idle(project_id)
    project = await ProjectService.aload(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Defensive cleanup for historical inconsistencies where raw scenes
    # may have been assigned text via a stale index update.
    transcription = await ProjectService.aload_transcription(project_id)
    if transcription:
        _enforce_raw_scene_invariants(transcription.scenes)
        await ProjectService.asave_transcription(project_id, transcription)

    project.phase = ProjectPhase.SCRIPT_RESTRUCTURE
    await ProjectService.asave(project)
    DrivePrewarmService.schedule(project_id, reason="raw-scenes-confirm")

    return {"status": "ok"}


@router.post("/reset")
@project_edit_locked
async def reset_raw_scenes(project_id: str):
    """Reset raw scene validation to the post-detection state (before user edits)."""
    NarratorService.ensure_idle(project_id)
    project = await ProjectService.aload(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = ProjectService.get_project_dir(project_id)

    if (project_dir / "raw_scene_detection_backup.json").exists():
        payload = {}
        for key in ("transcription", "scenes", "matches"):
            backup = project_dir / f"{key}_raw_backup.json"
            payload[f"{key}.json"] = backup.read_text() if backup.exists() else None
        payload["raw_scene_detection.json"] = (project_dir / "raw_scene_detection_backup.json").read_text()
        payload["raw_scene_narrator_undo.json"] = None
        NarratorService.commit(project_id, payload)
        project.phase = ProjectPhase.RAW_SCENE_VALIDATION
        await ProjectService.asave(project)
        return {"status": "ok"}

    # Restore transcription from backup
    backup_trans = project_dir / "transcription_raw_backup.json"
    if not backup_trans.exists():
        raise HTTPException(status_code=404, detail="No raw scene backup found — re-run transcription")

    transcription = Transcription.model_validate_json(backup_trans.read_text())
    await ProjectService.asave_transcription(project_id, transcription)

    # Restore matches from backup if available
    backup_matches = project_dir / "matches_raw_backup.json"
    if backup_matches.exists():
        match_list = MatchList.model_validate_json(backup_matches.read_text())
        await ProjectService.asave_matches(project_id, match_list)

    # Restore scenes.json from backup if available
    backup_scenes = project_dir / "scenes_raw_backup.json"
    if backup_scenes.exists():
        scene_list = SceneList.model_validate_json(backup_scenes.read_text())
        await ProjectService.asave_scenes(project_id, scene_list)

    # Set phase back to raw scene validation
    project.phase = ProjectPhase.RAW_SCENE_VALIDATION
    await ProjectService.asave(project)

    return {"status": "ok"}


def _persist_detection_after_validation(
    *,
    project_id: str,
    invalidated_raw_indices: set[int],
    updated_scenes: list[SceneTranscription],
) -> None:
    """Persist manual RAW->TTS choices by filtering stale raw candidates.

    This intentionally does not modify merge/remap behavior. It only updates
    persisted detection metadata so reloading /raw-scenes reflects user choices.
    """
    detection_file = ProjectService.get_project_dir(project_id) / "raw_scene_detection.json"
    if not detection_file.exists():
        return

    detection = RawSceneDetectionResult.model_validate_json(detection_file.read_text())

    if invalidated_raw_indices:
        detection.candidates = [
            candidate
            for candidate in detection.candidates
            if candidate.scene_index not in invalidated_raw_indices
        ]

    # Keep candidate indices aligned with current transcription after merge/reindex.
    for candidate in detection.candidates:
        for scene in updated_scenes:
            if (
                abs(scene.start_time - candidate.start_time) < 0.01
                and abs(scene.end_time - candidate.end_time) < 0.01
            ):
                candidate.scene_index = scene.scene_index
                break

    detection.has_raw_scenes = len(detection.candidates) > 0
    write_text_atomic(detection_file, detection.model_dump_json(indent=2))


def _enforce_raw_scene_invariants(scenes: list[SceneTranscription]) -> None:
    """Ensure raw scenes never carry text/words payloads."""
    for scene in scenes:
        if scene.is_raw:
            scene.text = ""
            scene.words = []


def _merge_invalidated_scenes(scenes: list[SceneTranscription]) -> list[SceneTranscription]:
    """Merge invalidated (non-raw) scenes that were formerly raw back into adjacent TTS scenes.

    A formerly-raw scene that was invalidated has is_raw=False but empty words.
    We merge it into the previous TTS scene (or next if it's the first scene).
    """
    if not scenes:
        return scenes

    # Merge only effectively-empty non-raw scenes.
    # If a user provided text while marking a scene as TTS, keep that scene so the
    # manual transcription remains attached to its own scene index.
    result: list[SceneTranscription] = []
    for scene in scenes:
        if not scene.is_raw and not scene.words and not scene.text.strip():
            # Empty non-raw scene — merge into previous
            if result and not result[-1].is_raw:
                prev = result[-1]
                prev.end_time = scene.end_time
                continue
            # Keep leading spans and spans after raw audio; never silently
            # discard them or extend a raw neighbour over rejected speech.

        result.append(scene)

    # Re-index
    for idx, s in enumerate(result):
        s.scene_index = idx

    return result


def _scene_structure_changed(
    before_scenes: list[SceneTranscription],
    after_scenes: list[SceneTranscription],
) -> bool:
    if len(before_scenes) != len(after_scenes):
        return True
    for before, after in zip(before_scenes, after_scenes):
        if before.scene_index != after.scene_index:
            return True
        if abs(before.start_time - after.start_time) > 1e-6:
            return True
        if abs(before.end_time - after.end_time) > 1e-6:
            return True
    return False


def _scene_overlap(a: SceneTranscription, b: SceneTranscription) -> float:
    return max(0.0, min(a.end_time, b.end_time) - max(a.start_time, b.start_time))


def _remap_matches_after_scene_structure_change(
    *,
    before_scenes: list[SceneTranscription],
    after_scenes: list[SceneTranscription],
    matches: list[SceneMatch],
) -> list[SceneMatch]:
    matches_by_index = {match.scene_index: match for match in matches}
    remapped_matches = []

    for after_scene in after_scenes:
        best_scene = None
        best_key = None
        for before_scene in before_scenes:
            match = matches_by_index.get(before_scene.scene_index)
            if match is None:
                continue
            overlap = _scene_overlap(before_scene, after_scene)
            if overlap <= 0:
                continue

            key = (0 if before_scene.is_raw else 1, overlap)
            if best_key is None or key > best_key:
                best_key = key
                best_scene = before_scene

        selected_match = None
        if best_scene is not None:
            selected_match = matches_by_index.get(best_scene.scene_index)
        elif after_scene.scene_index in matches_by_index:
            selected_match = matches_by_index.get(after_scene.scene_index)

        if selected_match is None:
            continue

        remapped_matches.append(
            selected_match.model_copy(update={"scene_index": after_scene.scene_index})
        )

    return remapped_matches
