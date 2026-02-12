"""TikTok video downloader service using yt-dlp."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..config import settings
from ..utils.subprocess_runner import CommandTimeoutError, run_command, terminate_process


@dataclass
class DownloadProgress:
    """Progress information for video download."""

    status: str  # starting, downloading, complete, error
    progress: float = 0.0  # 0-1
    message: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
        }


class DownloaderService:
    """Service for downloading TikTok videos using yt-dlp."""

    DOWNLOAD_TIMEOUT_SECONDS = 1800.0
    FFPROBE_TIMEOUT_SECONDS = 30.0

    @staticmethod
    def get_output_path(project_id: str) -> Path:
        """Get the output path for a downloaded video."""
        return settings.projects_dir / project_id / "tiktok.mp4"

    @staticmethod
    async def _has_audio_stream(video_path: Path) -> bool | None:
        """Return whether a media file contains at least one audio stream."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video_path),
        ]
        try:
            result = await run_command(cmd, timeout_seconds=DownloaderService.FFPROBE_TIMEOUT_SECONDS)
        except (CommandTimeoutError, FileNotFoundError):
            return None

        if result.returncode != 0:
            return None

        return bool(result.stdout.decode().strip())

    @classmethod
    async def download(
        cls,
        url: str,
        project_id: str,
    ) -> AsyncIterator[DownloadProgress]:
        """
        Download a TikTok video and yield progress updates.

        Args:
            url: TikTok video URL
            project_id: Project ID for storing the video

        Yields:
            DownloadProgress objects with status updates
        """
        yield DownloadProgress("starting", 0, "Preparing download...")

        output_path = cls.get_output_path(project_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # yt-dlp command with progress output.
        # Format selector prefers audio+video but falls back to video-only if needed.
        format_selector = (
            "bv*[ext=mp4]+ba[ext=m4a]/"
            "bv*+ba/"
            "b[ext=mp4][acodec!=none]/"
            "b[acodec!=none]/"
            "bv*[ext=mp4]/"
            "bv*/"
            "b"
        )
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-playlist",
            "-f",
            format_selector,
            "--merge-output-format",
            "mp4",
            "-o", str(output_path),
            "--force-overwrites",
            url,
        ]

        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        aborted = False

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stderr_task = asyncio.create_task(
                process.stderr.read() if process.stderr is not None else asyncio.sleep(0, result=b"")
            )

            last_progress = 0.0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + cls.DOWNLOAD_TIMEOUT_SECONDS

            while True:
                if process.stdout is None:
                    break

                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError

                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                if not line:
                    break

                line_str = line.decode().strip()
                if not line_str:
                    continue

                # Parse yt-dlp progress output
                # Example: "[download]  45.2% of 12.50MiB at 2.50MiB/s ETA 00:03"
                if "[download]" in line_str and "%" in line_str:
                    try:
                        percent_str = line_str.split("%")[0].split()[-1]
                        progress = float(percent_str) / 100.0
                        if progress > last_progress:
                            last_progress = progress
                            yield DownloadProgress(
                                "downloading",
                                progress,
                                f"Downloading: {percent_str}%",
                            )
                    except (ValueError, IndexError):
                        pass

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)

            stderr = (await stderr_task).decode() if stderr_task is not None else ""
            if process.returncode != 0:
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error=f"yt-dlp failed with code {process.returncode}: {stderr}",
                )
                return

            # Verify file exists
            if not output_path.exists():
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error="Download completed but file not found",
                )
                return

            has_audio = await cls._has_audio_stream(output_path)
            if has_audio is False:
                output_path.unlink(missing_ok=True)
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error=(
                        "Downloaded video has no audio stream. "
                        "This TikTok may not have audio or the audio stream is unavailable. "
                        "Note: Audio is required for transcription in this project."
                    ),
                )
                return

            yield DownloadProgress("complete", 1.0, "Download complete!")

        except asyncio.CancelledError:
            aborted = True
            if process is not None:
                await terminate_process(process)
            raise
        except asyncio.TimeoutError:
            aborted = True
            if process is not None:
                await terminate_process(process)
            yield DownloadProgress(
                "error",
                0,
                "",
                error=f"Download timed out after {int(cls.DOWNLOAD_TIMEOUT_SECONDS)} seconds",
            )
        except FileNotFoundError:
            aborted = True
            yield DownloadProgress(
                "error",
                0,
                "",
                error="yt-dlp not found. Please install it: pip install yt-dlp",
            )
        except Exception as e:
            aborted = True
            if process is not None:
                await terminate_process(process)
            yield DownloadProgress("error", 0, "", error=str(e))
        finally:
            if aborted and stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task

    @staticmethod
    async def get_video_info(video_path: Path) -> dict:
        """
        Get video metadata using ffprobe.

        Args:
            video_path: Path to the video file

        Returns:
            Dict with duration, fps, width, height
        """
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]

        try:
            result = await run_command(
                cmd,
                timeout_seconds=DownloaderService.FFPROBE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout.decode())

            # Find video stream
            video_stream = None
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    video_stream = stream
                    break

            if not video_stream:
                return {}

            # Parse FPS from r_frame_rate (e.g., "30/1" or "30000/1001")
            fps = 30.0
            r_frame_rate = video_stream.get("r_frame_rate", "30/1")
            if "/" in r_frame_rate:
                num, den = r_frame_rate.split("/")
                if int(den) > 0:
                    fps = int(num) / int(den)

            return {
                "duration": float(data.get("format", {}).get("duration", 0)),
                "fps": fps,
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
            }

        except Exception:
            return {}
