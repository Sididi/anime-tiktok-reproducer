"""Processing pipeline service for final video generation."""

import asyncio
import html
import json
import logging
import math
import re
import shutil
import tempfile
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, AsyncIterator

import spacy

from ..config import settings
from ..models import MatchList, Project, Transcription, SceneMatch
from ..models.transcription import Word, SceneTranscription
from ..utils.media_binaries import is_media_binary_override_error
from ..utils.subprocess_runner import CommandTimeoutError, run_command
from ..utils.timing import compute_adjusted_scene_end_times
from .anime_library import AnimeLibraryService, SubtitleSidecarEntry
from .transcriber import TranscriberService
from .otio_timing import ClipTiming, OTIOTimingCalculator, FrameRateInfo
from .gap_resolution import GapResolutionService
from .export_service import ExportService
from .music_config_service import MusicConfigService
from .premiere_subtitle_baker import PremiereSubtitleBakerService
from .project_service import ProjectService
from .auto_editor_profiles import AutoEditorProfile, PRODUCTION_AUTO_EDITOR_PROFILE
from .forced_alignment import ForcedAlignmentService, PreparedAlignmentAudio
from .voice_config_service import VoiceConfigService

logger = logging.getLogger("uvicorn.error")


# =============================================================================
# spaCy helpers for determiner detection
# =============================================================================

# Cache of loaded spaCy models
_SPACY_MODELS: dict[str, spacy.Language] = {}

SPACY_MODEL_MAP = {
    "fr": "fr_core_news_sm",
    "en": "en_core_web_sm",
    "es": "es_core_news_sm",
}


def _get_spacy_model(lang: str) -> spacy.Language:
    """Load and cache the spaCy model for the given language."""
    if lang not in _SPACY_MODELS:
        model_name = SPACY_MODEL_MAP.get(lang, "en_core_web_sm")
        try:
            _SPACY_MODELS[lang] = spacy.load(model_name, disable=["ner", "parser"])
        except OSError:
            # Model not installed, fall back to English
            import sys
            print(f"[WARNING] spaCy model '{model_name}' not found, falling back to en_core_web_sm", file=sys.stderr)
            if "en" not in _SPACY_MODELS:
                _SPACY_MODELS["en"] = spacy.load("en_core_web_sm", disable=["ner", "parser"])
            _SPACY_MODELS[lang] = _SPACY_MODELS["en"]
    return _SPACY_MODELS[lang]


def is_determiner(word: str, language: str) -> bool:
    """Check if a word is a determiner using spaCy POS tagging."""
    nlp = _get_spacy_model(language)
    doc = nlp(word)
    if doc and len(doc) > 0:
        return doc[0].pos_ == "DET"
    return False


# Subject pronouns that start clauses (should not be isolated at end of subtitle)
SUBJECT_PRONOUNS = {
    "fr": {"je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "j'", "c'", "ça"},
    "en": {"i", "you", "he", "she", "it", "we", "they"},
    "es": {"yo", "tú", "él", "ella", "usted", "nosotros", "nosotras", "vosotros", "vosotras", "ellos", "ellas", "ustedes"},
}


def is_clause_starter(word: str, language: str) -> bool:
    """
    Check if a word should not be isolated at the end of a subtitle block.

    This includes:
    - Determiners (le, la, un, the, a, el, la...)
    - Subject pronouns that start clauses (il, elle, he, she, yo, él...)

    These words introduce the next element (noun or verb) and should stay with it.
    """
    # Check if it's a determiner
    if is_determiner(word, language):
        return True

    # Check if it's a subject pronoun
    word_lower = word.lower().strip()
    lang_pronouns = SUBJECT_PRONOUNS.get(language, SUBJECT_PRONOUNS.get("en", set()))
    if word_lower in lang_pronouns:
        return True

    return False


def strip_punctuation(text: str) -> str:
    """Strip leading/trailing punctuation from a word, keeping apostrophes in contractions."""
    import re
    # Remove leading punctuation (except apostrophe for contractions like l', d', j')
    text = re.sub(r'^[^\w\'\']+',"", text)
    # Remove trailing punctuation
    text = re.sub(r'[^\w\'\']+$', "", text)
    return text


# Sentence-ending punctuation that should trigger a subtitle break
SENTENCE_ENDING_PUNCT = {'.', '!', '?', '…', ':', ';'}

# Guardrail for low-confidence single-word subtitles.
LOW_CONF_THRESHOLD = 0.05
LOW_CONF_SINGLE_WORD_MIN_DURATION_SEC = 0.22
MIN_GAP_FOR_EXTENSION_SEC = 0.30

KNOWN_MEDIA_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".aiff",
    ".aif",
)


def _strip_known_media_extension(name: str) -> str:
    """Strip only supported media extensions, not arbitrary dotted suffixes."""
    clean_name = str(name or "").strip()
    lower_name = clean_name.lower()
    for ext in KNOWN_MEDIA_EXTENSIONS:
        if lower_name.endswith(ext):
            return clean_name[:-len(ext)]
    return clean_name
NEXT_WORD_SAFETY_SEC = 1.0 / 60.0
SRT_OBVIOUS_SILENCE_GAP_SEC = 0.5


def has_sentence_ending(text: str) -> bool:
    """Check if a word ends with sentence-ending punctuation."""
    if not text:
        return False
    # Check if the original text (before stripping) ends with sentence punctuation
    return any(text.rstrip().endswith(p) for p in SENTENCE_ENDING_PUNCT)


@dataclass
class ProcessingProgress:
    """Progress information for processing."""

    status: str  # starting, processing, complete, error, gaps_detected
    step: str = ""  # Current step ID
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    download_url: str | None = None
    # Gap resolution fields
    gaps_detected: bool = False
    gap_count: int = 0
    total_gap_duration: float = 0.0
    # Duration warning fields
    duration_warning: bool = False
    audio_duration_seconds: float = 0.0
    raw_scenes_duration_seconds: float = 0.0
    total_duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "step": self.step,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "download_url": self.download_url,
            "gaps_detected": self.gaps_detected,
            "gap_count": self.gap_count,
            "total_gap_duration": self.total_gap_duration,
            "duration_warning": self.duration_warning,
            "audio_duration_seconds": self.audio_duration_seconds,
            "raw_scenes_duration_seconds": self.raw_scenes_duration_seconds,
            "total_duration_seconds": self.total_duration_seconds,
        }


@dataclass(frozen=True)
class PlaybackAudioSegment:
    """Contiguous TTS audio slice or inserted silence for final playback."""

    scene_index: int
    kind: str  # "audio" or "silence"
    duration: float
    source_start: float = 0.0
    source_end: float = 0.0


@dataclass(frozen=True)
class ResolvedSceneSource:
    """Resolved source clip information shared by processing and JSX generation."""

    scene_index: int
    source_path: Path
    clip_name: str
    source_in_frame: int
    source_out_frame: int
    source_in_seconds: float
    source_out_seconds: float
    source_duration_seconds: float
    used_alternative: bool = False


