import asyncio
import os
import re
import statistics
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import AsyncIterator

from ..models import Word, SceneTranscription, Transcription, SceneList
from ..services import ProjectService
from ..utils.process_cleanup import shutdown_torch_compile_workers


class TranscriptionProgress:
    """Progress information for transcription."""

    def __init__(
        self,
        status: str,
        progress: float = 0,
        message: str = "",
        transcription: Transcription | None = None,
        error: str | None = None,
    ):
        self.status = status
        self.progress = progress
        self.message = message
        self.transcription = transcription
        self.error = error

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
        }
        if self.transcription is not None:
            result["transcription"] = self.transcription.model_dump()
        return result


# Language code mapping
LANGUAGE_MAP = {
    "auto": None,
    "en": "en",
    "anglais": "en",
    "es": "es",
    "espagnol": "es",
    "fr": "fr",
    "français": "fr",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".3gp", ".ts", ".m2ts",
}


class TranscriberService:
    """Service for transcribing audio using WhisperX."""

    _asr_models: dict = {}
    _align_models: dict = {}
    _unsafe_env_applied: bool = False

    @classmethod
    def _ensure_unsafe_torch_load_env(cls) -> None:
        """Set runtime env knobs before importing WhisperX/Torch stacks."""
        if cls._unsafe_env_applied:
            return
        os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
        # Avoid huge compile-worker fanout (defaults to up to 32 processes).
        os.environ.setdefault("TORCHINDUCTOR_COMPILE_THREADS", "1")
        # Keep joblib from starting large process pools in inference code paths.
        os.environ.setdefault("JOBLIB_MULTIPROCESSING", "0")
        cls._unsafe_env_applied = True

    @staticmethod
    def _cleanup_runtime_workers() -> None:
        """Shut down transient TorchInductor compile pools after ASR."""
        shutdown_torch_compile_workers()

    @staticmethod
    def _get_device() -> str:
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    @staticmethod
    def _has_audio_stream(media_path: Path) -> bool | None:
        """Return whether input has at least one audio stream.

        Returns None when ffprobe is unavailable or probing fails.
        """
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(media_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None

        if result.returncode != 0:
            return None

        return bool(result.stdout.strip())

    @staticmethod
    def _extract_audio_for_whisper(media_path: Path, output_wav: Path) -> None:
        """Extract first audio stream to 16kHz mono PCM WAV for WhisperX."""
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(media_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_wav),
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg is required to extract audio for transcription.") from exc

        if result.returncode == 0 and output_wav.exists():
            return

        stderr = result.stderr.strip()
        lower_stderr = stderr.lower()
        if (
            "matches no streams" in lower_stderr
            or "does not contain any stream" in lower_stderr
            or ("stream map" in lower_stderr and "audio" in lower_stderr)
        ):
            raise ValueError(
                f"Input file has no usable audio stream ({media_path.name}). "
                "Transcription needs an audio track."
            )

        raise RuntimeError(
            "Failed to extract audio for transcription. "
            f"ffmpeg error: {stderr[:400]}"
        )

    @staticmethod
    def _get_compute_type(device: str) -> str:
        return "float16" if device == "cuda" else "int8"

    @classmethod
    def _load_asr_model(cls, model_size: str, device: str, compute_type: str):
        key = (model_size, device, compute_type)
        if key in cls._asr_models:
            return cls._asr_models[key]

        cls._ensure_unsafe_torch_load_env()

        import whisperx

        model = whisperx.load_model(model_size, device, compute_type=compute_type)
        cls._asr_models[key] = model
        return model

    @classmethod
    def _load_align_model(cls, language_code: str, device: str):
        key = (language_code or "unknown", device)
        if key in cls._align_models:
            return cls._align_models[key]

        cls._ensure_unsafe_torch_load_env()

        import whisperx

        model_a, metadata = whisperx.load_align_model(language_code=language_code, device=device)
        cls._align_models[key] = (model_a, metadata)
        return model_a, metadata

    @staticmethod
    def _segment_words_from_text(segment: dict) -> list[dict]:
        text = segment.get("text") or ""
        words = [w for w in text.split() if w]
        if not words:
            return []
        start = segment.get("start")
        end = segment.get("end")
        if start is None or end is None:
            return []
        duration = max(end - start, 0.0)
        step = duration / max(len(words), 1)
        out = []
        for idx, word in enumerate(words):
            w_start = start + step * idx
            w_end = w_start + step
            out.append({
                "text": word,
                "start": w_start,
                "end": w_end,
                "confidence": segment.get("confidence", 1.0),
            })
        return out

    @classmethod
    def _extract_words_from_segments(cls, segments: list[dict]) -> list[dict]:
        words: list[dict] = []
        for segment in segments:
            segment_words = segment.get("words") or []
            if segment_words:
                for word in segment_words:
                    text = word.get("word") or word.get("text") or ""
                    text = text.strip()
                    if not text:
                        continue
                    start = word.get("start")
                    end = word.get("end")
                    if start is None or end is None:
                        continue
                    confidence = word.get("score")
                    if confidence is None:
                        confidence = word.get("confidence")
                    if confidence is None:
                        confidence = word.get("probability")
                    if confidence is None:
                        confidence = 1.0
                    words.append({
                        "text": text,
                        "start": float(start),
                        "end": float(end),
                        "confidence": float(confidence),
                    })
            else:
                words.extend(cls._segment_words_from_text(segment))
        return words

    @classmethod
    def _transcribe_sync(
        cls,
        audio_path: Path,
        language: str | None = None,
        model_size: str = "large-v3",
    ) -> tuple[list[dict], str]:
        cls._ensure_unsafe_torch_load_env()
        import whisperx

        temp_audio_path: Path | None = None
        try:
            device = cls._get_device()
            compute_type = cls._get_compute_type(device)
            batch_size = 16 if device == "cuda" else 4

            has_audio = cls._has_audio_stream(audio_path)
            if has_audio is False:
                raise ValueError(
                    f"Input file has no audio stream ({audio_path.name}). "
                    "Transcription needs an audio track. Re-download the TikTok with audio "
                    "or provide a file that includes audio."
                )

            load_path = audio_path
            if audio_path.suffix.lower() in VIDEO_EXTENSIONS:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    temp_audio_path = Path(tmp.name)
                cls._extract_audio_for_whisper(audio_path, temp_audio_path)
                load_path = temp_audio_path

            try:
                audio = whisperx.load_audio(str(load_path))
            except Exception as exc:
                msg = str(exc)
                if "Output file does not contain any stream" in msg:
                    raise ValueError(
                        f"Failed to read audio from {load_path.name}. "
                        "The file appears to be video-only (no audio stream)."
                    ) from exc
                raise

            model = cls._load_asr_model(model_size, device, compute_type)
            result = model.transcribe(
                audio,
                batch_size=batch_size,
                num_workers=0,
                language=language,
            )

            detected_language = result.get("language") or (language or "en")
            segments = result.get("segments") or []

            try:
                model_a, metadata = cls._load_align_model(detected_language, device)
                aligned = whisperx.align(segments, model_a, metadata, audio, device)
                segments = aligned.get("segments") or segments
            except Exception:
                # If alignment fails, fall back to segment-level timings.
                pass

            words = cls._extract_words_from_segments(segments)
            return words, detected_language
        finally:
            if temp_audio_path and temp_audio_path.exists():
                temp_audio_path.unlink(missing_ok=True)
            cls._cleanup_runtime_workers()

    @staticmethod
    def _normalize_token(token: str) -> str:
        if not token:
            return ""
        token = token.strip().lower()
        token = unicodedata.normalize("NFKD", token)
        token = "".join(ch for ch in token if not unicodedata.combining(ch))
        token = re.sub(r"[^a-z0-9']+", "", token)
        return token

    @classmethod
    def _sequence_align(cls, script_tokens: list[str], asr_tokens: list[str]) -> list[int | None]:
        n = len(script_tokens)
        m = len(asr_tokens)
        if n == 0:
            return []

        # DP for sequence alignment: substitution mismatch is more costly than insert/delete.
        sub_mismatch_cost = 2
        ins_cost = 1
        del_cost = 1

        dp = [[0] * (m + 1) for _ in range(n + 1)]
        back = [[None] * (m + 1) for _ in range(n + 1)]

        for i in range(1, n + 1):
            dp[i][0] = i * del_cost
            back[i][0] = "del"
        for j in range(1, m + 1):
            dp[0][j] = j * ins_cost
            back[0][j] = "ins"

        for i in range(1, n + 1):
            s_tok = script_tokens[i - 1]
            for j in range(1, m + 1):
                a_tok = asr_tokens[j - 1]
                sub_cost = 0 if s_tok and s_tok == a_tok else sub_mismatch_cost

                cost_sub = dp[i - 1][j - 1] + sub_cost
                cost_del = dp[i - 1][j] + del_cost
                cost_ins = dp[i][j - 1] + ins_cost

                best = cost_sub
                best_step = "sub"
                if cost_del < best:
                    best = cost_del
                    best_step = "del"
                if cost_ins < best:
                    best = cost_ins
                    best_step = "ins"

                dp[i][j] = best
                back[i][j] = best_step

        mapping: list[int | None] = [None] * n
        i = n
        j = m
        while i > 0 or j > 0:
            step = back[i][j]
            if step == "sub":
                s_tok = script_tokens[i - 1]
                a_tok = asr_tokens[j - 1]
                if s_tok and s_tok == a_tok:
                    mapping[i - 1] = j - 1
                i -= 1
                j -= 1
            elif step == "del":
                mapping[i - 1] = None
                i -= 1
            else:
                j -= 1

        return mapping

    @staticmethod
    def _median_word_duration(words: list[dict]) -> float:
        durations = [
            w["end"] - w["start"]
            for w in words
            if w.get("end") is not None and w.get("start") is not None
        ]
        durations = [d for d in durations if d > 0]
        if not durations:
            return 0.2
        return max(0.05, statistics.median(durations))

    @classmethod
    def _fill_missing_word_times(cls, entries: list[dict], default_duration: float) -> None:
        if not entries:
            return

        known = [i for i, e in enumerate(entries) if e.get("start") is not None and e.get("end") is not None]
        if not known:
            t = 0.0
            for e in entries:
                e["start"] = t
                e["end"] = t + default_duration
                t = e["end"]
            return

        first = known[0]
        t = entries[first]["start"]
        for i in range(first - 1, -1, -1):
            end = t
            start = max(0.0, end - default_duration)
            entries[i]["start"] = start
            entries[i]["end"] = end
            t = start

        for idx in range(len(known) - 1):
            left = known[idx]
            right = known[idx + 1]
            gap_count = right - left - 1
            if gap_count <= 0:
                continue

            left_end = entries[left]["end"]
            right_start = entries[right]["start"]
            gap = right_start - left_end

            if gap <= 0:
                step = default_duration
            else:
                step = gap / (gap_count + 1)

            for offset in range(1, gap_count + 1):
                start = left_end + step * (offset - 1)
                duration = min(default_duration, step)
                entries[left + offset]["start"] = start
                entries[left + offset]["end"] = start + duration

        last = known[-1]
        t = entries[last]["end"]
        for i in range(last + 1, len(entries)):
            start = t
            end = start + default_duration
            entries[i]["start"] = start
            entries[i]["end"] = end
            t = end

        # Enforce monotonic timings
        prev_end = 0.0
        min_duration = 0.02
        for e in entries:
            start = e.get("start", 0.0)
            end = e.get("end", start + default_duration)
            if start < prev_end:
                start = prev_end
            if end < start + min_duration:
                end = start + min_duration
            e["start"] = start
            e["end"] = end
            prev_end = end

    @classmethod
    def _align_script_to_words(
        cls,
        script: dict,
        timed_words: list[dict],
    ) -> list[SceneTranscription]:
        script_entries: list[dict] = []
        for scene_data in script.get("scenes", []):
            scene_index = scene_data.get("scene_index", 0)
            text = scene_data.get("text", "")
            for raw_word in text.split():
                script_entries.append({
                    "scene_index": scene_index,
                    "text": raw_word,
                    "norm": cls._normalize_token(raw_word),
                })

        asr_entries = []
        for word in timed_words:
            text = word.get("text", "")
            norm = cls._normalize_token(text)
            if not norm:
                continue
            asr_entries.append({
                "text": text,
                "norm": norm,
                "start": word.get("start"),
                "end": word.get("end"),
                "confidence": word.get("confidence", 1.0),
            })

        mapping = cls._sequence_align(
            [e["norm"] for e in script_entries],
            [e["norm"] for e in asr_entries],
        )

        aligned_entries = []
        for idx, entry in enumerate(script_entries):
            mapped_idx = mapping[idx] if idx < len(mapping) else None
            if mapped_idx is not None:
                asr = asr_entries[mapped_idx]
                aligned_entries.append({
                    "scene_index": entry["scene_index"],
                    "text": entry["text"],
                    "start": asr.get("start"),
                    "end": asr.get("end"),
                    "confidence": asr.get("confidence", 1.0),
                })
            else:
                aligned_entries.append({
                    "scene_index": entry["scene_index"],
                    "text": entry["text"],
                    "start": None,
                    "end": None,
                    "confidence": 0.0,
                })

        default_duration = cls._median_word_duration(timed_words)
        cls._fill_missing_word_times(aligned_entries, default_duration)

        scene_word_map: dict[int, list[Word]] = {}
        for entry in aligned_entries:
            scene_word_map.setdefault(entry["scene_index"], []).append(Word(
                text=entry["text"],
                start=float(entry["start"]),
                end=float(entry["end"]),
                confidence=float(entry.get("confidence", 0.0)),
            ))

        scene_transcriptions = []
        for scene_data in script.get("scenes", []):
            scene_index = scene_data.get("scene_index", 0)
            scene_text = scene_data.get("text", "")
            words = scene_word_map.get(scene_index, [])
            if words:
                start_time = words[0].start
                end_time = words[-1].end
            else:
                start_time = 0.0
                end_time = 0.0

            scene_transcriptions.append(SceneTranscription(
                scene_index=scene_index,
                text=scene_text,
                words=words,
                start_time=start_time,
                end_time=end_time,
            ))

        return scene_transcriptions

    @classmethod
    def _assign_words_to_scenes(
        cls,
        words: list[dict],
        scenes: SceneList,
    ) -> list[SceneTranscription]:
        """Assign transcribed words to scenes based on timing."""
        scene_transcriptions = []

        for scene in scenes.scenes:
            scene_words = []
            scene_text_parts = []

            for word in words:
                # Word belongs to scene if its midpoint is within scene bounds
                word_mid = (word["start"] + word["end"]) / 2
                if scene.start_time <= word_mid < scene.end_time:
                    scene_words.append(Word(
                        text=word["text"],
                        start=word["start"],
                        end=word["end"],
                        confidence=word["confidence"],
                    ))
                    scene_text_parts.append(word["text"])

            scene_transcriptions.append(SceneTranscription(
                scene_index=scene.index,
                text=" ".join(scene_text_parts),
                words=scene_words,
                start_time=scene.start_time,
                end_time=scene.end_time,
            ))

        return scene_transcriptions

    @classmethod
    async def transcribe(
        cls,
        project_id: str,
        language: str = "auto",
    ) -> AsyncIterator[TranscriptionProgress]:
        """Transcribe a project's video and yield progress updates."""
        yield TranscriptionProgress("starting", 0, "Loading project...")

        try:
            # Load project and scenes
            project = ProjectService.load(project_id)
            if not project or not project.video_path:
                yield TranscriptionProgress("error", 0, "", error="Project or video not found")
                return

            scenes = ProjectService.load_scenes(project_id)
            if not scenes or not scenes.scenes:
                yield TranscriptionProgress("error", 0, "", error="No scenes found")
                return

            video_path = Path(project.video_path)

            yield TranscriptionProgress("starting", 0.1, "Loading transcription model...")

            # Map language
            lang_code = LANGUAGE_MAP.get(language.lower(), language if language != "auto" else None)

            yield TranscriptionProgress("transcribing", 0.2, "Transcribing audio (this may take a while)...")

            # Run transcription in thread pool (use large-v3 for original TikTok video)
            loop = asyncio.get_event_loop()
            words, detected_lang = await loop.run_in_executor(
                None,
                lambda: cls._transcribe_sync(video_path, lang_code, "large-v3"),
            )

            yield TranscriptionProgress("processing", 0.8, "Assigning words to scenes...")

            # Assign words to scenes
            scene_transcriptions = cls._assign_words_to_scenes(words, scenes)

            transcription = Transcription(
                language=detected_lang,
                scenes=scene_transcriptions,
            )

            # Save transcription
            ProjectService.save_transcription(project_id, transcription)

            yield TranscriptionProgress(
                "complete",
                1.0,
                f"Transcribed {len(words)} words in {detected_lang}",
                transcription=transcription,
            )

        except Exception as e:
            yield TranscriptionProgress("error", 0, "", error=str(e))

    @classmethod
    def transcribe_with_alignment(
        cls,
        audio_path: Path,
        script: dict,
        model_size: str = "large-v3",
    ) -> Transcription:
        """
        Transcribe audio and align with a known script.

        Uses WhisperX for transcription + alignment, then aligns the script
        to the timed words with a sequence alignment step.

        Args:
            audio_path: Path to the audio file
            script: Script JSON with scenes and text
            model_size: Whisper model size (default: large-v3)

        Returns:
            Transcription with word-level timings
        """
        script_lang = script.get("language")
        if isinstance(script_lang, str):
            lang_code = LANGUAGE_MAP.get(script_lang.lower(), script_lang)
        else:
            lang_code = None

        # Transcribe to get timed words (WhisperX alignment)
        words, detected_lang = cls._transcribe_sync(audio_path, lang_code, model_size)

        # Align script text to timed words
        scene_transcriptions = cls._align_script_to_words(script, words)

        return Transcription(
            language=script.get("language", detected_lang),
            scenes=scene_transcriptions,
        )
