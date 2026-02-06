"""Processing pipeline service for final video generation."""

import asyncio
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import AsyncIterator

import spacy

from ..config import settings
from ..models import Project, Transcription, SceneMatch
from ..models.transcription import Word
from ..utils.timing import compute_adjusted_scene_end_times
from .transcriber import TranscriberService
from .otio_timing import OTIOTimingCalculator, FrameRateInfo
from .gap_resolution import GapResolutionService


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

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()

        if process.returncode != 0:
            # Default to 24fps if detection fails
            return Fraction(24, 1)

        fps_str = stdout.decode().strip()
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
    async def convert_audio_for_auto_editor(
        cls,
        input_path: Path,
        output_path: Path,
    ) -> Path:
        """
        Convert audio to a format compatible with auto-editor (48kHz stereo).

        Auto-editor has issues with some audio formats (e.g., 24kHz mono TTS audio).
        This converts to a standard 48kHz stereo WAV format.

        Args:
            input_path: Path to input audio file
            output_path: Path for converted audio file

        Returns:
            Path to converted audio file
        """
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", "48000",  # 48kHz sample rate
            "-ac", "2",       # Stereo
            str(output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Audio conversion failed: {stderr.decode()}")

        return output_path

    @classmethod
    async def run_auto_editor(
        cls,
        audio_path: Path,
        audio_output_path: Path,
        xml_output_path: Path,
    ) -> bool:
        """
        Run auto-editor on TTS audio twice:
        1. Export as audio file (for WhisperX transcription)
        2. Export as XML (lossless reference for timing)

        Args:
            audio_path: Path to input audio file
            audio_output_path: Path for output audio file
            xml_output_path: Path for output Premiere XML file

        Returns:
            True if successful
        """
        backend_dir = settings.data_dir.parent

        # Convert audio to compatible format first (auto-editor has issues with some formats)
        converted_audio_path = audio_path.parent / "tts_converted.wav"
        await cls.convert_audio_for_auto_editor(audio_path, converted_audio_path)

        # Common auto-editor settings
        base_args = [
            "--edit", "audio",
            "--no-open",
        ]

        # Run 1: Export as audio file for WhisperX transcription
        audio_cmd = [
            "pixi", "run", "--locked", "--",
            "auto-editor",
            str(converted_audio_path),
            *base_args,
            "-o", str(audio_output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *audio_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"auto-editor (audio export) failed: {stderr.decode()}")

        # Run 2: Export as Premiere XML for lossless timing reference
        xml_cmd = [
            "pixi", "run", "--locked", "--",
            "auto-editor",
            str(converted_audio_path),
            *base_args,
            "--export", "premiere",
            "-o", str(xml_output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *xml_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"auto-editor (XML export) failed: {stderr.decode()}")

        # Clean up converted audio
        if converted_audio_path.exists():
            converted_audio_path.unlink()

        return True

    @classmethod
    def generate_fcp_xml(
        cls,
        project: Project,
        transcription: Transcription,
        matches: list[SceneMatch],
        audio_filename: str,
        srt_filename: str,
        sources_dir: str = "sources",
    ) -> str:
        """
        Generate a Final Cut Pro 7 XML file for Premiere Pro import.

        NOTE: This generates a reference XML that places clips at approximate positions.
        The ExtendScript (.jsx) file should be used for proper import with speed adjustments,
        as FCP XML speed filters don't import correctly into Premiere Pro.

        Args:
            project: Project data
            transcription: Transcription with word timings from edited TTS audio
            matches: Scene matches with source timing
            audio_filename: Name of the edited TTS audio file
            srt_filename: Name of the SRT subtitle file
            sources_dir: Folder name for source video files

        Returns:
            The generated FCP XML content
        """
        import xml.etree.ElementTree as ET
        from xml.dom import minidom

        # Use 23.976 fps for compatibility (most anime source fps)
        # This is also a common timeline fps for video editing
        sequence_fps = 23.976
        source_fps = 23.976  # Most anime is 23.976fps

        # Calculate total duration from transcription (in frames)
        total_duration_secs = 0.0
        if transcription.scenes:
            last_scene = transcription.scenes[-1]
            if last_scene.words:
                total_duration_secs = last_scene.words[-1].end
        total_duration_frames = int(total_duration_secs * sequence_fps)

        # Compute adjusted end times to eliminate gaps between scenes
        # Each scene's end is extended to the next scene's start
        adjusted_ends = compute_adjusted_scene_end_times(
            scenes=transcription.scenes,
            get_scene_index=lambda s: s.scene_index,
            get_first_word_start=lambda s: s.words[0].start if s.words else None,
            get_last_word_end=lambda s: s.words[-1].end if s.words else None,
        )

        # Build clip data with timing from transcription
        clips = []
        for i, scene_trans in enumerate(transcription.scenes):
            if not scene_trans.words:
                continue

            # Find corresponding match
            match = next(
                (m for m in matches if m.scene_index == scene_trans.scene_index),
                None,
            )
            if not match or not match.episode:
                continue

            # Timeline position from transcription words (seconds)
            # Use adjusted end time to eliminate gaps between scenes
            timeline_start = scene_trans.words[0].start
            timeline_end = adjusted_ends.get(scene_trans.scene_index, scene_trans.words[-1].end)
            target_duration = timeline_end - timeline_start

            # Source timing from match (seconds)
            source_in = match.start_time
            source_out = match.end_time
            source_duration = source_out - source_in

            # Calculate speed factor (source_duration / target_duration)
            # If source is 2s and target is 3s, speed = 0.667 (slow down to 66.7%)
            speed = source_duration / target_duration if target_duration > 0 else 1.0

            clips.append({
                "scene_index": scene_trans.scene_index,
                "source_path": match.episode,
                "source_filename": Path(match.episode).name,
                "source_in": source_in,
                "source_out": source_out,
                "source_duration": source_duration,
                "timeline_start": timeline_start,
                "timeline_end": timeline_end,
                "target_duration": target_duration,
                "speed": speed,
            })

        def seconds_to_frames(secs: float, fps: float = sequence_fps) -> int:
            return int(secs * fps)

        # Build XML structure
        root = ET.Element("xmeml", version="4")
        sequence = ET.SubElement(root, "sequence", id="sequence-1")

        ET.SubElement(sequence, "name").text = f"ATR_{project.id}"
        ET.SubElement(sequence, "duration").text = str(total_duration_frames)

        rate = ET.SubElement(sequence, "rate")
        ET.SubElement(rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

        # Timecode
        timecode = ET.SubElement(sequence, "timecode")
        tc_rate = ET.SubElement(timecode, "rate")
        ET.SubElement(tc_rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(tc_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"
        ET.SubElement(timecode, "string").text = "00:00:00:00"
        ET.SubElement(timecode, "frame").text = "0"
        ET.SubElement(timecode, "displayformat").text = "NDF"

        media = ET.SubElement(sequence, "media")

        # Video tracks
        video = ET.SubElement(media, "video")

        video_format = ET.SubElement(video, "format")
        video_sample = ET.SubElement(video_format, "samplecharacteristics")
        ET.SubElement(video_sample, "width").text = "1080"
        ET.SubElement(video_sample, "height").text = "1920"
        ET.SubElement(video_sample, "pixelaspectratio").text = "square"
        sample_rate = ET.SubElement(video_sample, "rate")
        ET.SubElement(sample_rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(sample_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

        video_track = ET.SubElement(video, "track")

        # Track source files to avoid duplicates
        file_refs = {}

        for idx, clip in enumerate(clips):
            clipitem = ET.SubElement(video_track, "clipitem", id=f"clipitem-{idx + 1}")

            ET.SubElement(clipitem, "name").text = f"Scene {clip['scene_index'] + 1}"

            clip_rate = ET.SubElement(clipitem, "rate")
            ET.SubElement(clip_rate, "timebase").text = str(int(sequence_fps))
            ET.SubElement(clip_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

            # Timeline position in SEQUENCE frames
            start_frame = seconds_to_frames(clip["timeline_start"], sequence_fps)
            end_frame = seconds_to_frames(clip["timeline_end"], sequence_fps)

            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)

            # Source in/out points in SOURCE frames (using source fps)
            in_frame = seconds_to_frames(clip["source_in"], source_fps)
            out_frame = seconds_to_frames(clip["source_out"], source_fps)

            ET.SubElement(clipitem, "in").text = str(in_frame)
            ET.SubElement(clipitem, "out").text = str(out_frame)

            # File reference
            filename = clip["source_filename"]
            file_id = f"file-{filename.replace('.', '-').replace(' ', '_')}"

            if file_id not in file_refs:
                file_elem = ET.SubElement(clipitem, "file", id=file_id)
                ET.SubElement(file_elem, "name").text = filename
                ET.SubElement(file_elem, "pathurl").text = f"{sources_dir}/{filename}"

                file_rate = ET.SubElement(file_elem, "rate")
                ET.SubElement(file_rate, "timebase").text = str(int(source_fps))
                ET.SubElement(file_rate, "ntsc").text = "TRUE" if abs(source_fps - 23.976) < 0.01 else "FALSE"

                # Assume source is 1 hour long (will be read from actual file)
                ET.SubElement(file_elem, "duration").text = str(int(source_fps * 3600))

                file_media = ET.SubElement(file_elem, "media")
                file_video = ET.SubElement(file_media, "video")
                file_sample = ET.SubElement(file_video, "samplecharacteristics")
                ET.SubElement(file_sample, "width").text = "1920"
                ET.SubElement(file_sample, "height").text = "1080"

                file_refs[file_id] = True
            else:
                # Reference existing file
                ET.SubElement(clipitem, "file", id=file_id)

            # NOTE: We don't add speed filter here because FCP XML speed filters
            # don't import correctly into Premiere Pro. Use the ExtendScript instead.
            # Add a comment marker with speed info for reference
            speed_pct = clip["speed"] * 100
            marker = ET.SubElement(clipitem, "marker")
            ET.SubElement(marker, "name").text = f"Speed: {speed_pct:.0f}% (use JSX script)"
            ET.SubElement(marker, "in").text = str(in_frame)
            ET.SubElement(marker, "out").text = str(in_frame)

        # Audio tracks
        audio = ET.SubElement(media, "audio")

        audio_format = ET.SubElement(audio, "format")
        audio_sample = ET.SubElement(audio_format, "samplecharacteristics")
        ET.SubElement(audio_sample, "samplerate").text = "48000"
        ET.SubElement(audio_sample, "depth").text = "16"

        audio_track = ET.SubElement(audio, "track")

        # TTS Audio clip
        audio_clipitem = ET.SubElement(audio_track, "clipitem", id="audio-tts")
        ET.SubElement(audio_clipitem, "name").text = "TTS Audio"

        audio_clip_rate = ET.SubElement(audio_clipitem, "rate")
        ET.SubElement(audio_clip_rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(audio_clip_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

        ET.SubElement(audio_clipitem, "start").text = "0"
        ET.SubElement(audio_clipitem, "end").text = str(total_duration_frames)
        ET.SubElement(audio_clipitem, "in").text = "0"
        ET.SubElement(audio_clipitem, "out").text = str(total_duration_frames)

        audio_file = ET.SubElement(audio_clipitem, "file", id="file-tts-audio")
        ET.SubElement(audio_file, "name").text = audio_filename
        ET.SubElement(audio_file, "pathurl").text = audio_filename
        ET.SubElement(audio_file, "duration").text = str(total_duration_frames)

        audio_file_media = ET.SubElement(audio_file, "media")
        audio_file_audio = ET.SubElement(audio_file_media, "audio")
        audio_file_sample = ET.SubElement(audio_file_audio, "samplecharacteristics")
        ET.SubElement(audio_file_sample, "samplerate").text = "48000"
        ET.SubElement(audio_file_sample, "depth").text = "16"

        # Add sequence markers for each scene
        markers_elem = ET.SubElement(sequence, "marker")
        for scene_trans in transcription.scenes:
            if scene_trans.words:
                marker = ET.SubElement(markers_elem, "marker")
                frame = seconds_to_frames(scene_trans.words[0].start, sequence_fps)
                ET.SubElement(marker, "name").text = f"Scene {scene_trans.scene_index + 1}"
                ET.SubElement(marker, "in").text = str(frame)
                ET.SubElement(marker, "out").text = str(frame)

        # Convert to pretty XML string
        rough_string = ET.tostring(root, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        xml_content = reparsed.toprettyxml(indent="  ")

        # Add DOCTYPE
        lines = xml_content.split("\n")
        lines.insert(1, "<!DOCTYPE xmeml>")

        return "\n".join(lines)

    @classmethod
    def generate_jsx_script(
        cls,
        project: Project,
        transcription: Transcription,
        matches: list[SceneMatch],
        source_fps: Fraction | None = None,
    ) -> str:
        """
        Generate a production-ready Premiere Pro 2025 ExtendScript (.jsx) file.

        This generates a script matching the working_script.jsx v7.1 format exactly.
        Uses the QE (Quality Engineering) DOM for reliable:
        - 60fps vertical sequence creation via .sqpreset
        - Speed adjustments via qeItem.setSpeed()
        - 4-Track Structure: V4(Subtitles), V3(Main), V2(Border), V1(Background)
        - Scaling: V1 (183%), V3 (68%)

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
            if not scene_trans.words:
                continue

            # Find corresponding match (fallback to best alternative if missing)
            match = matches_by_scene.get(scene_trans.scene_index)
            if not match:
                continue

            episode = match.episode
            source_in_sec = match.start_time
            source_out_sec = match.end_time
            used_alternative = False

            if not episode:
                # Use best available alternative to avoid dropping the scene (prevents gaps)
                alternative = next((alt for alt in match.alternatives if alt.episode), None)
                if alternative:
                    episode = alternative.episode
                    source_in_sec = alternative.start_time
                    source_out_sec = alternative.end_time
                    used_alternative = True
                else:
                    continue

            # Timeline position from TTS transcription words (seconds)
            # Use adjusted end time to eliminate gaps between scenes
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
                source_path=Path(episode),
                bundle_filename=Path(episode).stem,
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
                "clipName": Path(episode).stem,
                "source_in": round(clip_timing.source_in_seconds, 6),
                "source_out": round(clip_timing.source_out_seconds, 6),
                "clip_duration": round(clip_timing.source_duration.to_seconds(), 4),
                "target_duration": round(clip_timing.target_duration.to_seconds(), 4),
                "speed_ratio": round(float(clip_timing.speed_ratio), 4),
                "effective_speed": round(float(clip_timing.effective_speed), 4),
                "leaves_gap": clip_timing.leaves_gap,  # True if 75% floor hit
                "used_alternative": used_alternative,
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

        # Generate scenes JSON with proper indentation
        scenes_json = json.dumps(scenes, indent=4, ensure_ascii=False)
        # Indent each line for proper JSX formatting
        scenes_json_indented = "\n".join("  " + line for line in scenes_json.split("\n"))

        jsx_content = f'''/**
 * Anime TikTok Reproducer - Premiere Pro 2025 Automation Script (v7.1 - CLEANED)
 *
 * CHANGES from v6:
 * - 4-Track Structure: V4(Subtitles - Reserved), V3(Main), V2(Border), V1(Background).
 * - Interleaved Speed & Placement for V1 and V3.
 * - Scaling: V1 (183%), V3 (68%).
 * - Audio: Cleans A2 before placing TTS.
 */

(function () {{
  // ========================================================================
  // 1. CONFIGURATION
  // ========================================================================
  var SCRIPT_FILE = new File($.fileName);
  var ROOT_DIR = SCRIPT_FILE.parent.fsName;
  var ASSETS_DIR = ROOT_DIR + "/assets";
  var SOURCES_DIR = ROOT_DIR + "/sources";

  var SEQUENCE_PRESET_PATH = ASSETS_DIR + "/TikTok60fps.sqpreset";
  var BORDER_MOGRT_PATH = ASSETS_DIR + "/White border 5px.mogrt";
  var AUDIO_FILENAME = "tts_edited.wav";

  // --- SCENES DATA ---
  var scenes =
{scenes_json_indented};

  // ========================================================================
  // 2. LOGGING & UTILS
  // ========================================================================
  function log(msg) {{
    $.writeln("[ATR] " + msg);
  }}
  function sleep(ms) {{
    $.sleep(ms);
  }}
  var TICKS_PER_SECOND = 254016000000; // Premiere Pro timebase constant
  var SEQ_FPS = 60; // TikTok preset is 60fps
  var TICKS_PER_FRAME = TICKS_PER_SECOND / SEQ_FPS;

  function snapSecondsToFrame(sec) {{
    // Add small epsilon to prevent floating-point rounding errors
    // Values like 17.583333 (representing 1055/60) become 1054.99998 due to float precision
    // Without epsilon, this can cause 1-frame drift when truncated elsewhere
    return Math.round(sec * SEQ_FPS + 0.0001) / SEQ_FPS;
  }}

  function secondsToTicks(sec) {{
    // Snap to frame boundaries to avoid 1-frame drift
    // Add small epsilon to prevent floating-point rounding errors (same as snapSecondsToFrame)
    return Math.round(sec * SEQ_FPS + 0.0001) * TICKS_PER_FRAME;
  }}

  function buildTimeFromSeconds(sec) {{
    var t = new Time();
    try {{
      // 2025: ticks might need to be a String or Number.
      // Safe to assign Number, PPro handles it.
      t.ticks = secondsToTicks(sec).toString();
    }} catch (e) {{
      t.seconds = sec;
    }}
    return t;
  }}

  function getStartTicks(item) {{
    if (!item || !item.start) return null;
    // PPro 2024/2025: .ticks is often a String. Parse it!
    if (item.start.ticks !== undefined) {{
      var val = parseInt(item.start.ticks, 10);
      if (!isNaN(val)) return val;
    }}
    // Fallback
    if (typeof item.start.seconds === "number") {{
      return secondsToTicks(item.start.seconds);
    }}
    return null;
  }}

  function findTrackItemAtStart(track, startSeconds, nameRef) {{
    if (!track || !track.clips) return null;

    var targetTicks = secondsToTicks(startSeconds);
    // Relaxed tolerance
    var toleranceTicks = secondsToTicks(0.1);

    var bestItem = null;
    var minDiff = toleranceTicks + 1;

    for (var i = 0; i < track.clips.numItems; i++) {{
      var item = track.clips[i];
      if (!item) continue;

      var itemTicks = getStartTicks(item);
      if (itemTicks === null) continue;

      var diff = Math.abs(itemTicks - targetTicks);

      if (diff <= toleranceTicks) {{
        // Name Check
        if (nameRef) {{
          var itemName = item.name ? item.name.toString() : "";
          if (itemName.replace(/\\s/g, "") !== "") {{
            // Check containment both ways
            if (
              itemName.indexOf(nameRef) === -1 &&
              nameRef.indexOf(itemName) === -1
            ) {{
              continue;
            }}
          }}
        }}

        // We found a candidate. Is it the best one?
        if (diff < minDiff) {{
          minDiff = diff;
          bestItem = item;
        }}
      }}
    }}
    return bestItem;
  }}

  function setTrackItemInOut(track, startSeconds, inSeconds, outSeconds, nameRef) {{
    var item = findTrackItemAtStart(track, startSeconds, nameRef);
    if (!item) return null;
    try {{
      item.inPoint = buildTimeFromSeconds(inSeconds);
      item.outPoint = buildTimeFromSeconds(outSeconds);
    }} catch (e) {{
      log("Warning: Failed to set in/out for item at " + startSeconds);
    }}
    return item;
  }}

  function logClipDuration(item, targetSeconds, label) {{
    if (!item || !label) return;
    try {{
      var dur = null;
      if (item.duration && typeof item.duration.seconds === "number") {{
        dur = item.duration.seconds;
      }} else if (item.duration && typeof item.duration.ticks === "number") {{
        dur = item.duration.ticks / TICKS_PER_SECOND;
      }}
      if (typeof dur === "number") {{
        log(
          label +
            " duration " +
            dur.toFixed(4) +
            "s (target " +
            targetSeconds.toFixed(4) +
            "s)"
        );
      }}
    }} catch (e) {{}}
  }}

  function findProjectItem(name) {{
    var findInBin = function (bin) {{
      for (var i = 0; i < bin.children.numItems; i++) {{
        var item = bin.children[i];
        if (item.name === name) return item;
        if (item.type === ProjectItemType.BIN) {{
          var found = findInBin(item);
          if (found) return found;
        }}
      }}
      return null;
    }};
    return findInBin(app.project.rootItem);
  }}

  function getOrImportClip(clipName) {{
    var cleanName = clipName.replace(/^\\s+|\\s+$/g, "");
    var nameNoExt = cleanName.replace(/\\.[^\\.]+$/, "");

    var item = findProjectItem(cleanName);
    if (item) return item;
    item = findProjectItem(nameNoExt);
    if (item) return item;

    var searchPaths = [
      ROOT_DIR + "/" + cleanName,
      ROOT_DIR + "/" + cleanName + ".wav",
      SOURCES_DIR + "/" + cleanName,
      SOURCES_DIR + "/" + nameNoExt + ".mkv",
      SOURCES_DIR + "/" + nameNoExt + ".mp4",
    ];

    for (var i = 0; i < searchPaths.length; i++) {{
      var f = new File(searchPaths[i]);
      if (f.exists) {{
        app.project.importFiles([f.fsName], true, app.project.rootItem, false);
        item = findProjectItem(f.name);
        if (!item) item = findProjectItem(f.displayName);
        if (!item) item = findProjectItem(nameNoExt);
        return item;
      }}
    }}
    log("Error: Clip not found: " + cleanName);
    return null;
  }}

  // ========================================================================
  // 3. MAIN LOGIC
  // ========================================================================
  function main() {{
    app.enableQE();
    if (!app.project) {{
      alert("Open a project.");
      return;
    }}

    var seqName = "ATR_Layered_" + Math.floor(Math.random() * 9999);
    var presetFile = new File(SEQUENCE_PRESET_PATH);
    var sequence;

    if (presetFile.exists) {{
      qe.project.newSequence(seqName, presetFile.fsName);
      sequence = app.project.activeSequence;
    }} else {{
      sequence = app.project.createNewSequence(seqName, "ID_1");
    }}

    // --- ENSURE TRACKS (V=4, A=2) ---
    ensureVideoTracks(sequence, 4);
    ensureAudioTracks(sequence, 2);

    // Mapping Tracks
    // V1: Index 0 (Back)
    // V2: Index 1 (Border)
    // V3: Index 2 (Main)
    // V4: Index 3 (Subs)

    var v1 = sequence.videoTracks[0];
    var v2 =
      sequence.videoTracks.numTracks > 1 ? sequence.videoTracks[1] : null;
    var v3 =
      sequence.videoTracks.numTracks > 2 ? sequence.videoTracks[2] : null;
    var v4 =
      sequence.videoTracks.numTracks > 3 ? sequence.videoTracks[3] : null;

    var a1 = sequence.audioTracks[0];
    var a2 = sequence.audioTracks.numTracks > 1 ? sequence.audioTracks[1] : a1;

    // --- MUTE A1 (Clip Audio) ---
    try {{
      a1.setMute(1);
    }} catch (e) {{}}

    // --- MARKERS ---
    log("Creating Markers...");
    for (var i = 0; i < scenes.length; i++) {{
      var mStart = snapSecondsToFrame(scenes[i].start);
      var mEnd = snapSecondsToFrame(scenes[i].end);
      var m = sequence.markers.createMarker(mStart);
      m.name = "Scene " + scenes[i].scene_index;
      m.duration = mEnd - mStart;
    }}

    // --- INTERLEAVED PROCESSING (V1 & V3) ---
    // V1 (Background) & V3 (Main)
    log("Processing Scenes (Layering & Speed)...");
    var nameCleaner = function (n) {{
      return n.replace(/\\.[^\\.]+$/, "");
    }}; // Helper

    for (var i = 0; i < scenes.length; i++) {{
      var s = scenes[i];
      var startSec = snapSecondsToFrame(s.start);
      var clip = getOrImportClip(s.clipName);
      var cleanName = nameCleaner(s.clipName);

      if (clip) {{
        // 1. PLACE ON V3 (Main)
        if (v3) v3.overwriteClip(clip, startSec);

        // 2. PLACE ON V1 (Background)
        if (v1) v1.overwriteClip(clip, startSec);

        // 2b. SET PER-INSTANCE IN/OUT (TrackItem) TO AVOID UNIT AMBIGUITY
        sleep(200);
        var v3Item = null;
        var v1Item = null;
        var a1Item = null;
        var a2Item = null;
        if (v3) {{
          v3Item = setTrackItemInOut(
            v3,
            startSec,
            s.source_in,
            s.source_out,
            cleanName
          );
          if (!v3Item) {{
            sleep(200);
            v3Item = setTrackItemInOut(
              v3,
              startSec,
              s.source_in,
              s.source_out,
              cleanName
            );
          }}
        }}
        if (v1) {{
          v1Item = setTrackItemInOut(
            v1,
            startSec,
            s.source_in,
            s.source_out,
            cleanName
          );
          if (!v1Item) {{
            sleep(200);
            v1Item = setTrackItemInOut(
              v1,
              startSec,
              s.source_in,
              s.source_out,
              cleanName
            );
          }}
        }}
        if (a1) {{
          a1Item = setTrackItemInOut(
            a1,
            startSec,
            s.source_in,
            s.source_out,
            cleanName
          );
          if (!a1Item) {{
            sleep(200);
            a1Item = setTrackItemInOut(
              a1,
              startSec,
              s.source_in,
              s.source_out,
              cleanName
            );
          }}
        }}
        if (a2 && a2 !== a1) {{
          a2Item = setTrackItemInOut(
            a2,
            startSec,
            s.source_in,
            s.source_out,
            cleanName
          );
          if (!a2Item) {{
            sleep(200);
            a2Item = setTrackItemInOut(
              a2,
              startSec,
              s.source_in,
              s.source_out,
              cleanName
            );
          }}
        }}

        // Force backend update & clear selection to avoid "Invalid TrackItem" assertion
        // The assertion often happens if a previous selection is invalid.
        sleep(1000);
        clearSelection(sequence);
        sleep(200);

        // 3. ENFORCE DURATION (ALL SPEEDS)
        // Always enforce the target timeline duration, even at 1.0x.
        // If in/out fails or speed is exactly 1.0, this prevents huge clip lengths.
        var newDurationSeconds = snapSecondsToFrame(s.clip_duration / s.effective_speed);
        if (v3Item) {{ try {{ var newEnd = v3Item.start.seconds + newDurationSeconds; v3Item.end = buildTimeFromSeconds(newEnd); }} catch (e) {{}} }}
        if (v1Item) {{ try {{ var newEnd = v1Item.start.seconds + newDurationSeconds; v1Item.end = buildTimeFromSeconds(newEnd); }} catch (e) {{}} }}
        if (a1Item) {{ try {{ var newEnd = a1Item.start.seconds + newDurationSeconds; a1Item.end = buildTimeFromSeconds(newEnd); }} catch (e) {{}} }}
        if (a2Item) {{ try {{ var newEnd = a2Item.start.seconds + newDurationSeconds; a2Item.end = buildTimeFromSeconds(newEnd); }} catch (e) {{}} }}

        // 4. APPLY SPEED (Both V1, V3, A1, A2)
        // QE setSpeed often fails to ripple-edit duration for speedups, so we pre-resize above.
        if (Math.abs(s.effective_speed - 1.0) > 0.01) {{
          if (v3)
            safeApplySpeedQE(startSec, s.effective_speed, 2, "Video", cleanName);
          if (v1)
            safeApplySpeedQE(startSec, s.effective_speed, 0, "Video", cleanName);
          if (a1 && a1Item)
            safeApplySpeedQE(startSec, s.effective_speed, 0, "Audio", cleanName);
          if (a2 && a2Item && a2 !== a1)
            safeApplySpeedQE(startSec, s.effective_speed, 1, "Audio", cleanName);
        }}

        // 4. APPLY SCALE (Standard API)
        // Need to find the items we just placed.
        if (v3) setScaleAndPosition(v3, startSec, 68); // Main Scaled Down
        if (v1) setScaleAndPosition(v1, startSec, 183); // Background Scaled Up

        sleep(200);
        if (v3) {{
          var v3ItemForLog = findTrackItemAtStart(v3, startSec, cleanName);
          if (v3ItemForLog)
            logClipDuration(v3ItemForLog, s.target_duration, "Scene " + s.scene_index);
        }}
      }}
    }}

    // --- V2: BORDER MOGRT ---
    if (v2 && new File(BORDER_MOGRT_PATH).exists) {{
      log("Adding Border Mogrt to V2...");
      try {{
        // Insert once at 0
        var totalDuration =
          scenes.length > 0 ? snapSecondsToFrame(scenes[scenes.length - 1].end) : 0;
        if (totalDuration > 0) {{
          var mgt = sequence.importMGT(BORDER_MOGRT_PATH, 0, 1, 0); // Index 1 starts V2 ?? Wait, numTracks test used sequence.videoTracks[1]?
          // No, importMGT(path, time, videoTrackIndex, audioTrackIndex)
          // The script previously used index 1.
          if (mgt) {{
            mgt.end = totalDuration;
            log("Border Mogrt inserted. Duration: " + totalDuration);
          }}
        }}
      }} catch (e) {{
        log("Border Mogrt Error: " + e.message);
      }}
    }}

    // --- IMPORT TTS (A2) & CLEANUP A3 ---
    log("Importing TTS to A2...");
    if (a2) {{
      var ttsItem = getOrImportClip(AUDIO_FILENAME);
      if (ttsItem) {{
        a2.overwriteClip(ttsItem, 0);
      }}
    }}
    // Cleanup all audio tracks except A1 and A2 (TTS)
    cleanupAudioTracks(a2, 1, AUDIO_FILENAME);

    // --- V4: SUBTITLES (Reserved) ---
    // Subtitles will be added manually later.
    // Logic removed as requested.

    alert("Script Complete (v7 Layered - Fixes Applied).");
  }}

  // ========================================================================
  // 4. HELPERS
  // ========================================================================

  function clearSelection(sequence) {{
    if (!sequence) return;
    try {{
      var tracks = sequence.videoTracks;
      for (var i = 0; i < tracks.numTracks; i++) {{
        var track = tracks[i];
        for (var j = 0; j < track.clips.numItems; j++) {{
          track.clips[j].setSelected(false, true);
        }}
      }}
      // Clear Audio as well if needed? Usually audio tracks are less prone to this crash but good practice.
      // Skipping to save time/performance unless necessary.
    }} catch (e) {{}}
  }}

  function safeApplySpeedQE(
    startTime,
    speed,
    trackIndex,
    trackType,
    clipNameRef
  ) {{
    try {{
      var qeSeq = qe.project.getActiveSequence();
      if (!qeSeq) return;
      var qeTrack;
      if (trackType === "Audio") qeTrack = qeSeq.getAudioTrackAt(trackIndex);
      else qeTrack = qeSeq.getVideoTrackAt(trackIndex);
      if (!qeTrack) return;

      // Search for the item with Name validation and Time tolerance
      // Iterate ALL items to find the best match or correct item
      for (var i = 0; i < qeTrack.numItems; i++) {{
        try {{
          var item = qeTrack.getItemAt(i);
          // Defensive: access properties safely
          if (!item || typeof item.start === "undefined") continue;

          // Time Check using Ticks (Robust)
          var startTicks = null;
          // 1. Try Ticks (String or Number)
          try {{
            if (item.start.ticks !== undefined) {{
              startTicks = parseInt(item.start.ticks, 10);
            }}
          }} catch (e0) {{}}

          // 2. Fallback to Seconds
          if (isNaN(startTicks) || startTicks === null) {{
            try {{
              if (typeof item.start.seconds === "number")
                startTicks = secondsToTicks(item.start.seconds);
              else if (typeof item.start.secs === "number")
                startTicks = secondsToTicks(item.start.secs);
            }} catch (e1) {{}}
          }}

          var matchTime = false;
          if (typeof startTicks === "number" && !isNaN(startTicks)) {{
            // Use relaxed tolerance (0.2s)
            matchTime =
              Math.abs(startTicks - secondsToTicks(startTime)) <
              secondsToTicks(0.2);
          }} else {{
            // Last resort fallback
            try {{
              matchTime = Math.abs(item.start.secs - startTime) < 0.2;
            }} catch (e3) {{}}
          }}

          if (matchTime) {{
            // Name Check (if ref provided)
            if (clipNameRef) {{
              var itemName = item.name ? item.name.toString() : "";

              // CRITICAL FIX: Ignore empty names which passed checks previously
              if (itemName.replace(/\\s/g, "") === "") {{
                // log("Skipping item with empty name at " + startTime);
                continue;
              }}

              // Check if one contains the other (handle extensions)
              // clipNameRef is "cleanName" (no extension). itemName might have extension.
              // We need to be careful: "clip" vs "clip.mp4"
              var match = false;

              // 1. Exact or Substring match
              if (itemName.indexOf(clipNameRef) !== -1) match = true;
              if (clipNameRef.indexOf(itemName) !== -1) match = true;

              if (!match) {{
                // log("Skipping speed on mismatch: '" + itemName + "' vs '" + clipNameRef + "'");
                continue;
              }}
            }}

            try {{
              // args: speed, stretch, reverse, ripple, flicker
              item.setSpeed(speed, "", false, false, false);
              // log("Speed Applied: " + (speed*100).toFixed(1) + "% to " + item.name + " at " + startTime);
            }} catch (err) {{
              log("Speed Apply Error: " + err.message);
            }}
            return; // Done
          }}
        }} catch (e) {{}} // Ignore individual item access errors
      }}
      log(
        "Warning: Could not find clip at " +
          startTime +
          " (" +
          clipNameRef +
          ") for Speed change."
      );
    }} catch (e) {{
      log("QE Speed Fail: " + e.message);
    }}
  }}

  function setScaleAndPosition(track, startTime, scaleVal) {{
    // Find item in Track (Standard API)
    for (var i = 0; i < track.clips.numItems; i++) {{
      var item = track.clips[i];
      // Standard API timings are in seconds (usually) or ticks.
      // item.start.seconds is available in 2025?
      // Use ticks if needed, but 'seconds' property usually works.
      var itemStartTicks = getStartTicks(item);
      if (
        (typeof itemStartTicks === "number" &&
          Math.abs(itemStartTicks - secondsToTicks(startTime)) <
            secondsToTicks(0.2)) ||
        (item.start &&
          typeof item.start.seconds === "number" &&
          Math.abs(item.start.seconds - startTime) < 0.2)
      ) {{
        var m = item.components[1]; // Motion is usually index 1 (Opacity is 0 or 2?)
        // Actually index varies. Search for "Motion" or "Trajectoire"
        for (var c = 0; c < item.components.numItems; c++) {{
          if (
            item.components[c].displayName === "Motion" ||
            item.components[c].displayName === "Trajectoire"
          ) {{
            m = item.components[c];
            break;
          }}
        }}
        if (m) {{
          // Scale is usually prop 0 or 1.
          // Position is usually prop 0. Scale prop 1.
          // "Scale" or "Echelle"
          for (var p = 0; p < m.properties.numItems; p++) {{
            var prop = m.properties[p];
            if (
              prop.displayName === "Scale" ||
              prop.displayName === "Echelle" ||
              prop.displayName === "\\u00c9chelle"
            ) {{
              prop.setValue(scaleVal, true);
              break;
            }}
          }}
        }}
        return;
      }}
    }}
  }}

  function ensureVideoTracks(sequence, desiredCount) {{
    if (!sequence || !sequence.videoTracks) return;
    var existing = sequence.videoTracks.numTracks;
    if (existing >= desiredCount) return;

    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) return;
    var toAdd = desiredCount - existing;
    try {{
      // addTracks(videoCount, insertAfterVideoIdx, audioCount)
      qeSeq.addTracks(toAdd, Math.max(0, existing - 1), 0);
    }} catch (e) {{
      // fallback
      for (var i = 0; i < toAdd; i++) {{
        try {{
          qeSeq.addTracks(1, Math.max(0, existing - 1 + i), 0);
        }} catch (e2) {{}}
      }}
    }}
  }}

  function ensureAudioTracks(sequence, desiredCount) {{
    if (!sequence || !sequence.audioTracks) return;
    var existing = sequence.audioTracks.numTracks;
    if (existing >= desiredCount) return;

    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) return;
    var toAdd = desiredCount - existing;
    try {{
      // addTracks(video, insertAfterVideo, audio, insertAfterAudio)
      qeSeq.addTracks(0, 0, toAdd, Math.max(0, existing - 1));
    }} catch (e) {{
      for (var i = 0; i < toAdd; i++) {{
        try {{
          qeSeq.addTracks(0);
        }} catch (e2) {{}}
      }}
    }}
  }}

  function cleanupAudioTracks(ttsTrack, ttsTrackIndex, ttsName) {{
    var seq = app.project.activeSequence;
    if (!seq || !seq.audioTracks) return;
    for (var i = 0; i < seq.audioTracks.numTracks; i++) {{
      if (i === 0) continue; // keep A1
      var track = seq.audioTracks[i];
      if (!track || !track.clips) continue;
      for (var j = track.clips.numItems - 1; j >= 0; j--) {{
        var clip = track.clips[j];
        var nm = clip && clip.projectItem ? clip.projectItem.name : "";
        var keep = i === ttsTrackIndex && nm === ttsName;
        if (!keep) {{
          try {{
            clip.remove(false, true);
          }} catch (e1) {{
            try {{
              clip.remove();
            }} catch (e2) {{}}
          }}
        }}
      }}
    }}
  }}

  main();
}})();
'''
        return jsx_content

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
        - No temporal gaps: end of block N = start of block N+1 (except obvious silence > 0.3s)
        - Never isolate a determiner at the end of a block

        Args:
            transcription: Transcription with word timings
            language: Language code for determiner detection (fr, en, es)

        Returns:
            SRT file content
        """
        srt_blocks = []
        block_index = 1

        # Collect all words with timings across all scenes
        all_words = []
        for scene_trans in transcription.scenes:
            all_words.extend(scene_trans.words)

        if not all_words:
            return ""

        i = 0
        while i < len(all_words):
            current_block = []
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
                            # Pop the clause starter - it will start the next block
                            i -= 1  # Rewind to re-process it
                            current_block.pop()
                            # Recalculate current_len using cleaned text
                            current_len = sum(len(w.text) for w in current_block)
                            if len(current_block) > 1:
                                current_len += len(current_block) - 1  # spaces
                    break

            # Create the SRT block (only if we have words)
            if current_block:
                # Determine end time:
                # - If there's a next word, check for gap
                # - If gap > 0.3s (obvious silence), use current block's last word end
                # - Otherwise, extend to next word's start (no temporal gap)
                if i < len(all_words):
                    gap = all_words[i].start - current_block[-1].end
                    if gap > 0.3:
                        # Obvious silence - don't force continuity
                        end_time = current_block[-1].end
                    else:
                        # Continuity: extend to next word's start
                        end_time = all_words[i].start
                else:
                    # Last block - use natural end
                    end_time = current_block[-1].end

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
    def get_assets_dir(cls) -> Path:
        """Get the static assets directory (contains .sqpreset, .mogrt, etc.)."""
        # Assets are in the repository root /assets folder
        return Path(__file__).parent.parent.parent.parent / "assets"

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
    ) -> AsyncIterator[ProcessingProgress]:
        """
        Run the full processing pipeline.

        Generates a Premiere Pro project bundle with:
        - JSX automation script (v7.1 format matching working_script.jsx)
        - Processed TTS audio with auto-editor cuts
        - SRT subtitles
        - Source video clips
        - Required assets (sequence preset, border MOGRT)

        Track layout (created by JSX script):
        - V4: Reserved for subtitles
        - V3: Main video (Scale 68%)
        - V2: White border MOGRT
        - V1: Background (Scale 183%)
        - A1: Original anime audio (MUTED)
        - A2: TTS audio

        Args:
            project: Project data
            new_script: New restructured script JSON
            audio_path: Path to uploaded TTS audio
            matches: Scene matches with source timings

        Yields:
            ProcessingProgress updates
        """
        output_dir = cls.get_output_dir(project.id)
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = cls.get_assets_dir()

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
                        start_time=s["words"][0]["start"] if s["words"] else 0.0,
                        end_time=s["words"][-1]["end"] if s["words"] else 0.0,
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
                auto_editor_xml_path = output_dir / "auto_editor_cuts.xml"
                await cls.run_auto_editor(audio_path, edited_audio_path, auto_editor_xml_path)

                yield ProcessingProgress(
                    "processing",
                    "transcription",
                    0.3,
                    "Extracting word timings from audio...",
                )

                # Step 2: Transcribe edited audio for timings
                transcriber = TranscriberService()

                # Run transcription in thread pool
                loop = asyncio.get_event_loop()
                new_transcription = await loop.run_in_executor(
                    None,
                    transcriber.transcribe_with_alignment,
                    edited_audio_path,
                    new_script,
                )
                cls.normalize_transcription_timings(new_transcription)

                # Save transcription for gap detection
                transcription_timing_path = output_dir / "transcription_timing.json"
                transcription_data = {
                    "language": new_transcription.language,
                    "scenes": [
                        {
                            "scene_index": s.scene_index,
                            "text": s.text,
                            "words": [{"text": w.text, "start": w.start, "end": w.end, "confidence": w.confidence} for w in s.words],
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
                    # Gaps detected - pause processing for user to resolve
                    total_gap_duration = sum(g.gap_duration for g in gaps)

                    # Backup current matches before gap resolution modifies them
                    # This allows the user to reset and start over
                    matches_backup_path = project_dir / "matches_before_gaps.json"
                    if not matches_backup_path.exists():
                        # Only backup if we haven't already (avoid overwriting with modified matches)
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
            source_fps = None
            for match in matches:
                if match.episode:
                    episode_path = Path(match.episode)
                    if episode_path.exists():
                        source_fps = await cls.detect_video_fps(episode_path)
                        break  # Use first valid episode's FPS

            # Step 4: Generate JSX script (v7.1 format - matching working_script.jsx)
            jsx_content = cls.generate_jsx_script(
                project, new_transcription, matches, source_fps=source_fps
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
            srt_path = output_dir / "subtitles.srt"
            srt_path.write_text(srt_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "bundling",
                0.7,
                "Creating project bundle...",
            )

            # Step 5: Bundle everything
            bundle_path = cls.get_output_dir(project.id).parent / "project_bundle.zip"

            # Collect unique source episodes
            source_episodes: set[str] = set()
            for match in matches:
                if match.episode:
                    source_episodes.add(match.episode)

            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add JSX script (main entry point)
                zf.write(jsx_path, "import_project.jsx")

                # Add edited TTS audio (root level)
                zf.write(edited_audio_path, "tts_edited.wav")

                # Add SRT subtitles
                zf.write(srt_path, "subtitles.srt")

                # Add static assets to assets/ folder
                sequence_preset = assets_dir / "TikTok60fps.sqpreset"
                if sequence_preset.exists():
                    zf.write(sequence_preset, "assets/TikTok60fps.sqpreset")

                border_mogrt = assets_dir / "White border 5px.mogrt"
                if border_mogrt.exists():
                    zf.write(border_mogrt, "assets/White border 5px.mogrt")

                # Add source episode files to sources/ folder
                # Episode names in matches may be just filenames without extension,
                # so we need to resolve them to full paths using the anime library
                # Track by resolved path to avoid duplicates when same episode
                # appears with different name formats (e.g., name vs full path)
                episode_paths_in_bundle = {}
                for episode_name in source_episodes:
                    # Try to resolve the episode name to a full path
                    resolved_path = GapResolutionService.resolve_episode_path(episode_name)

                    if resolved_path and resolved_path.exists():
                        # Skip if we already added this resolved path
                        resolved_str = str(resolved_path)
                        if resolved_str in episode_paths_in_bundle:
                            continue

                        # Use just the filename in sources/ folder
                        dest_name = f"sources/{resolved_path.name}"
                        zf.write(resolved_path, dest_name)
                        episode_paths_in_bundle[resolved_str] = dest_name
                    else:
                        # Log warning but continue - some episodes may not be found
                        import sys
                        print(f"[WARNING] Could not resolve episode: {episode_name}", file=sys.stderr)

                # Add episode mapping file (for debugging)
                zf.writestr(
                    "source_mapping.json",
                    json.dumps(episode_paths_in_bundle, indent=2),
                )

                # Count scenes for README
                scene_count = len([m for m in matches if m.episode])
                # Use the bundle paths we actually included
                episode_list = "\n".join(f"  - {Path(bundle_path).name}" for bundle_path in episode_paths_in_bundle.values())

                readme = f"""Anime TikTok Reproducer - Project Bundle
=========================================

Project ID: {project.id}
Generated: {datetime.now().isoformat()}
Scenes: {scene_count}

=== CONTENTS ===

import_project.jsx     - Premiere Pro 2025 automation script (RUN THIS)
tts_edited.wav         - Processed TTS audio (silences removed)
subtitles.srt          - Word-timed subtitles
source_mapping.json    - Original path to bundle path mapping

assets/
  TikTok60fps.sqpreset - Sequence preset (1080x1920 @ 60fps)
  White border 5px.mogrt - Border MOGRT for V2 track

sources/
{episode_list}

=== USAGE (Premiere Pro 2025) ===

1. Extract this entire ZIP to a folder
2. Open Adobe Premiere Pro 2025
3. Create or open a project
4. File > Scripts > Run Script... > Select "import_project.jsx"
5. The script will automatically:
   - Create a new sequence with correct settings
   - Import and place all clips
   - Apply speed adjustments
   - Set scale values
   - Add the border MOGRT
   - Place TTS audio

=== TRACK LAYOUT ===

V4 [Subtitles]       - Reserved for manually added subtitles
V3 [Main Video]      - Anime clips at 68% scale (centered)
V2 [White Border]    - Border MOGRT spanning entire duration
V1 [Blurred BG]      - Same clips at 183% scale

A2 [TTS Audio]       - Generated voice with silence removed
A1 [Anime Audio]     - Original audio from clips (MUTED)

=== AFTER RUNNING SCRIPT ===

1. Check clip positions match markers
2. Import subtitles.srt if needed:
   - File > Import > subtitles.srt
   - Drag to V4 video track
3. Review any clips with gaps (75% speed floor was hit)
4. Add transitions and polish
5. Export!

=== TECHNICAL NOTES ===

- Sequence: 60fps, 1080x1920 (vertical TikTok format)
- Speed Logic: source_duration / target_duration
- 75% Floor: Clips won't slow below 75% (leaves gap)
- Scale: V3 at 68%, V1 at 183%
- Border: Single MOGRT spanning entire timeline
"""
                zf.writestr("README.txt", readme)

            download_url = f"/api/projects/{project.id}/download/bundle"

            # Clear processing state now that we're done
            cls.clear_processing_state(project.id)

            yield ProcessingProgress(
                "complete",
                "bundling",
                1.0,
                "Processing complete!",
                download_url=download_url,
            )

        except Exception as e:
            yield ProcessingProgress(
                "error",
                "",
                0,
                "",
                error=str(e),
            )
