from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

from ..config import settings
from .anime_library import AnimeLibraryService


class SourceChunkStreamingService:
    """Serve browser-safe source chunks for manual preview workflows."""

    CACHE_DIR_NAME = "source_stream_chunks_v1"
    PROFILE_VERSION = "v1"

    DEFAULT_CHUNK_DURATION_SECONDS = 30.0
    DEFAULT_CHUNK_STEP_SECONDS = 20.0
    DEFAULT_SEEK_GUARD_SECONDS = 5.0
    MIN_CHUNK_DURATION_SECONDS = 3.0
    MAX_CHUNK_DURATION_SECONDS = 120.0

    GLOBAL_MAX_WORKERS = 2
    ENCODE_TIMEOUT_SECONDS = 300.0

    GC_MAX_CACHE_BYTES = 8 * 1024 * 1024 * 1024
    GC_STALE_SECONDS = 7 * 24 * 3600
    GC_MIN_INTERVAL_SECONDS = 45.0

    _global_semaphore: asyncio.Semaphore | None = None
    _source_locks_guard = threading.Lock()
    _source_locks: dict[str, threading.Lock] = {}

    _gc_lock = threading.Lock()
    _last_gc_run_epoch: float = 0.0

    _nvenc_checked = False
    _nvenc_available = False

    @classmethod
    def get_cache_dir(cls) -> Path:
        return settings.cache_dir / cls.CACHE_DIR_NAME

    @classmethod
    def _get_global_semaphore(cls) -> asyncio.Semaphore:
        if cls._global_semaphore is None:
            cls._global_semaphore = asyncio.Semaphore(cls.GLOBAL_MAX_WORKERS)
        return cls._global_semaphore

    @classmethod
    def _get_source_lock(cls, source_path: Path) -> threading.Lock:
        key = str(source_path.resolve())
        with cls._source_locks_guard:
            lock = cls._source_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._source_locks[key] = lock
            return lock

    @staticmethod
    def _probe_stream_sync(video_path: Path) -> dict | None:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt",
            "-of",
            "json",
            str(video_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        streams = payload.get("streams", [])
        if not streams:
            return None
        stream = streams[0]
        return stream if isinstance(stream, dict) else None

    @staticmethod
    def _probe_duration_sync(video_path: Path) -> float | None:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0:
            return None

        raw = result.stdout.strip()
        if not raw:
            return None

        try:
            duration = float(raw)
        except ValueError:
            return None

        return duration if duration > 0 else None

    @classmethod
    def _touch_access_time_sync(cls, path: Path) -> None:
        try:
            stat = path.stat()
            now = time.time()
            os.utime(path, (now, stat.st_mtime))
        except OSError:
            return

    @classmethod
    def _is_nvenc_available_sync(cls) -> bool:
        if cls._nvenc_checked:
            return cls._nvenc_available

        cls._nvenc_checked = True
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            cls._nvenc_available = False
            return False

        cls._nvenc_available = result.returncode == 0 and "h264_nvenc" in result.stdout
        return cls._nvenc_available

    @classmethod
    def _build_chunk_key_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        profile: str,
    ) -> str:
        stat = source_path.stat()
        payload = (
            f"{cls.PROFILE_VERSION}|{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
            f"{chunk_start:.3f}|{chunk_duration:.3f}|{profile}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _chunk_path_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        profile: str,
    ) -> Path:
        key = cls._build_chunk_key_sync(
            source_path=source_path,
            chunk_start=chunk_start,
            chunk_duration=chunk_duration,
            profile=profile,
        )
        return cls.get_cache_dir() / f"{key}.mp4"

    @classmethod
    def _normalize_chunk_window_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        chunk_step: float,
    ) -> tuple[float, float, float]:
        source_duration = cls._probe_duration_sync(source_path) or 0.0

        duration = float(chunk_duration)
        duration = max(duration, cls.MIN_CHUNK_DURATION_SECONDS)
        duration = min(duration, cls.MAX_CHUNK_DURATION_SECONDS)

        start = max(float(chunk_start), 0.0)
        step = max(float(chunk_step), 0.001)

        if source_duration > 0:
            duration = min(duration, source_duration)
            max_start = max(source_duration - duration, 0.0)
            start = min(start, max_start)

        start = math.floor(start / step) * step
        start = round(max(start, 0.0), 3)
        if source_duration > 0:
            duration = min(
                max(duration, cls.MIN_CHUNK_DURATION_SECONDS),
                source_duration,
            )
        else:
            duration = max(duration, cls.MIN_CHUNK_DURATION_SECONDS)
        duration = round(duration, 3)

        if source_duration > 0:
            max_start = max(source_duration - duration, 0.0)
            start = round(min(start, max_start), 3)

        return start, duration, source_duration

    @classmethod
    def _build_gpu_command_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        output_path: Path,
    ) -> list[str]:
        source_codec = AnimeLibraryService.get_primary_video_codec_sync(source_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
        ]

        codec = (source_codec or "").lower().strip()
        if codec == "av1":
            cmd.extend(["-c:v", "av1_cuvid"])
        elif codec == "h264":
            cmd.extend(["-c:v", "h264_cuvid"])
        elif codec == "hevc":
            cmd.extend(["-c:v", "hevc_cuvid"])

        cmd.extend(
            [
                "-ss",
                f"{chunk_start:.3f}",
                "-i",
                str(source_path),
                "-t",
                f"{chunk_duration:.3f}",
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p5",
                "-rc",
                "constqp",
                "-qp",
                "24",
                "-b:v",
                "0",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )

        return cmd

    @classmethod
    def _build_cpu_command_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        output_path: Path,
    ) -> list[str]:
        return [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{chunk_start:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{chunk_duration:.3f}",
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    @classmethod
    def _validate_chunk_sync(cls, path: Path) -> bool:
        if not path.exists() or path.stat().st_size <= 0:
            return False

        stream = cls._probe_stream_sync(path)
        if stream is None:
            return False

        codec = str(stream.get("codec_name", "")).lower()
        pix_fmt = str(stream.get("pix_fmt", "")).lower()
        duration = cls._probe_duration_sync(path)

        return codec == "h264" and pix_fmt in {"yuv420p", "yuvj420p"} and (duration or 0.0) > 0.05

    @classmethod
    def _encode_chunk_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        tmp_path: Path,
    ) -> bool:
        if cls._is_nvenc_available_sync():
            gpu_cmd = cls._build_gpu_command_sync(
                source_path=source_path,
                chunk_start=chunk_start,
                chunk_duration=chunk_duration,
                output_path=tmp_path,
            )
            try:
                result = subprocess.run(
                    gpu_cmd,
                    capture_output=True,
                    text=True,
                    timeout=cls.ENCODE_TIMEOUT_SECONDS,
                    check=False,
                )
                if result.returncode == 0:
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        cpu_cmd = cls._build_cpu_command_sync(
            source_path=source_path,
            chunk_start=chunk_start,
            chunk_duration=chunk_duration,
            output_path=tmp_path,
        )
        result = subprocess.run(
            cpu_cmd,
            capture_output=True,
            text=True,
            timeout=cls.ENCODE_TIMEOUT_SECONDS,
            check=False,
        )
        return result.returncode == 0

    @classmethod
    def _run_gc_sync(cls) -> None:
        cache_dir = cls.get_cache_dir()
        if not cache_dir.exists():
            return

        now = time.time()
        with cls._gc_lock:
            if now - cls._last_gc_run_epoch < cls.GC_MIN_INTERVAL_SECONDS:
                return
            cls._last_gc_run_epoch = now

            for tmp_path in cache_dir.glob("*.tmp.mp4"):
                try:
                    if now - tmp_path.stat().st_mtime > 3600:
                        tmp_path.unlink(missing_ok=True)
                except OSError:
                    continue

            entries: list[tuple[Path, os.stat_result]] = []
            for path in cache_dir.glob("*.mp4"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((path, stat))

            stale_cutoff = now - cls.GC_STALE_SECONDS
            for path, stat in list(entries):
                atime = stat.st_atime if stat.st_atime > 0 else stat.st_mtime
                if atime < stale_cutoff:
                    path.unlink(missing_ok=True)

            entries = []
            total_size = 0
            for path in cache_dir.glob("*.mp4"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((path, stat))
                total_size += stat.st_size

            if total_size <= cls.GC_MAX_CACHE_BYTES:
                return

            entries.sort(key=lambda item: item[1].st_atime)
            for path, stat in entries:
                if total_size <= cls.GC_MAX_CACHE_BYTES:
                    break
                try:
                    path.unlink(missing_ok=True)
                    total_size -= stat.st_size
                except OSError:
                    continue

    @classmethod
    def _ensure_chunk_sync(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float,
        profile: str,
    ) -> Path:
        cache_dir = cls.get_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)

        output_path = cls._chunk_path_sync(
            source_path=source_path,
            chunk_start=chunk_start,
            chunk_duration=chunk_duration,
            profile=profile,
        )
        tmp_path = output_path.with_suffix(".tmp.mp4")

        source_lock = cls._get_source_lock(source_path)
        with source_lock:
            if output_path.exists() and cls._validate_chunk_sync(output_path):
                cls._touch_access_time_sync(output_path)
                return output_path

            if output_path.exists():
                output_path.unlink(missing_ok=True)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

            encoded = cls._encode_chunk_sync(
                source_path=source_path,
                chunk_start=chunk_start,
                chunk_duration=chunk_duration,
                tmp_path=tmp_path,
            )
            if not encoded:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError("Failed to encode source chunk")

            if not cls._validate_chunk_sync(tmp_path):
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError("Encoded source chunk is invalid")

            tmp_path.replace(output_path)
            cls._touch_access_time_sync(output_path)
            cls._run_gc_sync()
            return output_path

    @classmethod
    async def get_descriptor(cls, source_path: Path) -> dict[str, object]:
        stream = await asyncio.to_thread(cls._probe_stream_sync, source_path)
        duration = await asyncio.to_thread(cls._probe_duration_sync, source_path)
        compatible = await asyncio.to_thread(
            AnimeLibraryService.is_browser_preview_compatible,
            source_path,
        )

        codec = str((stream or {}).get("codec_name", ""))
        pix_fmt = str((stream or {}).get("pix_fmt", ""))

        return {
            "mode": "passthrough" if compatible else "chunked",
            "duration": float(duration or 0.0),
            "codec": codec,
            "pix_fmt": pix_fmt,
            "chunk_duration": cls.DEFAULT_CHUNK_DURATION_SECONDS,
            "chunk_step": cls.DEFAULT_CHUNK_STEP_SECONDS,
            "seek_guard_seconds": cls.DEFAULT_SEEK_GUARD_SECONDS,
        }

    @classmethod
    async def get_chunk(
        cls,
        *,
        source_path: Path,
        chunk_start: float,
        chunk_duration: float | None = None,
    ) -> Path:
        requested_duration = (
            float(chunk_duration)
            if chunk_duration is not None
            else cls.DEFAULT_CHUNK_DURATION_SECONDS
        )

        normalized_start, normalized_duration, _source_duration = await asyncio.to_thread(
            cls._normalize_chunk_window_sync,
            source_path=source_path,
            chunk_start=chunk_start,
            chunk_duration=requested_duration,
            chunk_step=cls.DEFAULT_CHUNK_STEP_SECONDS,
        )

        semaphore = cls._get_global_semaphore()
        async with semaphore:
            return await asyncio.to_thread(
                cls._ensure_chunk_sync,
                source_path=source_path,
                chunk_start=normalized_start,
                chunk_duration=normalized_duration,
                profile="preview",
            )