@dataclass(frozen=True)
class SrtEntry:
    """Rendered subtitle entry with explicit timing."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class RawSceneSubtitleImageEntry:
    """Image-based raw-scene subtitle asset placed directly on V4."""

    scene_index: int
    start: float
    end: float
    relative_asset_path: str


class ProcessingService:
    """Service for processing the final video generation pipeline."""

    FFPROBE_TIMEOUT_SECONDS = 30.0
    AUTO_EDITOR_TIMEOUT_SECONDS = 1800.0
    PREMIERE_JSX_TEMPLATE_PATH = (
        Path(__file__).resolve().parent / "templates" / "premiere_import_project_v77.jsx"
    )
    CLASSIC_SUBTITLE_TIMING_RELATIVE_PATH = "subtitles/subtitle_timings.srt"
    RAW_SCENE_TEXT_SUBTITLE_TIMING_RELATIVE_PATH = (
        "raw_scene_subtitles/text_subtitles.srt"
    )
    RAW_SCENE_TEXT_SUBTITLE_MOGRT_RELATIVE_DIR = "raw_scene_subtitles/text_mogrts"
    _gap_candidate_prewarm_tasks: dict[str, asyncio.Task[None]] = {}

    @classmethod
    def _resolve_source_reference(cls, episode: str) -> tuple[Path, str]:
        """Resolve a match episode to a source path and Premiere-safe clip name."""
        resolved_path = GapResolutionService.resolve_episode_path(episode)
        if resolved_path and resolved_path.exists():
            return resolved_path, resolved_path.stem

        fallback_path = Path(episode)
        if fallback_path.exists():
            return fallback_path, _strip_known_media_extension(fallback_path.name)

        return fallback_path, _strip_known_media_extension(episode)

    @classmethod
    async def _collect_required_source_groups(
        cls,
        matches: list[SceneMatch],
    ) -> list[tuple[Path, list[SceneMatch]]]:
        """Resolve and dedupe the final source episodes required by processing."""
        manifest = await AnimeLibraryService.ensure_episode_manifest()
        paths_by_key: dict[str, Path] = {}
        matches_by_key: dict[str, list[SceneMatch]] = {}
        unresolved: list[str] = []

        for match in matches:
            episode = str(match.episode or "").strip()
            if not episode:
                continue

            resolved = AnimeLibraryService.resolve_episode_path(episode, manifest)
            if resolved is None:
                fallback = Path(episode)
                if fallback.exists():
                    resolved = fallback

            if resolved is None or not resolved.exists():
                unresolved.append(episode)
                continue

            key = str(resolved.resolve())
            if key not in paths_by_key:
                paths_by_key[key] = resolved
                matches_by_key[key] = []
            matches_by_key[key].append(match)

        if unresolved:
            preview = ", ".join(unresolved[:3])
            if len(unresolved) > 3:
                preview += ", ..."
            raise RuntimeError(f"Unable to resolve required source episode(s): {preview}")

        return [
            (paths_by_key[key], matches_by_key[key])
            for key in sorted(paths_by_key)
        ]

    @staticmethod
    def _update_absolute_match_episode_hints(
        matches: list[SceneMatch],
        *,
        old_source_path: Path,
        new_source_path: Path,
    ) -> bool:
        """Keep absolute-path manual matches valid if the normalized filename changes."""
        if old_source_path.resolve() == new_source_path.resolve():
            return False

        changed = False
        old_resolved = old_source_path.resolve()
        new_value = str(new_source_path)
        for match in matches:
            episode_hint = str(match.episode or "").strip()
            if not episode_hint:
                continue
            candidate = Path(episode_hint)
            if not candidate.is_absolute():
                continue
            try:
                candidate_resolved = candidate.resolve()
            except OSError:
                candidate_resolved = candidate
            if candidate_resolved == old_resolved:
                match.episode = new_value
                changed = True
        return changed

    @staticmethod
    def _format_source_normalization_message(
        *,
        current: int,
        total: int,
        result,
    ) -> str:
        if not result.changed:
            return f"Source already compatible ({current}/{total}): {result.source_path.name}"

        if result.action == "remux_to_mp4":
            action = "Remuxed source for Premiere"
        elif result.action == "audio_to_aac":
            action = "Normalized source audio for Premiere"
        elif result.action == "full_h264_aac_transcode":
            action = "Transcoded source for Premiere"
        else:
            action = "Normalized source for Premiere"
        return f"{action} ({current}/{total}): {result.normalized_path.name}"

    @classmethod
    def schedule_gap_candidate_prewarm(
        cls,
        project_id: str,
        gaps: list,
        matches: list | None = None,
    ) -> None:
        """Start a background prewarm for gap candidate generation."""
        if not gaps:
            return

        existing = cls._gap_candidate_prewarm_tasks.get(project_id)
        if existing and not existing.done():
            return

        async def _run() -> None:
            try:
                await GapResolutionService.generate_candidates_batch_dedup(
                    gaps,
                    matches=matches,
                )
            except Exception:
                # Best-effort optimization only; failures should never block processing.
                return

        task = asyncio.create_task(_run())
        cls._gap_candidate_prewarm_tasks[project_id] = task

        def _cleanup(_: asyncio.Task[None]) -> None:
            current = cls._gap_candidate_prewarm_tasks.get(project_id)
            if current is task:
                cls._gap_candidate_prewarm_tasks.pop(project_id, None)

        task.add_done_callback(_cleanup)

    @staticmethod
    def get_output_dir(project_id: str) -> Path:
        """Get the output directory for processed files."""
        return settings.projects_dir / project_id / "output"

    @staticmethod
    def normalize_transcription_timings(transcription: Transcription) -> None:
        """Shift non-raw aligned timings so the earliest spoken word starts at 0s."""
        min_start = None
        for scene in transcription.scenes:
            for word in scene.words:
                if min_start is None or word.start < min_start:
                    min_start = word.start

        if min_start is None:
            return

        # Only shift if there's a meaningful leading offset
        if min_start <= 0.001:
            return

        for scene in transcription.scenes:
            if not scene.words:
                continue
            for word in scene.words:
                word.start = max(0.0, word.start - min_start)
                word.end = max(0.0, word.end - min_start)
            scene.start_time = max(0.0, scene.start_time - min_start)
            scene.end_time = max(0.0, scene.end_time - min_start)

    @staticmethod
    def _get_scene_duration_from_bounds(start_time: float, end_time: float) -> float:
        return max(float(end_time) - float(start_time), 0.0)

    @staticmethod
    def _get_wav_frame_range(
        start_seconds: float,
        end_seconds: float,
        frame_rate: int,
        total_frames: int,
    ) -> tuple[int, int]:
        start_frame = max(0, int(math.floor(start_seconds * frame_rate)))
        end_frame = min(total_frames, int(math.ceil(end_seconds * frame_rate)))
        if end_frame < start_frame:
            end_frame = start_frame
        return start_frame, end_frame

    @classmethod
    def _build_source_rate(cls, source_fps: Fraction | None) -> FrameRateInfo:
        """Build a consistent source-rate descriptor for match resolution and JSX."""
        if source_fps is not None:
            return FrameRateInfo.from_fps(float(source_fps))
        return FrameRateInfo(timebase=24, ntsc=True)

    @classmethod
    async def detect_first_source_fps(cls, matches: list[SceneMatch]) -> Fraction | None:
        """Detect source FPS once from the first resolvable episode in the match list."""
        for match in matches:
            if not match.episode:
                continue
            episode_path, _ = cls._resolve_source_reference(match.episode)
            if episode_path.exists():
                return await cls.detect_video_fps(episode_path)
        return None

    @classmethod
    def resolve_scene_sources(
        cls,
        matches: list[SceneMatch],
        source_rate: FrameRateInfo,
    ) -> dict[int, ResolvedSceneSource]:
        """Resolve source clips with frame-snapped in/out once for the whole pipeline."""
        resolved: dict[int, ResolvedSceneSource] = {}

        for match in matches:
            episode = match.episode
            source_in_raw_sec = match.start_time
            source_out_raw_sec = match.end_time
            used_alternative = False

            if not episode:
                alternative = next((alt for alt in match.alternatives if alt.episode), None)
                if alternative:
                    episode = alternative.episode
                    source_in_raw_sec = alternative.start_time
                    source_out_raw_sec = alternative.end_time
                    used_alternative = True
                else:
                    continue

            source_path, clip_name = cls._resolve_source_reference(episode)
            source_in_frame = source_rate.frames_from_seconds_at_or_after(source_in_raw_sec)
            source_out_frame = source_rate.frames_from_seconds_at_or_after(source_out_raw_sec)
            if source_out_frame <= source_in_frame:
                source_out_frame = source_in_frame + 1

            source_in_seconds = source_rate.seconds_from_frames(source_in_frame)
            source_out_seconds = source_rate.seconds_from_frames(source_out_frame)

            resolved[match.scene_index] = ResolvedSceneSource(
                scene_index=match.scene_index,
                source_path=source_path,
                clip_name=clip_name,
                source_in_frame=source_in_frame,
                source_out_frame=source_out_frame,
                source_in_seconds=source_in_seconds,
                source_out_seconds=source_out_seconds,
                source_duration_seconds=max(source_out_seconds - source_in_seconds, 0.0),
                used_alternative=used_alternative,
            )

        return resolved

    @classmethod
    def build_authoritative_playback_timeline(
        cls,
        transcription: Transcription,
        resolved_scene_sources: dict[int, ResolvedSceneSource] | None = None,
    ) -> tuple[Transcription, list[PlaybackAudioSegment]]:
        """Convert aligned TTS timings into the final playback timeline.

        Non-raw scenes consume contiguous TTS audio windows using the existing
        next-non-raw-start rule. Raw scenes insert real pauses into the final
        cursor using native matched-source duration.
        """
        adjusted_ends = compute_adjusted_scene_end_times(
            scenes=transcription.scenes,
            get_scene_index=lambda s: s.scene_index,
            get_first_word_start=lambda s: s.words[0].start if s.words else None,
            get_last_word_end=lambda s: s.words[-1].end if s.words else None,
        )

        transformed_scenes: list[SceneTranscription] = []
        playback_segments: list[PlaybackAudioSegment] = []
        cursor = 0.0

        for scene in transcription.scenes:
            if scene.is_raw:
                resolved_source = (resolved_scene_sources or {}).get(scene.scene_index)
                raw_duration = (
                    resolved_source.source_duration_seconds
                    if resolved_source is not None
                    else cls._get_scene_duration_from_bounds(
                        scene.start_time,
                        scene.end_time,
                    )
                )
                start_time = cursor
                end_time = cursor + raw_duration
                transformed_scenes.append(
                    SceneTranscription(
                        scene_index=scene.scene_index,
                        text=scene.text,
                        words=[],
                        start_time=start_time,
                        end_time=end_time,
                        is_raw=True,
                    )
                )
                playback_segments.append(
                    PlaybackAudioSegment(
                        scene_index=scene.scene_index,
                        kind="silence",
                        duration=raw_duration,
                    )
                )
                cursor = end_time
                continue

            if scene.words:
                source_start = float(scene.words[0].start)
                source_end = float(
                    adjusted_ends.get(scene.scene_index, scene.words[-1].end)
                )
                if source_end <= source_start:
                    source_end = float(scene.words[-1].end)
                if source_end < source_start:
                    source_end = source_start

                duration = max(source_end - source_start, 0.0)
                shift = cursor - source_start
                shifted_words = [
                    Word(
                        text=word.text,
                        start=word.start + shift,
                        end=word.end + shift,
                        confidence=word.confidence,
                    )
                    for word in scene.words
                ]
                start_time = cursor
                end_time = cursor + duration
                transformed_scenes.append(
                    SceneTranscription(
                        scene_index=scene.scene_index,
                        text=scene.text,
                        words=shifted_words,
                        start_time=start_time,
                        end_time=end_time,
                        is_raw=False,
                    )
                )
                playback_segments.append(
                    PlaybackAudioSegment(
                        scene_index=scene.scene_index,
                        kind="audio",
                        duration=duration,
                        source_start=source_start,
                        source_end=source_end,
                    )
                )
                cursor = end_time
                continue

            # Invalid non-raw scenes should still preserve cursor continuity so
            # downstream validation can fail with a coherent timeline.
            fallback_duration = cls._get_scene_duration_from_bounds(
                scene.start_time,
                scene.end_time,
            )
            transformed_scenes.append(
                SceneTranscription(
                    scene_index=scene.scene_index,
                    text=scene.text,
                    words=[],
                    start_time=cursor,
                    end_time=cursor + fallback_duration,
                    is_raw=False,
                )
            )
            cursor += fallback_duration

        return (
            Transcription(language=transcription.language, scenes=transformed_scenes),
            playback_segments,
        )

    @classmethod
    def rebuild_tts_audio_with_playback_segments(
        cls,
        contiguous_audio_path: Path,
        output_audio_path: Path,
        segments: list[PlaybackAudioSegment],
    ) -> None:
        """Rebuild the final TTS WAV by inserting exact silences for raw scenes."""
        if not contiguous_audio_path.exists():
            raise FileNotFoundError(f"Missing contiguous TTS audio: {contiguous_audio_path}")

        output_audio_path.parent.mkdir(parents=True, exist_ok=True)

        with wave.open(str(contiguous_audio_path), "rb") as src:
            params = src.getparams()
            frame_rate = src.getframerate()
            frame_size = src.getnchannels() * src.getsampwidth()
            total_frames = src.getnframes()

            with tempfile.NamedTemporaryFile(
                delete=False,
                suffix=".wav",
                dir=str(output_audio_path.parent),
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)

            try:
                with wave.open(str(tmp_path), "wb") as dst:
                    dst.setparams(params)
                    for segment in segments:
                        if segment.kind == "audio":
                            start_frame, end_frame = cls._get_wav_frame_range(
                                segment.source_start,
                                segment.source_end,
                                frame_rate,
                                total_frames,
                            )
                            src.setpos(start_frame)
                            dst.writeframes(src.readframes(end_frame - start_frame))
                            continue

                        silence_frames = max(
                            0,
                            int(round(float(segment.duration) * frame_rate)),
                        )
                        if silence_frames <= 0:
                            continue
                        dst.writeframes(b"\x00" * (silence_frames * frame_size))

                tmp_path.replace(output_audio_path)
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink()
                raise

    @staticmethod
    def _probe_wav_duration(path: Path) -> float:
        """Return the duration in seconds of a WAV file."""
        with wave.open(str(path), "rb") as wf:
            frame_rate = wf.getframerate()
            if frame_rate <= 0:
                return 0.0
            return wf.getnframes() / float(frame_rate)

    @staticmethod
    async def detect_video_fps(video_path: Path) -> Fraction:
        """
        Detect video frame rate using ffprobe, returning as a Fraction for precision.

        Handles NTSC rates (23.976 -> 24000/1001, 29.97 -> 30000/1001, 59.94 -> 60000/1001)
        and standard rates (24/1, 30/1, 60/1).

        Args:
            video_path: Path to video file

        Returns:
            Frame rate as a Fraction (e.g., Fraction(24000, 1001) for 23.976fps)
        """
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]

        try:
            result = await run_command(cmd, timeout_seconds=ProcessingService.FFPROBE_TIMEOUT_SECONDS)
        except CommandTimeoutError:
            # Default to 24fps if detection fails
            return Fraction(24, 1)
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            return Fraction(24, 1)

        if result.returncode != 0:
            return Fraction(24, 1)

        fps_str = result.stdout.decode().strip()
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return Fraction(int(num), int(den))
        else:
            # Handle decimal format (less common)
            fps_float = float(fps_str)
            # Detect NTSC rates
            if abs(fps_float - 23.976) < 0.01:
                return Fraction(24000, 1001)
            elif abs(fps_float - 29.97) < 0.01:
                return Fraction(30000, 1001)
            elif abs(fps_float - 59.94) < 0.01:
                return Fraction(60000, 1001)
            else:
                return Fraction(int(round(fps_float)), 1)

    @classmethod
    async def run_auto_editor(
        cls,
        audio_path: Path,
        audio_output_path: Path,
        profile: AutoEditorProfile | None = None,
    ) -> bool:
        """
        Run auto-editor on TTS audio and export the edited waveform.

        Args:
            audio_path: Path to input audio file
            audio_output_path: Path for output audio file
            profile: Optional auto-editor profile (defaults to PRODUCTION)

        Returns:
            True if successful
        """
        effective_profile = profile or PRODUCTION_AUTO_EDITOR_PROFILE
        logger.debug("auto-editor GPU acceleration is not applied for audio-only runs.")
        audio_cmd = [
            "pixi", "run", "--locked", "--",
            "auto-editor",
            str(audio_path),
            *effective_profile.command_args(),
            "-o", str(audio_output_path),
        ]

        audio_result = await run_command(
            audio_cmd,
            timeout_seconds=cls.AUTO_EDITOR_TIMEOUT_SECONDS,
        )
        if audio_result.returncode != 0:
            raise RuntimeError(
                f"auto-editor (audio export) failed: {audio_result.stderr.decode()}"
            )
        return True

    @classmethod
    def generate_jsx_script(
        cls,
        project: Project,
        transcription: Transcription,
        matches: list[SceneMatch],
        source_rate: FrameRateInfo | None = None,
        resolved_scene_sources: dict[int, ResolvedSceneSource] | None = None,
        subtitle_timing_relative_path: str = "subtitles/subtitle_timings.srt",
        raw_scene_subtitle_timing_relative_path: str = "raw_scene_subtitles/text_subtitles.srt",
        raw_scene_subtitle_mogrt_relative_dir: str = "raw_scene_subtitles/text_mogrts",
        music_filename: str = "",
        music_gain_db: float = -24.0,
    ) -> str:
        """
        Generate a production-ready Premiere Pro 2025 ExtendScript (.jsx) file.

        This generates a script matching the canonical v7.7 template.
        Uses the QE (Quality Engineering) DOM for reliable:
        - 60fps vertical sequence creation via .sqpreset
        - Speed adjustments via qeItem.setSpeed()
        - 4-Track Structure: V4(Subtitles), V3(Main), V2(Border), V1(Background)
        - Scaling: V1 (183%), V3 (75% grand mode / 68% small mode)

        Frame-Perfect Timing:
        - All timeline positions are snapped to 60fps frame grid
        - Uses OTIOTimingCalculator for Fraction-based speed calculations
        - Gaps only occur when 75% speed floor is reached

        Args:
            project: Project data
            transcription: Transcription with word timings
            matches: Scene matches with source timing
            source_rate: Resolved source frame rate information. If None, defaults to 23.976fps.
            resolved_scene_sources: Pre-resolved scene source timings shared with playback rebuild.
            subtitle_timing_relative_path: Relative path to the classic subtitle timing SRT.
            raw_scene_subtitle_timing_relative_path: Relative path to the raw-scene subtitle timing SRT.
            raw_scene_subtitle_mogrt_relative_dir: Relative path to baked raw-scene subtitle MOGRT files.
            music_filename: Optional music filename placed in /sources
            music_gain_db: Music gain in dB (used only when music_filename is set)

        Returns:
            The generated JSX script content (ES3 compatible)
        """
        # Set up frame-accurate timing calculator
        # Sequence rate: 60fps (non-NTSC for TikTok)
        sequence_rate = FrameRateInfo(timebase=60, ntsc=False)
        source_rate = source_rate or cls._build_source_rate(None)
        resolved_scene_sources = resolved_scene_sources or cls.resolve_scene_sources(
            matches,
            source_rate,
        )

        calculator = OTIOTimingCalculator(
            sequence_rate=sequence_rate,
            source_rate=source_rate,
        )

        # Build scenes data with frame-perfect timing
        scenes = []
        clip_timings = []  # For continuity validation

        for scene_trans in transcription.scenes:
            if not scene_trans.words and not scene_trans.is_raw:
                continue

            resolved_source = resolved_scene_sources.get(scene_trans.scene_index)
            if not resolved_source:
                continue

            # start_time/end_time are authoritative final playback bounds when
            # processing has rebuilt the raw-aware timeline. Fall back to
            # legacy word-derived timing only for older persisted data.
            if scene_trans.end_time > scene_trans.start_time:
                timeline_start_raw = scene_trans.start_time
                timeline_end_raw = scene_trans.end_time
            else:
                timeline_start_raw = scene_trans.words[0].start
                timeline_end_raw = scene_trans.words[-1].end

            # Snap timeline positions to 60fps frame grid BEFORE speed calculation
            # This keeps speed and placement perfectly aligned to frame boundaries
            timeline_start_frames = calculator.sequence_rate.frames_from_seconds(timeline_start_raw)
            timeline_start_snapped = calculator.sequence_rate.seconds_from_frames(
                timeline_start_frames
            )

            if scene_trans.is_raw:
                timeline_end_snapped = (
                    timeline_start_snapped + resolved_source.source_duration_seconds
                )
                clip_timing = ClipTiming(
                    scene_index=scene_trans.scene_index,
                    source_path=resolved_source.source_path,
                    bundle_filename=resolved_source.clip_name,
                    source_in=calculator.seconds_to_source_time(
                        resolved_source.source_in_seconds
                    ),
                    source_out=calculator.seconds_to_source_time(
                        resolved_source.source_out_seconds
                    ),
                    source_rate=source_rate,
                    timeline_start=calculator.seconds_to_timeline_time(
                        timeline_start_snapped
                    ),
                    timeline_end=calculator.seconds_to_timeline_time(
                        timeline_end_snapped
                    ),
                    timeline_rate=sequence_rate,
                    speed_ratio=Fraction(1, 1),
                    effective_speed=Fraction(1, 1),
                    leaves_gap=False,
                )
            else:
                timeline_end_frames = calculator.sequence_rate.frames_from_seconds(
                    timeline_end_raw
                )
                timeline_end_snapped = calculator.sequence_rate.seconds_from_frames(
                    timeline_end_frames
                )

                # Calculate frame-perfect timing using OTIO
                clip_timing = calculator.calculate_clip_timing(
                    scene_index=scene_trans.scene_index,
                    source_path=resolved_source.source_path,
                    bundle_filename=resolved_source.clip_name,
                    source_in_seconds=resolved_source.source_in_seconds,
                    source_out_seconds=resolved_source.source_out_seconds,
                    timeline_start_seconds=timeline_start_snapped,
                    timeline_end_seconds=timeline_end_snapped,
                )
            clip_timings.append(clip_timing)

            # Subtitle text for this scene
            subtitle_text = scene_trans.text if scene_trans.text else ""

            # Build scene data with frame-perfect values
            # effective_speed is stored as float for JSX (Premiere expects decimal)
            scenes.append({
                "scene_index": scene_trans.scene_index,
                "start": round(timeline_start_snapped, 6),  # Frame-snapped, more precision
                "end": round(timeline_end_snapped, 6),
                "text": subtitle_text,
                "clipName": resolved_source.clip_name,
                "source_in_frame": resolved_source.source_in_frame,
                "source_out_frame": resolved_source.source_out_frame,
                "source_in": round(clip_timing.source_in_seconds, 6),
                "source_out": round(clip_timing.source_out_seconds, 6),
                "clip_duration": round(clip_timing.source_duration.to_seconds(), 4),
                "target_duration": round(clip_timing.target_duration.to_seconds(), 4),
                "speed_ratio": round(float(clip_timing.speed_ratio), 4),
                "effective_speed": round(float(clip_timing.effective_speed), 4),
                "leaves_gap": clip_timing.leaves_gap,  # True if 75% floor hit
                "used_alternative": resolved_source.used_alternative,
                "is_raw": scene_trans.is_raw,
            })

        # Validate continuity - log warnings for intentional gaps
        issues = calculator.validate_clip_continuity(clip_timings, tolerance_frames=1)
        for issue in issues:
            if issue.issue_type == "gap":
                # Check if this is an expected gap (75% floor)
                scene_a = issue.between_scenes[0]
                clip_a = next((c for c in clip_timings if c.scene_index == scene_a), None)
                if clip_a and clip_a.leaves_gap:
                    # Expected gap due to 75% floor - this is fine
                    pass
                else:
                    # Unexpected gap - log warning (would be nice to surface this)
                    import sys
                    print(f"[WARNING] Unexpected {issue.duration_seconds*1000:.1f}ms gap "
                          f"between scenes {issue.between_scenes[0]} and {issue.between_scenes[1]} "
                          f"at {issue.position_seconds:.3f}s", file=sys.stderr)

        return cls._render_jsx_from_template(
            scenes=scenes,
            source_fps_num=source_rate.rate.numerator,
            source_fps_den=source_rate.rate.denominator,
            subtitle_timing_relative_path=subtitle_timing_relative_path,
            raw_scene_subtitle_timing_relative_path=raw_scene_subtitle_timing_relative_path,
            raw_scene_subtitle_mogrt_relative_dir=raw_scene_subtitle_mogrt_relative_dir,
            music_filename=music_filename,
            music_gain_db=music_gain_db,
        )

    @classmethod
    def _replace_template_once(
        cls,
        content: str,
        pattern: str,
        replacement: str,
        *,
        flags: int = 0,
        label: str,
    ) -> str:
        updated, count = re.subn(pattern, replacement, content, count=1, flags=flags)
        if count != 1:
            raise RuntimeError(f"Failed to patch JSX template section: {label}")
        return updated

    @staticmethod
    def _escape_js_string(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "\\r")
            .replace("\n", "\\n")
        )

    @classmethod
    def _render_jsx_from_template(
        cls,
        *,
        scenes: list[dict],
        source_fps_num: int,
        source_fps_den: int,
        subtitle_timing_relative_path: str,
        raw_scene_subtitle_timing_relative_path: str,
        raw_scene_subtitle_mogrt_relative_dir: str,
        music_filename: str,
        music_gain_db: float,
    ) -> str:
        template_path = cls.PREMIERE_JSX_TEMPLATE_PATH
        if not template_path.exists():
            raise FileNotFoundError(f"Missing Premiere JSX template: {template_path}")

        content = template_path.read_text(encoding="utf-8")

        # Apply grand_mode / small-mode patches before dynamic substitutions.
        # grand_mode=True  → keep template as-is (White border 10px, V3 scale 75%)
        # grand_mode=False → ship White border 5px and use the older V3 scale of 68%
        if not settings.grand_mode_enabled:
            content = cls._replace_template_once(
                content,
                r'var BORDER_MOGRT_PATH = ASSETS_DIR \+ "/White border 10px\.mogrt";',
                'var BORDER_MOGRT_PATH = ASSETS_DIR + "/White border 5px.mogrt";',
                label="BORDER_MOGRT_PATH",
            )
            content = cls._replace_template_once(
                content,
                r"if \(!setScaleOnItem\(v3Item, 75\) && v3\)",
                "if (!setScaleOnItem(v3Item, 68) && v3)",
                label="V3_SCALE_setScaleOnItem",
            )
            content = cls._replace_template_once(
                content,
                r"setScaleAndPosition\(v3, startSec, 75\); // Main Scaled Down",
                "setScaleAndPosition(v3, startSec, 68); // Main Scaled Down",
                label="V3_SCALE_setScaleAndPosition",
            )

        scenes_json = json.dumps(scenes, indent=4, ensure_ascii=False)
        scenes_json_indented = "\n".join("  " + line for line in scenes_json.split("\n"))

        content = cls._replace_template_once(
            content,
            r"var scenes = \[[\s\S]*?\];",
            "var scenes =\n" + scenes_json_indented + ";",
            flags=re.MULTILINE,
            label="scenes",
        )
        content = cls._replace_template_once(
            content,
            r"var SOURCE_FPS_NUM = \d+;",
            f"var SOURCE_FPS_NUM = {source_fps_num};",
            label="SOURCE_FPS_NUM",
        )
        content = cls._replace_template_once(
            content,
            r"var SOURCE_FPS_DEN = \d+;",
            f"var SOURCE_FPS_DEN = {source_fps_den};",
            label="SOURCE_FPS_DEN",
        )
        content = cls._replace_template_once(
            content,
            r'var MUSIC_FILENAME = "[^"]*";',
            f'var MUSIC_FILENAME = "{cls._escape_js_string(music_filename)}";',
            label="MUSIC_FILENAME",
        )
        content = cls._replace_template_once(
            content,
            r"var MUSIC_GAIN_DB = -?\d+(?:\.\d+)?;",
            f"var MUSIC_GAIN_DB = {music_gain_db};",
            label="MUSIC_GAIN_DB",
        )
        content = cls._replace_template_once(
            content,
            r'var SUBTITLE_SRT_PATH = ROOT_DIR \+ "[^"]*";',
            (
                'var SUBTITLE_SRT_PATH = ROOT_DIR + "/'
                + cls._escape_js_string(subtitle_timing_relative_path)
                + '";'
            ),
            label="SUBTITLE_SRT_PATH",
        )
        content = cls._replace_template_once(
            content,
            r"var SUBTITLE_MOGRT_DIR = [^;]+;",
            'var SUBTITLE_MOGRT_DIR = ROOT_DIR + "/subtitles";',
            label="SUBTITLE_MOGRT_DIR",
        )
        content = cls._replace_template_once(
            content,
            r"var RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR = [^;]+;",
            (
                'var RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR = ROOT_DIR + "/'
                + cls._escape_js_string(raw_scene_subtitle_mogrt_relative_dir)
                + '";'
            ),
            label="RAW_SCENE_TEXT_SUBTITLE_MOGRT_DIR",
        )
        content = cls._replace_template_once(
            content,
            r'var RAW_SCENE_TEXT_SUBTITLE_SRT_PATH = ROOT_DIR \+ "[^"]*";',
            (
                'var RAW_SCENE_TEXT_SUBTITLE_SRT_PATH = ROOT_DIR + "/'
                + cls._escape_js_string(raw_scene_subtitle_timing_relative_path)
                + '";'
            ),
            label="RAW_SCENE_TEXT_SUBTITLE_SRT_PATH",
        )
        return content

    @classmethod
    def _normalize_external_subtitle_text(cls, raw_text: str) -> str:
        text = str(raw_text or "").replace("\r", "\n")
        text = html.unescape(text)
        text = re.sub(r"\{[^{}]*\}", "", text)
        text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
        lines = [
            re.sub(r"\s+", " ", line.replace("\xa0", " ")).strip()
            for line in text.split("\n")
        ]
        lines = [line for line in lines if line]
        return " ".join(lines)

    @staticmethod
    def _format_srt_time(seconds: float) -> str:
        safe_seconds = max(0.0, float(seconds))
        hours = int(safe_seconds // 3600)
        minutes = int((safe_seconds % 3600) // 60)
        secs = int(safe_seconds % 60)
        millis = int((safe_seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    @classmethod
    def render_srt_entries(
        cls,
        entries: list[SrtEntry],
    ) -> str:
        if not entries:
            return ""

        sorted_entries = sorted(
            (
                entry
                for entry in entries
                if entry.text.strip() and entry.end > entry.start
            ),
            key=lambda entry: (entry.start, entry.end, entry.text.lower()),
        )
        blocks = [
            f"{idx}\n{cls._format_srt_time(entry.start)} --> {cls._format_srt_time(entry.end)}\n{entry.text}\n"
            for idx, entry in enumerate(sorted_entries, start=1)
        ]
        return "\n".join(blocks)

    @classmethod
    def generate_srt_entries(
        cls,
        transcription: Transcription,
        language: str = "fr",
    ) -> list[SrtEntry]:
        """Generate aggressive single-line subtitle entries from aligned TTS words."""
        entries: list[SrtEntry] = []

        raw_break_scene_indices: set[int] = set()
        next_non_raw_index: int | None = None
        saw_raw_since_last_non_raw = False
        for idx in range(len(transcription.scenes) - 1, -1, -1):
            scene_trans = transcription.scenes[idx]
            if scene_trans.is_raw:
                saw_raw_since_last_non_raw = True
                continue
            if scene_trans.words and next_non_raw_index is not None and saw_raw_since_last_non_raw:
                raw_break_scene_indices.add(scene_trans.scene_index)
            if scene_trans.words:
                next_non_raw_index = idx
                saw_raw_since_last_non_raw = False

        all_words: list[dict[str, object]] = []
        for scene_trans in transcription.scenes:
            if scene_trans.is_raw or not scene_trans.words:
                continue
            force_break_after_scene = scene_trans.scene_index in raw_break_scene_indices
            last_word_index = len(scene_trans.words) - 1
            for idx, word in enumerate(scene_trans.words):
                all_words.append(
                    {
                        "word": word,
                        "force_break_after": force_break_after_scene and idx == last_word_index,
                    }
                )

        if not all_words:
            return entries

        i = 0
        while i < len(all_words):
            current_block = []
            current_block_indices: list[int] = []
            current_len = 0

            while i < len(all_words):
                if (
                    current_block_indices
                    and all_words[current_block_indices[-1]]["force_break_after"]
                ):
                    break
                word_entry = all_words[i]
                word = word_entry["word"]
                word_text = strip_punctuation(word.text)
                if not word_text:
                    i += 1
                    continue

                new_len = current_len + len(word_text) + (1 if current_block else 0)
                word_count = len(current_block) + 1

                can_add = False
                if word_count <= 2:
                    can_add = new_len <= 20
                elif word_count == 3 and new_len < 12:
                    can_add = True

                if not current_block and len(word_text) > 20:
                    can_add = True

                if can_add:
                    word_copy = Word(
                        text=word_text,
                        start=word.start,
                        end=word.end,
                        confidence=word.confidence,
                    )
                    current_block.append(word_copy)
                    current_block_indices.append(i)
                    current_len = new_len
                    i += 1
                    if has_sentence_ending(word.text):
                        break
                else:
                    if len(current_block) >= 2:
                        last_word_text = current_block[-1].text
                        if is_clause_starter(last_word_text, language):
                            rewind_index = current_block_indices.pop()
                            current_block.pop()
                            i = rewind_index
                            current_len = sum(len(w.text) for w in current_block)
                            if len(current_block) > 1:
                                current_len += len(current_block) - 1
                    break

            if current_block:
                next_entry = all_words[i] if i < len(all_words) else None
                next_word = next_entry["word"] if next_entry else None
                force_break_after = bool(
                    all_words[current_block_indices[-1]]["force_break_after"]
                )

                if next_word and not force_break_after:
                    gap = next_word.start - current_block[-1].end
                    if gap > SRT_OBVIOUS_SILENCE_GAP_SEC:
                        end_time = current_block[-1].end
                    else:
                        end_time = next_word.start
                else:
                    end_time = current_block[-1].end

                if next_word and not force_break_after and len(current_block) == 1:
                    block_word = current_block[0]
                    block_duration = end_time - block_word.start
                    gap_to_next = next_word.start - block_word.end
                    if (
                        block_word.confidence <= LOW_CONF_THRESHOLD
                        and block_duration < LOW_CONF_SINGLE_WORD_MIN_DURATION_SEC
                        and gap_to_next > MIN_GAP_FOR_EXTENSION_SEC
                    ):
                        target_end = block_word.start + LOW_CONF_SINGLE_WORD_MIN_DURATION_SEC
                        max_allowed_end = next_word.start - NEXT_WORD_SAFETY_SEC
                        if max_allowed_end > end_time:
                            end_time = min(max(target_end, end_time), max_allowed_end)

                entries.append(
                    SrtEntry(
                        start=current_block[0].start,
                        end=end_time,
                        text=" ".join(w.text for w in current_block),
                    )
                )

        return entries

    @classmethod
    async def _build_raw_scene_image_render_plan(
        cls,
        project: Project,
        transcription: Transcription,
        resolved_scene_sources: dict[int, ResolvedSceneSource],
    ) -> dict[Path, dict[int, list[tuple[float, float]]]]:
        raw_scenes = [scene for scene in transcription.scenes if scene.is_raw]
        if not raw_scenes:
            return {}

        target_language = AnimeLibraryService.normalize_stream_language(
            project.output_language or transcription.language
        )
        probe_cache: dict[str, Any] = {}
        render_plan: dict[Path, dict[int, list[tuple[float, float]]]] = {}

        for scene in raw_scenes:
            resolved_source = resolved_scene_sources.get(scene.scene_index)
            if resolved_source is None:
                continue

            sidecar_source_path = AnimeLibraryService.resolve_subtitle_sidecar_source_path(
                resolved_source.source_path
            )
            if sidecar_source_path is not None:
                sidecar_entries = AnimeLibraryService.load_subtitle_sidecar_entries(
                    sidecar_source_path
                )
                selected_entry = AnimeLibraryService.select_preferred_subtitle_entry(
                    sidecar_entries,
                    target_language=target_language,
                )
                if selected_entry is None or selected_entry.kind != "image":
                    continue

                render_plan.setdefault(resolved_source.source_path, {}).setdefault(
                    selected_entry.stream_position,
                    [],
                ).append(
                    (
                        resolved_source.source_in_seconds,
                        resolved_source.source_out_seconds,
                    )
                )
                continue

            source_key = str(resolved_source.source_path.resolve())
            probe = probe_cache.get(source_key)
            if probe is None:
                probe = await asyncio.to_thread(
                    AnimeLibraryService.probe_source_media_sync,
                    resolved_source.source_path,
                )
                probe_cache[source_key] = probe
            if probe is None or not probe.subtitle_streams:
                continue

            synthetic_entries = [
                SubtitleSidecarEntry(
                    stream_index=stream.index,
                    stream_position=stream.stream_position,
                    codec_name=stream.codec_name,
                    language=stream.language,
                    raw_language=stream.raw_language,
                    title=stream.title,
                    kind=AnimeLibraryService._subtitle_kind_for_codec(stream.codec_name),
                    asset_filename="planned",
                )
                for stream in probe.subtitle_streams
            ]
            selected_entry = AnimeLibraryService.select_preferred_subtitle_entry(
                synthetic_entries,
                target_language=target_language,
            )
            if selected_entry is None or selected_entry.kind != "image":
                continue

            render_plan.setdefault(resolved_source.source_path, {}).setdefault(
                selected_entry.stream_position,
                [],
            ).append(
                (
                    resolved_source.source_in_seconds,
                    resolved_source.source_out_seconds,
                )
            )

        return render_plan

    @classmethod
    async def _collect_raw_scene_source_subtitles(
        cls,
        project: Project,
        transcription: Transcription,
        resolved_scene_sources: dict[int, ResolvedSceneSource],
        output_dir: Path,
    ) -> tuple[list[SrtEntry], list[RawSceneSubtitleImageEntry]]:
        raw_output_dir = output_dir / "raw_scene_subtitles"
        if raw_output_dir.exists():
            shutil.rmtree(raw_output_dir, ignore_errors=True)

        raw_scenes = [scene for scene in transcription.scenes if scene.is_raw]
        if not raw_scenes:
            return [], []

        target_language = AnimeLibraryService.normalize_stream_language(
            project.output_language or transcription.language
        )
        text_entries: list[SrtEntry] = []
        image_entries: list[RawSceneSubtitleImageEntry] = []
        parsed_text_cache: dict[str, list[Any]] = {}
        parsed_cue_cache: dict[str, list[dict[str, Any]]] = {}
        rendered_cue_cache: dict[tuple[str, int], Path | None] = {}

        for scene in raw_scenes:
            resolved_source = resolved_scene_sources.get(scene.scene_index)
            if resolved_source is None:
                continue
            sidecar_entries = AnimeLibraryService.load_subtitle_sidecar_entries(
                resolved_source.source_path
            )
            selected_entry = AnimeLibraryService.select_preferred_subtitle_entry(
                sidecar_entries,
                target_language=target_language,
            )
            if selected_entry is None:
                continue

            asset_path = AnimeLibraryService.get_subtitle_sidecar_asset_path(
                resolved_source.source_path,
                selected_entry,
            )
            if asset_path is None or not asset_path.exists():
                continue

            source_window_start = resolved_source.source_in_seconds
            source_window_end = resolved_source.source_out_seconds
            timeline_scene_start = scene.start_time

            if selected_entry.kind == "text":
                cache_key = str(asset_path.resolve())
                parsed_entries = parsed_text_cache.get(cache_key)
                if parsed_entries is None:
                    parsed_entries = PremiereSubtitleBakerService.parse_srt_entries(asset_path)
                    parsed_text_cache[cache_key] = parsed_entries
                for entry in parsed_entries:
                    clipped_start = max(entry.start, source_window_start)
                    clipped_end = min(entry.end, source_window_end)
                    if clipped_end <= clipped_start:
                        continue
                    normalized_text = cls._normalize_external_subtitle_text(entry.text)
                    if not normalized_text:
                        continue
                    text_entries.append(
                        SrtEntry(
                            start=timeline_scene_start + (clipped_start - source_window_start),
                            end=timeline_scene_start + (clipped_end - source_window_start),
                            text=normalized_text,
                        )
                    )
                continue

            cue_manifest_path = AnimeLibraryService.get_subtitle_sidecar_cue_manifest_path(
                resolved_source.source_path,
                selected_entry,
            )
            if cue_manifest_path is None or not cue_manifest_path.exists():
                continue
            cache_key = str(cue_manifest_path.resolve())
            cue_entries = parsed_cue_cache.get(cache_key)
            if cue_entries is None:
                try:
                    cue_payload = json.loads(cue_manifest_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    cue_payload = {}
                raw_cues = cue_payload.get("cues", []) if isinstance(cue_payload, dict) else []
                cue_entries = [cue for cue in raw_cues if isinstance(cue, dict)]
                parsed_cue_cache[cache_key] = cue_entries

            for cue_idx, cue in enumerate(cue_entries, start=1):
                try:
                    cue_index = int(cue.get("cue_index", cue_idx))
                except (TypeError, ValueError):
                    cue_index = cue_idx
                try:
                    cue_start = float(cue.get("start"))
                    cue_end = float(cue.get("end"))
                except (TypeError, ValueError):
                    continue
                clipped_start = max(cue_start, source_window_start)
                clipped_end = min(cue_end, source_window_end)
                if clipped_end <= clipped_start:
                    continue
                cue_asset_filename = str(cue.get("asset_filename", "")).strip()
                source_cue_asset_path: Path | None = None
                if cue_asset_filename:
                    candidate_asset_path = cue_manifest_path.parent / cue_asset_filename
                    if candidate_asset_path.exists():
                        source_cue_asset_path = candidate_asset_path
                if source_cue_asset_path is None:
                    cache_entry_key = (cache_key, cue_index)
                    if cache_entry_key not in rendered_cue_cache:
                        rendered_cue_cache[cache_entry_key] = (
                            await AnimeLibraryService.ensure_subtitle_sidecar_cue_asset(
                                resolved_source.source_path,
                                selected_entry,
                                cue_index=cue_index,
                                cue_start=cue_start,
                                cue_end=cue_end,
                            )
                        )
                    source_cue_asset_path = rendered_cue_cache[cache_entry_key]
                if source_cue_asset_path is None or not source_cue_asset_path.exists():
                    continue
                raw_output_dir.mkdir(parents=True, exist_ok=True)
                target_name = (
                    f"scene_{scene.scene_index:04d}_cue_{cue_index:04d}{source_cue_asset_path.suffix.lower()}"
                )
                target_path = raw_output_dir / target_name
                if not target_path.exists():
                    shutil.copy2(source_cue_asset_path, target_path)
                image_entries.append(
                    RawSceneSubtitleImageEntry(
                        scene_index=scene.scene_index,
                        start=timeline_scene_start + (clipped_start - source_window_start),
                        end=timeline_scene_start + (clipped_end - source_window_start),
                        relative_asset_path=f"raw_scene_subtitles/{target_name}",
                    )
                )

        if image_entries:
            manifest_path = raw_output_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "scene_index": entry.scene_index,
                                "start": entry.start,
                                "end": entry.end,
                                "relative_asset_path": entry.relative_asset_path,
                            }
                            for entry in sorted(
                                image_entries,
                                key=lambda entry: (entry.start, entry.end, entry.relative_asset_path),
                            )
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        return text_entries, image_entries

    @classmethod
    def generate_srt(
        cls,
        transcription: Transcription,
        language: str = "fr",
        extra_entries: list[SrtEntry] | None = None,
    ) -> str:
        merged_entries = cls.generate_srt_entries(transcription, language=language)
        if extra_entries:
            merged_entries.extend(extra_entries)
        return cls.render_srt_entries(merged_entries)

    @staticmethod
    def _processing_asset_path(asset_name: str) -> Path:
        return Path(__file__).resolve().parents[3] / "assets" / asset_name

    @classmethod
    def _bake_subtitle_mogrt_set(
        cls,
        *,
        template_mogrt_path: Path,
        srt_content: str,
        srt_path: Path,
        output_dir: Path,
        label: str,
    ) -> None:
        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        if srt_path.exists():
            srt_path.unlink()

        if not srt_content.strip():
            return

        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(srt_content, encoding="utf-8")
        output_dir.mkdir(parents=True, exist_ok=True)

        bake_result = PremiereSubtitleBakerService.bake_from_srt(
            template_mogrt_path=template_mogrt_path,
            srt_path=srt_path,
            output_dir=output_dir,
        )
        if bake_result.generated_count != bake_result.entries_count:
            raise RuntimeError(
                f"{label} MOGRT bake mismatch: "
                f"{bake_result.generated_count} generated for {bake_result.entries_count} SRT entries"
            )
        if bake_result.entries_count <= 0:
            raise RuntimeError(f"{label} MOGRT bake produced no entries.")

    @staticmethod
    def _create_srt_block_aggressive(
        index: int,
        words: list,
        end_time: float,
    ) -> str:
        """Create a single SRT block for aggressive/Hormozi style (single line only)."""
        if not words:
            return ""

        start_time = words[0].start

        # Single line text - no line breaks
        text = " ".join(w.text for w in words)

        return f"{index}\n{ProcessingService._format_srt_time(start_time)} --> {ProcessingService._format_srt_time(end_time)}\n{text}\n"

    @classmethod
    def generate_subtitle_style_preset(cls) -> str:
        """
        Generate a Premiere Pro caption style preset (.prfpset).

        This creates a centered subtitle style optimized for TikTok/short-form video.
        The preset can be imported into Premiere Pro's Essential Graphics panel.

        Returns:
            JSON content for the .prfpset file
        """
        # Premiere Pro caption style preset format (JSON-based)
        preset = {
            "name": "ATR TikTok Subtitles",
            "fontFamily": "Arial",
            "fontStyle": "Bold",
            "fontSize": 72,
            "fontColor": "#FFFFFF",
            "backgroundColor": "#000000",
            "backgroundOpacity": 0.75,
            "textAlignment": "center",
            "verticalPosition": 0.85,  # 85% from top (near bottom)
            "horizontalPosition": 0.5,  # Centered
            "strokeColor": "#000000",
            "strokeWidth": 3,
            "shadowEnabled": True,
            "shadowColor": "#000000",
            "shadowOpacity": 0.5,
            "shadowDistance": 2,
            "shadowAngle": 135,
            "lineSpacing": 1.2,
            "maxWidth": 0.9,  # 90% of frame width
        }
        return json.dumps(preset, indent=2)

    @classmethod
    def generate_subtitle_style_guide(cls) -> str:
        """
        Generate a text guide for applying subtitle styles in Premiere Pro.

        Returns:
            Markdown-formatted style guide
        """
        return """# Subtitle Style Guide for Premiere Pro

