"""Processing pipeline service for final video generation."""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import AsyncIterator

import spacy

from ..config import settings
from ..models import Project, Transcription, SceneMatch
from ..models.transcription import Word
from ..utils.media_binaries import is_media_binary_override_error
from ..utils.subprocess_runner import CommandTimeoutError, run_command
from ..utils.timing import compute_adjusted_scene_end_times
from .anime_library import AnimeLibraryService
from .transcriber import TranscriberService
from .otio_timing import OTIOTimingCalculator, FrameRateInfo
from .gap_resolution import GapResolutionService
from .export_service import ExportService
from .music_config_service import MusicConfigService
from .premiere_subtitle_baker import PremiereSubtitleBakerService
from .project_service import ProjectService
from .auto_editor_profiles import PRODUCTION_AUTO_EDITOR_PROFILE
from .forced_alignment import ForcedAlignmentService, PreparedAlignmentAudio

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
        }


class ProcessingService:
    """Service for processing the final video generation pipeline."""

    FFPROBE_TIMEOUT_SECONDS = 30.0
    AUTO_EDITOR_TIMEOUT_SECONDS = 1800.0
    PREMIERE_JSX_TEMPLATE_PATH = (
        Path(__file__).resolve().parent / "templates" / "premiere_import_project_v77.jsx"
    )
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
    def schedule_gap_candidate_prewarm(cls, project_id: str, gaps: list) -> None:
        """Start a background prewarm for gap candidate generation."""
        if not gaps:
            return

        existing = cls._gap_candidate_prewarm_tasks.get(project_id)
        if existing and not existing.done():
            return

        async def _run() -> None:
            try:
                await GapResolutionService.generate_candidates_batch_dedup(gaps)
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
        """Shift transcription timings so the earliest word starts at 0s."""
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
            for word in scene.words:
                word.start = max(0.0, word.start - min_start)
                word.end = max(0.0, word.end - min_start)
            scene.start_time = max(0.0, scene.start_time - min_start)
            scene.end_time = max(0.0, scene.end_time - min_start)

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
    ) -> bool:
        """
        Run auto-editor on TTS audio and export the edited waveform.

        Args:
            audio_path: Path to input audio file
            audio_output_path: Path for output audio file

        Returns:
            True if successful
        """
        # This flow only edits audio; there is no meaningful GPU acceleration path
        # to enable for auto-editor here.
        logger.debug("auto-editor GPU acceleration is not applied for audio-only runs.")
        audio_cmd = [
            "pixi", "run", "--locked", "--",
            "auto-editor",
            str(audio_path),
            *PRODUCTION_AUTO_EDITOR_PROFILE.command_args(),
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
        source_fps: Fraction | None = None,
        subtitle_filename: str = "subtitles.srt",
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
            source_fps: Source video frame rate as Fraction (e.g., 24000/1001 for 23.976)
                        If None, defaults to 23.976fps
            subtitle_filename: Root-level SRT filename to reference in JSX
            music_filename: Optional music filename placed in /sources
            music_gain_db: Music gain in dB (used only when music_filename is set)

        Returns:
            The generated JSX script content (ES3 compatible)
        """
        # Set up frame-accurate timing calculator
        # Sequence rate: 60fps (non-NTSC for TikTok)
        sequence_rate = FrameRateInfo(timebase=60, ntsc=False)

        # Source rate: detect from anime or default to 23.976fps
        if source_fps is not None:
            # Determine if NTSC from the fraction
            fps_float = float(source_fps)
            source_rate = FrameRateInfo.from_fps(fps_float)
        else:
            source_rate = FrameRateInfo(timebase=24, ntsc=True)  # Default 23.976

        calculator = OTIOTimingCalculator(
            sequence_rate=sequence_rate,
            source_rate=source_rate,
        )

        # Compute adjusted end times to eliminate gaps between scenes
        # Each scene's end is extended to the next scene's start
        adjusted_ends_jsx = compute_adjusted_scene_end_times(
            scenes=transcription.scenes,
            get_scene_index=lambda s: s.scene_index,
            get_first_word_start=lambda s: s.words[0].start if s.words else None,
            get_last_word_end=lambda s: s.words[-1].end if s.words else None,
        )

        # Build scenes data with frame-perfect timing
        scenes = []
        clip_timings = []  # For continuity validation
        matches_by_scene = {m.scene_index: m for m in matches}

        for scene_trans in transcription.scenes:
            if not scene_trans.words and not scene_trans.is_raw:
                continue

            # Find corresponding match (fallback to best alternative if missing)
            match = matches_by_scene.get(scene_trans.scene_index)
            if not match:
                continue

            episode = match.episode
            source_in_raw_sec = match.start_time
            source_out_raw_sec = match.end_time
            used_alternative = False

            if not episode:
                # Use best available alternative to avoid dropping the scene (prevents gaps)
                alternative = next((alt for alt in match.alternatives if alt.episode), None)
                if alternative:
                    episode = alternative.episode
                    source_in_raw_sec = alternative.start_time
                    source_out_raw_sec = alternative.end_time
                    used_alternative = True
                else:
                    continue

            source_path, clip_name = cls._resolve_source_reference(episode)

            # Snap source in/out to source-frame boundaries using "at-or-after" semantics.
            # This matches what users validate in browser playback at non-frame timestamps.
            source_in_frames = source_rate.frames_from_seconds_at_or_after(source_in_raw_sec)
            source_out_frames = source_rate.frames_from_seconds_at_or_after(source_out_raw_sec)
            if source_out_frames <= source_in_frames:
                source_out_frames = source_in_frames + 1

            source_in_sec = source_rate.seconds_from_frames(source_in_frames)
            source_out_sec = source_rate.seconds_from_frames(source_out_frames)

            # Timeline position: raw scenes use start_time/end_time directly,
            # TTS scenes use word timings with adjusted ends
            if scene_trans.is_raw:
                timeline_start_raw = scene_trans.start_time
                timeline_end_raw = scene_trans.end_time
            else:
                timeline_start_raw = scene_trans.words[0].start
                timeline_end_raw = adjusted_ends_jsx.get(
                    scene_trans.scene_index, scene_trans.words[-1].end
                )

            # Snap timeline positions to 60fps frame grid BEFORE speed calculation
            # This keeps speed and placement perfectly aligned to frame boundaries
            timeline_start_frames = calculator.sequence_rate.frames_from_seconds(timeline_start_raw)
            timeline_end_frames = calculator.sequence_rate.frames_from_seconds(timeline_end_raw)
            timeline_start_snapped = calculator.sequence_rate.seconds_from_frames(
                timeline_start_frames
            )
            timeline_end_snapped = calculator.sequence_rate.seconds_from_frames(
                timeline_end_frames
            )

            # Calculate frame-perfect timing using OTIO
            clip_timing = calculator.calculate_clip_timing(
                scene_index=scene_trans.scene_index,
                source_path=source_path,
                bundle_filename=clip_name,
                source_in_seconds=source_in_sec,
                source_out_seconds=source_out_sec,
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
                "clipName": clip_name,
                "source_in_frame": source_in_frames,
                "source_out_frame": source_out_frames,
                "source_in": round(clip_timing.source_in_seconds, 6),
                "source_out": round(clip_timing.source_out_seconds, 6),
                "clip_duration": round(clip_timing.source_duration.to_seconds(), 4),
                "target_duration": round(clip_timing.target_duration.to_seconds(), 4),
                "speed_ratio": round(float(clip_timing.speed_ratio), 4),
                "effective_speed": round(float(clip_timing.effective_speed), 4),
                "leaves_gap": clip_timing.leaves_gap,  # True if 75% floor hit
                "used_alternative": used_alternative,
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
            subtitle_filename=subtitle_filename,
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
        subtitle_filename: str,
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
            f'var SUBTITLE_SRT_PATH = ROOT_DIR + "/{cls._escape_js_string(subtitle_filename)}";',
            label="SUBTITLE_SRT_PATH",
        )
        content = cls._replace_template_once(
            content,
            r"var SUBTITLE_MOGRT_DIR = [^;]+;",
            'var SUBTITLE_MOGRT_DIR = ROOT_DIR + "/subtitles";',
            label="SUBTITLE_MOGRT_DIR",
        )
        return content

    @classmethod
    def generate_srt(
        cls,
        transcription: Transcription,
        language: str = "fr",
    ) -> str:
        """
        Generate SRT subtitles with aggressive Hormozi-style segmentation.

        Rules:
        - 1-2 words per subtitle ideally
        - Max 3 words ONLY if total length < 12 characters
        - Max 20 characters per subtitle (spaces included)
        - Single line only (never 2 lines)
        - No temporal gaps: end of block N = start of block N+1 (except obvious silence > 0.5s)
        - Never isolate a determiner at the end of a block

        Args:
            transcription: Transcription with word timings
            language: Language code for determiner detection (fr, en, es)

        Returns:
            SRT file content
        """
        srt_blocks = []
        block_index = 1

        # Collect all words with timings across all scenes (skip raw scenes)
        all_words = []
        for scene_trans in transcription.scenes:
            if scene_trans.is_raw:
                continue
            all_words.extend(scene_trans.words)

        if not all_words:
            return ""

        i = 0
        while i < len(all_words):
            current_block = []
            current_block_indices: list[int] = []
            current_len = 0

            while i < len(all_words):
                word = all_words[i]
                # Strip punctuation from word text
                word_text = strip_punctuation(word.text)

                # Skip empty words (e.g., standalone punctuation)
                if not word_text:
                    i += 1
                    continue

                # Calculate new length with space (if not first word)
                new_len = current_len + len(word_text) + (1 if current_block else 0)
                word_count = len(current_block) + 1

                # Determine if we can add this word
                can_add = False

                if word_count <= 2:
                    # For 1-2 words, just check 20 char limit
                    can_add = new_len <= 20
                elif word_count == 3 and new_len < 12:
                    # Allow 3rd word only if total stays under 12 chars
                    can_add = True

                # Special case: if word itself is > 20 chars, accept it alone
                if not current_block and len(word_text) > 20:
                    can_add = True

                if can_add:
                    # Store cleaned word text
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

                    # Check if original word had sentence-ending punctuation
                    # If so, break here to start a new subtitle block
                    if has_sentence_ending(word.text):
                        break
                else:
                    # Can't add this word - check if last word is a clause starter
                    # (determiner or subject pronoun) that shouldn't be isolated
                    # But only if we have more than 1 word (to avoid infinite loop)
                    if len(current_block) >= 2:
                        last_word_text = current_block[-1].text
                        if is_clause_starter(last_word_text, language):
                            # Rewind to the popped word's original index so we do not
                            # accidentally land on trailing punctuation and skip it.
                            rewind_index = current_block_indices.pop()
                            current_block.pop()
                            i = rewind_index
                            # Recalculate current_len using cleaned text
                            current_len = sum(len(w.text) for w in current_block)
                            if len(current_block) > 1:
                                current_len += len(current_block) - 1  # spaces
                    break

            # Create the SRT block (only if we have words)
            if current_block:
                next_word = all_words[i] if i < len(all_words) else None

                # Determine end time:
                # - If there's a next word, check for gap
                # - If gap > SRT_OBVIOUS_SILENCE_GAP_SEC (obvious silence), use current block's last word end
                # - Otherwise, extend to next word's start (no temporal gap)
                if next_word:
                    gap = next_word.start - current_block[-1].end
                    if gap > SRT_OBVIOUS_SILENCE_GAP_SEC:
                        # Obvious silence - don't force continuity
                        end_time = current_block[-1].end
                    else:
                        # Continuity: extend to next word's start
                        end_time = next_word.start
                else:
                    # Last block - use natural end
                    end_time = current_block[-1].end

                # Guardrail: when a single low-confidence word is followed by a
                # clear silence, avoid extremely short flashes by extending up to
                # a minimum duration, while keeping a frame of safety.
                if next_word and len(current_block) == 1:
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

                block = cls._create_srt_block_aggressive(
                    block_index, current_block, end_time
                )
                srt_blocks.append(block)
                block_index += 1

        return "\n".join(srt_blocks)

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

        # Format timestamps
        def format_srt_time(seconds: float) -> str:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            millis = int((seconds % 1) * 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

        # Single line text - no line breaks
        text = " ".join(w.text for w in words)

        return f"{index}\n{format_srt_time(start_time)} --> {format_srt_time(end_time)}\n{text}\n"

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
    def clear_processing_state(cls, project_id: str) -> None:
        """Clear saved processing state after completion."""
        output_dir = cls.get_output_dir(project_id)
        state_path = output_dir / "processing_state.json"
        if state_path.exists():
            state_path.unlink()

        project_dir = settings.projects_dir / project_id
        flag_path = project_dir / "gaps_resolved.flag"
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
        - A2: TTS audio

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

        # Check if we're resuming after gap resolution
        resuming_after_gaps = cls.check_has_saved_state(project.id) and cls.check_gaps_resolved(project.id)

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
            cls.normalize_transcription_timings(new_transcription)

            yield ProcessingProgress(
                "processing",
                "jsx_generation",
                0.5,
                "Resuming - Generating Premiere Pro JSX script...",
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
                # Step 1: Auto-editor (generates edited audio with silences removed)
                edited_audio_path = output_dir / "tts_edited.wav"
                upload_manifest = ForcedAlignmentService.load_upload_manifest(project.id)
                if upload_manifest and upload_manifest.get("mode") == "audio_parts":
                    prepared_audio = await ForcedAlignmentService.prepare_audio_from_parts(
                        project_id=project.id,
                        output_dir=output_dir,
                        tts_speed=float(project.tts_speed or 1.0),
                        auto_editor_runner=cls.run_auto_editor,
                    )
                    edited_audio_path = prepared_audio.edited_audio_path
                else:
                    await cls.run_auto_editor(audio_path, edited_audio_path)
                    prepared_audio = PreparedAlignmentAudio(
                        mode="single_audio",
                        edited_audio_path=edited_audio_path,
                        segment_audio_paths=[],
                        manifest=upload_manifest or ForcedAlignmentService.build_single_audio_manifest(
                            script_payload=new_script,
                        ),
                    )

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
                                import shutil
                                shutil.copy(matches_path, matches_backup_path)

                        candidates_by_scene = await GapResolutionService.generate_candidates_batch_dedup(gaps)
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
                        cls.schedule_gap_candidate_prewarm(project.id, gaps)

                        # Backup current matches before gap resolution modifies them
                        matches_backup_path = project_dir / "matches_before_gaps.json"
                        if not matches_backup_path.exists():
                            matches_path = project_dir / "matches.json"
                            if matches_path.exists():
                                import shutil
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

                yield ProcessingProgress(
                    "processing",
                    "jsx_generation",
                    0.5,
                    "Generating Premiere Pro JSX script...",
                )

            # Step 3: Detect source FPS from first available episode
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

            source_fps = None
            for match in matches:
                if match.episode:
                    episode_path, _ = cls._resolve_source_reference(match.episode)
                    if episode_path.exists():
                        source_fps = await cls.detect_video_fps(episode_path)
                        break  # Use first valid episode's FPS

            srt_filename = ExportService.subtitle_filename(project)

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

            # Step 4: Generate JSX script from canonical v7.7 template
            jsx_content = cls.generate_jsx_script(
                project,
                new_transcription,
                matches,
                source_fps=source_fps,
                subtitle_filename=srt_filename,
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

            # Step 4: Generate SRT subtitles (aggressive Hormozi style)
            srt_content = cls.generate_srt(new_transcription, language=new_transcription.language)
            srt_path = output_dir / srt_filename
            srt_path.write_text(srt_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "subtitle_mogrt_bake",
                0.7,
                "Baking subtitle MOGRT files...",
            )

            subtitle_template_path = Path(__file__).resolve().parents[3] / "assets" / "SPM_Anime_Subtitle.mogrt"
            subtitles_output_dir = output_dir / "subtitles"
            bake_result = PremiereSubtitleBakerService.bake_from_srt(
                template_mogrt_path=subtitle_template_path,
                srt_path=srt_path,
                output_dir=subtitles_output_dir,
            )
            if bake_result.generated_count != bake_result.entries_count:
                raise RuntimeError(
                    "Subtitle MOGRT bake mismatch: "
                    f"{bake_result.generated_count} generated for {bake_result.entries_count} SRT entries"
                )
            if bake_result.entries_count <= 0:
                raise RuntimeError("Subtitle MOGRT bake produced no entries.")

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
