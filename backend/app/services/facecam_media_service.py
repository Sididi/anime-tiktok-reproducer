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


def camera_segments(keep: list[tuple[int, int]], playback: list, time_scale: float = 1.0) -> list[dict]:
    """Compose original→clean voice intervals with clean voice→final playback."""
    mapping, clean_cursor = [], 0.0
    for start, end in keep:
        length = (end - start) / FPS * time_scale
        mapping.append((clean_cursor, clean_cursor + length, start / FPS * time_scale))
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
    @staticmethod
    def apply_speed(clean: Path, camera: Path, work: Path, speed: float) -> tuple[Path, Path, float]:
        """Retiming is opt-in. At 1x return the original paths without any I/O."""
        if speed == 1.0:
            return clean, camera, 1.0
        from .audio_speed_service import AudioSpeedService
        from .processing import ProcessingService
        voice = work / "voice_speed.wav"
        asyncio.run(AudioSpeedService.apply_speed(clean, voice, speed))
        # atempo can differ by a few samples from duration / requested_speed.
        # Use the measured ratio for both the picture and its edit map so that
        # rounding does not accumulate drift or clip the final word.
        original_duration = ProcessingService._probe_wav_duration(clean)
        retimed_duration = ProcessingService._probe_wav_duration(voice)
        if original_duration <= 0 or retimed_duration <= 0:
            raise ValueError("La modification de vitesse n'a produit aucun audio.")
        time_scale = retimed_duration / original_duration
        # Reserve the camera path; video encoding belongs to final processing,
        # never to an audio preview (including a speed-only adjustment).
        return voice, work / "facecam.mp4", time_scale

    @staticmethod
    def retime_camera(camera: Path, destination: Path, time_scale: float):
        if destination.is_file():
            return
        temporary = destination.with_name("facecam.tmp.mp4")
        try:
            ffmpeg(["-i", str(camera), "-an", "-vf", f"setpts=(PTS-STARTPTS)*{time_scale:.12f},fps=60",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-movflags", "+faststart", str(temporary)])
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def ensure_camera(cls, project_id: str):
        state = FacecamService.load(project_id)
        if not prepared_is_current(state):
            raise ValueError("Préparez l'aperçu Facecam avant le traitement.")
        if state.auto_editor.speed != 1.0:
            cls.retime_camera(Path(state.recording["directory"]) / "facecam.mp4",
                              Path(state.prepared["camera_path"]), state.prepared["camera_time_scale"])

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
        # The content hash identifies the take. Reimporting the same video must
        # not decode its audio or analyze its speech again. Publish complete files only.
        if not (root / "voice.wav").is_file():
            ffmpeg(["-i", str(original), "-map", "0:a:0", "-vn", "-af", "aresample=48000:async=1:first_pts=0", "-ar", str(RATE), "-ac", "1", str(root / "voice.tmp.wav")])
            (root / "voice.tmp.wav").replace(root / "voice.wav")
        if not (root / "facecam.mp4").is_file():
            ffmpeg(["-i", str(original), "-map", "0:v:0", "-an", "-vf", "fps=60:start_time=0,format=yuv420p",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-movflags", "+faststart", str(root / "facecam.tmp.mp4")])
            (root / "facecam.tmp.mp4").replace(root / "facecam.mp4")
        master = probe(root / "facecam.mp4")
        write_text_atomic(root / "source_media.json", json.dumps({"camera_duration": float(master["format"]["duration"])}))
        master_info = master["streams"][0]
        scale = max(1080 / master_info["width"], 1920 / master_info["height"]) * 100
        words = cls.speech_timing(root)["words"]
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
    def alignment_cache(cls, project_id: str, state) -> Path:
        from .project_service import ProjectService
        payload = {"language": "fr", "scenes": state.assignment}
        reference = ProjectService.load_transcription(project_id)
        key = hashlib.sha256(json.dumps({"version": 1, "script": payload,
            "reference": reference.model_dump()}, sort_keys=True).encode()).hexdigest()[:20]
        return Path(state.recording["directory"]) / f"alignment-{key}" / "source_transcription.json"

    @classmethod
    def needs_speech_analysis(cls, project_id: str) -> bool:
        state = FacecamService.load(project_id)
        if cls.prepared_audio_exists(state):
            return False
        if not state.recording:
            return True
        return not ((Path(state.recording["directory"]) / "speech_timing.json").is_file()
                    and cls.alignment_cache(project_id, state).is_file())

    @staticmethod
    def prepared_audio_exists(state) -> bool:
        return prepared_is_current(state) and all((Path(state.prepared["directory"]) / name).is_file()
            for name in ("narration.wav", "transcription.json", "voice_clean.wav"))

    @classmethod
    def source_alignment(cls, project_id: str, state, root: Path, timing: dict):
        """Align the approved script once on the immutable, uncut source audio."""
        from .forced_alignment import ForcedAlignmentService
        from .project_service import ProjectService
        from .transcriber import TranscriberService
        cache = cls.alignment_cache(project_id, state)
        directory = cache.parent
        if not cache.is_file():
            directory.mkdir(exist_ok=True)
            payload = {"language": "fr", "scenes": state.assignment}
            reference = ProjectService.load_transcription(project_id)
            with TranscriberService.model_session():
                alignment = ForcedAlignmentService._align_single_audio(
                    project_id=project_id, script_payload=payload, reference_transcription=reference,
                    manifest=cls.manifest(payload), edited_audio_path=root / "voice.wav", align_language="fr",
                    coarse_model_size="large-v3", output_dir=directory, coarse_words=timing["words"])
            ForcedAlignmentService.write_alignment_report(directory, alignment.report)
            write_text_atomic(cache, alignment.transcription.model_dump_json())
        return Transcription.model_validate_json(cache.read_text()), ForcedAlignmentService.report_path(directory)

    @staticmethod
    def map_source_alignment(source: Transcription, keep: list[tuple[int, int]], time_scale: float) -> Transcription:
        """Map native word timestamps through silence cuts and measured audio speed.

        Raw anime dialogue has its own clock and must retain its original duration.
        """
        def mapped(timestamp):
            return sum(max(0.0, min(timestamp, b / FPS) - a / FPS) for a, b in keep) * time_scale
        result = source.model_copy(deep=True)
        for scene in result.scenes:
            if scene.is_raw:
                continue
            scene.start_time, scene.end_time = mapped(scene.start_time), mapped(scene.end_time)
            for word in scene.words:
                word.start, word.end = mapped(word.start), mapped(word.end)
        result.unrecovered_gaps = [(mapped(a), mapped(b)) for a, b in result.unrecovered_gaps]
        return result

    @classmethod
    def prepare(cls, project_id: str):
        from .processing import ProcessingService
        from .project_service import ProjectService
        state = FacecamService.load(project_id)
        if state.approved_revision != state.revision or not state.recording:
            raise ValueError("Validez le script et importez un enregistrement.")
        if state.recording.get("reviewed_revision") != state.revision:
            raise ValueError("Vérifiez les différences entre votre script et votre enregistrement.")
        if cls.prepared_audio_exists(state):
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
        media_cache = root / "source_media.json"
        if not media_cache.is_file():  # One metadata probe for recordings imported before caching.
            write_text_atomic(media_cache, json.dumps({"camera_duration": float(probe(root / "facecam.mp4")["format"]["duration"])}))
        duration = min(duration, json.loads(media_cache.read_text())["camera_duration"])
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
        voice, camera, time_scale = cls.apply_speed(clean, root / "facecam.mp4", work, tuning.speed)
        source_alignment, report_path = cls.source_alignment(project_id, state, root, timing)
        aligned = cls.map_source_alignment(source_alignment, keep, time_scale)
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
        # Word timings and source_start share the optionally retimed voice clock.
        transcription, playback = ProcessingService.build_authoritative_playback_timeline(
            aligned, resolved, contiguous_audio_duration=ProcessingService._probe_wav_duration(voice),
            preserve_audio_start=True)
        output_voice = work / "narration.wav"
        ProcessingService.rebuild_tts_audio_with_playback_segments(voice, output_voice, playback,
            apply_tail_fade=False, exact_sample_boundaries=True)
        clips = camera_segments(keep, playback, time_scale)
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
            "camera_path": str(camera), "camera_time_scale": time_scale,
            "keep_frames": [list(interval) for interval in keep], "camera_segments": clips, "cover_start": end,
            "duration_seconds": end + 1,
            "alignment_path": str(report_path),
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
                # Raw dialogue is independent of narration speed, silence cuts
                # and background music. Decode it once per source/range/language.
                stat = source.stat()
                key = hashlib.sha256(json.dumps([str(source.resolve()), stat.st_size, stat.st_mtime_ns,
                    match.start_time, round(scene.end_time - scene.start_time, 6), project.output_language]).encode()).hexdigest()[:20]
                cache = FacecamService.directory(project_id) / "preview-audio"
                cache.mkdir(exist_ok=True)
                clip = cache / f"raw-{key}.wav"
                if not clip.is_file():
                    from .anime_library import AnimeLibraryService
                    policy = AnimeLibraryService.build_source_audio_selection_policy(source, target_language=project.output_language)
                    temporary = clip.with_suffix(".tmp.wav")
                    ffmpeg(["-ss", str(match.start_time), "-i", str(source), "-t", str(scene.end_time - scene.start_time),
                        "-map", f"0:{policy.selected_stream_index}", "-vn", "-ar", str(RATE), "-ac", "1", str(temporary)])
                    temporary.replace(clip)
                mix = mix.overlay(AudioSegment.from_wav(clip), position=round(scene.start_time * 1000))
        if project.resolved_music_key():
            music = MusicConfigService.get_music(project.resolved_music_key())
            bed = AudioSegment.from_file(music.file_path) + float(music.volume_db)
            if len(bed):
                bed = (bed * (len(mix) // len(bed) + 1))[:len(mix)]
                mix = mix.overlay(bed)
        mix.export(work / "preview.tmp.wav", format="wav")
        (work / "preview.tmp.wav").replace(work / "preview.wav")
        return FacecamService.update(project_id, {"prepared": {**state.prepared,
            "preview_music_key": project.resolved_music_key(), "preview_version": uuid.uuid4().hex}}, state.revision)