## Recommended Settings for TikTok-Style Subtitles

### Font Settings
- **Font Family**: Arial or Montserrat (Bold)
- **Font Size**: 72-90px (depending on text length)
- **Font Color**: White (#FFFFFF)

### Background/Box
- **Background Color**: Black (#000000)
- **Background Opacity**: 75%
- **Padding**: 10px horizontal, 5px vertical

### Text Style
- **Stroke**: Black, 3px width
- **Shadow**: Black, 50% opacity, 2px distance, 135° angle
- **Text Alignment**: Center

### Position
- **Vertical Position**: 85% from top (near bottom of frame)
- **Horizontal Position**: Center
- **Max Width**: 90% of frame width

## How to Apply in Premiere Pro

### Method 1: Essential Graphics Panel
1. Select your captions on the timeline
2. Open Window > Essential Graphics
3. Use the text formatting options to match the settings above
4. Save as a preset: Click ≡ menu > Save Style Preset

### Method 2: Import Captions + Style
1. Import the SRT file: File > Import
2. Drag the SRT to the timeline
3. Select all caption clips
4. Open Essential Graphics and apply formatting

### Method 3: Auto-Style with MOGRT (Advanced)
1. Create a custom MOGRT template with these settings
2. Apply to caption track via Essential Graphics

## Quick Keyboard Shortcuts
- Ctrl+Shift+K: Add marker
- Ctrl+D: Default transition
- Ctrl+M: Export Media
"""

    @classmethod
    def check_has_saved_state(cls, project_id: str) -> bool:
        """Check if there's saved processing state (interrupted by gaps)."""
        output_dir = cls.get_output_dir(project_id)
        state_path = output_dir / "processing_state.json"
        return state_path.exists()

    @classmethod
    def check_gaps_resolved(cls, project_id: str) -> bool:
        """Check if gap resolution has been completed."""
        project_dir = settings.projects_dir / project_id
        return (project_dir / "gaps_resolved.flag").exists()

    @classmethod
    def check_duration_warning_acknowledged(cls, project_id: str) -> bool:
        """Check if the duration warning has been acknowledged by the user."""
        project_dir = settings.projects_dir / project_id
        return (project_dir / "duration_warning_acknowledged.flag").exists()

    @classmethod
    def clear_processing_state(cls, project_id: str) -> None:
        """Clear saved processing state after completion."""
        output_dir = cls.get_output_dir(project_id)
        state_path = output_dir / "processing_state.json"
        if state_path.exists():
            state_path.unlink()

        project_dir = settings.projects_dir / project_id
        for flag_name in ("gaps_resolved.flag", "duration_warning_acknowledged.flag"):
            flag_path = project_dir / flag_name
            if flag_path.exists():
                flag_path.unlink()

    @classmethod
    async def process(
        cls,
        project: Project,
        new_script: dict,
        audio_path: Path,
        matches: list[SceneMatch],
        *,
        reference_transcription: Transcription,
    ) -> AsyncIterator[ProcessingProgress]:
        """
        Run the full processing pipeline.

        Generates core project output files:
        - JSX automation script (v7.1 format matching working_script.jsx)
        - Processed TTS audio with auto-editor silence removal
        - SRT subtitles

        Bundling and cloud export are handled by export routes/services.

        Track layout (created by JSX script):
        - V4: Reserved for subtitles
        - V3: Main video (Scale 75% grand mode / 68% small mode)
        - V2: White border MOGRT (10px grand mode / 5px small mode)
        - V1: Background (Scale 183%)
        - A1: Original anime audio (MUTED)
        - A2: TTS audio with inserted raw-scene pauses
        - A3: Music bed
        - A4: Raw-scene source audio (active)

        Args:
            project: Project data
            new_script: New restructured script JSON
            audio_path: Path to uploaded TTS audio
            matches: Scene matches with source timings
            reference_transcription: Final validated transcription used as the
                raw-scene timing/source-of-truth reference

        Yields:
            ProcessingProgress updates
        """
        output_dir = cls.get_output_dir(project.id)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Check if we're resuming after gap resolution or duration warning
        resuming_after_gaps = cls.check_has_saved_state(project.id) and cls.check_gaps_resolved(project.id)
        resuming_after_duration_warning = False
        if not resuming_after_gaps and cls.check_has_saved_state(project.id):
            _peek_state = json.loads((output_dir / "processing_state.json").read_text())
            if (
                _peek_state.get("step") == "duration_warning"
                and cls.check_duration_warning_acknowledged(project.id)
            ):
                resuming_after_duration_warning = True
        source_fps = await cls.detect_first_source_fps(matches)
        source_rate = cls._build_source_rate(source_fps)
        playback_scene_sources = cls.resolve_scene_sources(matches, source_rate)

        if resuming_after_gaps:
            # Load saved state and skip to JSX generation
            state_path = output_dir / "processing_state.json"
            state = json.loads(state_path.read_text())
            edited_audio_path = Path(state["edited_audio_path"])
            transcription_timing_path = Path(state["transcription_path"])

            # Load transcription from saved state
            transcription_data = json.loads(transcription_timing_path.read_text())

            # Reconstruct Transcription object
            from ..models import Transcription
            from ..models.transcription import SceneTranscription, Word
            new_transcription = Transcription(
                language=transcription_data.get("language", "fr"),  # Default to French for backwards compat
                scenes=[
                    SceneTranscription(
                        scene_index=s["scene_index"],
                        text=s["text"],
                        words=[Word(**w) for w in s["words"]],
                        start_time=float(s.get("start_time", s["words"][0]["start"] if s["words"] else 0.0)),
                        end_time=float(s.get("end_time", s["words"][-1]["end"] if s["words"] else 0.0)),
                        is_raw=bool(s.get("is_raw", False)),
                    )
                    for s in transcription_data["scenes"]
                ],
            )
        elif resuming_after_duration_warning:
            # Load saved state — skip auto-editor, resume from transcription
            state_path = output_dir / "processing_state.json"
            state = json.loads(state_path.read_text())
            edited_audio_path = Path(state["edited_audio_path"])
            pa_state = state["prepared_audio"]
            prepared_audio = PreparedAlignmentAudio(
                mode=pa_state["mode"],
                edited_audio_path=Path(pa_state["edited_audio_path"]),
                segment_audio_paths=[Path(p) for p in pa_state["segment_audio_paths"]],
                manifest=pa_state["manifest"],
            )
            yield ProcessingProgress(
                "processing",
                "auto_editor",
                0.2,
                "Auto-editor complete (resuming after duration warning)...",
            )
        else:
            yield ProcessingProgress(
                "processing",
                "auto_editor",
                0.1,
                "Running auto-editor on TTS audio...",
            )

        try:
            if not resuming_after_gaps:
                if not resuming_after_duration_warning:
                    # Resolve auto-editor profile from voice_key if available
                    auto_editor_profile = PRODUCTION_AUTO_EDITOR_PROFILE
                    if project.voice_key:
                        try:
                            auto_editor_profile = VoiceConfigService.get_auto_editor_profile(project.voice_key)
                        except ValueError:
                            pass  # voice no longer in config, use default

                    # Step 1: Auto-editor (generates edited audio with silences removed)
                    edited_audio_path = output_dir / "tts_edited.wav"
                    upload_manifest = ForcedAlignmentService.load_upload_manifest(project.id)
                    if upload_manifest and upload_manifest.get("mode") == "audio_parts":
                        from functools import partial
                        runner = partial(cls.run_auto_editor, profile=auto_editor_profile)
                        prepared_audio = await ForcedAlignmentService.prepare_audio_from_parts(
                            project_id=project.id,
                            output_dir=output_dir,
                            tts_speed=float(project.tts_speed or 1.0),
                            auto_editor_runner=runner,
                        )
                        edited_audio_path = prepared_audio.edited_audio_path
                    else:
                        await cls.run_auto_editor(audio_path, edited_audio_path, profile=auto_editor_profile)
                        prepared_audio = PreparedAlignmentAudio(
                            mode="single_audio",
                            edited_audio_path=edited_audio_path,
                            segment_audio_paths=[],
                            manifest=ForcedAlignmentService.build_single_audio_manifest(
                                script_payload=new_script,
                            ),
                        )

                    # Duration check — warn if total estimated duration < 61s
                    TIKTOK_MIN_DURATION_SECONDS = 61.0
                    tts_duration = cls._probe_wav_duration(edited_audio_path)
                    raw_scenes_duration = sum(
                        resolved.source_duration_seconds
                        for scene in reference_transcription.scenes
                        if scene.is_raw
                        for resolved in [playback_scene_sources.get(scene.scene_index)]
                        if resolved is not None
                    )
                    total_estimated_duration = tts_duration + raw_scenes_duration

                    if total_estimated_duration < TIKTOK_MIN_DURATION_SECONDS:
                        # Save state so we can resume from transcription without re-running auto-editor
                        processing_state = {
                            "step": "duration_warning",
                            "edited_audio_path": str(edited_audio_path),
                            "prepared_audio": {
                                "mode": prepared_audio.mode,
                                "edited_audio_path": str(prepared_audio.edited_audio_path),
                                "segment_audio_paths": [str(p) for p in prepared_audio.segment_audio_paths],
                                "manifest": prepared_audio.manifest,
                            },
                        }
                        state_path = output_dir / "processing_state.json"
                        state_path.write_text(json.dumps(processing_state, indent=2))

                        yield ProcessingProgress(
                            "duration_warning",
                            "auto_editor",
                            0.2,
                            f"Audio duration ({total_estimated_duration:.0f}s) is under 1min01",
                            duration_warning=True,
                            audio_duration_seconds=round(tts_duration, 2),
                            raw_scenes_duration_seconds=round(raw_scenes_duration, 2),
                            total_duration_seconds=round(total_estimated_duration, 2),
                        )
                        return  # Pause — frontend will show warning modal

                yield ProcessingProgress(
                    "processing",
                    "transcription",
                    0.3,
                    "Extracting word timings from audio...",
                )

                # Step 2: Run forced alignment in a worker thread
                loop = asyncio.get_event_loop()
                try:
                    alignment_result = await loop.run_in_executor(
                        None,
                        lambda: ForcedAlignmentService.align_known_script(
                            project_id=project.id,
                            script_payload=new_script,
                            reference_transcription=reference_transcription,
                            prepared_audio=prepared_audio,
                            output_dir=output_dir,
                        ),
                    )
                    new_transcription = alignment_result.transcription
                    cls.normalize_transcription_timings(new_transcription)
                    new_transcription, playback_segments = cls.build_authoritative_playback_timeline(
                        new_transcription,
                        playback_scene_sources,
                    )
                    cls.rebuild_tts_audio_with_playback_segments(
                        edited_audio_path,
                        edited_audio_path,
                        playback_segments,
                    )
                finally:
                    # Always attempt model unload, including failure paths.
                    TranscriberService.unload_models()

                # Save transcription for gap detection
                transcription_timing_path = output_dir / "transcription_timing.json"
                transcription_data = {
                    "language": new_transcription.language,
                    "scenes": [
                        {
                            "scene_index": s.scene_index,
                            "text": s.text,
                            "words": [{"text": w.text, "start": w.start, "end": w.end, "confidence": w.confidence} for w in s.words],
                            "start_time": s.start_time,
                            "end_time": s.end_time,
                            "is_raw": s.is_raw,
                        }
                        for s in new_transcription.scenes
                    ]
                }
                transcription_timing_path.write_text(json.dumps(transcription_data, indent=2))

                # Also save to project root for gap resolution endpoint
                project_dir = settings.projects_dir / project.id
                gap_transcription_path = project_dir / "gap_detection_transcription.json"
                gap_transcription_path.write_text(json.dumps(transcription_data, indent=2))

                yield ProcessingProgress(
                    "processing",
                    "gap_detection",
                    0.35,
                    "Checking for clips with gaps...",
                )

                # Step 2b: Check for gaps (scenes that hit 75% speed floor)
                # Skip this check if gaps were already resolved in a previous run
                gaps_already_resolved = cls.check_gaps_resolved(project.id)

                if gaps_already_resolved:
                    # User already resolved/skipped gaps in a previous run
                    # Don't ask them to do it again
                    gaps = []
                else:
                    gaps = GapResolutionService.calculate_gaps(matches, transcription_data["scenes"])

                if gaps:
                    total_gap_duration = sum(g.gap_duration for g in gaps)

                    if settings.gaps_full_auto_enabled:
                        # Resolve gaps inline without frontend round trip
                        yield ProcessingProgress(
                            "processing",
                            "gap_detection",
                            0.36,
                            f"Auto-resolving {len(gaps)} gap(s)...",
                        )

                        # Backup current matches before modification
                        matches_backup_path = project_dir / "matches_before_gaps.json"
                        if not matches_backup_path.exists():
                            from ..models import MatchList
                            matches_path = project_dir / "matches.json"
                            if matches_path.exists():
                                shutil.copy(matches_path, matches_backup_path)

                        candidates_by_scene = await GapResolutionService.generate_candidates_batch_dedup(
                            gaps,
                            matches=matches,
                        )
                        selection_result = await GapResolutionService.select_autofill_candidates_overlap_aware(
                            matches=matches,
                            gaps=gaps,
                            candidates_by_scene=candidates_by_scene,
                        )

                        for gap in gaps:
                            best = selection_result.selected_candidates_by_scene.get(gap.scene_index)
                            if best is None:
                                candidates = candidates_by_scene.get(gap.scene_index, [])
                                if candidates:
                                    best = candidates[0]
                            if best:
                                for match in matches:
                                    if match.scene_index == gap.scene_index:
                                        match.start_time = best.start_time
                                        match.end_time = best.end_time
                                        match.speed_ratio = float(best.effective_speed)
                                        match.confirmed = True
                                        break

                        # Save updated matches
                        from ..models import MatchList
                        ProjectService.save_matches(project.id, MatchList(matches=matches))
                        (project_dir / "gaps_resolved.flag").touch()

                        yield ProcessingProgress(
                            "processing",
                            "gap_detection",
                            0.4,
                            f"Auto-resolved {len(gaps)} gap(s)",
                        )
                        # Continue to JSX generation (don't return)
                    else:
                        # Manual flow: pause processing for user to resolve
                        # Prewarm scene-cut/fps analysis in background so /gaps loads faster.
                        cls.schedule_gap_candidate_prewarm(project.id, gaps, matches)

                        # Backup current matches before gap resolution modifies them
                        matches_backup_path = project_dir / "matches_before_gaps.json"
                        if not matches_backup_path.exists():
                            matches_path = project_dir / "matches.json"
                            if matches_path.exists():
                                shutil.copy(matches_path, matches_backup_path)

                        # Save current processing state so we can resume later
                        processing_state = {
                            "step": "gap_detection",
                            "edited_audio_path": str(edited_audio_path),
                            "transcription_path": str(transcription_timing_path),
                        }
                        state_path = output_dir / "processing_state.json"
                        state_path.write_text(json.dumps(processing_state, indent=2))

                        yield ProcessingProgress(
                            "gaps_detected",
                            "gap_detection",
                            0.4,
                            f"Found {len(gaps)} clip(s) with gaps that need resolution",
                            gaps_detected=True,
                            gap_count=len(gaps),
                            total_gap_duration=total_gap_duration,
                        )
                        return  # Stop processing here - frontend will redirect to gap resolution

            # Step 3: Normalize only the source episodes used by final matches.
            playback_scene_sources = cls.resolve_scene_sources(matches, source_rate)
            raw_scene_image_render_plan = await cls._build_raw_scene_image_render_plan(
                project,
                new_transcription,
                playback_scene_sources,
            )
            source_groups = await cls._collect_required_source_groups(matches)
            total_source_groups = len(source_groups)
            matches_updated = False

            if total_source_groups == 0:
                yield ProcessingProgress(
                    "processing",
                    "source_normalization",
                    0.45,
                    "No source episodes require normalization.",
                )
            else:
                for idx, (resolved_source_path, source_matches) in enumerate(source_groups, start=1):
                    before_fraction = (idx - 1) / total_source_groups
                    after_fraction = idx / total_source_groups
                    yield ProcessingProgress(
                        "processing",
                        "source_normalization",
                        0.4 + 0.09 * before_fraction,
                        f"Checking source compatibility ({idx}/{total_source_groups}): "
                        f"{resolved_source_path.name}",
                    )
                    normalization_result = await AnimeLibraryService.normalize_source_for_processing(
                        resolved_source_path,
                        subtitle_image_render_windows=raw_scene_image_render_plan.get(
                            resolved_source_path,
                        ),
                    )
                    if cls._update_absolute_match_episode_hints(
                        source_matches,
                        old_source_path=resolved_source_path,
                        new_source_path=normalization_result.normalized_path,
                    ):
                        matches_updated = True
                    yield ProcessingProgress(
                        "processing",
                        "source_normalization",
                        0.4 + 0.09 * after_fraction,
                        cls._format_source_normalization_message(
                            current=idx,
                            total=total_source_groups,
                            result=normalization_result,
                        ),
                    )

            if matches_updated:
                ProjectService.save_matches(project.id, MatchList(matches=matches))

            yield ProcessingProgress(
                "processing",
                "jsx_generation",
                0.5,
                (
                    "Resuming - Generating Premiere Pro JSX script..."
                    if resuming_after_gaps
                    else "Generating Premiere Pro JSX script..."
                ),
            )

            alignment_issues = ForcedAlignmentService.validate_transcription_basics(new_transcription)
            if alignment_issues:
                existing_report = ForcedAlignmentService.report_path(output_dir)
                if existing_report.exists():
                    try:
                        report_payload = json.loads(existing_report.read_text(encoding="utf-8"))
                    except Exception:
                        report_payload = {}
                else:
                    report_payload = {}
                report_payload = {
                    **report_payload,
                    "status": "error",
                    "mode": report_payload.get("mode") or "unknown",
                    "global_issues": [
                        *list(report_payload.get("global_issues") or []),
                        *alignment_issues,
                    ],
                }
                ForcedAlignmentService.write_alignment_report(output_dir, report_payload)
                raise RuntimeError(
                    "Aligned transcription failed validation: "
                    + "; ".join(alignment_issues[:5])
                )

            srt_filename = ExportService.subtitle_filename(project)
            classic_subtitle_timing_relative_path = (
                cls.CLASSIC_SUBTITLE_TIMING_RELATIVE_PATH
            )
            raw_scene_subtitle_timing_relative_path = (
                cls.RAW_SCENE_TEXT_SUBTITLE_TIMING_RELATIVE_PATH
            )
            raw_scene_subtitle_mogrt_relative_dir = (
                cls.RAW_SCENE_TEXT_SUBTITLE_MOGRT_RELATIVE_DIR
            )

            # Resolve optional music settings for Premiere automation.
            music_filename = ""
            music_gain_db = -24.0
            if project.music_key:
                try:
                    music = MusicConfigService.get_music(project.music_key)
                except ValueError as exc:
                    logger.warning("Unknown music key '%s': %s", project.music_key, exc)
                else:
                    music_path = Path(music.file_path)
                    if music_path.exists():
                        music_filename = music_path.name
                        music_gain_db = float(music.volume_db)
                    else:
                        logger.warning(
                            "Configured music file is missing on disk: %s",
                            music_path,
                        )

            resolved_scene_sources = cls.resolve_scene_sources(matches, source_rate)

            # Step 4: Generate JSX script from canonical v7.7 template
            jsx_content = cls.generate_jsx_script(
                project,
                new_transcription,
                matches,
                source_rate=source_rate,
                resolved_scene_sources=resolved_scene_sources,
                subtitle_timing_relative_path=classic_subtitle_timing_relative_path,
                raw_scene_subtitle_timing_relative_path=raw_scene_subtitle_timing_relative_path,
                raw_scene_subtitle_mogrt_relative_dir=raw_scene_subtitle_mogrt_relative_dir,
                music_filename=music_filename,
                music_gain_db=music_gain_db,
            )
            jsx_path = output_dir / "import_project.jsx"
            jsx_path.write_text(jsx_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "srt_generation",
                0.6,
                "Creating subtitles...",
            )

            raw_text_subtitle_entries, _raw_image_subtitle_entries = (
                await cls._collect_raw_scene_source_subtitles(
                    project,
                    new_transcription,
                    resolved_scene_sources,
                    output_dir,
                )
            )

            # Step 4: Generate SRT subtitles (aggressive Hormozi style)
            classic_srt_content = cls.render_srt_entries(
                cls.generate_srt_entries(
                    new_transcription,
                    language=new_transcription.language,
                )
            )
            srt_content = cls.generate_srt(
                new_transcription,
                language=new_transcription.language,
                extra_entries=raw_text_subtitle_entries,
            )
            raw_scene_srt_content = cls.render_srt_entries(raw_text_subtitle_entries)
            srt_path = output_dir / srt_filename
            srt_path.write_text(srt_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "subtitle_mogrt_bake",
                0.7,
                "Baking subtitle MOGRT files...",
            )

            classic_subtitle_template_path = cls._processing_asset_path(
                "SPM_Anime_Subtitle.mogrt"
            )
            raw_scene_subtitle_template_path = cls._processing_asset_path(
                "SPM_Anime_Subtitle_Raw.mogrt"
            )

            cls._bake_subtitle_mogrt_set(
                template_mogrt_path=classic_subtitle_template_path,
                srt_content=classic_srt_content,
                srt_path=output_dir / classic_subtitle_timing_relative_path,
                output_dir=output_dir / "subtitles",
                label="Classic subtitle",
            )
            cls._bake_subtitle_mogrt_set(
                template_mogrt_path=raw_scene_subtitle_template_path,
                srt_content=raw_scene_srt_content,
                srt_path=output_dir / raw_scene_subtitle_timing_relative_path,
                output_dir=output_dir / raw_scene_subtitle_mogrt_relative_dir,
                label="Raw-scene subtitle",
            )

            # Step 5: Generate title overlay images (if video_overlay is set)
            if project.video_overlay and project.video_overlay.get("title"):
                yield ProcessingProgress(
                    "processing",
                    "overlay_image_generation",
                    0.82,
                    "Generating title overlay images...",
                )
                from .title_image_generator import TitleImageGeneratorService

                overlay_paths = TitleImageGeneratorService.generate(
                    title=project.video_overlay["title"],
                    category=project.video_overlay.get("category", ""),
                    output_dir=output_dir,
                )
                logger.info(
                    "Generated title overlays: %s",
                    {k: str(v) for k, v in overlay_paths.items()},
                )

                # Store overlay image paths in project
                project.video_overlay["title_image"] = str(overlay_paths["title"])
                project.video_overlay["category_image"] = str(overlay_paths["category"])
                ProjectService.save(project)
            else:
                yield ProcessingProgress(
                    "processing",
                    "overlay_image_generation",
                    0.82,
                    "Skipping title overlay image generation (no overlay configured)",
                )

            # Clear processing state now that we're done
            cls.clear_processing_state(project.id)

            yield ProcessingProgress(
                "complete",
                "overlay_image_generation",
                1.0,
                "Processing complete!",
            )

        except Exception as e:
            yield ProcessingProgress(
                "error",
                "",
                0,
                "",
                error=str(e),
            )
