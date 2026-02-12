"""TikTok video downloader service using yt-dlp."""

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..config import settings


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

    @staticmethod
    def get_output_path(project_id: str) -> Path:
        """Get the output path for a downloaded video."""
        return settings.projects_dir / project_id / "tiktok.mp4"

    @staticmethod
    def _has_audio_stream(video_path: Path) -> bool | None:
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
        # Format selector guarantees that every fallback includes an audio codec.
        format_selector = (
            "bv*[ext=mp4]+ba[ext=m4a]/"
            "bv*+ba/"
            "b[ext=mp4][acodec!=none]/"
            "b[acodec!=none]"
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

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            last_progress = 0.0

            while True:
                if process.stdout is None:
                    break

                line = await process.stdout.readline()
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

            # Wait for process to complete
            await process.wait()

            if process.returncode != 0:
                stderr = ""
                if process.stderr:
                    stderr = (await process.stderr.read()).decode()
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

            has_audio = cls._has_audio_stream(output_path)
            if has_audio is False:
                output_path.unlink(missing_ok=True)
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error=(
                        "Downloaded video has no audio stream. "
                        "yt-dlp now enforces audio formats; please retry download."
                    ),
                )
                return

            yield DownloadProgress("complete", 1.0, "Download complete!")

        except FileNotFoundError:
            yield DownloadProgress(
                "error",
                0,
                "",
                error="yt-dlp not found. Please install it: pip install yt-dlp",
            )
        except Exception as e:
            yield DownloadProgress("error", 0, "", error=str(e))

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
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()

            data = json.loads(stdout.decode())

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
