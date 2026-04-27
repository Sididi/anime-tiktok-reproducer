from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

from ..config import settings
from ..utils.media_binaries import get_media_subprocess_env, rewrite_media_command
from .anime_library import AnimeLibraryService


class BrowserMediaService:
    """Build and cache browser-friendly preview proxies for dense playback."""

    CACHE_DIR_NAME = "browser_media_previews_v1"
    PROFILE_VERSION = "v1"
    PREVIEW_TIMEOUT_SECONDS = 3600.0

    PROJECT_PROFILE = "project"
    SOURCE_PROFILE = "source"

    _generation_lock: asyncio.Lock | None = None
    _generation_inflight: set[str] = set()
    _preview_locks_guard = threading.Lock()
    _preview_locks: dict[str, threading.Lock] = {}

    @classmethod
    def _get_generation_lock(cls) -> asyncio.Lock:
        if cls._generation_lock is None:
            cls._generation_lock = asyncio.Lock()
        return cls._generation_lock

    @classmethod
    def _get_preview_lock(cls, source_path: Path, profile: str) -> threading.Lock:
        key = f"{profile}|{source_path.resolve()}"
        with cls._preview_locks_guard:
            lock = cls._preview_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._preview_locks[key] = lock
            return lock

    @classmethod
    def get_preview_dir(cls, profile: str) -> Path:
        return settings.cache_dir / cls.CACHE_DIR_NAME / profile

    @classmethod
    def _build_preview_key_sync(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
    ) -> str:
        stat = source_path.stat()
        payload = {
            "version": cls.PROFILE_VERSION,
            "profile": profile,
            "include_audio": include_audio,
            "path": str(source_path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @classmethod
    def get_preview_path_sync(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
    ) -> Path:
        key = cls._build_preview_key_sync(
            source_path,
            profile=profile,
            include_audio=include_audio,
        )
        return cls.get_preview_dir(profile) / f"{key}.mp4"

    @staticmethod
    def _probe_primary_audio_codec_sync(source_path: Path) -> str | None:
        cmd = rewrite_media_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "json",
                str(source_path),
            ]
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=get_media_subprocess_env(cmd),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        streams = payload.get("streams") or []
        if not streams:
            return None

        codec = str((streams[0] or {}).get("codec_name", "")).strip().lower()
        return codec or None

    @classmethod
    def is_browser_preview_compatible_sync(
        cls,
        source_path: Path,
        *,
        include_audio: bool,
    ) -> bool:
        if source_path.suffix.lower() != ".mp4":
            return False

        codec = AnimeLibraryService.get_primary_video_codec_sync(source_path)
        if codec != "h264":
            return False

        stream = AnimeLibraryService._probe_video_stream_sync(source_path)
        if stream is None:
            return False

        pix_fmt = str(stream.get("pix_fmt", "")).strip().lower()
        if pix_fmt not in {"yuv420p", "yuvj420p"}:
            return False

        if not include_audio:
            return True

        audio_codec = cls._probe_primary_audio_codec_sync(source_path)
        return audio_codec in {None, "aac", "mp3"}

    @classmethod
    def _build_gpu_command_sync(
        cls,
        source_path: Path,
        *,
        include_audio: bool,
        output_path: Path,
    ) -> list[str]:
        source_codec = AnimeLibraryService.get_primary_video_codec_sync(source_path)
        cmd = AnimeLibraryService._build_gpu_h264_base_cmd(
            source_path,
            source_codec=source_codec,
        )
        if include_audio:
            cmd.extend(
                [
                    "-map",
                    "0:a:0?",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                ]
            )
        else:
            cmd.append("-an")
        cmd.append(str(output_path))
        return rewrite_media_command(cmd)

    @classmethod
    def _build_cpu_command_sync(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
        output_path: Path,
    ) -> list[str]:
        cmd = ["ffmpeg", "-y", "-i", str(source_path), "-map", "0:v:0"]
        if profile == cls.PROJECT_PROFILE:
            vf = "scale=w=540:h=960:force_original_aspect_ratio=decrease"
        else:
            vf = "scale=w=854:h=480:force_original_aspect_ratio=decrease"
        cmd.extend(
            [
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
            ]
        )
        if include_audio:
            cmd.extend(
                [
                    "-map",
                    "0:a:0?",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                ]
            )
        else:
            cmd.append("-an")
        cmd.append(str(output_path))
        return rewrite_media_command(cmd)

    @classmethod
    def _is_valid_preview_sync(cls, preview_path: Path) -> bool:
        if not preview_path.exists() or preview_path.stat().st_size <= 0:
            return False
        stream = AnimeLibraryService._probe_video_stream_sync(preview_path)
        if stream is None:
            return False
        codec = str(stream.get("codec_name", "")).strip().lower()
        pix_fmt = str(stream.get("pix_fmt", "")).strip().lower()
        duration = AnimeLibraryService._probe_video_duration_sync(preview_path)
        return codec == "h264" and pix_fmt in {"yuv420p", "yuvj420p"} and (duration or 0.0) > 0.05

    @classmethod
    def ensure_preview_proxy_sync(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
    ) -> Path | None:
        if not source_path.exists() or not source_path.is_file():
            return None

        if cls.is_browser_preview_compatible_sync(
            source_path,
            include_audio=include_audio,
        ):
            return source_path

        preview_dir = cls.get_preview_dir(profile)
        preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = cls.get_preview_path_sync(
            source_path,
            profile=profile,
            include_audio=include_audio,
        )
        tmp_path = preview_path.with_suffix(".tmp.mp4")
        lock = cls._get_preview_lock(source_path, profile)
        with lock:
            if preview_path.exists():
                if cls._is_valid_preview_sync(preview_path):
                    return preview_path
                with suppress(OSError):
                    preview_path.unlink()

            if tmp_path.exists():
                with suppress(OSError):
                    tmp_path.unlink()

            commands = [
                cls._build_gpu_command_sync(
                    source_path,
                    include_audio=include_audio,
                    output_path=tmp_path,
                ),
                cls._build_cpu_command_sync(
                    source_path,
                    profile=profile,
                    include_audio=include_audio,
                    output_path=tmp_path,
                ),
            ]

            last_error = None
            for cmd in commands:
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=cls.PREVIEW_TIMEOUT_SECONDS,
                        check=False,
                        env=get_media_subprocess_env(cmd),
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                    last_error = str(exc)
                    continue

                if result.returncode == 0 and cls._is_valid_preview_sync(tmp_path):
                    tmp_path.replace(preview_path)
                    return preview_path

                last_error = result.stderr.strip() or "preview ffmpeg failed"
                with suppress(OSError):
                    tmp_path.unlink()

            if last_error:
                with suppress(OSError):
                    tmp_path.unlink()
            return None

    @classmethod
    async def resolve_preview_path(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
        allow_generate: bool = True,
    ) -> Path:
        if allow_generate:
            resolved = await asyncio.to_thread(
                cls.ensure_preview_proxy_sync,
                source_path,
                profile=profile,
                include_audio=include_audio,
            )
            return resolved if resolved is not None else source_path

        compatible = await asyncio.to_thread(
            cls.is_browser_preview_compatible_sync,
            source_path,
            include_audio=include_audio,
        )
        if compatible:
            return source_path

        preview_path = await asyncio.to_thread(
            cls.get_preview_path_sync,
            source_path,
            profile=profile,
            include_audio=include_audio,
        )
        preview_valid = await asyncio.to_thread(cls._is_valid_preview_sync, preview_path)
        if preview_valid:
            return preview_path
        if preview_path.exists():
            await asyncio.to_thread(lambda: preview_path.unlink(missing_ok=True))
        return source_path

    @classmethod
    async def trigger_preview_generation(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
    ) -> None:
        key = f"{profile}|{include_audio}|{source_path.resolve()}"
        lock = cls._get_generation_lock()
        async with lock:
            if key in cls._generation_inflight:
                return
            cls._generation_inflight.add(key)

        async def _run() -> None:
            try:
                await asyncio.to_thread(
                    cls.ensure_preview_proxy_sync,
                    source_path,
                    profile=profile,
                    include_audio=include_audio,
                )
            finally:
                async with lock:
                    cls._generation_inflight.discard(key)

        asyncio.create_task(_run())

    @classmethod
    async def wait_for_preview(
        cls,
        source_path: Path,
        *,
        profile: str,
        include_audio: bool,
        timeout_seconds: float = 1.5,
        poll_interval_seconds: float = 0.15,
    ) -> Path | None:
        preview_path = await asyncio.to_thread(
            cls.get_preview_path_sync,
            source_path,
            profile=profile,
            include_audio=include_audio,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.0)
        while True:
            ready = await asyncio.to_thread(cls._is_valid_preview_sync, preview_path)
            if ready:
                return preview_path
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(max(poll_interval_seconds, 0.05))
