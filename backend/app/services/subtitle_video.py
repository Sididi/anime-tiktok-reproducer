"""
Main service for generating transparent subtitle videos.
Uses Pillow for frame rendering and FFmpeg for video encoding.
"""

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator, Callable

from ..models.subtitle import (
    SubtitleGenerationProgress,
    SubtitleStyle,
    SubtitleWord,
)
from ..models import Transcription
from .subtitle_renderer import SubtitleFrameRenderer
from .subtitle_styles import get_style, list_styles
from .project_service import ProjectService


class SubtitleVideoService:
    """Service for generating transparent subtitle videos."""

    FPS = 60
    FRAME_PATTERN = "%06d.png"

    @classmethod
    def _get_output_dir(cls, project_id: str) -> Path:
        """Get output directory for subtitle videos."""
        output_dir = ProjectService.get_project_dir(project_id) / "subtitles"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    @classmethod
    def _get_preview_dir(cls, project_id: str) -> Path:
        """Get directory for style preview videos."""
        preview_dir = cls._get_output_dir(project_id) / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        return preview_dir

    @classmethod
    def _transcription_to_words(cls, transcription: Transcription) -> list[SubtitleWord]:
        """Convert Transcription model to flat list of SubtitleWord."""
        words = []
        for scene in transcription.scenes:
            for word in scene.words:
                words.append(SubtitleWord(
                    text=word.text,
                    start=word.start,
                    end=word.end,
                ))
        return words

    @classmethod
    def _get_video_duration(cls, words: list[SubtitleWord]) -> float:
        """Get total duration from word timings."""
        if not words:
            return 0.0
        return max(w.end for w in words)

    @classmethod
    async def _render_frames(
        cls,
        renderer: SubtitleFrameRenderer,
        words: list[SubtitleWord],
        duration: float,
        output_dir: Path,
        progress_callback: Callable[[float], None] | None = None,
    ) -> int:
        """
        Render all frames to PNG files.

        Returns number of frames rendered.
        """
        total_frames = int(duration * cls.FPS)
        if total_frames <= 0:
            return 0

        loop = asyncio.get_event_loop()

        for frame_idx in range(total_frames):
            current_time = frame_idx / cls.FPS

            # Render frame (CPU-bound, run in executor)
            frame = await loop.run_in_executor(
                None,
                renderer.render_frame,
                words,
                current_time,
            )

            # Save frame
            frame_path = output_dir / (cls.FRAME_PATTERN % frame_idx)
            await loop.run_in_executor(None, frame.save, str(frame_path), "PNG")

            # Report progress
            if progress_callback and frame_idx % 30 == 0:  # Every 0.5 seconds
                progress = frame_idx / total_frames
                await progress_callback(progress)

        return total_frames

    @classmethod
    async def _encode_video(
        cls,
        frames_dir: Path,
        output_path: Path,
        output_format: str,
        fps: int = 60,
    ) -> bool:
        """
        Encode frames to video with FFmpeg.

        Args:
            frames_dir: Directory containing PNG frames
            output_path: Output video path
            output_format: "webm" or "mov"
            fps: Frames per second

        Returns:
            True if successful
        """
        input_pattern = str(frames_dir / cls.FRAME_PATTERN)

        if output_format == "webm":
            # WebM VP9 with alpha channel
            cmd = [
                "ffmpeg",
                "-y",
                "-framerate", str(fps),
                "-i", input_pattern,
                "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p",
                "-b:v", "2M",
                "-auto-alt-ref", "0",
                str(output_path),
            ]
        elif output_format == "mov":
            # MOV ProRes 4444 with alpha channel
            cmd = [
                "ffmpeg",
                "-y",
                "-framerate", str(fps),
                "-i", input_pattern,
                "-c:v", "prores_ks",
                "-profile:v", "4",  # ProRes 4444
                "-pix_fmt", "yuva444p10le",
                str(output_path),
            ]
        else:
            raise ValueError(f"Unsupported output format: {output_format}")

        # Use asyncio.create_subprocess_exec for safe command execution
        # All arguments are passed as separate list elements (no shell injection)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {stderr.decode()}")

        return True

    @classmethod
    async def generate_subtitle_video(
        cls,
        project_id: str,
        style_id: str,
        output_format: str = "webm",
        use_new_tts: bool = True,
    ) -> AsyncIterator[SubtitleGenerationProgress]:
        """
        Generate a transparent subtitle video for a project.

        Args:
            project_id: Project ID
            style_id: Subtitle style ID
            output_format: "webm" or "mov"
            use_new_tts: Use new TTS transcription (True) or original (False)

        Yields:
            Progress updates
        """
        yield SubtitleGenerationProgress(
            status="starting",
            progress=0.0,
            message="Loading project data...",
        )

        # Get style
        style = get_style(style_id)
        if not style:
            yield SubtitleGenerationProgress(
                status="error",
                progress=0.0,
                message="",
                error=f"Unknown style: {style_id}",
            )
            return

        # Load transcription
        if use_new_tts:
            # Load new TTS transcription from processing output
            new_tts_file = ProjectService.get_project_dir(project_id) / "new_tts_transcription.json"
            if new_tts_file.exists():
                transcription = Transcription.model_validate_json(new_tts_file.read_text())
            else:
                yield SubtitleGenerationProgress(
                    status="error",
                    progress=0.0,
                    message="",
                    error="New TTS transcription not found. Please complete processing first.",
                )
                return
        else:
            transcription = ProjectService.load_transcription(project_id)
            if not transcription:
                yield SubtitleGenerationProgress(
                    status="error",
                    progress=0.0,
                    message="",
                    error="Transcription not found",
                )
                return

        # Convert to words
        words = cls._transcription_to_words(transcription)
        if not words:
            yield SubtitleGenerationProgress(
                status="error",
                progress=0.0,
                message="",
                error="No words found in transcription",
            )
            return

        duration = cls._get_video_duration(words)

        yield SubtitleGenerationProgress(
            status="rendering",
            progress=0.05,
            message=f"Rendering {int(duration * cls.FPS)} frames...",
        )

        # Create temp directory for frames
        temp_dir = None
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="subtitle_frames_"))

            # Create renderer
            renderer = SubtitleFrameRenderer(style)

            # Progress callback
            async def report_progress(frame_progress: float):
                # Frames are 5-80% of total progress
                total_progress = 0.05 + frame_progress * 0.75

            # Render frames
            await cls._render_frames(
                renderer, words, duration, temp_dir, report_progress
            )

            yield SubtitleGenerationProgress(
                status="encoding",
                progress=0.80,
                message=f"Encoding to {output_format.upper()}...",
            )

            # Encode video
            output_dir = cls._get_output_dir(project_id)
            output_filename = f"subtitles_{style_id}.{output_format}"
            output_path = output_dir / output_filename

            await cls._encode_video(temp_dir, output_path, output_format)

            yield SubtitleGenerationProgress(
                status="complete",
                progress=1.0,
                message="Subtitle video generated successfully",
                output_file=output_filename,
            )

        except Exception as e:
            yield SubtitleGenerationProgress(
                status="error",
                progress=0.0,
                message="",
                error=str(e),
            )

        finally:
            # Clean up temp directory
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    @classmethod
    async def generate_style_previews(
        cls,
        project_id: str,
        duration: float = 7.0,
    ) -> AsyncIterator[SubtitleGenerationProgress]:
        """
        Generate preview videos for all 15 styles.

        Args:
            project_id: Project ID
            duration: Preview duration in seconds

        Yields:
            Progress updates
        """
        yield SubtitleGenerationProgress(
            status="starting",
            progress=0.0,
            message="Loading transcription...",
        )

        # Try new TTS first, fallback to original
        new_tts_file = ProjectService.get_project_dir(project_id) / "new_tts_transcription.json"
        if new_tts_file.exists():
            transcription = Transcription.model_validate_json(new_tts_file.read_text())
        else:
            transcription = ProjectService.load_transcription(project_id)

        if not transcription:
            yield SubtitleGenerationProgress(
                status="error",
                progress=0.0,
                message="",
                error="No transcription found",
            )
            return

        words = cls._transcription_to_words(transcription)
        if not words:
            yield SubtitleGenerationProgress(
                status="error",
                progress=0.0,
                message="",
                error="No words found in transcription",
            )
            return

        # Limit words to preview duration
        preview_words = [w for w in words if w.start < duration]
        if not preview_words:
            preview_words = words[:20]  # Fallback to first 20 words

        styles = list_styles()
        total_styles = len(styles)
        preview_dir = cls._get_preview_dir(project_id)

        for idx, style in enumerate(styles):
            style_progress = idx / total_styles
            yield SubtitleGenerationProgress(
                status="rendering",
                progress=style_progress,
                message=f"Generating preview for {style.name}... ({idx + 1}/{total_styles})",
            )

            temp_dir = None
            try:
                temp_dir = Path(tempfile.mkdtemp(prefix=f"preview_{style.id}_"))

                # Render frames
                renderer = SubtitleFrameRenderer(style)
                await cls._render_frames(renderer, preview_words, duration, temp_dir)

                # Encode to WebM (browser-friendly)
                output_path = preview_dir / f"{style.id}.webm"
                await cls._encode_video(temp_dir, output_path, "webm")

            except Exception as e:
                yield SubtitleGenerationProgress(
                    status="rendering",
                    progress=style_progress,
                    message=f"Failed to generate {style.name}: {e}",
                )
                continue

            finally:
                if temp_dir and temp_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)

        yield SubtitleGenerationProgress(
            status="complete",
            progress=1.0,
            message=f"Generated {total_styles} style previews",
        )

    @classmethod
    def get_available_files(cls, project_id: str) -> dict:
        """
        Get list of available subtitle video files.

        Returns:
            Dict with 'videos' and 'previews' lists
        """
        output_dir = cls._get_output_dir(project_id)
        preview_dir = cls._get_preview_dir(project_id)

        videos = []
        if output_dir.exists():
            for f in output_dir.iterdir():
                if f.is_file() and f.suffix in [".webm", ".mov"]:
                    videos.append(f.name)

        previews = []
        if preview_dir.exists():
            for f in preview_dir.iterdir():
                if f.is_file() and f.suffix == ".webm":
                    previews.append(f.name)

        return {
            "videos": sorted(videos),
            "previews": sorted(previews),
        }
