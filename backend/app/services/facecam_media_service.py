"""Real-voice preparation with one frame-quantized picture/audio edit map."""
from __future__ import annotations

import difflib
import asyncio
import hashlib
import json
import math
import re
import shutil
import subprocess
import wave
import uuid
from pathlib import Path

from ..models import Transcription
from .facecam_service import FacecamService
from .atomic_files import write_text_atomic
from .facecam_auto_editor import keep_intervals, prepared_is_current, protected_spans, settings_signature, verify_speech_retained

FPS = 60
RATE = 48000


def probe(path: Path) -> dict:
    from ..utils.media_binaries import get_ffprobe_binary, get_media_subprocess_env
    result = subprocess.run([get_ffprobe_binary(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, check=True, timeout=60, env=get_media_subprocess_env([get_ffprobe_binary()]))
    return json.loads(result.stdout)


def ffmpeg(args: list[str], timeout=1800):
    from ..utils.media_binaries import get_ffmpeg_binary, get_media_subprocess_env
    result = subprocess.run([get_ffmpeg_binary(), "-nostdin", "-y", *args], capture_output=True, timeout=timeout, env=get_media_subprocess_env([get_ffmpeg_binary()]))
    if result.returncode:
        raise RuntimeError("Préparation média impossible : " + result.stderr.decode(errors="replace")[-1200:])
    return result


def compare_words(expected: str, actual: str) -> list[dict]:
    def words(text):
        return re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)
    a, b = words(expected), words(actual)
    return [{"kind": tag, "expected": " ".join(a[i:j]), "spoken": " ".join(b[k:l])}
            for tag, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if tag != "equal"]


def camera_segments(keep: list[tuple[int, int]], playback: list) -> list[dict]:
    """Compose original→clean voice intervals with clean voice→final playback."""
    mapping, clean_cursor = [], 0.0
    for start, end in keep:
        length = (end - start) / FPS
        mapping.append((clean_cursor, clean_cursor + length, start / FPS))
        clean_cursor += length
    result, timeline = [], 0.0
    for segment in playback:
        if segment.kind == "audio":
            for begin, end, original in mapping:
                lo, hi = max(begin, segment.source_start), min(end, segment.source_end)
                if hi > lo:
                    t0 = round((timeline + lo - segment.source_start) * FPS)
                    t1 = round((timeline + hi - segment.source_start) * FPS)
                    if t1 > t0:
                        result.append({"start_frame": t0, "end_frame": t1,
                            "source_frame": round((original + lo - begin) * FPS)})
        timeline += segment.duration
    return result


class FacecamMediaService:
    @classmethod
    def ingest(cls, project_id: str, upload: Path):
        state = FacecamService.load(project_id)
        if state.approved_revision != state.revision:
            raise ValueError("Validez le script avant d'importer l'enregistrement.")
        info = probe(upload)
        streams = info.get("streams", [])
        if not any(s.get("codec_type") == "video" for s in streams) or not any(s.get("codec_type") == "audio" for s in streams):
            raise ValueError("La vidéo doit contenir une image et une piste audio.")
        duration = float(info.get("format", {}).get("duration") or 0)
        if duration <= 0:
            raise ValueError("Durée de l'enregistrement invalide.")
        with upload.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        root = FacecamService.directory(project_id) / "recordings" / digest
        root.mkdir(parents=True, exist_ok=True)
        original = root / ("original" + upload.suffix.lower())
        shutil.move(str(upload), original)
        # Silent CFR master is the only camera asset imported by JSX; audio lives exclusively on A2.
        # Both streams use the input's common zero. Pad delayed streams instead of
        # resetting picture/audio independently (phone recordings may have offsets).
        ffmpeg(["-i", str(original), "-map", "0:v:0", "-an", "-vf", "fps=60:start_time=0,format=yuv420p",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-movflags", "+faststart", str(root / "facecam.mp4")])
        ffmpeg(["-i", str(original), "-map", "0:a:0", "-vn", "-af", "aresample=48000:async=1:first_pts=0", "-ar", str(RATE), "-ac", "1", str(root / "voice.wav")])
        master_info = probe(root / "facecam.mp4")["streams"][0]
        scale = max(1080 / master_info["width"], 1920 / master_info["height"]) * 100
        from .transcriber import TranscriberService
        with TranscriberService.model_session():
            words, _ = TranscriberService._transcribe_sync(root / "voice.wav", "fr", "large-v3")
            cls.save_speech_timing(root, words, TranscriberService._last_unrecovered_gaps)
        spoken = " ".join(str(w.get("word", w.get("text", ""))) for w in words)
        differences = compare_words(state.spoken_text(), spoken)
        result = FacecamService.update(project_id, {"prepared": None, "processed_signature": None, "recording": {
            "fingerprint": digest, "directory": str(root), "original_name": original.name,
            "duration": duration, "scale": scale, "transcript": spoken, "differences": differences,
            "reviewed_revision": state.revision if not differences else None,
        }}, state.revision)
        FacecamService.invalidate_output(project_id)
        return result

    @classmethod
    def review(cls, project_id, revision):
        state = FacecamService.load(project_id)
        if not state.recording:
            raise ValueError("Importez votre enregistrement.")
        return FacecamService.update(project_id, {"recording": {**state.recording, "reviewed_revision": revision}, "prepared": None}, revision)

    @staticmethod
    def save_speech_timing(root: Path, words: list[dict], uncertain_spans: list):
        write_text_atomic(root / "speech_timing.json", json.dumps({
            "version": 1, "words": words, "uncertain_spans": uncertain_spans,
        }, ensure_ascii=False))

    @classmethod
    def speech_timing(cls, root: Path) -> dict:
        """Analyze old recordings once; tuning never reuses timings from cut audio."""
        from .transcriber import TranscriberService
        path = root / "speech_timing.json"
        if not path.exists():
            with TranscriberService.model_session():
                words, _ = TranscriberService._transcribe_sync(root / "voice.wav", "fr", "large-v3")
                cls.save_speech_timing(root, words, TranscriberService._last_unrecovered_gaps)
        timing = json.loads(path.read_text())
        if not timing.get("words"):
            raise ValueError("Aucune parole détectée dans l'enregistrement.")
        # Missing or malformed timings must never silently disable word protection.
        protected_spans(timing["words"], timing.get("uncertain_spans", []))
        return timing

    @classmethod
    def prepare(cls, project_id: str):
        from .forced_alignment import ForcedAlignmentService
        from .processing import ProcessingService
        from .project_service import ProjectService
        from .transcriber import TranscriberService
        state = FacecamService.load(project_id)
        if state.approved_revision != state.revision or not state.recording:
            raise ValueError("Validez le script et importez un enregistrement.")
        if state.recording.get("reviewed_revision") != state.revision:
            raise ValueError("Vérifiez les différences entre votre script et votre enregistrement.")
        if prepared_is_current(state) and all((Path(state.prepared["directory"]) / name).is_file()
                                             for name in ("narration.wav", "transcription.json", "voice_clean.wav")):
            return state
        root = Path(state.recording["directory"])
        # Never overwrite the last playable preview if analysis or alignment fails.
        signature = settings_signature(state.auto_editor)
        work = root / f"revision-{state.revision}-{signature}-{uuid.uuid4().hex[:8]}"
        work.mkdir(exist_ok=True)
        wav = root / "voice.wav"
        timing = cls.speech_timing(root)
        speech = protected_spans(timing["words"], timing.get("uncertain_spans", []))
        tuning = state.auto_editor
        detection = ffmpeg(["-i", str(wav), "-af",
            f"silencedetect=noise={tuning.noise_threshold_db}dB:d={tuning.min_silence_ms / 1000}", "-f", "null", "-"])
        silence_start, silences = None, []
        for line in detection.stderr.decode(errors="replace").splitlines():
            match = re.search(r"silence_start: ([\d.]+)", line)
            if match:
                silence_start = float(match[1])
            match = re.search(r"silence_end: ([\d.]+)", line)
            if match and silence_start is not None:
                silences.append((silence_start, float(match[1])))
                silence_start = None
        duration = ProcessingService._probe_wav_duration(wav)
        duration = min(duration, float(probe(root / "facecam.mp4")["format"]["duration"]))
        if silence_start is not None:
            silences.append((silence_start, duration))
        keep = keep_intervals(duration, silences, tuning, speech)
        verify_speech_retained(keep, speech, duration)
        if not keep:
            raise ValueError("Aucune parole détectée dans l'enregistrement.")
        clean = work / "voice_clean.wav"
        with wave.open(str(wav), "rb") as src, wave.open(str(clean), "wb") as dst:
            dst.setparams(src.getparams())
            frame_size = src.getnchannels() * src.getsampwidth()
            for start, end in keep:
                src.setpos(start * (RATE // FPS))
                sample_count = (end - start) * (RATE // FPS)
                frames = src.readframes(sample_count)
                dst.writeframes(frames + b"\x00" * (sample_count * frame_size - len(frames)))
        payload = {"language": "fr", "scenes": state.assignment}
        # One contiguous known-text segment per narration block, without any TTS normalization.
        manifest = cls.manifest(payload)
        reference = ProjectService.load_transcription(project_id)
        with TranscriberService.model_session():
            alignment = ForcedAlignmentService._align_single_audio(
                project_id=project_id, script_payload=payload, reference_transcription=reference,
                manifest=manifest, edited_audio_path=clean, align_language="fr", coarse_model_size="large-v3", output_dir=work)
        ForcedAlignmentService.write_alignment_report(work, alignment.report)
        project = FacecamService.project(project_id)
        matches = ProjectService.load_matches(project_id).matches
        # Bound the additional outro to actual source media; existing gap resolution handles short coverage.
        outro = matches[-1]
        source, _ = ProcessingService._resolve_source_reference(outro.episode, library_type=project.library_type)
        if source.exists():
            source_duration = float(probe(source).get("format", {}).get("duration") or 0)
            if source_duration > 0 and outro.end_time > source_duration:
                outro.end_time = source_duration
                outro.start_time = min(outro.start_time, max(0, source_duration - 3))
                from ..models import MatchList
                ProjectService.save_matches(project_id, MatchList(matches=matches))
        source_fps = asyncio.run(ProcessingService.detect_first_source_fps(matches, library_type=project.library_type))
        resolved = ProcessingService.resolve_scene_sources(matches, ProcessingService._build_source_rate(source_fps), library_type=project.library_type)
        # Do not shift only word timings: source_start must stay in the clean WAV's coordinate system.
        transcription, playback = ProcessingService.build_authoritative_playback_timeline(
            alignment.transcription, resolved, contiguous_audio_duration=ProcessingService._probe_wav_duration(clean),
            preserve_audio_start=True)
        output_voice = work / "narration.wav"
        ProcessingService.rebuild_tts_audio_with_playback_segments(clean, output_voice, playback,
            apply_tail_fade=False, exact_sample_boundaries=True)
        clips = camera_segments(keep, playback)
        end = math.ceil(ProcessingService._probe_wav_duration(output_voice) * FPS - 1e-7) / FPS
        write_text_atomic(work / "transcription.json", transcription.model_dump_json())
        cut_report = {"source_duration_seconds": duration,
            "clean_duration_seconds": sum(b - a for a, b in keep) / FPS,
            "removed_seconds": max(0, duration - sum(b - a for a, b in keep) / FPS),
            "cut_count": len(keep) - 1 + int(keep[0][0] > 0) + int(keep[-1][1] < math.ceil(duration * FPS - 1e-7)),
            "protected_word_count": len(timing["words"]), "clipped_word_count": 0,
            "uncertain_span_count": len(timing.get("uncertain_spans", []))}
        write_text_atomic(work / "cut_report.json", json.dumps(cut_report, indent=2))
        prepared = {"revision": state.revision, "fingerprint": state.recording["fingerprint"], "directory": str(work),
            "auto_editor": tuning.model_dump(), "auto_editor_signature": signature, "cut_report": cut_report,
            "keep_frames": [list(interval) for interval in keep], "camera_segments": clips, "cover_start": end,
            "duration_seconds": end + 1,
            "alignment_path": str(ForcedAlignmentService.report_path(work)),
            "scenes": [{"kind": "cover", "start_frame": round(end * FPS), "duration_frames": FPS}]}
        with FacecamService.lock(project_id):
            current = FacecamService.load(project_id)
            if current.auto_editor != tuning or not current.recording or current.recording["fingerprint"] != state.recording["fingerprint"]:
                raise ValueError("Les réglages ou l'enregistrement ont changé. Préparez à nouveau l'aperçu.")
            if current.revision != state.revision:
                raise ValueError("Le script a changé. Préparez à nouveau l'aperçu.")
            FacecamService.invalidate_output(project_id)
            return FacecamService.update(project_id, {"prepared": prepared, "processed_signature": None}, state.revision)

    @staticmethod
    def manifest(payload):
        segments, fragments = [], []
        def flush():
            if fragments:
                text = " ".join(s["text"] for s in fragments)
                segments.append({"id": len(segments), "text": text, "character_count": len(text),
                    "scene_indices": [s["scene_index"] for s in fragments], "scene_fragments": list(fragments)})
                fragments.clear()
        for scene in payload["scenes"]:
            if scene["text"]:
                fragments.append(scene)
            else:
                flush()
        flush()
        return {"version": 1, "mode": "single_audio", "language": "fr", "segments": segments}

    @classmethod
    def preview(cls, project_id):
        from pydub import AudioSegment
        from .processing import ProcessingService
        from .project_service import ProjectService
        from .music_config_service import MusicConfigService
        state = cls.prepare(project_id)
        project = FacecamService.project(project_id)
        work = Path(state.prepared["directory"])
        mix = AudioSegment.from_wav(work / "narration.wav")
        transcription = Transcription.model_validate_json((work / "transcription.json").read_text())
        matches = {m.scene_index: m for m in ProjectService.load_matches(project_id).matches}
        for scene in transcription.scenes:
            if scene.is_raw:
                match = matches[scene.scene_index]
                source, _ = ProcessingService._resolve_source_reference(match.episode, library_type=project.library_type)
                from .anime_library import AnimeLibraryService
                policy = AnimeLibraryService.build_source_audio_selection_policy(source, target_language=project.output_language)
                clip = work / f"raw-{scene.scene_index}.wav"
                ffmpeg(["-ss", str(match.start_time), "-i", str(source), "-t", str(scene.end_time - scene.start_time),
                    "-map", f"0:{policy.selected_stream_index}", "-vn", "-ar", str(RATE), "-ac", "1", str(clip)])
                mix = mix.overlay(AudioSegment.from_wav(clip), position=round(scene.start_time * 1000))
        if project.resolved_music_key():
            music = MusicConfigService.get_music(project.resolved_music_key())
            bed = AudioSegment.from_file(music.file_path) + float(music.volume_db)
            if len(bed):
                bed = (bed * (len(mix) // len(bed) + 1))[:len(mix)]
                mix = mix.overlay(bed)
        mix.export(work / "preview.wav", format="wav")
        return FacecamService.update(project_id, {"prepared": {**state.prepared,
            "preview_music_key": project.resolved_music_key(), "preview_version": uuid.uuid4().hex}}, state.revision)
