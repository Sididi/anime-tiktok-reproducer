"""Recoverable raw-scene classification over immutable source evidence.

Mutators are called under ProjectLocks by their outer async caller. Model
inference happens outside that lock; this module only derives and saves JSON.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from ..models import MatchList, ProjectPhase, Scene, SceneList, Transcription
from ..models.raw_scene import DiarizationAnalysis, RawSceneDetectionResult
from .atomic_files import write_text_atomic
from .project_service import ProjectService
from .raw_scene_detector import RawSceneDetectorService

logger = logging.getLogger(__name__)
SOURCE_FILE = "raw_scene_source.json"
UNDO_FILE = "raw_scene_narrator_undo.json"
STATE_FILES = (
    "transcription.json", "scenes.json", "matches.json", "raw_scene_detection.json",
    "transcription_raw_backup.json", "scenes_raw_backup.json",
    "matches_raw_backup.json", "raw_scene_detection_backup.json",
)
EDITABLE_PHASES = {ProjectPhase.TRANSCRIPTION, ProjectPhase.RAW_SCENE_VALIDATION}


class NarratorSource(BaseModel):
    version: Literal[1] = 1
    analysis_id: str
    audio_sha256: str
    video_signature: tuple[str, int, int]
    transcription: Transcription
    scenes: SceneList
    matches: MatchList | None = None
    analysis: DiarizationAnalysis
    pure_mode: bool = False


class NarratorService:
    active_transcriptions: set[str] = set()

    @staticmethod
    def video_signature(path: str) -> tuple[str, int, int]:
        file = Path(path)
        stat = file.stat()
        return str(file.resolve()), stat.st_size, stat.st_mtime_ns

    @staticmethod
    def audio_hash(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    @classmethod
    def make_source(cls, project, transcription, scenes, matches, analysis) -> NarratorSource:
        return NarratorSource(
            analysis_id=uuid.uuid4().hex,
            audio_sha256=cls.audio_hash(ProjectService.get_project_dir(project.id) / "audio_16khz.wav"),
            video_signature=cls.video_signature(project.video_path),
            transcription=transcription.model_copy(deep=True),
            scenes=scenes.model_copy(deep=True),
            matches=matches.model_copy(deep=True) if matches is not None else None,
            analysis=analysis,
            pure_mode=project.library_type == "pure",
        )

    @classmethod
    def load_source(cls, project_id: str) -> NarratorSource | None:
        path = ProjectService.get_project_dir(project_id) / SOURCE_FILE
        try:
            source = NarratorSource.model_validate_json(path.read_text())
            project = ProjectService.load(project_id)
            if not project or not project.video_path:
                return None
            if source.video_signature != cls.video_signature(project.video_path):
                return None
            if source.audio_sha256 != cls.audio_hash(path.parent / "audio_16khz.wav"):
                return None
            return source
        except (OSError, ValueError):
            return None

    @staticmethod
    def read_state(project_id: str) -> dict[str, str | None]:
        directory = ProjectService.get_project_dir(project_id)
        return {name: (directory / name).read_text() if (directory / name).exists() else None
                for name in STATE_FILES}

    @classmethod
    def revision(cls, project_id: str) -> str:
        directory = ProjectService.get_project_dir(project_id)
        project = ProjectService.load(project_id)
        source = directory / SOURCE_FILE
        # Includes all working/reset files: any intervening text/scene edit
        # invalidates Undo even when it came through an older API client.
        state = [cls.read_state(project_id), project.phase if project else None,
                 source.read_text() if source.exists() else None]
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    @classmethod
    def commit(cls, project_id: str, payload: dict[str, str | None]) -> None:
        """Rollback the whole affected bundle if any individual write fails."""
        directory = ProjectService.get_project_dir(project_id)
        before = {name: (directory / name).read_text() if (directory / name).exists() else None
                  for name in payload}

        def write(values):
            for name, content in values.items():
                path = directory / name
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    write_text_atomic(path, content)
        try:
            write(payload)
        except Exception:
            try:
                write(before)
            except Exception:
                logger.exception("Narrator state rollback failed for %s", project_id)
            raise

    @classmethod
    def derive(cls, source: NarratorSource, narrator_id: str | list[str] | None) -> dict[str, str | None]:
        from .transcriber import TranscriberService

        original = source.transcription.scenes
        updated, detection = RawSceneDetectorService.classify(
            source.analysis, original, pure_mode=source.pure_mode,
            narrator_ids=[narrator_id] if isinstance(narrator_id, str) else narrator_id,
        )
        detection.analysis_id = source.analysis_id
        detection.selection_origin = "manual" if narrator_id is not None else "automatic"
        parents = detection.scene_parent_indices or [s.scene_index for s in original]
        matches = TranscriberService.remap_raw_scene_match_snapshot(
            original, updated, source.matches, parent_scene_indices=parents,
        )
        raw = {candidate.scene_index for candidate in detection.candidates}
        for scene in updated:
            scene.is_raw = scene.scene_index in raw
            if scene.is_raw:
                scene.text, scene.words = "", []
        transcription = source.transcription.model_copy(deep=True)
        transcription.scenes = updated
        scene_list = SceneList(scenes=[Scene(index=s.scene_index, start_time=s.start_time,
                                           end_time=s.end_time) for s in updated])
        if any(s.scene_index != i or s.end_time <= s.start_time for i, s in enumerate(updated)):
            raise ValueError("Invalid derived scene structure")
        if matches is not None:
            TranscriberService.validate_match_snapshot_alignment(updated, matches.matches)
        values = {
            "transcription": transcription.model_dump_json(indent=2),
            "scenes": scene_list.model_dump_json(indent=2),
            "matches": matches.model_dump_json(indent=2) if matches is not None else None,
        }
        payload = {f"{key}.json": value for key, value in values.items()}
        payload.update({f"{key}_raw_backup.json": value for key, value in values.items()})
        payload["raw_scene_detection.json"] = detection.model_dump_json(indent=2)
        payload["raw_scene_detection_backup.json"] = payload["raw_scene_detection.json"]
        return payload

    @classmethod
    def publish_source(cls, project_id: str, source: NarratorSource) -> None:
        payload = cls.derive(source, None)
        # Written first: the complete words are durable before working text
        # can be cleared. All files, including this one, roll back on failure.
        cls.commit(project_id, {SOURCE_FILE: source.model_dump_json(indent=2),
                                UNDO_FILE: None, **payload})

    @classmethod
    def ensure_idle(cls, project_id: str) -> None:
        if project_id in cls.active_transcriptions:
            raise HTTPException(409, "Transcription is running. Wait for it to finish.")

    @classmethod
    def check_edit(cls, project_id: str, expected_revision: str) -> NarratorSource:
        cls.ensure_idle(project_id)
        project = ProjectService.load(project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        if project.phase not in EDITABLE_PHASES:
            raise HTTPException(409, "Narrator correction is available during transcription and raw-scene validation.")
        if expected_revision != cls.revision(project_id):
            raise HTTPException(409, "This project changed. Reload before correcting the narrator.")
        source = cls.load_source(project_id)
        if source is None or source.analysis.error or not source.analysis.segments:
            raise HTTPException(409, "Re-run transcription to prepare recoverable voice analysis.")
        return source

    @classmethod
    def apply(cls, project_id: str, narrator_id: str | list[str] | None, expected_revision: str) -> None:
        source = cls.check_edit(project_id, expected_revision)
        if narrator_id is not None:
            selected = [narrator_id] if isinstance(narrator_id, str) else narrator_id
            if not selected or any(speaker not in {s[2] for s in source.analysis.segments} for speaker in selected):
                raise HTTPException(422, "Select at least one known narrator speaker")
        payload = cls.derive(source, narrator_id)
        previous = cls.read_state(project_id)
        old_undo = ProjectService.get_project_dir(project_id) / UNDO_FILE
        old_undo_text = old_undo.read_text() if old_undo.exists() else None
        try:
            # Make Undo durable before replacing any live output.
            cls.commit(project_id, {UNDO_FILE: json.dumps({"state": previous}), **payload})
            write_text_atomic(old_undo, json.dumps({"state": previous, "revision": cls.revision(project_id)}))
        except Exception:
            cls.commit(project_id, {**previous, UNDO_FILE: old_undo_text})
            raise

    @classmethod
    def undo(cls, project_id: str, expected_revision: str) -> None:
        cls.check_edit(project_id, expected_revision)
        undo = cls.available_undo(project_id)
        if undo is None:
            raise HTTPException(409, "Undo is no longer available because the project changed.")
        cls.commit(project_id, {**undo["state"], UNDO_FILE: None})

    @classmethod
    def available_undo(cls, project_id: str) -> dict | None:
        path = ProjectService.get_project_dir(project_id) / UNDO_FILE
        try:
            undo = json.loads(path.read_text())
            if undo.get("revision") == cls.revision(project_id) and set(undo["state"]) == set(STATE_FILES):
                return undo
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    @staticmethod
    def words_in_span(source: NarratorSource, start: float, end: float):
        return [word.model_copy(deep=True) for scene in source.transcription.scenes for word in scene.words
                if start <= (word.start if word.end - word.start > 1 else (word.start + word.end) / 2) < end]

    @classmethod
    def metadata(cls, project_id: str) -> dict:
        source = cls.load_source(project_id)
        project = ProjectService.load(project_id)
        speakers = []
        recoverable_text = {}
        if source:
            current = ProjectService.load_transcription(project_id)
            if current:
                recoverable_text = {str(scene.scene_index): " ".join(
                    word.text for word in cls.words_in_span(source, scene.start_time, scene.end_time)
                ) for scene in current.scenes if scene.is_raw}
            _, durations = RawSceneDetectorService._identify_tts_speaker(source.analysis.segments)
            for speaker_id, duration in sorted(durations.items()):
                turns = [(s, e) for s, e, k in source.analysis.segments if k == speaker_id]
                # Prefer longer turns; avoid samples containing overlapping voices.
                clean = [(s, e) for s, e in turns if not any(
                    k != speaker_id and a < e and b > s for a, b, k in source.analysis.segments)]
                samples = []
                for start, end in sorted(clean or turns, key=lambda t: t[1] - t[0], reverse=True)[:3]:
                    end = min(end, start + 8)
                    samples.append({"start_time": start, "end_time": end,
                                    "text": " ".join(w.text for w in cls.words_in_span(source, start, end))})
                speakers.append({"speaker_id": speaker_id, "duration": duration, "samples": samples})
        available = bool(source and source.analysis.segments and not source.analysis.error)
        reason = None
        if not available:
            reason = "Re-run transcription to prepare recoverable voice analysis."
        elif project_id in cls.active_transcriptions:
            reason = "Transcription is running."
        elif project and project.phase not in EDITABLE_PHASES:
            reason = "Narrator correction is available during transcription and raw-scene validation."
        return {"speakers": speakers, "recoverable_text": recoverable_text, "revision": cls.revision(project_id),
                "analysis_id": source.analysis_id if source else None,
                "correction_available": available and reason is None, "unavailable_reason": reason,
                "undo_available": reason is None and cls.available_undo(project_id) is not None,
                "warning": source.analysis.warning if source else None}
