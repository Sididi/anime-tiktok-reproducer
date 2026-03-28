"""TikTok video downloader service using yt-dlp."""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..config import settings
from ..utils.media_binaries import (
    get_media_subprocess_env,
    get_ytdlp_ffmpeg_location,
    is_media_binary_override_error,
)
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


@dataclass(frozen=True)
class _DownloadCommandResult:
    """Outcome of one yt-dlp subprocess invocation."""

    returncode: int | None = None
    stderr: str = ""
    error: str | None = None


class DownloaderService:
    """Service for downloading TikTok videos using yt-dlp."""

    DOWNLOAD_TIMEOUT_SECONDS = 1800.0
    FFPROBE_TIMEOUT_SECONDS = 30.0
    MUX_TIMEOUT_SECONDS = 300.0
    AUDIO_RECOVERY_DURATION_TOLERANCE_SECONDS = 0.25
    PRIMARY_FORMAT_SELECTOR = (
        "bv*[ext=mp4]+ba[ext=m4a]/"
        "bv*+ba/"
        "b[ext=mp4][acodec!=none]/"
        "b[acodec!=none]/"
        "bv*[ext=mp4]/"
        "bv*/"
        "b"
    )
    AUDIO_RECOVERY_FORMAT_SELECTOR = "download"
    AUDIO_REQUIRED_ERROR_MESSAGE = (
        "Downloaded video has no audio stream. "
        "This TikTok may not have audio or the audio stream is unavailable. "
        "Note: Audio is required for transcription in this project."
    )

    @staticmethod
    def get_output_path(project_id: str) -> Path:
        """Get the output path for a downloaded video."""
        return settings.projects_dir / project_id / "tiktok.mp4"

    @classmethod
    def _build_download_command(
        cls,
        url: str,
        output_path: Path,
        *,
        format_selector: str,
    ) -> list[str]:
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
            "-o",
            str(output_path),
            "--force-overwrites",
        ]
        ffmpeg_location = get_ytdlp_ffmpeg_location()
        if ffmpeg_location is not None:
            cmd.extend(["--ffmpeg-location", ffmpeg_location])
        cmd.append(url)
        return cmd

    @classmethod
    def _build_primary_download_command(cls, url: str, output_path: Path) -> list[str]:
        return cls._build_download_command(
            url,
            output_path,
            format_selector=cls.PRIMARY_FORMAT_SELECTOR,
        )

    @classmethod
    def _build_audio_recovery_command(cls, url: str, output_path: Path) -> list[str]:
        return cls._build_download_command(
            url,
            output_path,
            format_selector=cls.AUDIO_RECOVERY_FORMAT_SELECTOR,
        )

    @staticmethod
    def _cleanup_paths(*paths: Path) -> None:
        for path in paths:
            path.unlink(missing_ok=True)

    @staticmethod
    def _replace_file(source_path: Path, dest_path: Path) -> None:
        dest_path.unlink(missing_ok=True)
        source_path.replace(dest_path)

    @staticmethod
    def _extract_ffmpeg_location(cmd: list[str]) -> str | None:
        if "--ffmpeg-location" not in cmd:
            return None
        location_index = cmd.index("--ffmpeg-location") + 1
        if location_index >= len(cmd):
            return None
        return cmd[location_index]

    @classmethod
    async def _stream_download_command(
        cls,
        cmd: list[str],
        *,
        progress_message_prefix: str,
    ) -> AsyncIterator[DownloadProgress | _DownloadCommandResult]:
        process: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        aborted = False

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=get_media_subprocess_env(cmd, extra_binary=cls._extract_ffmpeg_location(cmd)),
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

                if "[download]" in line_str and "%" in line_str:
                    try:
                        percent_str = line_str.split("%")[0].split()[-1]
                        progress = float(percent_str) / 100.0
                        if progress > last_progress:
                            last_progress = progress
                            yield DownloadProgress(
                                "downloading",
                                progress,
                                f"{progress_message_prefix}: {percent_str}%",
                            )
                    except (ValueError, IndexError):
                        pass

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)
            stderr = (await stderr_task).decode() if stderr_task is not None else ""
            yield _DownloadCommandResult(returncode=process.returncode, stderr=stderr)
        except asyncio.CancelledError:
            aborted = True
            if process is not None:
                await terminate_process(process)
            raise
        except asyncio.TimeoutError:
            aborted = True
            if process is not None:
                await terminate_process(process)
            yield _DownloadCommandResult(
                error=f"Download timed out after {int(cls.DOWNLOAD_TIMEOUT_SECONDS)} seconds",
            )
        except FileNotFoundError:
            aborted = True
            yield _DownloadCommandResult(
                error="yt-dlp not found. Please install it: pip install yt-dlp",
            )
        except Exception as exc:
            aborted = True
            if process is not None:
                await terminate_process(process)
            yield _DownloadCommandResult(error=str(exc))
        finally:
            if aborted and stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task

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
        except CommandTimeoutError:
            return None
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            return None

        if result.returncode != 0:
            return None

        return bool(result.stdout.decode().strip())

    @classmethod
    async def _can_mux_recovered_audio(
        cls,
        primary_path: Path,
        recovery_path: Path,
    ) -> bool:
        primary_info = await cls.get_video_info(primary_path)
        recovery_info = await cls.get_video_info(recovery_path)
        primary_duration = primary_info.get("duration")
        recovery_duration = recovery_info.get("duration")
        if not isinstance(primary_duration, (int, float)) or primary_duration <= 0:
            return False
        if not isinstance(recovery_duration, (int, float)) or recovery_duration <= 0:
            return False
        return abs(primary_duration - recovery_duration) <= cls.AUDIO_RECOVERY_DURATION_TOLERANCE_SECONDS

    @classmethod
    async def _mux_recovered_audio(
        cls,
        *,
        video_path: Path,
        audio_source_path: Path,
        output_path: Path,
    ) -> str | None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_source_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            str(output_path),
        ]

        try:
            result = await run_command(cmd, timeout_seconds=cls.MUX_TIMEOUT_SECONDS)
        except CommandTimeoutError:
            return f"Audio recovery mux timed out after {int(cls.MUX_TIMEOUT_SECONDS)} seconds"
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            return "ffmpeg not found. Please install ffmpeg."

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            return stderr or "ffmpeg failed to mux recovered audio"

        return None

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
        recovery_path = output_path.with_name(f"{output_path.stem}.recovery{output_path.suffix}")
        mux_path = output_path.with_name(f"{output_path.stem}.muxed{output_path.suffix}")
        cls._cleanup_paths(recovery_path, mux_path)

        try:
            primary_result: _DownloadCommandResult | None = None
            async for event in cls._stream_download_command(
                cls._build_primary_download_command(url, output_path),
                progress_message_prefix="Downloading",
            ):
                if isinstance(event, DownloadProgress):
                    yield event
                else:
                    primary_result = event

            if primary_result is None:
                cls._cleanup_paths(output_path, recovery_path, mux_path)
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error="yt-dlp exited without reporting a result",
                )
                return

            if primary_result.error is not None:
                cls._cleanup_paths(output_path, recovery_path, mux_path)
                yield DownloadProgress("error", 0, "", error=primary_result.error)
                return

            if primary_result.returncode != 0:
                cls._cleanup_paths(output_path, recovery_path, mux_path)
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error=f"yt-dlp failed with code {primary_result.returncode}: {primary_result.stderr}",
                )
                return

            if not output_path.exists():
                cls._cleanup_paths(output_path, recovery_path, mux_path)
                yield DownloadProgress(
                    "error",
                    0,
                    "",
                    error="Download completed but file not found",
                )
                return

            has_audio = await cls._has_audio_stream(output_path)
            if has_audio is False:
                yield DownloadProgress("downloading", 0.0, "Recovering audio track...")

                recovery_result: _DownloadCommandResult | None = None
                async for event in cls._stream_download_command(
                    cls._build_audio_recovery_command(url, recovery_path),
                    progress_message_prefix="Recovering audio",
                ):
                    if isinstance(event, DownloadProgress):
                        yield event
                    else:
                        recovery_result = event

                if recovery_result is None:
                    cls._cleanup_paths(output_path, recovery_path, mux_path)
                    yield DownloadProgress(
                        "error",
                        0,
                        "",
                        error="yt-dlp audio recovery exited without reporting a result",
                    )
                    return

                if recovery_result.error is not None:
                    cls._cleanup_paths(output_path, recovery_path, mux_path)
                    yield DownloadProgress("error", 0, "", error=recovery_result.error)
                    return

                if recovery_result.returncode != 0:
                    cls._cleanup_paths(output_path, recovery_path, mux_path)
                    yield DownloadProgress(
                        "error",
                        0,
                        "",
                        error=(
                            f"yt-dlp audio recovery failed with code {recovery_result.returncode}: "
                            f"{recovery_result.stderr}"
                        ),
                    )
                    return

                if not recovery_path.exists():
                    cls._cleanup_paths(output_path, recovery_path, mux_path)
                    yield DownloadProgress(
                        "error",
                        0,
                        "",
                        error="Audio recovery completed but file not found",
                    )
                    return

                recovery_has_audio = await cls._has_audio_stream(recovery_path)
                if recovery_has_audio is False:
                    cls._cleanup_paths(output_path, recovery_path, mux_path)
                    yield DownloadProgress(
                        "error",
                        0,
                        "",
                        error=cls.AUDIO_REQUIRED_ERROR_MESSAGE,
                    )
                    return

                should_mux = await cls._can_mux_recovered_audio(output_path, recovery_path)
                if should_mux:
                    yield DownloadProgress("downloading", 0.0, "Merging recovered audio...")
                    mux_error = await cls._mux_recovered_audio(
                        video_path=output_path,
                        audio_source_path=recovery_path,
                        output_path=mux_path,
                    )
                    mux_has_audio = await cls._has_audio_stream(mux_path) if mux_error is None else False
                    if mux_error is None and mux_has_audio is not False:
                        cls._replace_file(mux_path, output_path)
                        cls._cleanup_paths(recovery_path)
                    else:
                        cls._cleanup_paths(mux_path)
                        cls._replace_file(recovery_path, output_path)
                else:
                    cls._replace_file(recovery_path, output_path)

                final_has_audio = await cls._has_audio_stream(output_path)
                if final_has_audio is False:
                    cls._cleanup_paths(output_path, recovery_path, mux_path)
                    yield DownloadProgress(
                        "error",
                        0,
                        "",
                        error=cls.AUDIO_REQUIRED_ERROR_MESSAGE,
                    )
                    return

            yield DownloadProgress("complete", 1.0, "Download complete!")

        except asyncio.CancelledError:
            cls._cleanup_paths(recovery_path, mux_path)
            raise
        except Exception as exc:
            cls._cleanup_paths(recovery_path, mux_path)
            yield DownloadProgress("error", 0, "", error=str(exc))
        finally:
            cls._cleanup_paths(recovery_path, mux_path)

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

        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            return {}
        except Exception:
            return {}
