from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

from ..config import settings
from ..utils.media_binaries import get_media_subprocess_env, rewrite_media_command
from .anime_library import AnimeLibraryService
from .source_chunk_streaming_service import SourceChunkStreamingService


_SEGMENT_FILENAME_PATTERN = "seg_%04d.ts"
_ENDLIST_MARKER = b"#EXT-X-ENDLIST"


class SourceHlsStreamingService:
    """Serve browser-safe source episodes as HLS for manual preview workflows."""

    CACHE_DIR_NAME = "source_hls_v1"
    PROFILE_VERSION = "v1"
    PROFILE_NAME = "preview854p_audio"

    TARGET_WIDTH = 854
    TARGET_HEIGHT = 480
    SEGMENT_DURATION_SECONDS = 4.0

    START_OFFSET_BOUNDARY_SECONDS = 30.0
    START_OFFSET_PREROLL_SECONDS = 5.0

    GLOBAL_MAX_WORKERS = 2
    ENCODE_TIMEOUT_SECONDS = 30 * 60.0
    FIRST_SEGMENT_WAIT_SECONDS = 15.0
    FIRST_SEGMENT_POLL_INTERVAL_SECONDS = 0.1

    GC_MAX_CACHE_BYTES = 16 * 1024 * 1024 * 1024
    GC_STALE_SECONDS = 7 * 24 * 3600
    GC_MIN_INTERVAL_SECONDS = 60.0

    DONE_MARKER = "encode.done"
    FAILED_MARKER = "encode.failed"
    LOCK_MARKER = "encode.lock"
    PLAYLIST_NAME = "playlist.m3u8"

    _global_semaphore: asyncio.Semaphore | None = None
    _encoder_lock = asyncio.Lock()
    _encoders_inflight: dict[str, asyncio.Task[Path]] = {}
    _source_locks_guard = threading.Lock()
    _source_locks: dict[str, threading.Lock] = {}

    _gc_lock = threading.Lock()
    _last_gc_run_epoch: float = 0.0

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

    @classmethod
    def snap_start_offset_sync(cls, target_time: float | None) -> float:
        if target_time is None or target_time <= cls.START_OFFSET_PREROLL_SECONDS:
            return 0.0
        candidate = max(0.0, float(target_time) - cls.START_OFFSET_PREROLL_SECONDS)
        boundary = cls.START_OFFSET_BOUNDARY_SECONDS
        if boundary <= 0:
            return round(candidate, 3)
        return round((candidate // boundary) * boundary, 3)

    @classmethod
    def build_source_hash_sync(
        cls,
        source_path: Path,
        start_offset: float = 0.0,
    ) -> str:
        resolved = source_path.resolve()
        stat = resolved.stat()
        offset_label = f"{max(0.0, float(start_offset)):.3f}"
        payload = (
            f"{cls.PROFILE_VERSION}|{resolved}|{stat.st_size}|{stat.st_mtime_ns}|"
            f"{cls.PROFILE_NAME}|{cls.TARGET_WIDTH}x{cls.TARGET_HEIGHT}|"
            f"seg={cls.SEGMENT_DURATION_SECONDS}|ss={offset_label}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @classmethod
    def _dir_for_hash(cls, source_hash: str) -> Path:
        return cls.get_cache_dir() / source_hash

    @classmethod
    def _playlist_path_for_dir(cls, encode_dir: Path) -> Path:
        return encode_dir / cls.PLAYLIST_NAME

    @classmethod
    def _is_encode_done(cls, encode_dir: Path) -> bool:
        return (encode_dir / cls.DONE_MARKER).exists()

    @classmethod
    def _is_encode_failed(cls, encode_dir: Path) -> bool:
        return (encode_dir / cls.FAILED_MARKER).exists()

    @classmethod
    def _playlist_has_endlist(cls, playlist_path: Path) -> bool:
        if not playlist_path.exists():
            return False
        try:
            with playlist_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                chunk = max(0, size - 256)
                fh.seek(chunk, os.SEEK_SET)
                tail = fh.read()
            return _ENDLIST_MARKER in tail
        except OSError:
            return False

    @classmethod
    def _touch_access_time_sync(cls, path: Path) -> None:
        try:
            stat = path.stat()
            os.utime(path, (time.time(), stat.st_mtime))
        except OSError:
            return

    @classmethod
    def _build_gpu_command(
        cls,
        *,
        source_path: Path,
        encode_dir: Path,
        start_offset: float,
    ) -> list[str]:
        source_codec = (AnimeLibraryService.get_primary_video_codec_sync(source_path) or "").lower().strip()

        cmd: list[str] = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
        ]
        if source_codec == "av1":
            cmd.extend(["-c:v", "av1_cuvid"])
        elif source_codec == "h264":
            cmd.extend(["-c:v", "h264_cuvid"])
        elif source_codec == "hevc":
            cmd.extend(["-c:v", "hevc_cuvid"])

        if start_offset > 0:
            cmd.extend(["-ss", f"{start_offset:.3f}"])

        cmd.extend(
            [
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                (
                    f"scale_cuda={cls.TARGET_WIDTH}:{cls.TARGET_HEIGHT}"
                    ":force_original_aspect_ratio=decrease"
                    ":force_divisible_by=2,"
                    "hwdownload,format=nv12,"
                    f"pad={cls.TARGET_WIDTH}:{cls.TARGET_HEIGHT}"
                    ":(ow-iw)/2:(oh-ih)/2,setsar=1"
                ),
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p5",
                "-rc",
                "constqp",
                "-qp",
                "22",
                "-profile:v",
                "main",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-hls_time",
                f"{cls.SEGMENT_DURATION_SECONDS:.3f}",
                "-hls_list_size",
                "0",
                "-hls_segment_type",
                "mpegts",
                "-hls_flags",
                "independent_segments+temp_file",
                "-hls_segment_filename",
                str(encode_dir / _SEGMENT_FILENAME_PATTERN),
                "-f",
                "hls",
                str(encode_dir / cls.PLAYLIST_NAME),
            ]
        )
        return rewrite_media_command(cmd)

    @classmethod
    def _build_cpu_command(
        cls,
        *,
        source_path: Path,
        encode_dir: Path,
        start_offset: float,
    ) -> list[str]:
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        if start_offset > 0:
            cmd.extend(["-ss", f"{start_offset:.3f}"])
        cmd.extend([
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            (
                f"scale={cls.TARGET_WIDTH}:{cls.TARGET_HEIGHT}"
                ":force_original_aspect_ratio=decrease,"
                f"pad={cls.TARGET_WIDTH}:{cls.TARGET_HEIGHT}"
                ":(ow-iw)/2:(oh-ih)/2,setsar=1"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-hls_time",
            f"{cls.SEGMENT_DURATION_SECONDS:.3f}",
            "-hls_list_size",
            "0",
            "-hls_segment_type",
            "mpegts",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_segment_filename",
            str(encode_dir / _SEGMENT_FILENAME_PATTERN),
            "-f",
            "hls",
            str(encode_dir / cls.PLAYLIST_NAME),
        ])
        return rewrite_media_command(cmd)

    @classmethod
    def _run_ffmpeg_sync(
        cls,
        *,
        source_path: Path,
        encode_dir: Path,
        start_offset: float,
    ) -> bool:
        if SourceChunkStreamingService._is_nvenc_available_sync():
            gpu_cmd = cls._build_gpu_command(
                source_path=source_path,
                encode_dir=encode_dir,
                start_offset=start_offset,
            )
            try:
                result = subprocess.run(
                    gpu_cmd,
                    capture_output=True,
                    text=True,
                    timeout=cls.ENCODE_TIMEOUT_SECONDS,
                    check=False,
                    env=get_media_subprocess_env(gpu_cmd),
                )
                if result.returncode == 0 and cls._playlist_path_for_dir(encode_dir).exists():
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            for stale in encode_dir.glob("seg_*.ts"):
                stale.unlink(missing_ok=True)
            cls._playlist_path_for_dir(encode_dir).unlink(missing_ok=True)

        cpu_cmd = cls._build_cpu_command(
            source_path=source_path,
            encode_dir=encode_dir,
            start_offset=start_offset,
        )
        try:
            result = subprocess.run(
                cpu_cmd,
                capture_output=True,
                text=True,
                timeout=cls.ENCODE_TIMEOUT_SECONDS,
                check=False,
                env=get_media_subprocess_env(cpu_cmd),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and cls._playlist_path_for_dir(encode_dir).exists()

    @classmethod
    def _encode_once_sync(
        cls,
        *,
        source_path: Path,
        encode_dir: Path,
        start_offset: float,
    ) -> Path:
        lock = cls._get_source_lock(source_path)
        with lock:
            if cls._is_encode_done(encode_dir):
                cls._touch_access_time_sync(encode_dir)
                return cls._playlist_path_for_dir(encode_dir)

            encode_dir.mkdir(parents=True, exist_ok=True)
            (encode_dir / cls.FAILED_MARKER).unlink(missing_ok=True)
            (encode_dir / cls.LOCK_MARKER).write_text(f"{os.getpid()}|{time.time()}")

            try:
                ok = cls._run_ffmpeg_sync(
                    source_path=source_path,
                    encode_dir=encode_dir,
                    start_offset=start_offset,
                )
            finally:
                (encode_dir / cls.LOCK_MARKER).unlink(missing_ok=True)

            if not ok:
                (encode_dir / cls.FAILED_MARKER).write_text(str(time.time()))
                raise RuntimeError("Failed to encode source HLS stream")

            (encode_dir / cls.DONE_MARKER).write_text(str(time.time()))
            cls._touch_access_time_sync(encode_dir)
            cls._run_gc_sync()
            return cls._playlist_path_for_dir(encode_dir)

    @classmethod
    async def _encode_async(
        cls,
        *,
        source_path: Path,
        encode_dir: Path,
        start_offset: float,
    ) -> Path:
        semaphore = cls._get_global_semaphore()
        async with semaphore:
            return await asyncio.to_thread(
                cls._encode_once_sync,
                source_path=source_path,
                encode_dir=encode_dir,
                start_offset=start_offset,
            )

    @classmethod
    async def _ensure_encoder_task(
        cls,
        *,
        source_path: Path,
        source_hash: str,
        encode_dir: Path,
        start_offset: float,
    ) -> asyncio.Task[Path]:
        async with cls._encoder_lock:
            existing = cls._encoders_inflight.get(source_hash)
            if existing is not None and not existing.done():
                return existing

            task: asyncio.Task[Path] = asyncio.create_task(
                cls._encode_async(
                    source_path=source_path,
                    encode_dir=encode_dir,
                    start_offset=start_offset,
                )
            )
            cls._encoders_inflight[source_hash] = task

            def _cleanup(_finished: asyncio.Task[Path]) -> None:
                current = cls._encoders_inflight.get(source_hash)
                if current is _finished:
                    cls._encoders_inflight.pop(source_hash, None)

            task.add_done_callback(_cleanup)
            return task

    @classmethod
    async def _wait_for_first_segment(cls, *, encode_dir: Path, task: asyncio.Task[Path]) -> None:
        deadline = time.monotonic() + cls.FIRST_SEGMENT_WAIT_SECONDS
        playlist = cls._playlist_path_for_dir(encode_dir)
        first_segment = encode_dir / "seg_0000.ts"
        while time.monotonic() < deadline:
            if task.done():
                task.result()
                return
            if playlist.exists() and first_segment.exists() and first_segment.stat().st_size > 0:
                return
            await asyncio.sleep(cls.FIRST_SEGMENT_POLL_INTERVAL_SECONDS)

        if task.done():
            task.result()
            return
        raise TimeoutError("Timed out waiting for HLS first segment")

    @classmethod
    async def ensure_playlist(
        cls,
        source_path: Path,
        *,
        start_offset: float = 0.0,
    ) -> tuple[str, Path, bool, float]:
        """Ensure the encode is started; return (hash, playlist_path, encode_done, start_offset)."""
        offset = max(0.0, float(start_offset))
        source_hash = await asyncio.to_thread(
            cls.build_source_hash_sync, source_path, offset
        )
        encode_dir = cls._dir_for_hash(source_hash)

        if cls._is_encode_done(encode_dir) and cls._playlist_path_for_dir(encode_dir).exists():
            await asyncio.to_thread(cls._touch_access_time_sync, encode_dir)
            return source_hash, cls._playlist_path_for_dir(encode_dir), True, offset

        task = await cls._ensure_encoder_task(
            source_path=source_path,
            source_hash=source_hash,
            encode_dir=encode_dir,
            start_offset=offset,
        )

        if cls._is_encode_done(encode_dir) and cls._playlist_path_for_dir(encode_dir).exists():
            return source_hash, cls._playlist_path_for_dir(encode_dir), True, offset

        await cls._wait_for_first_segment(encode_dir=encode_dir, task=task)

        playlist_path = cls._playlist_path_for_dir(encode_dir)
        if not playlist_path.exists():
            raise RuntimeError("HLS playlist missing after first-segment wait")

        done = cls._is_encode_done(encode_dir)
        return source_hash, playlist_path, done, offset

    @classmethod
    def resolve_segment(cls, source_hash: str, filename: str) -> Path:
        safe_name = filename.replace("\\", "/").split("/")[-1]
        if not safe_name.startswith("seg_") or not safe_name.endswith(".ts"):
            raise FileNotFoundError(filename)
        encode_dir = cls._dir_for_hash(source_hash)
        candidate = encode_dir / safe_name
        if not candidate.exists():
            raise FileNotFoundError(filename)
        try:
            candidate.relative_to(encode_dir.resolve())
        except ValueError as exc:
            raise FileNotFoundError(filename) from exc
        cls._touch_access_time_sync(encode_dir)
        return candidate

    @classmethod
    def is_complete(cls, source_hash: str) -> bool:
        encode_dir = cls._dir_for_hash(source_hash)
        if cls._is_encode_done(encode_dir):
            return True
        return cls._playlist_has_endlist(cls._playlist_path_for_dir(encode_dir))

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

            entries: list[tuple[Path, float, int]] = []
            for child in cache_dir.iterdir():
                if not child.is_dir():
                    continue
                lock_file = child / cls.LOCK_MARKER
                if lock_file.exists() and now - lock_file.stat().st_mtime < cls.ENCODE_TIMEOUT_SECONDS:
                    continue
                try:
                    stat = child.stat()
                except OSError:
                    continue
                atime = stat.st_atime if stat.st_atime > 0 else stat.st_mtime
                total = 0
                for item in child.rglob("*"):
                    try:
                        total += item.stat().st_size
                    except OSError:
                        continue
                entries.append((child, atime, total))

            stale_cutoff = now - cls.GC_STALE_SECONDS
            for path, atime, _size in list(entries):
                if atime < stale_cutoff:
                    cls._delete_dir_sync(path)
                    entries.remove((path, atime, _size))

            total_size = sum(size for _, _, size in entries)
            if total_size <= cls.GC_MAX_CACHE_BYTES:
                return

            entries.sort(key=lambda item: item[1])
            for path, atime, size in entries:
                if total_size <= cls.GC_MAX_CACHE_BYTES:
                    break
                cls._delete_dir_sync(path)
                total_size -= size

    @classmethod
    def _delete_dir_sync(cls, path: Path) -> None:
        try:
            for item in sorted(path.rglob("*"), reverse=True):
                if item.is_file() or item.is_symlink():
                    item.unlink(missing_ok=True)
                elif item.is_dir():
                    try:
                        item.rmdir()
                    except OSError:
                        continue
            path.rmdir()
        except OSError:
            return
