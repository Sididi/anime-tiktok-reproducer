"""Non-destructive take cleanup, before script comparison and silence editing."""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
import unicodedata
import uuid
import wave
from pathlib import Path

from ..models.facecam import RecordingCut, RecordingEdit, RecordingSuggestion
from .atomic_files import write_text_atomic
from .facecam_cut_timing import cached_cut_timing, refine_cut_timing
from .facecam_service import FacecamService, StaleFacecamRevision

FPS = 60
RATE = 48000
EDIT_VERSION = 1


def cut_frames(cuts: list[RecordingCut], duration: float) -> list[tuple[int, int]]:
    """Union removals on the camera clock, rounding inward to preserve neighbours."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Durée de l'enregistrement invalide.")
    if len({cut.id for cut in cuts}) != len(cuts):
        raise ValueError("Identifiants de coupes dupliqués.")
    intervals = []
    for cut in cuts:
        if cut.start >= cut.end or cut.end > duration + 1e-6:
            raise ValueError("Une coupe dépasse l'enregistrement ou a des bornes inversées.")
        if cut.enabled:
            start = math.ceil(cut.start * FPS - 1e-7)
            end = math.floor(cut.end * FPS + 1e-7)
            if end <= start:
                raise ValueError("Une coupe doit durer au moins une image (1/60 s).")
            intervals.append((start, end))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    if duration - sum(b - a for a, b in merged) / FPS < .25:
        raise ValueError("Conservez au moins un passage de l'enregistrement.")
    return merged


def keep_frames(cuts: list[tuple[int, int]], duration: float) -> list[tuple[int, int]]:
    result, cursor = [], 0
    for start, end in cuts:
        if start > cursor:
            result.append((cursor, start))
        cursor = end
    last = math.ceil(duration * FPS - 1e-7)
    if cursor < last:
        result.append((cursor, last))
    return result


def waveform(root: Path, duration: float) -> list[float]:
    """Bounded-memory PCM peak extraction; the browser never decodes a whole take."""
    path = root / "cut_waveform.json"
    if not path.is_file():
        import numpy as np
        peaks = []
        with wave.open(str(root / "voice.wav"), "rb") as src:
            if src.getsampwidth() != 2 or src.getnchannels() != 1:
                raise ValueError("Format audio de la prise incompatible.")
            count = min(src.getnframes(), round(duration * src.getframerate()))
            hop = max(src.getframerate() // 100, math.ceil(count / 120000))
            remaining = count
            while remaining > 0:
                n = min(remaining, hop * 100)
                samples = np.frombuffer(src.readframes(n), dtype="<i2")
                if not len(samples):
                    break
                for offset in range(0, len(samples), hop):
                    group = samples[offset:offset + hop]
                    peaks.extend((round(float(group.min()) / 32768, 4), round(float(group.max()) / 32768, 4)))
                remaining -= len(samples)
        write_text_atomic(path, json.dumps(peaks or [0, 0]))
    return json.loads(path.read_text())


def word_text(word: dict) -> str:
    return str(word.get("word", word.get("text", "")))


def tokens(text: str) -> list[str]:
    normalized = "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", normalized)


def validate_suggestions(payload: dict, words: list[dict], duration: float, uncertain_spans=()) -> list[RecordingSuggestion]:
    """LLM confidence cannot bypass source evidence or choose arbitrary timestamps."""
    if not isinstance(payload, dict) or not isinstance(payload.get("suggestions"), list):
        raise ValueError("Réponse d'analyse des reprises invalide.")
    result = []
    for item in payload["suggestions"][:50]:
        if not isinstance(item, dict):
            continue
        indices = [item.get(k) for k in ("start_word", "end_word", "restart_word", "restart_end_word")]
        if any(type(i) is not int for i in indices):
            continue
        start, end, restart, restart_end = indices
        if not 0 <= start < end <= restart < restart_end <= len(words):
            continue
        if item.get("confidence") not in {"high", "review"}:
            continue
        removed = " ".join(word_text(w) for w in words[start:end])
        retained = " ".join(word_text(w) for w in words[restart:restart_end])
        a, b = tokens(removed), tokens(retained)
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        shared = sum(block.size for block in matcher.get_matching_blocks())
        # A one-word variation, even called "high" by the model, is not a fail.
        gap = float(words[restart]["start"]) - float(words[end - 1]["end"])
        speech_duration = float(words[end - 1]["end"]) - float(words[start]["start"])
        if len(a) < 3 or shared < 2 or speech_duration < .7 or not 0 <= gap <= 8:
            continue
        if not any(len(w) >= 4 for w in set(a) & set(b)):
            continue
        lo = max(0, float(words[start]["start"]) - .04)
        if start:
            lo = max(lo, float(words[start - 1]["end"]) + .05)
        # The end of a suggestion reaches into the pause, never into the restart.
        hi = min(duration, float(words[end]["start"]) - .04)
        if hi <= lo or hi - lo > 30:
            continue
        cues = [i for i, w in enumerate(a) if w in {"pardon", "reprends", "recommence", "desole", "merde"}]
        evidence = a[:cues[0]] if cues else a
        repeated = sum(block.size for block in difflib.SequenceMatcher(None, evidence, b, autojunk=False).get_matching_blocks())
        uncertain = any(float(x) < hi and float(y) > lo for x, y in uncertain_spans)
        high = (item["confidence"] == "high" and repeated >= 4 and repeated / max(1, len(evidence)) >= .75
                and speech_duration >= 1.2 and (gap >= .25 or bool(cues)) and not uncertain
                and lo <= float(words[start]["start"]) and hi >= float(words[end - 1]["end"]))
        suggestion = RecordingSuggestion(id=f"ai-{start}-{end}", start=lo, end=hi, origin="ai",
            enabled=high, confidence="high" if high else "review",
            reason=str(item.get("reason") or "Reprise de phrase possible, à écouter.")[:300],
            removed_text=removed, retained_text=retained)
        try:
            cut_frames([suggestion], duration)
        except ValueError:
            continue
        result.append(suggestion)
    # Never suggest deleting the later good take via an overlapping second proposal.
    accepted = []
    for item in sorted(result, key=lambda s: s.start):
        if not accepted or item.start >= accepted[-1].end:
            accepted.append(item)
    return accepted


class FacecamCutService:
    @staticmethod
    def source_preview(root: Path):
        from .facecam_media_service import ffmpeg
        path = root / "source_preview.mp4"
        if not path.is_file():
            ffmpeg(["-i", str(root / "facecam.mp4"), "-i", str(root / "voice.wav"), "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(root / "source_preview.tmp.mp4")])
            (root / "source_preview.tmp.mp4").replace(path)
        return path

    @staticmethod
    def new_edit(recording: dict, *, applied=False) -> RecordingEdit:
        return RecordingEdit(id=uuid.uuid4().hex, source_directory=recording["directory"],
            source_fingerprint=recording["fingerprint"], original_name=recording["original_name"],
            duration=recording["duration"], scale=recording.get("scale", 100), applied=applied)

    @classmethod
    def analyze(cls, project_id: str):
        from .facecam_media_service import FacecamMediaService
        state = FacecamService.load(project_id)
        if not state.recording:
            raise ValueError("Importez votre enregistrement.")
        edit = state.recording_edit or cls.new_edit(state.recording, applied=True)
        root = Path(edit.source_directory)
        # Older takes have no browser preview yet. Publish their editor only once
        # this asset is ready, so an early request cannot cache an unsupported MOV.
        if not state.recording_edit:
            cls.source_preview(root)
            waveform(root, edit.duration)
        edit.analysis_status, edit.analysis_error = "analyzing", None
        FacecamService.update(project_id, {"recording_edit": edit})
        try:
            cls.source_preview(root)
            waveform(root, edit.duration)
            timing = refine_cut_timing(root, FacecamMediaService.speech_timing(root), edit.duration)
            words = timing["words"]
            FacecamService.update(project_id, {"job": {"status": "processing", "step": "Repérage des reprises",
                "message": "Repérage des reprises · les cuts manuels sont déjà disponibles"}})
            prompt = (Path(__file__).parents[2] / "prompts/facecam/recording_cuts.md").read_text()
            payload = {"script_context": state.spoken_text(), "words": [
                {"i": i, "text": word_text(w), "start": w["start"], "end": w["end"]} for i, w in enumerate(words)]}
            response = FacecamService.llm(project_id, prompt + "\n" + json.dumps(payload, ensure_ascii=False))
            suggestions = validate_suggestions(response, words, edit.duration, timing.get("uncertain_spans", []))
            write_text_atomic(root / "cut_analysis.json", json.dumps({
                "source_fingerprint": edit.source_fingerprint,
                "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "timing_key": timing.get("source_key"), "word_count": len(words),
                "recovery_windows": timing.get("recovery_windows", []), "response": response,
                "suggestions": [s.model_dump() for s in suggestions],
            }, ensure_ascii=False))
            edit.suggestions = suggestions
            edit.analysis_status = "ready"
        except Exception:
            # Analysis is advisory. A provider/GPU failure must not trap the original take.
            import logging
            logging.getLogger(__name__).exception("Facecam cut analysis failed for %s", project_id)
            edit.analysis_status = "error"
            edit.analysis_error = "Analyse des reprises indisponible. Vous pouvez couper manuellement ou réessayer."
        with FacecamService.lock(project_id):
            current = FacecamService.load(project_id)
            if not current.recording_edit or current.recording_edit.id != edit.id:
                raise StaleFacecamRevision("L'enregistrement a changé. Rouvrez le montage.")
            return FacecamService.update(project_id, {"recording_edit": edit})

    @classmethod
    def data(cls, project_id: str, edit_id: str):
        edit = FacecamService.load(project_id).recording_edit
        if not edit or edit.id != edit_id:
            raise StaleFacecamRevision("L'enregistrement a changé. Rouvrez le montage.")
        root = Path(edit.source_directory)
        timing_path = root / "speech_timing.json"
        timing = json.loads(timing_path.read_text()) if timing_path.is_file() else {"words": []}
        words = (cached_cut_timing(root, timing, edit.duration) or timing).get("words", [])
        return {"id": edit.id, "duration": edit.duration, "peaks": waveform(root, edit.duration),
            "words": [{"text": word_text(w), "start": w["start"], "end": w["end"]} for w in words]}

    @classmethod
    def validate_request(cls, project_id: str, revision: int, edit_id: str, version: int, cuts: list[RecordingCut]):
        state = FacecamService.load(project_id)
        edit = state.recording_edit
        if state.revision != revision or state.approved_revision != revision:
            raise StaleFacecamRevision("Le script a changé. Validez sa version actuelle.")
        if not edit or edit.id != edit_id or edit.version != version:
            raise StaleFacecamRevision("Le montage ou l'enregistrement a changé. Rouvrez le montage.")
        cut_frames(cuts, edit.duration)
        return state, edit

    @classmethod
    def render(cls, edit: RecordingEdit, cuts: list[RecordingCut]) -> tuple[Path, str, float]:
        from .facecam_media_service import ffmpeg
        removals = cut_frames(cuts, edit.duration)
        source = Path(edit.source_directory)
        if not removals:
            return source, edit.source_fingerprint, edit.duration
        identity = {"version": EDIT_VERSION, "source": edit.source_fingerprint, "cuts": removals}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        root = source / "edits" / fingerprint
        root.mkdir(parents=True, exist_ok=True)
        keep = keep_frames(removals, edit.duration)
        duration = sum(b - a for a, b in keep) / FPS
        if not (root / "voice.wav").is_file():
            with wave.open(str(source / "voice.wav"), "rb") as src, wave.open(str(root / "voice.tmp.wav"), "wb") as dst:
                dst.setparams(src.getparams())
                size = src.getsampwidth() * src.getnchannels()
                for start, end in keep:
                    src.setpos(min(src.getnframes(), start * (RATE // FPS)))
                    left = (end - start) * (RATE // FPS)
                    while left:
                        count = min(left, RATE)
                        frames = src.readframes(count)
                        dst.writeframes(frames + b"\0" * (count * size - len(frames)))
                        left -= count
            (root / "voice.tmp.wav").replace(root / "voice.wav")
        if not (root / "facecam.mp4").is_file():
            # One decoder and one encoder, regardless of the number of cuts.
            select = "+".join(f"between(n,{a},{b - 1})" for a, b in keep)
            ffmpeg(["-i", str(source / "facecam.mp4"), "-an", "-vf", f"select='{select}',setpts=N/(60*TB)",
                "-c:v", "libx264", "-threads", "4", "-preset", "fast", "-crf", "18", "-r", "60",
                "-movflags", "+faststart", str(root / "facecam.tmp.mp4")])
            (root / "facecam.tmp.mp4").replace(root / "facecam.mp4")
        if not (root / "review.mp4").is_file():
            ffmpeg(["-i", str(root / "facecam.mp4"), "-i", str(root / "voice.wav"), "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(root / "review.tmp.mp4")])
            (root / "review.tmp.mp4").replace(root / "review.mp4")
        write_text_atomic(root / "source_media.json", json.dumps({"camera_duration": duration}))
        write_text_atomic(root / "recording_cuts.json", json.dumps({**identity, "keep_frames": keep}))
        return root, fingerprint, duration

    @classmethod
    def apply(cls, project_id: str, revision: int, edit_id: str, version: int, cuts: list[RecordingCut]):
        from .facecam_media_service import FacecamMediaService, compare_words
        state, edit = cls.validate_request(project_id, revision, edit_id, version, cuts)
        root, fingerprint, duration = cls.render(edit, cuts)
        FacecamService.update(project_id, {"job": {"status": "processing", "step": "Transcription du montage",
            "message": "Transcription du montage et comparaison au script"}})
        # Recognize the actual edited audio. An unchanged take can reuse its analysis.
        words = FacecamMediaService.speech_timing(root)["words"]
        spoken = " ".join(word_text(w) for w in words)
        differences = compare_words(state.spoken_text(), spoken)
        recording = {"fingerprint": fingerprint, "directory": str(root), "original_name": edit.original_name,
            "source_directory": edit.source_directory, "duration": duration, "scale": edit.scale,
            "transcript": spoken, "differences": differences,
            "reviewed_revision": None}
        with FacecamService.lock(project_id):
            cls.validate_request(project_id, revision, edit_id, version, cuts)
            updated = edit.model_copy(update={"cuts": cuts, "applied": True, "version": version + 1})
            result = FacecamService.update(project_id, {"recording": recording, "recording_edit": updated,
                "prepared": None, "processed_signature": None, "recording_review": None, "placement": None}, revision)
            FacecamService.invalidate_output(project_id)
            return result
