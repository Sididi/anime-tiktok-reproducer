"""Service for managing the anime library (indexing, listing, copying)."""

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import shutil
import threading
import tempfile
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from PIL import Image

from ..config import settings
from ..library_types import DEFAULT_LIBRARY_TYPE, LibraryType, coerce_library_type, resolve_scoped_library_path
from ..utils.media_binaries import (
    get_media_subprocess_env,
    is_media_binary_override_error,
    rewrite_media_command,
)
from ..utils.subprocess_runner import CommandTimeoutError, run_command, terminate_process

logger = logging.getLogger("uvicorn.error")


@dataclass
class IndexProgress:
    """Progress information for anime indexing."""

    status: str  # starting, copying, indexing, complete, error
    progress: float = 0.0  # 0-1
    message: str = ""
    current_file: str = ""
    total_files: int = 0
    completed_files: int = 0
    error: str | None = None
    anime_name: str | None = None
    prepared_library_paths: list[str] | None = None
    warnings: list[str] | None = None
    current_file_progress: float | None = None
    current_file_frames_processed: int | None = None
    current_file_total_frames: int | None = None
    current_file_batches_processed: int | None = None
    requested_batch_size: int | None = None
    effective_batch_size: int | None = None
    effective_decode_backend: str | None = None
    retry_reason: str | None = None

    def to_dict(self) -> dict:
        payload = {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "current_file": self.current_file,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "error": self.error,
        }
        if self.anime_name is not None:
            payload["anime_name"] = self.anime_name
        if self.prepared_library_paths is not None:
            payload["prepared_library_paths"] = self.prepared_library_paths
        if self.warnings is not None:
            payload["warnings"] = self.warnings
        if self.current_file_progress is not None:
            payload["current_file_progress"] = self.current_file_progress
        if self.current_file_frames_processed is not None:
            payload["current_file_frames_processed"] = self.current_file_frames_processed
        if self.current_file_total_frames is not None:
            payload["current_file_total_frames"] = self.current_file_total_frames
        if self.current_file_batches_processed is not None:
            payload["current_file_batches_processed"] = self.current_file_batches_processed
        if self.requested_batch_size is not None:
            payload["requested_batch_size"] = self.requested_batch_size
        if self.effective_batch_size is not None:
            payload["effective_batch_size"] = self.effective_batch_size
        if self.effective_decode_backend is not None:
            payload["effective_decode_backend"] = self.effective_decode_backend
        if self.retry_reason is not None:
            payload["retry_reason"] = self.retry_reason
        return payload


@dataclass
class SearcherExecutionMetadata:
    """Mutable metadata describing the active anime_searcher subprocess run."""

    requested_batch_size: int
    effective_batch_size: int
    requested_decode_backend: str
    effective_decode_backend: str | None = None
    retry_reason: str | None = None
    retry_warning: str | None = None


@dataclass(frozen=True)
class SourceVideoScan:
    """Readable vs unreadable source candidates discovered in one scan."""

    readable_files: tuple[Path, ...] = ()
    invalid_files: tuple[Path, ...] = ()

    @property
    def has_direct_videos(self) -> bool:
        return bool(self.readable_files or self.invalid_files)


@dataclass(frozen=True)
class SourceMediaStream:
    """Metadata for one probed media stream."""

    index: int
    stream_position: int
    codec_type: str
    codec_name: str | None
    language: str | None
    raw_language: str | None
    title: str | None
    handler_name: str | None
    is_default: bool = False
    duration: float | None = None


@dataclass(frozen=True)
class SourceMediaProbe:
    """Normalized media probe result for one source episode."""

    source_path: Path
    container_suffix: str
    format_name: str | None
    video_codec: str | None
    audio_codec: str | None
    pix_fmt: str | None
    fps: float | None
    duration: float | None
    has_audio: bool
    audio_streams: tuple[SourceMediaStream, ...] = ()
    subtitle_streams: tuple[SourceMediaStream, ...] = ()
    data_streams: tuple[SourceMediaStream, ...] = ()
    selected_audio_stream_index: int | None = None
    video_duration: float | None = None
    selected_audio_stream: SourceMediaStream | None = None


@dataclass(frozen=True)
class SourceNormalizationPlan:
    """Chosen compatibility action for one source episode."""

    action: str
    source_path: Path
    target_path: Path
    probe: SourceMediaProbe
    cleanup_source_after_commit: bool = False


@dataclass(frozen=True)
class SourceNormalizationResult:
    """Normalization result for one source episode."""

    action: str
    source_path: Path
    normalized_path: Path
    changed: bool


@dataclass(frozen=True)
class SubtitleSidecarEntry:
    """One preserved subtitle stream in a library sidecar."""

    stream_index: int
    stream_position: int
    codec_name: str | None
    language: str | None
    raw_language: str | None
    title: str | None
    kind: str
    asset_filename: str | None
    handler_name: str | None = None
    cue_manifest_filename: str | None = None
    status: str = "ok"
    error: str | None = None


class AnimeLibraryService:
    """Service for managing the anime library."""

    VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v"}
    TEXT_SUBTITLE_CODECS = {
        "ass",
        "mov_text",
        "srt",
        "ssa",
        "subrip",
        "text",
        "webvtt",
        "hdmv_text_subtitle",
    }
    IMAGE_SUBTITLE_CODECS = {
        "hdmv_pgs_subtitle",
    }
    SUBTITLE_SIDECAR_SUFFIX = ".atr_subtitles"
    SOURCE_IMPORT_MANIFEST_SUFFIX = ".atr_source.json"
    INDEX_DIR_NAME = ".index"
    MANIFEST_FILE = "manifest.json"
    LEGACY_METADATA_FILE = "metadata.json"
    STATE_FILE = "state.json"
    LIST_TIMEOUT_SECONDS = 120.0
    SEARCH_TIMEOUT_SECONDS = 120.0
    REMUX_TIMEOUT_SECONDS = 600.0
    INDEX_TIMEOUT_SECONDS = 7200.0
    SEARCHER_INDEX_FORMAT_VERSION = 4
    SEARCHER_ENGINE_PROFILE = "sscd_exact_resize_v1"
    SEARCHER_CUDA_OOM_BATCH_RETRY_LIMIT = 1
    SEARCHER_CUDA_OOM_RETRY_REASON = "cuda_oom_batch_downshift"
    PREVIEW_PROXY_TIMEOUT_SECONDS = 3600.0
    SOURCE_NORMALIZATION_TIMEOUT_SECONDS = 7200.0
    SUBTITLE_EXTRACTION_TIMEOUT_SECONDS = 1800.0
    FFPROBE_TIMEOUT_SECONDS = 30.0
    SOURCE_NORMALIZATION_AUDIO_BITRATE = "192k"
    SOURCE_NORMALIZATION_AUDIO_RATE = "48000"
    SOURCE_NORMALIZATION_PROFILE_H264_MP4_AAC = "h264_mp4_aac"
    GPU_HWACCEL = "cuda"
    GPU_H264_ENCODER = "h264_nvenc"
    LIBRARY_IMPORT_TRANSCODE_TIMEOUT_SECONDS = 7200.0
    PREMIERE_NATIVE_VIDEO_CODECS = {"h264", "hevc"}
    _CUDA_DECODERS: dict[str, str] = {
        "av1": "av1_cuvid",
        "h264": "h264_cuvid",
        "hevc": "hevc_cuvid",
        "vp9": "vp9_cuvid",
    }
    _CUDA_OOM_MARKERS = (
        "cuda out of memory",
        "torch.cuda.outofmemoryerror",
        "tried to allocate",
        "pytorch_cuda_alloc_conf",
        "expandable_segments:true",
    )

    _episode_manifest_cache: dict[str, dict] = {}
    _episode_manifest_locks: dict[str, asyncio.Lock] = {}
    _preview_generation_lock: asyncio.Lock | None = None
    _preview_generation_inflight: set[str] = set()
    _preview_proxy_locks_guard = threading.Lock()
    _preview_proxy_locks: dict[str, threading.Lock] = {}
    _LANGUAGE_ALIASES = {
        "en": "en",
        "eng": "en",
        "english": "en",
        "ja": "ja",
        "jpn": "ja",
        "jp": "ja",
        "jap": "ja",
        "japanese": "ja",
        "fr": "fr",
        "fra": "fr",
        "fre": "fr",
        "french": "fr",
        "francais": "fr",
        "français": "fr",
        "es": "es",
        "spa": "es",
        "spanish": "es",
        "espanol": "es",
        "español": "es",
        "pt": "pt",
        "por": "pt",
        "portuguese": "pt",
        "portugues": "pt",
        "de": "de",
        "deu": "de",
        "ger": "de",
        "german": "de",
        "deutsch": "de",
        "it": "it",
        "ita": "it",
        "italian": "it",
        "italiano": "it",
        "ru": "ru",
        "rus": "ru",
        "russian": "ru",
    }

    @staticmethod
    def get_library_root() -> Path:
        """Get the root directory that contains all typed libraries."""
        return settings.anime_library_path

    @classmethod
    def get_library_path(
        cls,
        library_type: LibraryType | str | None = None,
    ) -> Path:
        """Get the scoped typed library path from settings."""
        return resolve_scoped_library_path(cls.get_library_root(), library_type)

    @staticmethod
    def get_anime_searcher_path() -> Path:
        """Get the anime_searcher module path."""
        return settings.anime_searcher_path

    @staticmethod
    def _coerce_fps(value: object) -> float | None:
        """Convert candidate FPS value to a positive float when possible."""
        try:
            fps = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if fps <= 0:
            return None
        return fps

    @classmethod
    def _get_indexed_series_fps_sync(
        cls,
        anime_name: str,
        library_type: LibraryType | str | None = None,
    ) -> float | None:
        """Read FPS for an already indexed series from index metadata."""
        index_dir = cls.get_library_path(library_type) / cls.INDEX_DIR_NAME
        manifest_path = index_dir / cls.MANIFEST_FILE
        if manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = None

            if isinstance(payload, dict):
                if payload.get("version") != cls.SEARCHER_INDEX_FORMAT_VERSION:
                    return None
                if payload.get("engine_profile") != cls.SEARCHER_ENGINE_PROFILE:
                    return None
                series_map = payload.get("series", {})
                if isinstance(series_map, dict):
                    entry = series_map.get(anime_name)
                    if entry is not None:
                        if isinstance(entry, dict):
                            series_fps = cls._coerce_fps(entry.get("fps"))
                            if series_fps is not None:
                                return series_fps
                        config = payload.get("config", {})
                        if isinstance(config, dict):
                            return cls._coerce_fps(
                                config.get("default_fps")
                            )
        return None

    @classmethod
    def _is_cuda_oom_error(cls, message: str | None) -> bool:
        if not message:
            return False
        normalized = message.casefold()
        return all(marker in normalized for marker in ("cuda", "memory")) or any(
            marker in normalized for marker in cls._CUDA_OOM_MARKERS
        )

    @classmethod
    def _terminal_cuda_oom_message(cls, message: str) -> str:
        guidance = (
            " Retry also failed; this job exceeded available VRAM under the current parallel load."
        )
        return message if message.endswith(guidance.strip()) else f"{message}{guidance}"

    @staticmethod
    def _with_cli_flag(cmd: list[str], flag: str, value: str) -> list[str]:
        updated = list(cmd)
        if flag in updated:
            flag_index = updated.index(flag)
            if flag_index + 1 < len(updated):
                updated[flag_index + 1] = value
                return updated
        updated.extend([flag, value])
        return updated

    @classmethod
    def _annotate_searcher_progress(
        cls,
        progress: IndexProgress,
        *,
        anime_name: str,
        metadata: SearcherExecutionMetadata,
    ) -> IndexProgress:
        progress.anime_name = anime_name
        progress.requested_batch_size = metadata.requested_batch_size
        progress.effective_batch_size = metadata.effective_batch_size
        progress.effective_decode_backend = (
            progress.effective_decode_backend
            or metadata.effective_decode_backend
            or metadata.requested_decode_backend
        )
        progress.retry_reason = metadata.retry_reason
        return progress

    @classmethod
    async def _stream_searcher_index_command(
        cls,
        *,
        cmd: list[str],
        cwd: Path,
        total_files: int,
        anime_name: str,
        metadata: SearcherExecutionMetadata,
        progress_start: float,
        progress_span: float,
    ) -> AsyncIterator[IndexProgress]:
        current_batch_size = metadata.effective_batch_size
        retry_attempts = 0

        while True:
            current_cmd = cls._with_cli_flag(
                cmd,
                "--batch-size",
                str(current_batch_size),
            )
            retry_requested = False

            async for raw_progress in cls._stream_searcher_command(
                cmd=current_cmd,
                cwd=cwd,
                total_files=total_files,
                status="indexing",
                progress_start=progress_start,
                progress_span=progress_span,
            ):
                if raw_progress.effective_decode_backend:
                    metadata.effective_decode_backend = raw_progress.effective_decode_backend

                progress = cls._annotate_searcher_progress(
                    raw_progress,
                    anime_name=anime_name,
                    metadata=metadata,
                )

                if progress.status != "error":
                    yield progress
                    continue

                error_message = progress.error or progress.message or "anime_searcher command failed"
                reduced_batch_size = max(1, current_batch_size // 2)
                should_retry = (
                    cls._is_cuda_oom_error(error_message)
                    and retry_attempts < cls.SEARCHER_CUDA_OOM_BATCH_RETRY_LIMIT
                    and reduced_batch_size < current_batch_size
                )
                if should_retry:
                    retry_attempts += 1
                    current_batch_size = reduced_batch_size
                    metadata.effective_batch_size = current_batch_size
                    metadata.retry_reason = cls.SEARCHER_CUDA_OOM_RETRY_REASON
                    metadata.retry_warning = (
                        "CUDA OOM detected under the current parallel load; retrying "
                        f"with reduced batch size ({metadata.requested_batch_size} -> {current_batch_size}) "
                        "while keeping fp32."
                    )
                    yield cls._annotate_searcher_progress(
                        IndexProgress(
                            status="indexing",
                            message=metadata.retry_warning,
                            progress=max(progress.progress, progress_start),
                            total_files=total_files,
                            completed_files=progress.completed_files,
                            current_file=progress.current_file,
                            warnings=[metadata.retry_warning],
                        ),
                        anime_name=anime_name,
                        metadata=metadata,
                    )
                    retry_requested = True
                    break

                if cls._is_cuda_oom_error(error_message):
                    progress.error = cls._terminal_cuda_oom_message(error_message)
                yield progress
                return

            if not retry_requested:
                metadata.effective_batch_size = current_batch_size
                return

    @classmethod
    def _parse_searcher_progress_line(
        cls,
        *,
        line: str,
        status: str,
        total_files: int,
        progress_start: float,
        progress_span: float,
        text_line_index: int,
    ) -> IndexProgress | None:
        line_str = line.strip()
        if not line_str:
            return None

        try:
            payload = json.loads(line_str)
        except json.JSONDecodeError:
            denominator = max(total_files * 100, 1)
            return IndexProgress(
                status=status,
                message=line_str[:100],
                progress=progress_start + progress_span * (text_line_index / denominator),
                total_files=total_files,
            )

        if not isinstance(payload, dict) or "event" not in payload:
            denominator = max(total_files * 100, 1)
            return IndexProgress(
                status=status,
                message=line_str[:100],
                progress=progress_start + progress_span * (text_line_index / denominator),
                total_files=total_files,
            )

        event = str(payload.get("event"))
        message = str(payload.get("message") or "")
        if event == "error":
            error_message = str(payload.get("error") or message or "anime_searcher command failed")
            return IndexProgress(status="error", error=error_message)

        try:
            progress_value = float(payload.get("progress", 0.0))
        except (TypeError, ValueError):
            progress_value = 0.0
        progress_value = max(0.0, min(1.0, progress_value))

        try:
            completed_files = int(payload.get("completed_files", 0))
        except (TypeError, ValueError):
            completed_files = 0
        try:
            total_files_value = int(payload.get("total_files", total_files))
        except (TypeError, ValueError):
            total_files_value = total_files
        try:
            current_file_progress = payload.get("current_file_progress")
            current_file_progress_value = (
                None
                if current_file_progress is None
                else max(0.0, min(1.0, float(current_file_progress)))
            )
        except (TypeError, ValueError):
            current_file_progress_value = None
        try:
            current_file_frames_processed = payload.get("current_file_frames_processed")
            current_file_frames_processed_value = (
                None
                if current_file_frames_processed is None
                else int(current_file_frames_processed)
            )
        except (TypeError, ValueError):
            current_file_frames_processed_value = None
        try:
            current_file_total_frames = payload.get("current_file_total_frames")
            current_file_total_frames_value = (
                None if current_file_total_frames is None else int(current_file_total_frames)
            )
        except (TypeError, ValueError):
            current_file_total_frames_value = None
        try:
            current_file_batches_processed = payload.get("current_file_batches_processed")
            current_file_batches_processed_value = (
                None
                if current_file_batches_processed is None
                else int(current_file_batches_processed)
            )
        except (TypeError, ValueError):
            current_file_batches_processed_value = None
        effective_decode_backend = payload.get("decode_backend")
        effective_decode_backend_value = (
            None
            if effective_decode_backend in {None, ""}
            else str(effective_decode_backend)
        )
        try:
            effective_batch_size = payload.get("batch_size")
            effective_batch_size_value = (
                None if effective_batch_size is None else int(effective_batch_size)
            )
        except (TypeError, ValueError):
            effective_batch_size_value = None

        return IndexProgress(
            status=status,
            message=message[:100],
            current_file=str(payload.get("current_file") or ""),
            progress=progress_start + progress_span * progress_value,
            total_files=total_files_value,
            completed_files=completed_files,
            current_file_progress=current_file_progress_value,
            current_file_frames_processed=current_file_frames_processed_value,
            current_file_total_frames=current_file_total_frames_value,
            current_file_batches_processed=current_file_batches_processed_value,
            effective_batch_size=effective_batch_size_value,
            effective_decode_backend=effective_decode_backend_value,
        )

    @classmethod
    def get_episode_manifest_path(
        cls,
        library_type: LibraryType | str | None = None,
    ) -> Path:
        """Get path for cached episode index manifest."""
        scoped_type = coerce_library_type(library_type).value
        return settings.cache_dir / f"episodes_manifest__{scoped_type}.json"

    @classmethod
    def _get_manifest_lock(
        cls,
        library_type: LibraryType | str | None = None,
    ) -> asyncio.Lock:
        scoped_type = coerce_library_type(library_type).value
        lock = cls._episode_manifest_locks.get(scoped_type)
        if lock is None:
            lock = asyncio.Lock()
            cls._episode_manifest_locks[scoped_type] = lock
        return lock

    @classmethod
    def _get_preview_generation_lock(cls) -> asyncio.Lock:
        if cls._preview_generation_lock is None:
            cls._preview_generation_lock = asyncio.Lock()
        return cls._preview_generation_lock

    @classmethod
    def is_transient_library_video_path(cls, path: Path) -> bool:
        """Return True for importer-owned temporary media artifacts."""
        if path.suffix.lower() not in cls.VIDEO_EXTENSIONS:
            return False
        stem = path.stem.lower()
        return stem.endswith(".import.tmp") or stem.endswith(".normalize.tmp")

    @classmethod
    def _get_preview_proxy_lock(cls, source_path: Path) -> threading.Lock:
        """Return a per-source lock to serialize sync preview proxy generation."""
        key = str(source_path.resolve())
        with cls._preview_proxy_locks_guard:
            lock = cls._preview_proxy_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._preview_proxy_locks[key] = lock
            return lock

    @classmethod
    def _scan_library_episodes_sync(
        cls,
        library_type: LibraryType | str | None = None,
    ) -> dict:
        """Scan library once and build fast stem -> path index."""
        scoped_type = coerce_library_type(library_type)
        library_path = cls.get_library_path(scoped_type)
        episodes: list[str] = []
        by_stem: dict[str, list[str]] = {}

        if library_path.exists():
            for entry in library_path.rglob("*"):
                if (
                    not entry.is_file()
                    or entry.suffix.lower() not in cls.VIDEO_EXTENSIONS
                    or cls.is_transient_library_video_path(entry)
                ):
                    continue
                resolved = str(entry.resolve())
                episodes.append(resolved)

                stem = entry.stem
                for key in (stem, stem.lower()):
                    by_stem.setdefault(key, []).append(resolved)

        episodes = sorted(set(episodes))
        by_stem = {k: sorted(set(v)) for k, v in by_stem.items()}
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "library_root": str(library_path.resolve()),
            "episodes": episodes,
            "by_stem": by_stem,
        }

        manifest_path = cls.get_episode_manifest_path(scoped_type)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        cls._episode_manifest_cache[scoped_type.value] = manifest
        return manifest

    @classmethod
    def _load_episode_manifest_sync(
        cls,
        library_type: LibraryType | str | None = None,
    ) -> dict | None:
        """Load cached episode manifest if present."""
        scoped_type = coerce_library_type(library_type)
        cached = cls._episode_manifest_cache.get(scoped_type.value)
        if cached is not None:
            return cached

        manifest_path = cls.get_episode_manifest_path(scoped_type)
        if not manifest_path.exists():
            return None

        try:
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest.get("by_stem"), dict):
                return None
            cls._episode_manifest_cache[scoped_type.value] = manifest
            return manifest
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    async def ensure_episode_manifest(
        cls,
        *,
        force_refresh: bool = False,
        library_type: LibraryType | str | None = None,
    ) -> dict:
        """Ensure episode manifest exists, rebuilding if needed."""
        if not force_refresh:
            manifest = await asyncio.to_thread(cls._load_episode_manifest_sync, library_type)
            if manifest is not None:
                return manifest

        async with cls._get_manifest_lock(library_type):
            if not force_refresh:
                manifest = await asyncio.to_thread(cls._load_episode_manifest_sync, library_type)
                if manifest is not None:
                    return manifest
            return await asyncio.to_thread(cls._scan_library_episodes_sync, library_type)

    @classmethod
    def resolve_episode_path(
        cls,
        episode_name: str,
        manifest: dict | None = None,
        *,
        library_type: LibraryType | str | None = None,
    ) -> Path | None:
        """Resolve an episode path using cached manifest (no recursive scan)."""
        candidate = Path(episode_name)
        if candidate.is_absolute() and candidate.exists():
            return candidate

        library_path = cls.get_library_path(library_type)
        if candidate.suffix and not candidate.is_absolute():
            full = (library_path / candidate).resolve()
            if full.exists():
                return full

        manifest_data = manifest or cls._load_episode_manifest_sync(library_type)
        if manifest_data is None:
            return None

        lookup_keys = []
        stem = Path(episode_name).stem if Path(episode_name).suffix else episode_name
        for key in (episode_name, stem):
            lookup_keys.append(key)
            lookup_keys.append(key.lower())

        seen: set[str] = set()
        by_stem = manifest_data.get("by_stem", {})
        for key in lookup_keys:
            for raw_path in by_stem.get(key, []):
                if raw_path in seen:
                    continue
                seen.add(raw_path)
                path = Path(raw_path)
                if path.exists():
                    return path
        return None

    @classmethod
    def list_episode_paths(
        cls,
        manifest: dict | None = None,
        *,
        library_type: LibraryType | str | None = None,
    ) -> list[str]:
        """Return known episode absolute paths from manifest."""
        manifest_data = manifest or cls._load_episode_manifest_sync(library_type)
        if manifest_data is None:
            return []
        return [p for p in manifest_data.get("episodes", []) if Path(p).exists()]

    @classmethod
    def _get_source_normalization_profile(cls) -> str:
        profile = str(settings.source_normalization_profile or "").strip().lower()
        return profile or cls.SOURCE_NORMALIZATION_PROFILE_H264_MP4_AAC

    @classmethod
    def _list_direct_video_files_sync(cls, folder: Path) -> list[Path]:
        try:
            return sorted(
                entry
                for entry in folder.iterdir()
                if (
                    entry.is_file()
                    and entry.suffix.lower() in cls.VIDEO_EXTENSIONS
                    and not cls.is_transient_library_video_path(entry)
                )
            )
        except (OSError, PermissionError):
            return []

    @classmethod
    def _scan_source_video_paths_sync(cls, source_paths: list[Path]) -> SourceVideoScan:
        readable_files: list[Path] = []
        invalid_files: list[Path] = []
        seen_paths: set[str] = set()

        for source_path in source_paths:
            try:
                key = str(source_path.resolve())
            except OSError:
                key = str(source_path)
            if key in seen_paths:
                continue
            seen_paths.add(key)

            probe = cls._probe_media_sync(source_path)
            if probe is None or probe.duration is None:
                invalid_files.append(source_path)
            else:
                readable_files.append(source_path)

        return SourceVideoScan(
            readable_files=tuple(readable_files),
            invalid_files=tuple(invalid_files),
        )

    @classmethod
    def scan_direct_video_files_sync(cls, folder: Path) -> SourceVideoScan:
        return cls._scan_source_video_paths_sync(cls._list_direct_video_files_sync(folder))

    @staticmethod
    def _invalid_source_warning(source_path: Path) -> str:
        return f"Ignored unreadable source file: {source_path.name}"

    @staticmethod
    def _invalid_source_error_detail(invalid_files: tuple[Path, ...]) -> str:
        names = [path.name for path in invalid_files if path.name]
        if not names:
            return "Unreadable source files were detected."
        preview = ", ".join(names[:5])
        if len(names) > 5:
            preview += f", and {len(names) - 5} more"
        return f"Unreadable source files: {preview}"

    @staticmethod
    def _parse_ffprobe_rate(raw_value: object) -> float | None:
        raw = str(raw_value or "").strip()
        if not raw:
            return None
        try:
            if "/" in raw:
                num_raw, den_raw = raw.split("/", 1)
                num = float(num_raw)
                den = float(den_raw)
                if den == 0:
                    return None
                fps = num / den
            else:
                fps = float(raw)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

        if fps <= 0:
            return None
        return fps

    @staticmethod
    def _parse_ffprobe_duration(raw_value: object) -> float | None:
        raw = str(raw_value or "").strip()
        if not raw:
            return None
        try:
            duration = float(raw)
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        return duration

    @classmethod
    def normalize_stream_language(
        cls,
        raw_language: str | None,
        *,
        title: str | None = None,
        handler_name: str | None = None,
    ) -> str | None:
        """Normalize stream language tags and fallback title hints to a short code."""
        raw = str(raw_language or "").strip().lower()
        if raw:
            normalized = cls._normalize_language_token(raw)
            if normalized:
                return normalized

        haystack = " ".join(
            part for part in (title, handler_name) if part and str(part).strip()
        ).lower()
        if not haystack:
            return None
        for needle, normalized in (
            ("japanese", "ja"),
            (" jap ", "ja"),
            ("jpn", "ja"),
            ("english", "en"),
            (" eng ", "en"),
            ("french", "fr"),
            ("francais", "fr"),
            ("français", "fr"),
            ("spanish", "es"),
            ("español", "es"),
            ("espanol", "es"),
            ("portuguese", "pt"),
            ("portugues", "pt"),
            ("brazilian portuguese", "pt"),
            ("german", "de"),
            ("deutsch", "de"),
            ("italian", "it"),
            ("italiano", "it"),
            ("russian", "ru"),
        ):
            if needle.strip() in haystack:
                return normalized
        return None

    @classmethod
    def _normalize_language_token(cls, raw_language: str) -> str | None:
        token = str(raw_language or "").strip().lower()
        if not token:
            return None

        mapped = cls._LANGUAGE_ALIASES.get(token)
        if mapped:
            return mapped

        primary = re.split(r"[-_]", token, maxsplit=1)[0]
        mapped = cls._LANGUAGE_ALIASES.get(primary)
        if mapped:
            return mapped

        if len(primary) == 2 and primary.isascii() and primary.isalpha():
            return primary
        return None

    @classmethod
    def _subtitle_kind_for_codec(cls, codec_name: str | None) -> str:
        codec = str(codec_name or "").strip().lower()
        if codec in cls.TEXT_SUBTITLE_CODECS:
            return "text"
        if codec in cls.IMAGE_SUBTITLE_CODECS:
            return "image"
        return "unsupported"

    @classmethod
    def _build_stream_info(
        cls,
        stream: dict[str, Any],
        *,
        stream_position: int,
    ) -> SourceMediaStream | None:
        try:
            index = int(stream.get("index"))
        except (TypeError, ValueError):
            return None

        codec_type = str(stream.get("codec_type", "")).strip().lower()
        tags = stream.get("tags", {})
        if not isinstance(tags, dict):
            tags = {}
        disposition = stream.get("disposition", {})
        if not isinstance(disposition, dict):
            disposition = {}
        title = str(tags.get("title", "")).strip() or None
        raw_language = str(tags.get("language", "")).strip() or None
        handler_name = str(tags.get("handler_name", "")).strip() or None

        return SourceMediaStream(
            index=index,
            stream_position=stream_position,
            codec_type=codec_type,
            codec_name=str(stream.get("codec_name", "")).strip().lower() or None,
            language=cls.normalize_stream_language(
                raw_language,
                title=title,
                handler_name=handler_name,
            ),
            raw_language=raw_language,
            title=title,
            handler_name=handler_name,
            is_default=bool(disposition.get("default", 0)),
            duration=cls._parse_ffprobe_duration(stream.get("duration")),
        )

    @classmethod
    def select_preferred_audio_stream(
        cls,
        probe: SourceMediaProbe,
        *,
        target_language: str | None = None,
    ) -> SourceMediaStream | None:
        """Select the single audio stream kept in normalized sources."""
        return cls._select_preferred_audio_stream_from_streams(
            probe.audio_streams,
            target_language=target_language,
        )

    @classmethod
    def _select_preferred_audio_stream_from_streams(
        cls,
        streams: tuple[SourceMediaStream, ...] | list[SourceMediaStream],
        *,
        target_language: str | None = None,
    ) -> SourceMediaStream | None:
        """Select the preferred audio stream from an ordered stream list."""
        if not streams:
            return None

        preferred_languages: list[str] = []
        seen_languages: set[str] = set()
        for preferred_lang in (
            "ja",
            cls.normalize_stream_language(target_language),
            "en",
        ):
            if not preferred_lang or preferred_lang in seen_languages:
                continue
            preferred_languages.append(preferred_lang)
            seen_languages.add(preferred_lang)

        for preferred_lang in preferred_languages:
            for stream in streams:
                if stream.language == preferred_lang:
                    return stream

        default_stream = next((stream for stream in streams if stream.is_default), None)
        if default_stream is not None:
            return default_stream
        return min(streams, key=lambda stream: (stream.stream_position, stream.index))

    @classmethod
    def _with_preferred_audio_stream(
        cls,
        probe: SourceMediaProbe,
        *,
        target_language: str | None,
        source_import_payload: dict[str, Any] | None = None,
    ) -> SourceMediaProbe:
        preferred_audio_stream = cls.select_preferred_audio_stream(
            probe,
            target_language=target_language,
        )
        selected_audio_stream = preferred_audio_stream
        if len(probe.audio_streams) > 1:
            import_audio_streams = cls._load_source_import_audio_streams(source_import_payload)
            if import_audio_streams and not any(stream.language for stream in probe.audio_streams):
                preferred_original_stream = cls._select_preferred_audio_stream_from_streams(
                    import_audio_streams,
                    target_language=target_language,
                )
                if preferred_original_stream is not None:
                    mapped_current_stream = cls._audio_stream_by_position(
                        probe,
                        preferred_original_stream.stream_position,
                    )
                    if mapped_current_stream is not None:
                        preferred_audio_stream = mapped_current_stream
                        selected_audio_stream = preferred_original_stream
        audio_codec = None
        selected_audio_stream_index = None
        if preferred_audio_stream is not None:
            audio_codec = preferred_audio_stream.codec_name
            selected_audio_stream_index = preferred_audio_stream.index
        return replace(
            probe,
            audio_codec=audio_codec,
            selected_audio_stream_index=selected_audio_stream_index,
            selected_audio_stream=selected_audio_stream,
        )

    @staticmethod
    def _audio_stream_by_index(
        probe: SourceMediaProbe,
        stream_index: int | None,
    ) -> SourceMediaStream | None:
        if stream_index is None:
            return None
        for stream in probe.audio_streams:
            if stream.index == stream_index:
                return stream
        return None

    @staticmethod
    def _audio_stream_by_position(
        probe: SourceMediaProbe,
        stream_position: int | None,
    ) -> SourceMediaStream | None:
        if stream_position is None:
            return None
        for stream in probe.audio_streams:
            if stream.stream_position == stream_position:
                return stream
        return None

    @classmethod
    def get_subtitle_sidecar_dir(cls, source_path: Path) -> Path:
        """Return the deterministic sidecar directory for one normalized source."""
        return source_path.with_name(f"{source_path.stem}{cls.SUBTITLE_SIDECAR_SUFFIX}")

    @classmethod
    def get_subtitle_sidecar_manifest_path(cls, source_path: Path) -> Path:
        return cls.get_subtitle_sidecar_dir(source_path) / "manifest.json"

    @classmethod
    def _subtitle_sidecar_lookup_paths(cls, source_path: Path) -> tuple[Path, ...]:
        candidates = [source_path]
        normalized_target = cls._normalized_target_path(source_path)
        if normalized_target != source_path:
            candidates.append(normalized_target)

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return tuple(unique)

    @classmethod
    def resolve_subtitle_sidecar_source_path(cls, source_path: Path) -> Path | None:
        queue = deque(cls._subtitle_sidecar_lookup_paths(source_path))
        seen: set[str] = set()

        while queue:
            candidate = queue.popleft()
            key = str(candidate.resolve()) if candidate.exists() else str(candidate)
            if key in seen:
                continue
            seen.add(key)

            if cls.get_subtitle_sidecar_manifest_path(candidate).exists():
                return candidate

            payload = cls._load_source_import_manifest_sync(candidate)
            if not isinstance(payload, dict):
                continue
            linked_sidecar_source_raw = str(payload.get("sidecar_source_path", "")).strip()
            linked_sidecar_source = Path(linked_sidecar_source_raw) if linked_sidecar_source_raw else None
            if linked_sidecar_source is not None:
                queue.extend(cls._subtitle_sidecar_lookup_paths(linked_sidecar_source))
            original_source_raw = str(payload.get("source_path", "")).strip()
            original_source = Path(original_source_raw) if original_source_raw else None
            if original_source is not None:
                queue.extend(cls._subtitle_sidecar_lookup_paths(original_source))
        return None

    @classmethod
    def _subtitle_asset_basename(
        cls,
        stream: SourceMediaStream,
        *,
        extension: str,
    ) -> str:
        lang = stream.language or "und"
        return f"subtitle_stream_{stream.stream_position:02d}_{lang}{extension}"

    @classmethod
    def _probe_media_sync(cls, source_path: Path) -> SourceMediaProbe | None:
        """Probe container, codecs, fps, and duration for one media file."""
        cmd = rewrite_media_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                (
                    "format=format_name,duration:"
                    "stream=index,codec_type,codec_name,pix_fmt,avg_frame_rate,r_frame_rate,duration:"
                    "stream_tags=language,title,handler_name:"
                    "stream_disposition=default"
                ),
                "-of",
                "json",
                str(source_path),
            ]
        )
        try:
            env = get_media_subprocess_env(cmd)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            return None
        except subprocess.TimeoutExpired:
            return None

        if result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            streams = []

        video_stream = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and str(stream.get("codec_type", "")).lower() == "video"
            ),
            None,
        )
        if video_stream is None:
            return None

        audio_streams: list[SourceMediaStream] = []
        subtitle_streams: list[SourceMediaStream] = []
        data_streams: list[SourceMediaStream] = []
        audio_position = 0
        subtitle_position = 0
        data_position = 0
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            codec_type = str(stream.get("codec_type", "")).strip().lower()
            if codec_type == "audio":
                stream_info = cls._build_stream_info(stream, stream_position=audio_position)
                audio_position += 1
                if stream_info is not None:
                    audio_streams.append(stream_info)
            elif codec_type == "subtitle":
                stream_info = cls._build_stream_info(stream, stream_position=subtitle_position)
                subtitle_position += 1
                if stream_info is not None:
                    subtitle_streams.append(stream_info)
            elif codec_type == "data":
                stream_info = cls._build_stream_info(stream, stream_position=data_position)
                data_position += 1
                if stream_info is not None:
                    data_streams.append(stream_info)

        format_payload = payload.get("format", {})
        if not isinstance(format_payload, dict):
            format_payload = {}

        duration = cls._parse_ffprobe_duration(format_payload.get("duration"))

        av_durations: list[float] = []
        video_duration = cls._parse_ffprobe_duration(video_stream.get("duration"))
        if video_duration is not None:
            av_durations.append(video_duration)
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if str(stream.get("codec_type", "")).strip().lower() != "audio":
                continue
            audio_duration = cls._parse_ffprobe_duration(stream.get("duration"))
            if audio_duration is not None:
                av_durations.append(audio_duration)
        effective_duration = max(av_durations) if av_durations else duration

        fps = cls._parse_ffprobe_rate(video_stream.get("avg_frame_rate"))
        if fps is None:
            fps = cls._parse_ffprobe_rate(video_stream.get("r_frame_rate"))

        format_name = str(format_payload.get("format_name", "")).strip().lower() or None
        video_codec = str(video_stream.get("codec_name", "")).strip().lower() or None
        base_probe = SourceMediaProbe(
            source_path=source_path,
            container_suffix=source_path.suffix.lower(),
            format_name=format_name,
            video_codec=video_codec,
            audio_codec=None,
            pix_fmt=str(video_stream.get("pix_fmt", "")).strip().lower() or None,
            fps=fps,
            duration=effective_duration,
            has_audio=bool(audio_streams),
            audio_streams=tuple(audio_streams),
            subtitle_streams=tuple(subtitle_streams),
            data_streams=tuple(data_streams),
            video_duration=video_duration,
        )
        return cls._with_preferred_audio_stream(
            base_probe,
            target_language=None,
        )

    @classmethod
    def probe_source_media_sync(cls, source_path: Path) -> SourceMediaProbe | None:
        """Public sync probe wrapper used by normalization and tests."""
        return cls._probe_media_sync(source_path)

    @classmethod
    def classify_source_normalization(cls, probe: SourceMediaProbe) -> str:
        """Classify the cheapest compatibility action for Premiere-safe output.

        Since format normalization now happens at indexation time (unsupported
        sources are converted to MP4), the processing step usually only needs
        to handle audio-track selection and container fixups.
        """
        video_codec = (probe.video_codec or "").strip().lower()
        audio_codec = (probe.audio_codec or "").strip().lower()
        has_extra_audio_streams = len(probe.audio_streams) > 1
        has_subtitle_streams = len(probe.subtitle_streams) > 0
        has_data_streams = len(probe.data_streams) > 0
        premiere_compatible_container = probe.container_suffix == ".mp4"
        premiere_compatible_video = video_codec in cls.PREMIERE_NATIVE_VIDEO_CODECS

        # Already perfect: right codec, container, single audio, no junk streams.
        if (
            premiere_compatible_video
            and premiere_compatible_container
            and not has_extra_audio_streams
            and not has_subtitle_streams
            and not has_data_streams
            and (not probe.has_audio or audio_codec in {"aac", "pcm_s16le", "pcm_s24le", "pcm_s32le"})
        ):
            return "noop"

        # Source still has an incompatible video codec. Fall back to full H.264
        # MP4 transcode.
        if not premiere_compatible_video:
            return "full_h264_aac_transcode"

        # Container needs fixing (for example MKV/QuickTime with a native codec).
        if not premiere_compatible_container:
            return "remux_to_mp4"

        # Multiple audio streams → need to select one and remux.
        if has_extra_audio_streams:
            return "remux_audio_select"

        # Audio codec not Premiere-friendly for this container.
        if probe.has_audio and audio_codec not in {"aac", "pcm_s16le", "pcm_s24le", "pcm_s32le"}:
            return "audio_to_aac"

        # Container OK but has subtitle/data streams to strip, or multiple
        # audio streams remain.  The remux copies video and selected audio
        # while dropping everything else.
        return "remux_to_mp4"

    @staticmethod
    def _normalized_target_path(source_path: Path) -> Path:
        if source_path.suffix.lower() == ".mp4":
            return source_path
        return source_path.with_suffix(".mp4")

    @classmethod
    def _processing_normalized_target_path(
        cls,
        source_path: Path,
        *,
        project_id: str | None,
    ) -> Path:
        normalized_target = cls._normalized_target_path(source_path)
        if not project_id or cls._library_context_for_path(source_path) is None:
            return normalized_target

        from .project_service import ProjectService

        project_root = ProjectService.get_project_dir(project_id) / "processing_sources"
        try:
            library_root = cls.get_library_root().resolve()
            relative_path = normalized_target.resolve().relative_to(library_root)
            relative_parts = relative_path.parts[1:] if len(relative_path.parts) > 1 else relative_path.parts
        except (OSError, ValueError):
            relative_parts = (normalized_target.name,)
        return project_root.joinpath(*relative_parts)

    @classmethod
    def _serialize_source_import_audio_stream(
        cls,
        stream: SourceMediaStream,
    ) -> dict[str, Any]:
        return {
            "stream_index": stream.index,
            "stream_position": stream.stream_position,
            "language": stream.language,
            "raw_language": stream.raw_language,
            "title": stream.title,
            "handler_name": stream.handler_name,
            "is_default": stream.is_default,
        }

    @classmethod
    def _load_source_import_audio_streams(
        cls,
        payload: dict[str, Any] | None,
    ) -> tuple[SourceMediaStream, ...]:
        if not isinstance(payload, dict):
            return ()
        raw_streams = payload.get("audio_streams", [])
        if not isinstance(raw_streams, list):
            return ()

        streams: list[SourceMediaStream] = []
        for raw_stream in raw_streams:
            if not isinstance(raw_stream, dict):
                continue
            try:
                stream_index = int(raw_stream.get("stream_index"))
                stream_position = int(raw_stream.get("stream_position"))
            except (TypeError, ValueError):
                continue
            title = str(raw_stream.get("title", "")).strip() or None
            handler_name = str(raw_stream.get("handler_name", "")).strip() or None
            raw_language = str(raw_stream.get("raw_language", "")).strip() or None
            streams.append(
                SourceMediaStream(
                    index=stream_index,
                    stream_position=stream_position,
                    codec_type="audio",
                    codec_name=None,
                    language=cls.normalize_stream_language(
                        raw_stream.get("language") or raw_language,
                        title=title,
                        handler_name=handler_name,
                    ),
                    raw_language=raw_language,
                    title=title,
                    handler_name=handler_name,
                    is_default=bool(raw_stream.get("is_default", False)),
                )
            )
        return tuple(streams)

    @classmethod
    def _build_source_normalization_plan_sync(
        cls,
        source_path: Path,
        *,
        preferred_audio_language: str | None = None,
        target_path: Path | None = None,
    ) -> SourceNormalizationPlan:
        probe = cls._probe_media_sync(source_path)
        if probe is None:
            raise RuntimeError(f"Unable to probe source media: {source_path}")
        if probe.duration is None:
            raise RuntimeError(f"Unable to determine source duration: {source_path}")
        if probe.video_codec is None:
            raise RuntimeError(f"Unable to determine source video codec: {source_path}")
        source_import_payload = cls._load_source_import_manifest_sync(source_path)
        probe = cls._with_preferred_audio_stream(
            probe,
            target_language=preferred_audio_language,
            source_import_payload=source_import_payload,
        )
        resolved_target_path = target_path or cls._normalized_target_path(source_path)
        return SourceNormalizationPlan(
            action=cls.classify_source_normalization(probe),
            source_path=source_path,
            target_path=resolved_target_path,
            probe=probe,
            cleanup_source_after_commit=resolved_target_path != source_path,
        )

    @classmethod
    def _build_processing_reimport_plan_sync(
        cls,
        source_path: Path,
        *,
        preferred_audio_language: str | None = None,
        target_path: Path | None = None,
    ) -> SourceNormalizationPlan | None:
        current_probe = cls._probe_media_sync(source_path)
        if current_probe is None:
            return None
        if not cls._is_valid_normalized_probe(current_probe, reference_probe=current_probe):
            return None

        payload = cls._load_source_import_manifest_sync(source_path)
        if payload is None:
            return None

        original_source_raw = str(payload.get("original_source_path", "")).strip()
        if not original_source_raw:
            original_source_raw = str(payload.get("source_path", "")).strip()
        if not original_source_raw:
            return None

        original_source_path = Path(original_source_raw)
        try:
            if original_source_path.resolve() == source_path.resolve():
                return None
        except OSError:
            return None
        if not original_source_path.exists():
            return None

        original_probe = cls._probe_media_sync(original_source_path)
        if original_probe is None or len(original_probe.audio_streams) <= 1:
            return None
        if original_probe.duration is None or original_probe.video_codec is None:
            return None

        original_probe = cls._with_preferred_audio_stream(
            original_probe,
            target_language=preferred_audio_language,
            source_import_payload=payload,
        )
        if cls._is_valid_normalized_probe(current_probe, reference_probe=original_probe):
            return None

        resolved_target_path = target_path or source_path
        return SourceNormalizationPlan(
            action=cls.classify_source_normalization(original_probe),
            source_path=original_source_path,
            target_path=resolved_target_path,
            probe=original_probe,
            cleanup_source_after_commit=False,
        )

    @classmethod
    def _normalization_duration_tolerance_seconds(cls, probe: SourceMediaProbe) -> float:
        fps = probe.fps or 24.0
        return max(0.25, min(1.0, 3.0 / fps))

    @classmethod
    def _expected_normalized_duration_seconds(
        cls,
        reference_probe: SourceMediaProbe,
    ) -> float | None:
        selected_audio_stream = (
            reference_probe.selected_audio_stream
            or cls._audio_stream_by_index(
                reference_probe,
                reference_probe.selected_audio_stream_index,
            )
        )
        if (
            reference_probe.video_duration is not None
            and selected_audio_stream is not None
            and selected_audio_stream.duration is not None
        ):
            return max(reference_probe.video_duration, selected_audio_stream.duration)
        if reference_probe.video_duration is not None and not reference_probe.has_audio:
            return reference_probe.video_duration
        return reference_probe.duration

    @classmethod
    def _is_valid_normalized_probe(
        cls,
        normalized_probe: SourceMediaProbe | None,
        *,
        reference_probe: SourceMediaProbe,
    ) -> bool:
        if normalized_probe is None:
            return False
        if normalized_probe.container_suffix != ".mp4":
            return False
        video_codec = (normalized_probe.video_codec or "").strip().lower()
        if video_codec not in cls.PREMIERE_NATIVE_VIDEO_CODECS:
            return False
        if normalized_probe.duration is None:
            return False
        if normalized_probe.subtitle_streams:
            return False
        if normalized_probe.data_streams:
            return False

        normalized_audio = (normalized_probe.audio_codec or "").strip().lower()
        premiere_audio_codecs = {"aac", "pcm_s16le", "pcm_s24le", "pcm_s32le"}
        if reference_probe.has_audio:
            if (
                not normalized_probe.has_audio
                or normalized_audio not in premiere_audio_codecs
                or len(normalized_probe.audio_streams) != 1
            ):
                return False
        elif normalized_probe.has_audio:
            return False

        selected_audio_stream = (
            reference_probe.selected_audio_stream
            or cls._audio_stream_by_index(
                reference_probe,
                reference_probe.selected_audio_stream_index,
            )
        )
        if selected_audio_stream is not None and normalized_probe.audio_streams:
            normalized_audio_stream = normalized_probe.audio_streams[0]
            if selected_audio_stream.language and normalized_audio_stream.language:
                if normalized_audio_stream.language != selected_audio_stream.language:
                    return False
            elif (
                len(reference_probe.audio_streams) > 1
                and selected_audio_stream.language
                and normalized_audio_stream.language is None
            ):
                return False

        expected_duration = cls._expected_normalized_duration_seconds(reference_probe)
        if expected_duration is not None and normalized_probe.duration is not None:
            tolerance = cls._normalization_duration_tolerance_seconds(reference_probe)
            if abs(expected_duration - normalized_probe.duration) > tolerance:
                return False

        return normalized_probe.source_path.exists() and normalized_probe.source_path.stat().st_size > 0

    @classmethod
    def _build_selected_audio_map_args(cls, probe: SourceMediaProbe) -> list[str]:
        if probe.selected_audio_stream_index is None:
            return []
        return ["-map", f"0:{probe.selected_audio_stream_index}"]

    @classmethod
    def _build_audio_stream_metadata_args(
        cls,
        audio_streams: tuple[SourceMediaStream, ...] | list[SourceMediaStream],
    ) -> list[str]:
        args: list[str] = []
        default_indices = {
            idx for idx, stream in enumerate(audio_streams) if stream.is_default
        }
        for idx, stream in enumerate(audio_streams):
            language = (
                str(stream.raw_language or "").strip().lower()
                or cls.normalize_stream_language(
                    stream.language,
                    title=stream.title,
                    handler_name=stream.handler_name,
                )
                or "und"
            )
            args.extend(["-metadata:s:a:" + str(idx), f"language={language}"])
            if stream.title:
                args.extend(["-metadata:s:a:" + str(idx), f"title={stream.title}"])
            if stream.is_default:
                args.extend(["-disposition:a:" + str(idx), "default"])
            elif default_indices:
                args.extend(["-disposition:a:" + str(idx), "0"])
        return args

    @classmethod
    def _build_selected_audio_metadata_args(
        cls,
        probe: SourceMediaProbe,
    ) -> list[str]:
        if not probe.has_audio:
            return []
        selected_stream = (
            probe.selected_audio_stream
            or cls._audio_stream_by_index(probe, probe.selected_audio_stream_index)
        )
        if selected_stream is None:
            return []
        return cls._build_audio_stream_metadata_args((selected_stream,))

    @classmethod
    def _build_remux_to_mp4_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        return cls._build_common_normalization_cmd_from_probe(
            source_path,
            probe=probe,
        ) + [
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    @classmethod
    def _build_library_import_remux_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        return [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-dn",
            "-sn",
            "-write_tmcd",
            "0",
            "-c",
            "copy",
            *cls._build_audio_stream_metadata_args(probe.audio_streams),
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    @classmethod
    def _is_premiere_native_codec(cls, codec: str | None) -> bool:
        """Return True when video codec is natively supported by Premiere Pro."""
        return (codec or "").strip().lower() in cls.PREMIERE_NATIVE_VIDEO_CODECS

    @classmethod
    def _build_gpu_library_import_h264_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        source_codec: str | None = None,
        probe: SourceMediaProbe,
    ) -> list[str]:
        """Build a GPU-first library-import transcode to H.264/AAC MP4."""
        codec = (source_codec or "").strip().lower()
        cmd: list[str] = [
            "ffmpeg",
            "-y",
            "-hwaccel",
            cls.GPU_HWACCEL,
            "-hwaccel_output_format",
            cls.GPU_HWACCEL,
        ]
        cuda_decoder = cls._CUDA_DECODERS.get(codec)
        if cuda_decoder:
            cmd.extend(["-c:v", cuda_decoder])
        cmd.extend(
            [
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-dn",
                "-sn",
                "-write_tmcd",
                "0",
                "-vf",
                "scale_cuda=format=nv12",
                "-c:v",
                cls.GPU_H264_ENCODER,
                "-preset",
                "p5",
                "-rc",
                "constqp",
                "-qp",
                "23",
                "-b:v",
                "0",
                "-profile:v",
                "high",
            ]
        )
        cmd.extend(cls._build_audio_args(has_audio=probe.has_audio))
        cmd.extend(cls._build_audio_stream_metadata_args(probe.audio_streams))
        cmd.extend(
            [
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        return cmd

    @classmethod
    def _build_cpu_library_import_h264_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        """Build a CPU fallback library-import transcode to H.264/AAC MP4."""
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-dn",
            "-sn",
            "-write_tmcd",
            "0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
        ]
        cmd.extend(cls._build_audio_args(has_audio=probe.has_audio))
        cmd.extend(cls._build_audio_stream_metadata_args(probe.audio_streams))
        cmd.extend(
            [
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        return cmd

    @classmethod
    def _build_remux_audio_select_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        """Remux with selected audio track — video copied, audio to AAC.

        Functionally identical to ``_build_audio_to_aac_cmd``; the separate
        action name exists so that log messages distinguish *why* the remux
        happens (multi-track selection vs. incompatible audio codec).
        """
        return cls._build_audio_to_aac_cmd(source_path, output_path, probe=probe)

    @classmethod
    def _build_common_normalization_cmd_from_probe(
        cls,
        source_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        return [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            *cls._build_selected_audio_map_args(probe),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-dn",
            "-sn",
            "-write_tmcd",
            "0",
        ]

    @classmethod
    def _build_audio_to_aac_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        return cls._build_common_normalization_cmd_from_probe(
            source_path,
            probe=probe,
        ) + [
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            cls.SOURCE_NORMALIZATION_AUDIO_BITRATE,
            "-ac",
            "2",
            "-ar",
            cls.SOURCE_NORMALIZATION_AUDIO_RATE,
            *cls._build_selected_audio_metadata_args(probe),
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    @classmethod
    def _build_audio_args(cls, *, has_audio: bool) -> list[str]:
        if not has_audio:
            return []
        return [
            "-c:a",
            "aac",
            "-b:a",
            cls.SOURCE_NORMALIZATION_AUDIO_BITRATE,
            "-ac",
            "2",
            "-ar",
            cls.SOURCE_NORMALIZATION_AUDIO_RATE,
        ]

    @classmethod
    def _build_gpu_h264_aac_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        source_codec: str | None,
        probe: SourceMediaProbe,
    ) -> list[str]:
        return cls._build_gpu_h264_base_cmd(
            source_path,
            source_codec=source_codec,
        ) + cls._build_selected_audio_map_args(probe) + [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-dn",
            "-sn",
            "-write_tmcd",
            "0",
        ] + cls._build_audio_args(has_audio=probe.has_audio) + cls._build_selected_audio_metadata_args(probe) + [str(output_path)]

    @classmethod
    def _build_cpu_h264_aac_cmd(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        probe: SourceMediaProbe,
    ) -> list[str]:
        return cls._build_cpu_h264_base_cmd(source_path) + cls._build_selected_audio_map_args(
            probe
        ) + [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-dn",
            "-sn",
            "-write_tmcd",
            "0",
        ] + cls._build_audio_args(has_audio=probe.has_audio) + cls._build_selected_audio_metadata_args(probe) + [str(output_path)]

    @classmethod
    def _is_valid_prepared_library_probe(
        cls,
        prepared_probe: SourceMediaProbe | None,
        *,
        reference_probe: SourceMediaProbe | None = None,
        require_h264_video: bool = False,
    ) -> bool:
        if prepared_probe is None:
            return False
        if prepared_probe.container_suffix != ".mp4":
            return False
        video_codec = (prepared_probe.video_codec or "").strip().lower()
        if video_codec not in cls.PREMIERE_NATIVE_VIDEO_CODECS:
            return False
        if require_h264_video and video_codec != "h264":
            return False
        if prepared_probe.duration is None or prepared_probe.duration <= 0:
            return False
        if reference_probe is None:
            return True
        if prepared_probe.has_audio != reference_probe.has_audio:
            return False
        if reference_probe.has_audio and len(prepared_probe.audio_streams) != len(reference_probe.audio_streams):
            return False
        return True

    @staticmethod
    def _format_media_failure(result: object) -> str:
        if isinstance(result, CommandTimeoutError):
            return str(result)
        if isinstance(result, FileNotFoundError):
            return str(result)
        if not hasattr(result, "stderr") or not hasattr(result, "stdout"):
            return str(result)
        stderr = getattr(result, "stderr", b"") or b""
        stdout = getattr(result, "stdout", b"") or b""
        detail = (
            stderr.decode("utf-8", errors="replace").strip()
            or stdout.decode("utf-8", errors="replace").strip()
            or "unknown ffmpeg error"
        )
        lines = [
            line.strip()
            for line in detail.replace("\r", "\n").splitlines()
            if line.strip()
        ]
        noisy_prefixes = (
            "ffmpeg version",
            "built with",
            "configuration:",
            "libav",
            "Input #",
            "Metadata:",
            "Duration:",
            "Stream #",
            "Press [q] to stop",
            "frame=",
            "size=",
        )
        filtered_lines = [
            line for line in lines
            if not any(line.startswith(prefix) for prefix in noisy_prefixes)
        ]
        candidate_lines = filtered_lines or lines
        meaningful_markers = (
            "only supported",
            "not supported",
            "not currently supported",
            "could not write header",
            "incorrect codec parameters",
            "unknown encoder",
            "error opening output files",
            "invalid data found",
        )
        preferred_lines = [
            line for line in candidate_lines
            if any(marker in line.lower() for marker in meaningful_markers)
        ]
        if preferred_lines:
            selected_lines = preferred_lines[-2:]
            for line in candidate_lines[-3:]:
                lowered = line.lower()
                if (
                    line not in selected_lines
                    and (
                        "nothing was written into output file" in lowered
                        or "conversion failed" in lowered
                    )
                ):
                    selected_lines.append(line)
            return " | ".join(selected_lines)[:500]
        return " | ".join(candidate_lines[-3:])[:500]

    @classmethod
    async def _run_normalization_command(
        cls,
        cmd: list[str],
        *,
        timeout_seconds: float,
    ):
        return await run_command(cmd, timeout_seconds=timeout_seconds)

    @classmethod
    async def _run_library_import_h264_transcode(
        cls,
        source_path: Path,
        output_path: Path,
        *,
        source_codec: str | None,
        probe: SourceMediaProbe,
    ) -> None:
        gpu_error: str | None = None
        try:
            gpu_result = await run_command(
                rewrite_media_command(
                    cls._build_gpu_library_import_h264_cmd(
                        source_path,
                        output_path,
                        source_codec=source_codec,
                        probe=probe,
                    )
                ),
                timeout_seconds=cls.LIBRARY_IMPORT_TRANSCODE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            gpu_error = cls._format_media_failure(exc)
        except CommandTimeoutError as exc:
            gpu_error = cls._format_media_failure(exc)
        else:
            if gpu_result.returncode == 0:
                gpu_probe = await asyncio.to_thread(cls._probe_media_sync, output_path)
                if cls._is_valid_prepared_library_probe(
                    gpu_probe,
                    reference_probe=probe,
                    require_h264_video=True,
                ):
                    return
                with suppress(OSError):
                    await asyncio.to_thread(output_path.unlink)
                gpu_error = f"Transcoded H.264 MP4 output failed validation: {source_path.name}"
            else:
                gpu_error = cls._format_media_failure(gpu_result)

        try:
            cpu_result = await run_command(
                rewrite_media_command(
                    cls._build_cpu_library_import_h264_cmd(
                        source_path,
                        output_path,
                        probe=probe,
                    )
                ),
                timeout_seconds=cls.LIBRARY_IMPORT_TRANSCODE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
            raise RuntimeError(
                "Failed to transcode source to H.264 MP4 "
                f"(GPU: {gpu_error}; CPU: {cls._format_media_failure(exc)})"
            ) from exc
        except CommandTimeoutError as exc:
            raise RuntimeError(
                "Failed to transcode source to H.264 MP4 "
                f"(GPU: {gpu_error}; CPU: {cls._format_media_failure(exc)})"
            ) from exc

        if cpu_result.returncode != 0:
            raise RuntimeError(
                "Failed to transcode source to H.264 MP4 "
                f"(GPU: {gpu_error}; CPU: {cls._format_media_failure(cpu_result)})"
            )

    @classmethod
    def get_subtitle_sidecar_asset_path(
        cls,
        source_path: Path,
        entry: SubtitleSidecarEntry,
    ) -> Path | None:
        if not entry.asset_filename:
            return None
        resolved_source_path = cls.resolve_subtitle_sidecar_source_path(source_path) or source_path
        return cls.get_subtitle_sidecar_dir(resolved_source_path) / entry.asset_filename

    @classmethod
    def get_subtitle_sidecar_cue_manifest_path(
        cls,
        source_path: Path,
        entry: SubtitleSidecarEntry,
    ) -> Path | None:
        if not entry.cue_manifest_filename:
            return None
        resolved_source_path = cls.resolve_subtitle_sidecar_source_path(source_path) or source_path
        return cls.get_subtitle_sidecar_dir(resolved_source_path) / entry.cue_manifest_filename

    @classmethod
    def load_subtitle_sidecar_manifest(
        cls,
        source_path: Path,
    ) -> dict[str, Any] | None:
        resolved_source_path = cls.resolve_subtitle_sidecar_source_path(source_path) or source_path
        manifest_path = cls.get_subtitle_sidecar_manifest_path(resolved_source_path)
        if not manifest_path.exists():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def get_subtitle_sidecar_generated_from_path(
        cls,
        source_path: Path,
    ) -> Path | None:
        payload = cls.load_subtitle_sidecar_manifest(source_path)
        if not payload:
            return None
        generated_from_raw = str(payload.get("generated_from", "")).strip()
        if not generated_from_raw:
            return None
        return Path(generated_from_raw)

    @staticmethod
    def _cue_overlaps_any_window(
        cue_start: float,
        cue_end: float,
        windows: list[tuple[float, float]],
    ) -> bool:
        return any(cue_end > window_start and cue_start < window_end for window_start, window_end in windows)

    @staticmethod
    def _sidecar_cue_asset_relative_path(
        entry: SubtitleSidecarEntry,
        cue_index: int,
    ) -> str:
        asset_stem = Path(entry.asset_filename or "subtitle_stream").stem
        return f"{asset_stem}_cues/cue_{cue_index:04d}.png"

    @classmethod
    def _write_sidecar_cue_manifest(
        cls,
        cue_manifest_path: Path,
        cues: list[dict[str, Any]],
    ) -> None:
        cue_manifest_path.write_text(
            json.dumps({"cues": cues}, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def _update_sidecar_cue_manifest_asset(
        cls,
        cue_manifest_path: Path,
        *,
        cue_index: int,
        asset_filename: str,
    ) -> None:
        if not cue_manifest_path.exists():
            return
        try:
            payload = json.loads(cue_manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        raw_cues = payload.get("cues", []) if isinstance(payload, dict) else []
        if not isinstance(raw_cues, list):
            return

        updated = False
        for idx, cue in enumerate(raw_cues, start=1):
            if not isinstance(cue, dict):
                continue
            try:
                entry_index = int(cue.get("cue_index", idx))
            except (TypeError, ValueError):
                entry_index = idx
            if entry_index != cue_index:
                continue
            cue["asset_filename"] = asset_filename
            cue["cue_index"] = cue_index
            updated = True
            break

        if updated:
            cls._write_sidecar_cue_manifest(cue_manifest_path, raw_cues)

    @classmethod
    def load_subtitle_sidecar_entries(
        cls,
        source_path: Path,
    ) -> list[SubtitleSidecarEntry]:
        payload = cls.load_subtitle_sidecar_manifest(source_path)
        if not payload:
            return []

        raw_entries = payload.get("subtitle_streams", [])
        if not isinstance(raw_entries, list):
            return []

        entries: list[SubtitleSidecarEntry] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            raw_language = str(raw_entry.get("raw_language", "")).strip() or None
            title = str(raw_entry.get("title", "")).strip() or None
            handler_name = str(raw_entry.get("handler_name", "")).strip() or None
            stored_language = str(raw_entry.get("language", "")).strip() or None
            try:
                entries.append(
                    SubtitleSidecarEntry(
                        stream_index=int(raw_entry.get("stream_index")),
                        stream_position=int(raw_entry.get("stream_position")),
                        codec_name=str(raw_entry.get("codec_name", "")).strip().lower() or None,
                        language=cls.normalize_stream_language(
                            stored_language or raw_language,
                            title=title,
                            handler_name=handler_name,
                        ),
                        raw_language=raw_language,
                        title=title,
                        handler_name=handler_name,
                        kind=str(raw_entry.get("kind", "")).strip().lower() or "unsupported",
                        asset_filename=str(raw_entry.get("asset_filename", "")).strip() or None,
                        cue_manifest_filename=(
                            str(raw_entry.get("cue_manifest_filename", "")).strip() or None
                        ),
                        status=str(raw_entry.get("status", "")).strip().lower() or "ok",
                        error=str(raw_entry.get("error", "")).strip() or None,
                    )
                )
            except (TypeError, ValueError):
                continue
        return entries

    @staticmethod
    def _subtitle_entry_title_rank(entry: SubtitleSidecarEntry) -> int:
        title = str(entry.title or "").lower()
        if any(token in title for token in ("sign", "song", "forced")):
            return 2
        if any(token in title for token in ("sdh", "closed caption", "closed-caption", "cc")):
            return 1
        if any(token in title for token in ("dialogue", "dialog", "full", "default", "plain")):
            return 0
        return 0

    @classmethod
    def get_preferred_subtitle_language_groups(
        cls,
        entries: list[SubtitleSidecarEntry],
        *,
        target_language: str | None,
    ) -> list[tuple[str | None, list[SubtitleSidecarEntry]]]:
        usable_entries = [
            entry
            for entry in entries
            if entry.status == "ok"
            and entry.kind in {"text", "image"}
            and entry.asset_filename
        ]
        if not usable_entries:
            return []

        groups: dict[str | None, list[SubtitleSidecarEntry]] = {}
        first_position_by_language: dict[str | None, int] = {}
        for entry in usable_entries:
            groups.setdefault(entry.language, []).append(entry)
            first_position_by_language.setdefault(entry.language, entry.stream_position)

        normalized_target = cls.normalize_stream_language(target_language)
        ordered_languages: list[str | None] = []
        seen_languages: set[str | None] = set()
        for preferred_language in (normalized_target, "en"):
            if preferred_language not in groups or preferred_language in seen_languages:
                continue
            ordered_languages.append(preferred_language)
            seen_languages.add(preferred_language)

        fallback_languages = sorted(
            [
                language
                for language in groups
                if language not in seen_languages
            ],
            key=lambda language: (
                first_position_by_language.get(language, 10**9),
                language or "zzzz",
            ),
        )
        ordered_languages.extend(fallback_languages)

        return [
            (
                language,
                sorted(
                    groups[language],
                    key=lambda entry: (
                        0 if entry.kind == "text" else 1,
                        cls._subtitle_entry_title_rank(entry),
                        entry.stream_position,
                    ),
                ),
            )
            for language in ordered_languages
        ]

    @classmethod
    def select_preferred_subtitle_entry(
        cls,
        entries: list[SubtitleSidecarEntry],
        *,
        target_language: str | None,
    ) -> SubtitleSidecarEntry | None:
        language_groups = cls.get_preferred_subtitle_language_groups(
            entries,
            target_language=target_language,
        )
        if not language_groups:
            return None
        candidates = language_groups[0][1]

        def _rank(entry: SubtitleSidecarEntry) -> tuple[int, int, int, int]:
            kind_rank = 0 if entry.kind == "text" else 1
            return (
                kind_rank,
                cls._subtitle_entry_title_rank(entry),
                entry.stream_position,
                entry.stream_index,
            )

        return min(candidates, key=_rank)

    @classmethod
    async def _probe_pgs_cues_from_sup(
        cls,
        sup_path: Path,
    ) -> list[dict[str, float]]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_frames",
            "-select_streams",
            "s:0",
            "-show_entries",
            "frame=pts_time,num_rects",
            "-of",
            "json",
            str(sup_path),
        ]
        try:
            result = await cls._run_normalization_command(
                cmd,
                timeout_seconds=cls.FFPROBE_TIMEOUT_SECONDS,
            )
        except (CommandTimeoutError, FileNotFoundError):
            return []
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except json.JSONDecodeError:
            return []

        frames = payload.get("frames", [])
        if not isinstance(frames, list):
            return []

        cues: list[dict[str, float]] = []
        current_start: float | None = None
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            pts_raw = str(frame.get("pts_time", "")).strip()
            if not pts_raw:
                continue
            try:
                pts = float(pts_raw)
            except ValueError:
                continue
            try:
                num_rects = int(frame.get("num_rects", 0))
            except (TypeError, ValueError):
                num_rects = 0
            if num_rects > 0:
                if current_start is not None and pts > current_start:
                    cues.append({"start": current_start, "end": pts})
                current_start = pts
            elif current_start is not None and pts > current_start:
                cues.append({"start": current_start, "end": pts})
                current_start = None

        return cues

    @classmethod
    async def _render_pgs_cue_png_from_source(
        cls,
        *,
        source_path: Path,
        stream_position: int,
        cue_start: float,
        cue_end: float,
        output_path: Path,
    ) -> bool:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cue_duration = max(cue_end - cue_start, 0.0)
        if cue_duration <= 0:
            return False

        frame_sample_padding = 1.0 / 60.0
        window_padding = min(max(cue_duration * 0.2, 0.15), 0.75)
        window_start = max(cue_start - window_padding, 0.0)
        window_end = cue_end + window_padding
        window_duration = max(window_end - window_start, cue_duration + 0.3)

        for sample_ratio in (0.30, 0.55, 0.80):
            local_sample = (cue_start - window_start) + cue_duration * sample_ratio
            local_sample = min(
                max(local_sample, 0.0),
                max(window_duration - frame_sample_padding, 0.0),
            )
            render_cmd = [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                (
                    "color=color=black@0.0:size=1920x1080:"
                    f"rate=60:duration={window_duration:.3f},format=rgba"
                ),
                "-ss",
                f"{window_start:.3f}",
                "-t",
                f"{window_duration:.3f}",
                "-i",
                str(source_path),
                "-filter_complex",
                f"[0:v][1:s:{stream_position}]overlay=format=auto,format=rgba",
                "-ss",
                f"{local_sample:.3f}",
                "-frames:v",
                "1",
                str(output_path),
            ]
            try:
                render_result = await cls._run_normalization_command(
                    render_cmd,
                    timeout_seconds=cls.SUBTITLE_EXTRACTION_TIMEOUT_SECONDS,
                )
            except (CommandTimeoutError, FileNotFoundError):
                continue
            if render_result.returncode != 0 or not output_path.exists():
                continue
            if cls._png_has_visible_alpha(output_path):
                return True
            with suppress(FileNotFoundError):
                output_path.unlink()

        return False

    @classmethod
    async def ensure_subtitle_sidecar_cue_asset(
        cls,
        source_path: Path,
        entry: SubtitleSidecarEntry,
        *,
        cue_index: int,
        cue_start: float,
        cue_end: float,
    ) -> Path | None:
        sidecar_asset_path = cls.get_subtitle_sidecar_asset_path(source_path, entry)
        cue_manifest_path = cls.get_subtitle_sidecar_cue_manifest_path(source_path, entry)
        if sidecar_asset_path is None or cue_manifest_path is None:
            return None

        cue_relative_path = cls._sidecar_cue_asset_relative_path(entry, cue_index)
        cue_asset_path = cue_manifest_path.parent / cue_relative_path
        if cue_asset_path.exists():
            if cls._png_has_visible_alpha(cue_asset_path):
                cls._update_sidecar_cue_manifest_asset(
                    cue_manifest_path,
                    cue_index=cue_index,
                    asset_filename=cue_relative_path,
                )
                return cue_asset_path
            with suppress(FileNotFoundError):
                cue_asset_path.unlink()

        generated_from_path = cls.get_subtitle_sidecar_generated_from_path(source_path)
        if generated_from_path is None or not generated_from_path.exists():
            return None

        rendered = await cls._render_pgs_cue_png_from_source(
            source_path=generated_from_path,
            stream_position=entry.stream_position,
            cue_start=cue_start,
            cue_end=cue_end,
            output_path=cue_asset_path,
        )
        if not rendered or not cue_asset_path.exists():
            return None

        cls._update_sidecar_cue_manifest_asset(
            cue_manifest_path,
            cue_index=cue_index,
            asset_filename=cue_relative_path,
        )
        return cue_asset_path

    @staticmethod
    def _png_has_visible_alpha(path: Path) -> bool:
        try:
            with Image.open(path) as image:
                alpha = image.convert("RGBA").getchannel("A")
                return alpha.getbbox() is not None
        except Exception:
            return False

    @classmethod
    async def _write_subtitle_sidecar(
        cls,
        *,
        source_path: Path,
        normalized_target_path: Path,
        probe: SourceMediaProbe,
        subtitle_image_render_windows: dict[int, list[tuple[float, float]]] | None = None,
    ) -> None:
        if not probe.subtitle_streams:
            return

        sidecar_dir = cls.get_subtitle_sidecar_dir(normalized_target_path)
        tmp_dir = sidecar_dir.with_name(f"{sidecar_dir.name}.tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        manifest_entries: list[dict[str, Any]] = []
        render_windows = subtitle_image_render_windows or {}
        for stream in probe.subtitle_streams:
            kind = cls._subtitle_kind_for_codec(stream.codec_name)
            entry_payload: dict[str, Any] = {
                "stream_index": stream.index,
                "stream_position": stream.stream_position,
                "codec_name": stream.codec_name,
                "language": stream.language,
                "raw_language": stream.raw_language,
                "title": stream.title,
                "handler_name": stream.handler_name,
                "kind": kind,
                "asset_filename": None,
                "cue_manifest_filename": None,
                "status": "ok",
                "error": None,
            }
            try:
                if kind == "text":
                    asset_name = cls._subtitle_asset_basename(stream, extension=".srt")
                    asset_path = tmp_dir / asset_name
                    result = await cls._run_normalization_command(
                        [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-i",
                            str(source_path),
                            "-map",
                            f"0:s:{stream.stream_position}",
                            "-c:s",
                            "srt",
                            str(asset_path),
                        ],
                        timeout_seconds=cls.SUBTITLE_EXTRACTION_TIMEOUT_SECONDS,
                    )
                    if result.returncode != 0 or not asset_path.exists():
                        raise RuntimeError(cls._format_media_failure(result))
                    entry_payload["asset_filename"] = asset_name
                elif kind == "image":
                    asset_name = cls._subtitle_asset_basename(stream, extension=".sup")
                    asset_path = tmp_dir / asset_name
                    result = await cls._run_normalization_command(
                        [
                            "ffmpeg",
                            "-y",
                            "-v",
                            "error",
                            "-i",
                            str(source_path),
                            "-map",
                            f"0:s:{stream.stream_position}",
                            "-c:s",
                            "copy",
                            str(asset_path),
                        ],
                        timeout_seconds=cls.SUBTITLE_EXTRACTION_TIMEOUT_SECONDS,
                    )
                    if result.returncode != 0 or not asset_path.exists():
                        raise RuntimeError(cls._format_media_failure(result))
                    entry_payload["asset_filename"] = asset_name

                    cues = await cls._probe_pgs_cues_from_sup(asset_path)
                    if cues:
                        cue_manifest_name = asset_path.stem + ".cues.json"
                        cue_manifest_entries: list[dict[str, Any]] = []
                        requested_windows = render_windows.get(stream.stream_position, [])
                        for cue_idx, cue in enumerate(cues, start=1):
                            cue_start = float(cue["start"])
                            cue_end = float(cue["end"])
                            cue_entry: dict[str, Any] = {
                                "cue_index": cue_idx,
                                "start": cue_start,
                                "end": cue_end,
                            }
                            if requested_windows and cls._cue_overlaps_any_window(
                                cue_start,
                                cue_end,
                                requested_windows,
                            ):
                                cue_relative_path = cls._sidecar_cue_asset_relative_path(
                                    SubtitleSidecarEntry(
                                        stream_index=stream.index,
                                        stream_position=stream.stream_position,
                                        codec_name=stream.codec_name,
                                        language=stream.language,
                                        raw_language=stream.raw_language,
                                        title=stream.title,
                                        handler_name=stream.handler_name,
                                        kind=kind,
                                        asset_filename=asset_name,
                                    ),
                                    cue_idx,
                                )
                                cue_png_path = tmp_dir / cue_relative_path
                                rendered = await cls._render_pgs_cue_png_from_source(
                                    source_path=source_path,
                                    stream_position=stream.stream_position,
                                    cue_start=cue_start,
                                    cue_end=cue_end,
                                    output_path=cue_png_path,
                                )
                                if rendered:
                                    cue_entry["asset_filename"] = cue_relative_path
                                elif cue_png_path.exists():
                                    with suppress(FileNotFoundError):
                                        cue_png_path.unlink()
                            cue_manifest_entries.append(cue_entry)
                        if cue_manifest_entries:
                            cue_manifest_path = tmp_dir / cue_manifest_name
                            cls._write_sidecar_cue_manifest(
                                cue_manifest_path,
                                cue_manifest_entries,
                            )
                            entry_payload["cue_manifest_filename"] = cue_manifest_name
                else:
                    entry_payload["status"] = "unsupported"
            except Exception as exc:
                entry_payload["status"] = "error"
                entry_payload["error"] = str(exc)
            manifest_entries.append(entry_payload)

        manifest_path = tmp_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "source_path": str(normalized_target_path),
                    "generated_from": str(source_path),
                    "subtitle_streams": manifest_entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        if sidecar_dir.exists():
            shutil.rmtree(sidecar_dir, ignore_errors=True)
        tmp_dir.replace(sidecar_dir)

    @classmethod
    async def backfill_subtitle_sidecar(
        cls,
        *,
        normalized_target_path: Path,
        original_source_path: Path,
        subtitle_image_render_windows: dict[int, list[tuple[float, float]]] | None = None,
    ) -> None:
        if not normalized_target_path.exists():
            raise FileNotFoundError(f"Normalized source not found: {normalized_target_path}")
        if not original_source_path.exists():
            raise FileNotFoundError(f"Original source not found: {original_source_path}")
        probe = await asyncio.to_thread(cls._probe_media_sync, original_source_path)
        if probe is None:
            raise RuntimeError(f"Unable to probe source media: {original_source_path}")
        await cls._write_subtitle_sidecar(
            source_path=original_source_path,
            normalized_target_path=normalized_target_path,
            probe=probe,
            subtitle_image_render_windows=subtitle_image_render_windows,
        )

    @classmethod
    def _clone_subtitle_sidecar_sync(
        cls,
        source_path: Path,
        target_path: Path,
    ) -> bool:
        sidecar_source_path = cls.resolve_subtitle_sidecar_source_path(source_path)
        if sidecar_source_path is None:
            return False

        source_dir = cls.get_subtitle_sidecar_dir(sidecar_source_path)
        manifest_path = source_dir / "manifest.json"
        if not source_dir.exists() or not manifest_path.exists():
            return False

        target_dir = cls.get_subtitle_sidecar_dir(target_path)
        tmp_dir = target_dir.with_name(f"{target_dir.name}.tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.copytree(source_dir, tmp_dir)

        try:
            payload = json.loads((tmp_dir / "manifest.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        if isinstance(payload, dict):
            payload["source_path"] = str(target_path)
            (tmp_dir / "manifest.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )

        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        tmp_dir.replace(target_dir)
        return True

    @classmethod
    def _library_context_for_path(
        cls,
        source_path: Path,
    ) -> tuple[LibraryType, str] | None:
        resolved_source = source_path.resolve()
        for library_type in LibraryType:
            library_path = cls.get_library_path(library_type)
            try:
                rel = resolved_source.relative_to(library_path.resolve())
            except ValueError:
                continue
            parts = rel.parts
            if len(parts) < 2:
                return None
            return library_type, parts[0]
        return None

    @classmethod
    def _series_name_for_library_path(cls, source_path: Path) -> str | None:
        context = cls._library_context_for_path(source_path)
        if context is None:
            return None
        return context[1]

    @classmethod
    async def _postprocess_source_normalization_commit(cls, normalized_path: Path) -> None:
        context = cls._library_context_for_path(normalized_path)
        if context is None:
            return
        library_type, series_name = context
        await cls.ensure_episode_manifest(force_refresh=True, library_type=library_type)
        from .anime_matcher import AnimeMatcherService

        AnimeMatcherService.mark_series_updated(library_type, series_name)

    @classmethod
    async def normalize_source_for_processing(
        cls,
        source_path: Path,
        *,
        preferred_audio_language: str | None = None,
        subtitle_image_render_windows: dict[int, list[tuple[float, float]]] | None = None,
        project_id: str | None = None,
    ) -> SourceNormalizationResult:
        """Normalize one source episode for Premiere Pro import.

        Since format normalization now happens at indexation time, this step
        typically only remuxes the selected audio track. The full H.264 MP4
        transcode path remains as the fallback when a source still is not
        library-normalized.
        """
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        target_path = await asyncio.to_thread(
            cls._processing_normalized_target_path,
            source_path,
            project_id=project_id,
        )
        source_import_payload = await asyncio.to_thread(
            cls._load_source_import_manifest_sync,
            source_path,
        )
        plan = await asyncio.to_thread(
            cls._build_source_normalization_plan_sync,
            source_path,
            preferred_audio_language=preferred_audio_language,
            target_path=target_path,
        )
        reimport_plan = await asyncio.to_thread(
            cls._build_processing_reimport_plan_sync,
            source_path,
            preferred_audio_language=preferred_audio_language,
            target_path=target_path,
        )
        if reimport_plan is not None:
            plan = reimport_plan
        elif plan.action == "noop":
            return SourceNormalizationResult(
                action=plan.action,
                source_path=plan.source_path,
                normalized_path=plan.source_path,
                changed=False,
            )

        target_path = plan.target_path
        tmp_suffix = target_path.suffix or ".mp4"
        tmp_path = target_path.with_name(f"{target_path.stem}.normalize.tmp{tmp_suffix}")
        target_path.parent.mkdir(parents=True, exist_ok=True)

        manifest_original_source_raw = (
            str(source_import_payload.get("original_source_path", "")).strip()
            if isinstance(source_import_payload, dict)
            else ""
        )
        if not manifest_original_source_raw and isinstance(source_import_payload, dict):
            manifest_original_source_raw = str(source_import_payload.get("source_path", "")).strip()
        manifest_original_source_path = (
            Path(manifest_original_source_raw)
            if manifest_original_source_raw
            else plan.source_path
        )
        sidecar_source_path = (
            cls.resolve_subtitle_sidecar_source_path(source_path)
            or cls.resolve_subtitle_sidecar_source_path(plan.source_path)
            or source_path
        )

        async def _preserve_target_metadata() -> None:
            recorded_probe = plan.probe
            if (
                manifest_original_source_path != plan.source_path
                and manifest_original_source_path.exists()
            ):
                manifest_probe = await asyncio.to_thread(
                    cls._probe_media_sync,
                    manifest_original_source_path,
                )
                if manifest_probe is not None:
                    recorded_probe = cls._with_preferred_audio_stream(
                        manifest_probe,
                        target_language=preferred_audio_language,
                        source_import_payload=source_import_payload,
                    )
            await asyncio.to_thread(
                cls._record_source_import_manifest_sync,
                source_path,
                target_path,
                source_probe=recorded_probe,
                original_source_path=manifest_original_source_path,
                sidecar_source_path=sidecar_source_path,
            )

        async def _preserve_target_sidecar() -> None:
            try:
                if plan.probe.subtitle_streams:
                    await cls._write_subtitle_sidecar(
                        source_path=plan.source_path,
                        normalized_target_path=target_path,
                        probe=plan.probe,
                        subtitle_image_render_windows=subtitle_image_render_windows,
                    )
                    return
                cloned = await asyncio.to_thread(
                    cls._clone_subtitle_sidecar_sync,
                    sidecar_source_path,
                    target_path,
                )
                if not cloned:
                    logger.debug(
                        "No subtitle sidecar available to clone for %s -> %s",
                        sidecar_source_path,
                        target_path,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to preserve subtitle sidecar for %s: %s",
                    plan.source_path,
                    exc,
                )

        if target_path != source_path and target_path.exists():
            existing_probe = await asyncio.to_thread(cls._probe_media_sync, target_path)
            if cls._is_valid_normalized_probe(existing_probe, reference_probe=plan.probe):
                await _preserve_target_sidecar()
                await _preserve_target_metadata()
                if plan.cleanup_source_after_commit and source_path.exists():
                    await asyncio.to_thread(source_path.unlink)
                await cls._postprocess_source_normalization_commit(target_path)
                return SourceNormalizationResult(
                    action=plan.action,
                    source_path=source_path,
                    normalized_path=target_path,
                    changed=True,
                )
            await asyncio.to_thread(target_path.unlink)

        if tmp_path.exists():
            await asyncio.to_thread(tmp_path.unlink)

        try:
            if plan.action == "remux_to_mp4":
                try:
                    result = await cls._run_normalization_command(
                        cls._build_remux_to_mp4_cmd(
                            plan.source_path,
                            tmp_path,
                            probe=plan.probe,
                        ),
                        timeout_seconds=cls.REMUX_TIMEOUT_SECONDS,
                    )
                except (CommandTimeoutError, FileNotFoundError) as exc:
                    raise RuntimeError(
                        f"Failed to remux source for Premiere: {cls._format_media_failure(exc)}"
                    ) from exc
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Failed to remux source for Premiere: {cls._format_media_failure(result)}"
                    )
            elif plan.action in {"audio_to_aac", "remux_audio_select"}:
                try:
                    cmd = (
                        cls._build_remux_audio_select_cmd(
                            plan.source_path,
                            tmp_path,
                            probe=plan.probe,
                        )
                        if plan.action == "remux_audio_select"
                        else cls._build_audio_to_aac_cmd(
                            plan.source_path,
                            tmp_path,
                            probe=plan.probe,
                        )
                    )
                    result = await cls._run_normalization_command(
                        cmd,
                        timeout_seconds=cls.SOURCE_NORMALIZATION_TIMEOUT_SECONDS,
                    )
                except (CommandTimeoutError, FileNotFoundError) as exc:
                    raise RuntimeError(
                        "Failed to normalize source audio for Premiere: "
                        f"{cls._format_media_failure(exc)}"
                    ) from exc
                if result.returncode != 0:
                    raise RuntimeError(
                        "Failed to normalize source audio for Premiere: "
                        f"{cls._format_media_failure(result)}"
                    )
            elif plan.action == "full_h264_aac_transcode":
                gpu_error: str | None = None
                try:
                    gpu_result = await cls._run_normalization_command(
                        cls._build_gpu_h264_aac_cmd(
                            plan.source_path,
                            tmp_path,
                            source_codec=plan.probe.video_codec,
                            probe=plan.probe,
                        ),
                        timeout_seconds=cls.SOURCE_NORMALIZATION_TIMEOUT_SECONDS,
                    )
                except (CommandTimeoutError, FileNotFoundError) as exc:
                    gpu_error = cls._format_media_failure(exc)
                else:
                    if gpu_result.returncode == 0:
                        gpu_probe = await asyncio.to_thread(cls._probe_media_sync, tmp_path)
                        if cls._is_valid_normalized_probe(gpu_probe, reference_probe=plan.probe):
                            gpu_error = None
                        else:
                            gpu_error = f"Normalized output failed validation: {tmp_path.name}"
                    else:
                        gpu_error = cls._format_media_failure(gpu_result)

                if gpu_error is not None:
                    if tmp_path.exists():
                        await asyncio.to_thread(tmp_path.unlink)
                    try:
                        cpu_result = await cls._run_normalization_command(
                            cls._build_cpu_h264_aac_cmd(
                                plan.source_path,
                                tmp_path,
                                probe=plan.probe,
                            ),
                            timeout_seconds=cls.SOURCE_NORMALIZATION_TIMEOUT_SECONDS,
                        )
                    except (CommandTimeoutError, FileNotFoundError) as exc:
                        raise RuntimeError(
                            "Failed to transcode source for Premiere "
                            f"(GPU: {gpu_error}; CPU: {cls._format_media_failure(exc)})"
                        ) from exc
                    if cpu_result.returncode != 0:
                        raise RuntimeError(
                            "Failed to transcode source for Premiere "
                            f"(GPU: {gpu_error}; CPU: {cls._format_media_failure(cpu_result)})"
                        )
            else:
                raise RuntimeError(f"Unsupported normalization action: {plan.action}")

            normalized_probe = await asyncio.to_thread(cls._probe_media_sync, tmp_path)
            if not cls._is_valid_normalized_probe(normalized_probe, reference_probe=plan.probe):
                raise RuntimeError(f"Normalized output failed validation: {tmp_path.name}")

            await asyncio.to_thread(tmp_path.replace, target_path)
            await _preserve_target_sidecar()
            await _preserve_target_metadata()
            if plan.cleanup_source_after_commit and source_path.exists():
                await asyncio.to_thread(source_path.unlink)

            await cls._postprocess_source_normalization_commit(target_path)
            return SourceNormalizationResult(
                action=plan.action,
                source_path=source_path,
                normalized_path=target_path,
                changed=True,
            )
        except Exception:
            if tmp_path.exists():
                await asyncio.to_thread(tmp_path.unlink)
            raise

    @classmethod
    def get_preview_proxy_dir(cls) -> Path:
        """Directory holding browser-safe source preview proxies."""
        return settings.cache_dir / "source_previews"

    @classmethod
    def _build_preview_proxy_key(cls, source_path: Path) -> str:
        """Build a stable proxy key that changes when source content changes."""
        stat = source_path.stat()
        payload = f"{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @classmethod
    def get_preview_proxy_path(cls, source_path: Path) -> Path:
        """Compute the expected proxy path for a source file."""
        key = cls._build_preview_proxy_key(source_path)
        return cls.get_preview_proxy_dir() / f"{key}.mp4"

    @staticmethod
    def _probe_video_stream_sync(video_path: Path) -> dict | None:
        """Return ffprobe stream info for the first video stream."""
        cmd = rewrite_media_command(
            [
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
        )
        try:
            env = get_media_subprocess_env(cmd)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
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
    def _probe_video_duration_sync(video_path: Path) -> float | None:
        """Return video duration in seconds when ffprobe can parse the container."""
        cmd = rewrite_media_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
        )
        try:
            env = get_media_subprocess_env(cmd)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
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
    def _is_valid_preview_proxy_sync(cls, proxy_path: Path) -> bool:
        """Validate that a preview proxy is browser-safe and structurally readable."""
        if not proxy_path.exists() or proxy_path.stat().st_size <= 0:
            return False
        stream = cls._probe_video_stream_sync(proxy_path)
        if stream is None:
            return False
        codec = str(stream.get("codec_name", "")).strip().lower()
        pix_fmt = str(stream.get("pix_fmt", "")).strip().lower()
        duration = cls._probe_video_duration_sync(proxy_path)
        return (
            codec == "h264"
            and pix_fmt in {"yuv420p", "yuvj420p"}
            and duration is not None
        )

    @classmethod
    def get_primary_video_codec_sync(cls, video_path: Path) -> str | None:
        """Return normalized codec name for the first video stream."""
        stream = cls._probe_video_stream_sync(video_path)
        if stream is None:
            return None
        codec = str(stream.get("codec_name", "")).strip().lower()
        return codec or None

    @classmethod
    def _build_gpu_h264_base_cmd(
        cls,
        source_path: Path,
        *,
        source_codec: str | None = None,
    ) -> list[str]:
        """Build a strict GPU-only ffmpeg command for H.264 MP4 output."""
        codec = (source_codec or "").strip().lower()
        cmd = [
            "ffmpeg",
            "-y",
            "-hwaccel",
            cls.GPU_HWACCEL,
            "-hwaccel_output_format",
            cls.GPU_HWACCEL,
        ]
        # Force hardware decoder when known so conversion stays GPU-only.
        if codec == "av1":
            cmd.extend(["-c:v", "av1_cuvid"])
        elif codec == "h264":
            cmd.extend(["-c:v", "h264_cuvid"])
        elif codec == "hevc":
            cmd.extend(["-c:v", "hevc_cuvid"])

        cmd.extend(
            [
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-vf",
                "scale_cuda=format=nv12",
                "-c:v",
                cls.GPU_H264_ENCODER,
                "-preset",
                "p5",
                "-rc",
                "constqp",
                "-qp",
                "23",
                "-b:v",
                "0",
                "-profile:v",
                "high",
                "-movflags",
                "+faststart",
            ]
        )
        return cmd

    @classmethod
    def _build_cpu_h264_base_cmd(cls, source_path: Path) -> list[str]:
        """Build a deterministic CPU ffmpeg command for H.264 MP4 output."""
        return [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]

    @classmethod
    def is_browser_preview_compatible(cls, source_path: Path) -> bool:
        """
        Return True when a source file is safe to play directly in browser preview.

        Conservative rule: MP4 + H.264 + 4:2:0 8-bit.
        """
        if source_path.suffix.lower() != ".mp4":
            return False

        codec = cls.get_primary_video_codec_sync(source_path)
        if codec is None:
            return False

        stream = cls._probe_video_stream_sync(source_path)
        if stream is None:
            return False
        pix_fmt = str(stream.get("pix_fmt", "")).lower()
        return codec == "h264" and pix_fmt in {"yuv420p", "yuvj420p"}

    @classmethod
    def ensure_preview_proxy_sync(cls, source_path: Path) -> Path | None:
        """
        Ensure a browser-safe MP4 preview proxy exists for source_path.

        Returns:
            - original source path when direct playback is compatible
            - proxy path when transcoding is needed/succeeds
            - None when proxy creation fails
        """
        if not source_path.exists() or not source_path.is_file():
            return None

        if cls.is_browser_preview_compatible(source_path):
            return source_path

        preview_dir = cls.get_preview_proxy_dir()
        preview_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = cls.get_preview_proxy_path(source_path)
        tmp_path = proxy_path.with_suffix(".tmp.mp4")
        lock = cls._get_preview_proxy_lock(source_path)
        with lock:
            if proxy_path.exists():
                if cls._is_valid_preview_proxy_sync(proxy_path):
                    return proxy_path
                with suppress(OSError):
                    proxy_path.unlink()

            if tmp_path.exists():
                with suppress(OSError):
                    tmp_path.unlink()

            source_codec = cls.get_primary_video_codec_sync(source_path)
            base_cmd = cls._build_gpu_h264_base_cmd(
                source_path,
                source_codec=source_codec,
            )
            cmd_with_audio_copy = rewrite_media_command(
                base_cmd
                + [
                    "-map",
                    "0:a:0?",
                    "-c:a",
                    "copy",
                    str(tmp_path),
                ]
            )
            cmd_with_audio_aac = rewrite_media_command(
                base_cmd
                + [
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
                    str(tmp_path),
                ]
            )

            try:
                env = get_media_subprocess_env(cmd_with_audio_copy)
                result = subprocess.run(
                    cmd_with_audio_copy,
                    capture_output=True,
                    text=True,
                    timeout=cls.PREVIEW_PROXY_TIMEOUT_SECONDS,
                    check=False,
                    env=env,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                result = None

            if result is None or result.returncode != 0:
                try:
                    env = get_media_subprocess_env(cmd_with_audio_aac)
                    result = subprocess.run(
                        cmd_with_audio_aac,
                        capture_output=True,
                        text=True,
                        timeout=cls.PREVIEW_PROXY_TIMEOUT_SECONDS,
                        check=False,
                        env=env,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    result = None

            # Fallback to deterministic CPU path when GPU pipeline is unavailable.
            if result is None or result.returncode != 0:
                base_cmd = cls._build_cpu_h264_base_cmd(source_path)
                cmd_with_audio_copy = rewrite_media_command(
                    base_cmd
                    + [
                        "-map",
                        "0:a:0?",
                        "-c:a",
                        "copy",
                        str(tmp_path),
                    ]
                )
                cmd_with_audio_aac = rewrite_media_command(
                    base_cmd
                    + [
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
                        str(tmp_path),
                    ]
                )
                try:
                    env = get_media_subprocess_env(cmd_with_audio_copy)
                    result = subprocess.run(
                        cmd_with_audio_copy,
                        capture_output=True,
                        text=True,
                        timeout=cls.PREVIEW_PROXY_TIMEOUT_SECONDS,
                        check=False,
                        env=env,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    return None
                if result.returncode != 0:
                    try:
                        env = get_media_subprocess_env(cmd_with_audio_aac)
                        result = subprocess.run(
                            cmd_with_audio_aac,
                            capture_output=True,
                            text=True,
                            timeout=cls.PREVIEW_PROXY_TIMEOUT_SECONDS,
                            check=False,
                            env=env,
                        )
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        return None
                    if result.returncode != 0:
                        return None

            if not cls._is_valid_preview_proxy_sync(tmp_path):
                with suppress(OSError):
                    tmp_path.unlink()
                return None

            tmp_path.replace(proxy_path)
            if not cls._is_valid_preview_proxy_sync(proxy_path):
                with suppress(OSError):
                    proxy_path.unlink()
                return None
            return proxy_path

    @classmethod
    async def resolve_source_preview_path(
        cls,
        source_path: Path,
        *,
        allow_generate: bool = True,
    ) -> Path:
        """
        Resolve the best path to stream for browser preview.

        Falls back to original source when proxy generation fails.
        """
        if allow_generate:
            resolved = await asyncio.to_thread(cls.ensure_preview_proxy_sync, source_path)
            return resolved if resolved is not None else source_path

        compatible = await asyncio.to_thread(cls.is_browser_preview_compatible, source_path)
        if compatible:
            return source_path

        proxy_path = await asyncio.to_thread(cls.get_preview_proxy_path, source_path)
        proxy_is_valid = await asyncio.to_thread(
            cls._is_valid_preview_proxy_sync,
            proxy_path,
        )
        if proxy_is_valid:
            return proxy_path
        if proxy_path.exists():
            await asyncio.to_thread(
                lambda: proxy_path.unlink(missing_ok=True),
            )
        return source_path

    @classmethod
    async def trigger_preview_proxy_generation(cls, source_path: Path) -> None:
        """Kick off proxy generation once per source path (non-blocking)."""
        key = str(source_path.resolve())
        lock = cls._get_preview_generation_lock()
        async with lock:
            if key in cls._preview_generation_inflight:
                return
            cls._preview_generation_inflight.add(key)

        async def _run() -> None:
            try:
                await asyncio.to_thread(cls.ensure_preview_proxy_sync, source_path)
            finally:
                async with lock:
                    cls._preview_generation_inflight.discard(key)

        asyncio.create_task(_run())

    @classmethod
    async def wait_for_preview_proxy(
        cls,
        source_path: Path,
        *,
        timeout_seconds: float = 1.5,
        poll_interval_seconds: float = 0.15,
    ) -> Path | None:
        """Wait briefly for a generated preview proxy to appear on disk."""
        proxy_path = await asyncio.to_thread(cls.get_preview_proxy_path, source_path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_seconds, 0.0)

        while True:
            exists = await asyncio.to_thread(
                lambda: proxy_path.exists() and proxy_path.stat().st_size > 0
            )
            if exists:
                return proxy_path
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(max(poll_interval_seconds, 0.05))

    @classmethod
    async def list_indexed_anime(
        cls,
        *,
        library_type: LibraryType | str | None = None,
    ) -> list[str]:
        """
        List all indexed anime in the library.

        Returns:
            Sorted list of anime series names.
        """
        scoped_type = coerce_library_type(library_type)
        library_path = cls.get_library_path(scoped_type)
        searcher_path = cls.get_anime_searcher_path()

        # Call anime_searcher directly via pixi to avoid task-shell quoting issues.
        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "list", str(cls.get_library_root()), "--type", scoped_type.value, "--json",
        ]

        try:
            result = await run_command(
                cmd,
                cwd=searcher_path,
                timeout_seconds=cls.LIST_TIMEOUT_SECONDS,
            )
        except CommandTimeoutError as exc:
            raise RuntimeError(str(exc)) from exc

        if result.returncode != 0:
            # If index doesn't exist yet, return empty list
            if b"does not exist" in result.stderr or b"empty" in result.stderr.lower():
                return []
            raise RuntimeError(f"Failed to list anime: {result.stderr.decode()}")

        try:
            payload = json.loads(result.stdout.decode())
            series = payload.get("series", [])
            # CLI returns objects with {name, frames}, extract just names
            if series and isinstance(series[0], dict):
                return [s["name"] for s in series]
            return series
        except json.JSONDecodeError:
            return []

    @classmethod
    async def get_available_folders(cls, source_path: Path) -> list[str]:
        """
        List folders in a source path that could be indexed.

        Args:
            source_path: Path to scan for anime folders.

        Returns:
            List of folder names.
        """
        if not source_path.exists() or not source_path.is_dir():
            return []

        def _scan_folders() -> list[str]:
            folders = []
            for item in source_path.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    folders.append(item.name)
            return sorted(folders)

        return await asyncio.to_thread(_scan_folders)

    @classmethod
    def get_source_import_manifest_path(cls, prepared_path: Path) -> Path:
        return prepared_path.with_name(f"{prepared_path.name}{cls.SOURCE_IMPORT_MANIFEST_SUFFIX}")

    @classmethod
    def _load_source_import_manifest_sync(cls, prepared_path: Path) -> dict[str, Any] | None:
        manifest_path = cls.get_source_import_manifest_path(prepared_path)
        if not manifest_path.exists():
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _record_source_import_manifest_sync(
        cls,
        source_path: Path,
        prepared_path: Path,
        *,
        source_probe: SourceMediaProbe | None = None,
        original_source_path: Path | None = None,
        sidecar_source_path: Path | None = None,
    ) -> None:
        source_stat = source_path.stat()
        manifest_path = cls.get_source_import_manifest_path(prepared_path)
        resolved_original_source_path = original_source_path or source_path
        manifest_payload: dict[str, Any] = {
            "source_path": str(source_path.resolve()),
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "prepared_path": str(prepared_path.resolve()),
            "original_source_path": str(resolved_original_source_path.resolve()),
            "sidecar_source_path": str((sidecar_source_path or source_path).resolve()),
        }
        if source_probe is None:
            source_probe = cls._probe_media_sync(resolved_original_source_path)
        if source_probe is not None and source_probe.audio_streams:
            manifest_payload["audio_streams"] = [
                cls._serialize_source_import_audio_stream(stream)
                for stream in source_probe.audio_streams
            ]
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def _clear_source_import_manifest_sync(cls, prepared_path: Path) -> None:
        manifest_path = cls.get_source_import_manifest_path(prepared_path)
        with suppress(OSError):
            manifest_path.unlink()

    @classmethod
    def _source_matches_prepared_sync(cls, source_path: Path, prepared_path: Path) -> bool:
        if not prepared_path.exists():
            return False

        prepared_probe = cls._probe_media_sync(prepared_path)
        if not cls._is_valid_prepared_library_probe(prepared_probe):
            return False

        try:
            if source_path.resolve() == prepared_path.resolve():
                return True
        except OSError:
            return False

        payload = cls._load_source_import_manifest_sync(prepared_path)
        if payload is None:
            return False

        try:
            source_stat = source_path.stat()
        except OSError:
            return False

        source_probe = cls._probe_media_sync(source_path)
        if source_probe is None:
            return False
        if not cls._is_valid_prepared_library_probe(prepared_probe, reference_probe=source_probe):
            return False

        return (
            payload.get("source_path") == str(source_path.resolve())
            and int(payload.get("source_size", -1)) == source_stat.st_size
            and int(payload.get("source_mtime_ns", -1)) == source_stat.st_mtime_ns
        )

    @classmethod
    async def _prepare_single_source_for_library(
        cls,
        *,
        source_path: Path,
        dest_dir: Path,
    ) -> tuple[Path, str, bool]:
        source_codec = await asyncio.to_thread(cls.get_primary_video_codec_sync, source_path)
        codec_lower = (source_codec or "").strip().lower()
        is_premiere_native = cls._is_premiere_native_codec(codec_lower)
        source_suffix = source_path.suffix.lower()
        is_mp4 = source_suffix == ".mp4"
        needs_direct_h264_transcode = not is_premiere_native
        transcode_action = "Transcoding to H.264 MP4"

        # Decide destination path and action label.
        preferred_dest = dest_dir / (source_path.stem + ".mp4")
        if needs_direct_h264_transcode:
            action = transcode_action
        elif not is_mp4:
            action = "Remuxing"
        else:
            action = "Copying"

        actual_dest = preferred_dest

        existing_ready = await asyncio.to_thread(
            cls._source_matches_prepared_sync,
            source_path,
            preferred_dest,
        )
        if existing_ready:
            return preferred_dest, "Using existing", False

        # Probe source once up front so unreadable inputs never get copied into
        # the library via fallback paths.
        source_probe: SourceMediaProbe | None = None
        try:
            source_probe = await asyncio.to_thread(cls._probe_media_sync, source_path)
        except Exception as exc:
            logger.debug("Could not probe %s for subtitles: %s", source_path.name, exc)
            source_probe = None
        if source_probe is None or source_probe.duration is None:
            raise RuntimeError(f"Source file is unreadable: {source_path.name}")
        tmp_dest = preferred_dest.with_name(f"{preferred_dest.stem}.import.tmp.mp4")

        async def _cleanup_tmp_dest() -> None:
            if tmp_dest.exists():
                with suppress(OSError):
                    await asyncio.to_thread(tmp_dest.unlink)

        async def _commit_transcoded_tmp_dest() -> None:
            try:
                await cls._run_library_import_h264_transcode(
                    source_path,
                    tmp_dest,
                    source_codec=source_codec,
                    probe=source_probe,
                )
            except Exception:
                await _cleanup_tmp_dest()
                raise
            transcode_probe = await asyncio.to_thread(cls._probe_media_sync, tmp_dest)
            if not cls._is_valid_prepared_library_probe(
                transcode_probe,
                reference_probe=source_probe,
                require_h264_video=True,
            ):
                await _cleanup_tmp_dest()
                raise RuntimeError(
                    f"Transcoded H.264 MP4 output failed validation: {source_path.name}"
                )
            await asyncio.to_thread(tmp_dest.replace, preferred_dest)

        if needs_direct_h264_transcode:
            if tmp_dest.exists():
                await _cleanup_tmp_dest()
            await _commit_transcoded_tmp_dest()
        elif not is_mp4:
            # --- Remux native video to MP4, then fall back to full H.264 MP4 transcode ---
            if tmp_dest.exists():
                await _cleanup_tmp_dest()
            remux_error: str | None = None
            try:
                remux_result = await run_command(
                    cls._build_library_import_remux_cmd(
                        source_path,
                        tmp_dest,
                        probe=source_probe,
                    ),
                    timeout_seconds=cls.REMUX_TIMEOUT_SECONDS,
                )
            except FileNotFoundError as exc:
                if is_media_binary_override_error(exc):
                    raise
                remux_error = cls._format_media_failure(exc)
            except CommandTimeoutError as exc:
                remux_error = cls._format_media_failure(exc)
            else:
                remux_probe = None
                if remux_result.returncode == 0:
                    remux_probe = await asyncio.to_thread(cls._probe_media_sync, tmp_dest)
                if cls._is_valid_prepared_library_probe(remux_probe, reference_probe=source_probe):
                    await asyncio.to_thread(tmp_dest.replace, preferred_dest)
                else:
                    remux_error = cls._format_media_failure(remux_result)
                    if remux_result.returncode == 0:
                        remux_error = f"Prepared MP4 output failed validation for {source_path.name}"

            if remux_error is not None:
                await _cleanup_tmp_dest()
                action = transcode_action
                try:
                    await _commit_transcoded_tmp_dest()
                except RuntimeError as exc:
                    raise RuntimeError(
                        f"Failed to prepare source as MP4 (remux: {remux_error}; {exc})"
                    ) from exc
        elif preferred_dest != source_path:
            await asyncio.to_thread(shutil.copy2, source_path, preferred_dest)

        # Extract subtitles to sidecar.  Always attempt extraction regardless
        # of transform type — remux/transcode strip subtitle streams and even
        # plain copies benefit from having a sidecar for later use.
        if (
            source_probe is not None
            and source_probe.subtitle_streams
            and actual_dest.exists()
        ):
            try:
                await cls._write_subtitle_sidecar(
                    source_path=source_path,
                    normalized_target_path=actual_dest,
                    probe=source_probe,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to extract subtitle sidecar during import for %s: %s",
                    source_path.name,
                    exc,
                )

        if dest_dir == source_path.parent and actual_dest == preferred_dest and preferred_dest != source_path:
            with suppress(OSError):
                await asyncio.to_thread(source_path.unlink)

        if actual_dest.exists():
            await asyncio.to_thread(
                cls._record_source_import_manifest_sync,
                source_path,
                actual_dest,
                source_probe=source_probe,
            )

        return actual_dest, action, True

    @classmethod
    async def _verify_prepared_library_files(cls, prepared_files: list[Path]) -> str | None:
        await asyncio.sleep(1.0)
        for dest_file in prepared_files:
            if not dest_file.exists():
                return f"Prepared file missing after import: {dest_file.name}"
            file_stat = await asyncio.to_thread(dest_file.stat)
            if file_stat.st_size == 0:
                return f"Prepared file is empty: {dest_file.name}"
            prepared_probe = await asyncio.to_thread(cls._probe_media_sync, dest_file)
            if not cls._is_valid_prepared_library_probe(prepared_probe):
                return f"Prepared file is unreadable: {dest_file.name}"
        return None

    @classmethod
    def _write_temp_path_manifest(cls, paths: list[Path]) -> Path:
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".anime_searcher_manifest.json",
            prefix="anime_searcher_",
            dir=settings.cache_dir,
            delete=False,
            encoding="utf-8",
        ) as handle:
            json.dump({"files": [str(path) for path in paths]}, handle)
            return Path(handle.name)

    @classmethod
    def _anime_searcher_subprocess_env(cls, cmd: list[str]) -> dict[str, str]:
        env = get_media_subprocess_env(cmd)
        merged_env = dict(os.environ) if env is None else dict(env)
        allocator_conf = str(merged_env.get("PYTORCH_CUDA_ALLOC_CONF") or "").strip()
        allocator_hint = "expandable_segments:True"
        allocator_parts = [
            part.strip()
            for part in allocator_conf.split(",")
            if part.strip()
        ]
        if allocator_hint not in allocator_parts:
            allocator_parts.append(allocator_hint)
        merged_env["PYTORCH_CUDA_ALLOC_CONF"] = ",".join(allocator_parts)
        return merged_env

    @classmethod
    async def _stream_searcher_command(
        cls,
        *,
        cmd: list[str],
        cwd: Path,
        total_files: int,
        status: str,
        progress_start: float,
        progress_span: float,
    ) -> AsyncIterator[IndexProgress]:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=cls._anime_searcher_subprocess_env(cmd),
            start_new_session=True,
        )

        stdout_tail: deque[str] = deque(maxlen=5)
        stdout_line_count = 0
        stderr_chunks: deque[str] = deque()
        stderr_bytes = 0
        stderr_limit = 8192

        async def _drain_stderr() -> str:
            nonlocal stderr_bytes
            if process.stderr is None:
                return ""
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                decoded = chunk.decode("utf-8", errors="replace")
                stderr_chunks.append(decoded)
                stderr_bytes += len(decoded.encode("utf-8", errors="replace"))
                while stderr_chunks and stderr_bytes > stderr_limit:
                    dropped = stderr_chunks.popleft()
                    stderr_bytes -= len(dropped.encode("utf-8", errors="replace"))
            return "".join(stderr_chunks).strip()

        stderr_task = asyncio.create_task(_drain_stderr())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cls.INDEX_TIMEOUT_SECONDS
        aborted = False
        saw_explicit_error = False

        try:
            assert process.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError

                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                if not line:
                    break
                decoded = line.decode()
                stdout_line_count += 1
                stdout_tail.append(decoded)
                progress = cls._parse_searcher_progress_line(
                    line=decoded,
                    status=status,
                    total_files=total_files,
                    progress_start=progress_start,
                    progress_span=progress_span,
                    text_line_index=stdout_line_count,
                )
                if progress is None:
                    continue
                if progress.status == "error":
                    saw_explicit_error = True
                    aborted = True
                    await terminate_process(process, kill_group=True)
                    yield progress
                    return
                yield progress

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)
        except asyncio.CancelledError:
            aborted = True
            await terminate_process(process, kill_group=True)
            raise
        except asyncio.TimeoutError:
            aborted = True
            await terminate_process(process, kill_group=True)
            yield IndexProgress(
                status="error",
                error=(
                    f"anime_searcher command timed out after {int(cls.INDEX_TIMEOUT_SECONDS)} seconds. "
                    "Try reducing library size or retrying."
                ),
            )
            return
        finally:
            if aborted and process.returncode is None:
                await terminate_process(process, kill_group=True)

        stderr = await stderr_task
        if process.returncode != 0 and not saw_explicit_error:
            stdout_text = "".join(stdout_tail).strip()
            detail = stderr or stdout_text or "unknown error"
            yield IndexProgress(
                status="error",
                error=f"anime_searcher command failed: {detail}",
            )

    @classmethod
    async def _delete_library_file_artifacts(cls, source_path: Path) -> None:
        sidecar_source_path = cls.resolve_subtitle_sidecar_source_path(source_path) or source_path
        sidecar_dir = cls.get_subtitle_sidecar_dir(sidecar_source_path)
        if sidecar_dir.exists():
            await asyncio.to_thread(shutil.rmtree, sidecar_dir, True)
        await asyncio.to_thread(cls._clear_source_import_manifest_sync, source_path)
        if source_path.exists():
            await asyncio.to_thread(source_path.unlink)

    @classmethod
    async def index_anime(
        cls,
        source_folder: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        fps: float = 2.0,
        batch_size: int = 64,
        prefetch_batches: int = 3,
        transform_workers: int = 4,
        decode_backend: str = "auto",
        precision: str = "auto",
        require_gpu: bool = True,
    ) -> AsyncIterator[IndexProgress]:
        """
        Copy anime folder to library and index it.

        This method ensures all file copy/remux operations complete and files
        are verified before starting the indexing process to prevent race
        conditions.

        Args:
            source_folder: Path to folder containing episodes.
            anime_name: Name for the anime (default: folder name).
            fps: Requested FPS for indexing (used for new series).
            batch_size: Embedding batch size.
            prefetch_batches: Pipeline prefetch queue size.
            transform_workers: CPU worker count for image transforms.
            decode_backend: Requested frame decode backend.
            precision: Requested model precision.
            require_gpu: Fail if CUDA is unavailable.

        Yields:
            Progress updates during copying and indexing.
        """
        scoped_type = coerce_library_type(library_type)
        library_path = cls.get_library_path(scoped_type)
        searcher_path = cls.get_anime_searcher_path()

        library_path.mkdir(parents=True, exist_ok=True)

        if anime_name is None:
            anime_name = source_folder.name

        requested_fps = fps
        effective_fps = fps
        existing_series_fps = await asyncio.to_thread(
            cls._get_indexed_series_fps_sync,
            anime_name,
            scoped_type,
        )
        is_existing_series = existing_series_fps is not None
        if existing_series_fps is not None:
            effective_fps = existing_series_fps

        dest_path = library_path / anime_name

        yield IndexProgress(
            status="starting",
            message=f"Preparing to index {anime_name}",
            anime_name=anime_name,
        )

        if not source_folder.exists():
            yield IndexProgress(
                status="error",
                error=f"Source folder not found: {source_folder}",
                anime_name=anime_name,
            )
            return

        source_scan = await asyncio.to_thread(cls.scan_direct_video_files_sync, source_folder)
        video_files = list(source_scan.readable_files)
        skipped_warnings = [
            cls._invalid_source_warning(path)
            for path in source_scan.invalid_files
        ]

        if not video_files:
            if source_scan.invalid_files:
                yield IndexProgress(
                    status="error",
                    error=(
                        f"No readable video files found in {source_folder}. "
                        f"{cls._invalid_source_error_detail(source_scan.invalid_files)}"
                    ),
                    anime_name=anime_name,
                    warnings=skipped_warnings or None,
                )
                return
            yield IndexProgress(
                status="error",
                error=f"No video files found in {source_folder}",
                anime_name=anime_name,
            )
            return

        total_files = len(video_files)
        prepared_files: list[Path] = []

        if skipped_warnings:
            skipped_count = len(skipped_warnings)
            yield IndexProgress(
                status="copying",
                message=(
                    f"Skipping {skipped_count} unreadable source file"
                    f"{'s' if skipped_count != 1 else ''} before import"
                ),
                progress=0.0,
                total_files=total_files,
                anime_name=anime_name,
                warnings=skipped_warnings,
            )

        if dest_path != source_folder:
            yield IndexProgress(
                status="copying",
                message=f"Preparing {total_files} files for library import",
                total_files=total_files,
                anime_name=anime_name,
            )
            dest_path.mkdir(parents=True, exist_ok=True)
        else:
            yield IndexProgress(
                status="copying",
                message=f"Normalizing {total_files} files in library",
                total_files=total_files,
                anime_name=anime_name,
            )

        for i, video_file in enumerate(video_files):
            yield IndexProgress(
                status="copying",
                message=f"Preparing {video_file.name}",
                progress=(i + 0.5) / total_files * 0.3,
                current_file=video_file.name,
                total_files=total_files,
                completed_files=i,
                anime_name=anime_name,
            )

            actual_dest, action, _changed = await cls._prepare_single_source_for_library(
                source_path=video_file,
                dest_dir=dest_path,
            )
            if actual_dest not in prepared_files:
                prepared_files.append(actual_dest)

            yield IndexProgress(
                status="copying",
                message=f"{action} {video_file.name}",
                progress=(i + 1) / total_files * 0.3,
                current_file=video_file.name,
                total_files=total_files,
                completed_files=i + 1,
                anime_name=anime_name,
            )

        yield IndexProgress(
            status="copying",
            message="Verifying prepared files before indexing...",
            progress=0.3,
            total_files=total_files,
            completed_files=total_files,
            anime_name=anime_name,
        )
        verify_error = await cls._verify_prepared_library_files(prepared_files)
        if verify_error is not None:
            yield IndexProgress(
                status="error",
                error=verify_error,
                anime_name=anime_name,
            )
            return

        if is_existing_series and abs(effective_fps - requested_fps) > 1e-9:
            yield IndexProgress(
                status="indexing",
                message=(
                    f"{anime_name} already indexed at {effective_fps:g} fps; "
                    f"keeping existing FPS (requested {requested_fps:g} ignored)"
                ),
                progress=0.35,
                total_files=total_files,
                anime_name=anime_name,
            )

        yield IndexProgress(
            status="indexing",
            message=f"Indexing {anime_name} at {effective_fps:g} fps",
            progress=0.35,
            total_files=total_files,
            anime_name=anime_name,
            requested_batch_size=batch_size,
            effective_batch_size=batch_size,
            effective_decode_backend=decode_backend,
        )

        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "index", str(cls.get_library_root()),
            "--type", scoped_type.value,
            "--fps", str(effective_fps),
            "--series", anime_name,
            "--batch-size", str(batch_size),
            "--prefetch-batches", str(prefetch_batches),
            "--transform-workers", str(transform_workers),
            "--decode-backend", decode_backend,
            "--precision", precision,
            "--progress-json",
        ]
        if require_gpu:
            cmd.append("--require-gpu")

        searcher_metadata = SearcherExecutionMetadata(
            requested_batch_size=batch_size,
            effective_batch_size=batch_size,
            requested_decode_backend=decode_backend,
        )

        async for progress in cls._stream_searcher_index_command(
            cmd=cmd,
            cwd=searcher_path,
            total_files=total_files,
            anime_name=anime_name,
            metadata=searcher_metadata,
            progress_start=0.35,
            progress_span=0.60,
        ):
            yield progress
            if progress.status == "error":
                return

        await cls.ensure_episode_manifest(force_refresh=True, library_type=scoped_type)
        completion_warnings = list(skipped_warnings)
        if searcher_metadata.retry_warning and searcher_metadata.retry_warning not in completion_warnings:
            completion_warnings.append(searcher_metadata.retry_warning)

        yield IndexProgress(
            status="complete",
            message=f"Successfully indexed {anime_name}",
            progress=1.0,
            total_files=total_files,
            completed_files=total_files,
            anime_name=anime_name,
            prepared_library_paths=[str(path) for path in prepared_files],
            warnings=completion_warnings or None,
            requested_batch_size=batch_size,
            effective_batch_size=searcher_metadata.effective_batch_size,
            effective_decode_backend=(
                searcher_metadata.effective_decode_backend or decode_backend
            ),
            retry_reason=searcher_metadata.retry_reason,
        )

    @classmethod
    async def update_anime(
        cls,
        *,
        library_type: LibraryType | str | None = None,
        anime_name: str,
        source_paths: list[Path],
        batch_size: int = 64,
        prefetch_batches: int = 3,
        transform_workers: int = 4,
        decode_backend: str = "auto",
        precision: str = "auto",
        require_gpu: bool = True,
    ) -> AsyncIterator[IndexProgress]:
        """Prepare a precise list of source files then incrementally upsert them."""
        scoped_type = coerce_library_type(library_type)
        library_path = cls.get_library_path(scoped_type)
        searcher_path = cls.get_anime_searcher_path()
        library_path.mkdir(parents=True, exist_ok=True)

        yield IndexProgress(
            status="starting",
            message=f"Preparing incremental update for {anime_name}",
            anime_name=anime_name,
        )

        if not source_paths:
            yield IndexProgress(
                status="error",
                error="No source files provided for incremental update.",
                anime_name=anime_name,
            )
            return

        existing_series_fps = await asyncio.to_thread(
            cls._get_indexed_series_fps_sync,
            anime_name,
            scoped_type,
        )
        if existing_series_fps is None:
            yield IndexProgress(
                status="error",
                error=f"Series '{anime_name}' is not indexed yet. Use /anime/index first.",
                anime_name=anime_name,
            )
            return

        video_files: list[Path] = []
        seen_sources: set[str] = set()
        for raw_path in source_paths:
            candidate = raw_path.resolve()
            if str(candidate) in seen_sources:
                continue
            seen_sources.add(str(candidate))
            if not candidate.exists() or not candidate.is_file():
                yield IndexProgress(
                    status="error",
                    error=f"Source file not found: {candidate}",
                    anime_name=anime_name,
                )
                return
            if candidate.suffix.lower() not in cls.VIDEO_EXTENSIONS:
                yield IndexProgress(
                    status="error",
                    error=f"Unsupported source file type: {candidate.name}",
                    anime_name=anime_name,
                )
                return
            video_files.append(candidate)

        source_scan = await asyncio.to_thread(cls._scan_source_video_paths_sync, video_files)
        video_files = list(source_scan.readable_files)
        skipped_warnings = [
            cls._invalid_source_warning(path)
            for path in source_scan.invalid_files
        ]

        if not video_files:
            if source_scan.invalid_files:
                yield IndexProgress(
                    status="error",
                    error=(
                        f"No readable source files provided for incremental update. "
                        f"{cls._invalid_source_error_detail(source_scan.invalid_files)}"
                    ),
                    anime_name=anime_name,
                    warnings=skipped_warnings or None,
                )
                return
            yield IndexProgress(
                status="error",
                error="No source files provided for incremental update.",
                anime_name=anime_name,
            )
            return

        total_files = len(video_files)
        dest_dir = library_path / anime_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        prepared_files: list[Path] = []

        if skipped_warnings:
            skipped_count = len(skipped_warnings)
            yield IndexProgress(
                status="copying",
                message=(
                    f"Skipping {skipped_count} unreadable source file"
                    f"{'s' if skipped_count != 1 else ''} before update"
                ),
                progress=0.0,
                total_files=total_files,
                anime_name=anime_name,
                warnings=skipped_warnings,
            )

        yield IndexProgress(
            status="copying",
            message=f"Preparing {total_files} files for incremental update",
            total_files=total_files,
            anime_name=anime_name,
        )

        for i, video_file in enumerate(video_files):
            yield IndexProgress(
                status="copying",
                message=f"Preparing {video_file.name}",
                progress=(i + 0.5) / total_files * 0.3,
                current_file=video_file.name,
                total_files=total_files,
                completed_files=i,
                anime_name=anime_name,
            )

            actual_dest, action, _changed = await cls._prepare_single_source_for_library(
                source_path=video_file,
                dest_dir=dest_dir,
            )
            if actual_dest not in prepared_files:
                prepared_files.append(actual_dest)

            yield IndexProgress(
                status="copying",
                message=f"{action} {video_file.name}",
                progress=(i + 1) / total_files * 0.3,
                current_file=video_file.name,
                total_files=total_files,
                completed_files=i + 1,
                anime_name=anime_name,
            )

        yield IndexProgress(
            status="copying",
            message="Verifying prepared files before indexing...",
            progress=0.3,
            total_files=total_files,
            completed_files=total_files,
            anime_name=anime_name,
        )
        verify_error = await cls._verify_prepared_library_files(prepared_files)
        if verify_error is not None:
            yield IndexProgress(
                status="error",
                error=verify_error,
                anime_name=anime_name,
            )
            return

        manifest_path = await asyncio.to_thread(cls._write_temp_path_manifest, prepared_files)
        try:
            yield IndexProgress(
                status="indexing",
                message=f"Updating {anime_name} at {existing_series_fps:g} fps",
                progress=0.35,
                total_files=total_files,
                anime_name=anime_name,
                requested_batch_size=batch_size,
                effective_batch_size=batch_size,
                effective_decode_backend=decode_backend,
            )

            cmd = [
                "pixi", "run", "--locked",
                "python", "-m", "anime_searcher.cli",
                "update", str(cls.get_library_root()),
                "--type", scoped_type.value,
                "--series", anime_name,
                "--manifest", str(manifest_path),
                "--batch-size", str(batch_size),
                "--prefetch-batches", str(prefetch_batches),
                "--transform-workers", str(transform_workers),
                "--decode-backend", decode_backend,
                "--precision", precision,
                "--progress-json",
            ]
            if require_gpu:
                cmd.append("--require-gpu")

            searcher_metadata = SearcherExecutionMetadata(
                requested_batch_size=batch_size,
                effective_batch_size=batch_size,
                requested_decode_backend=decode_backend,
            )

            async for progress in cls._stream_searcher_index_command(
                cmd=cmd,
                cwd=searcher_path,
                total_files=total_files,
                anime_name=anime_name,
                metadata=searcher_metadata,
                progress_start=0.35,
                progress_span=0.60,
            ):
                yield progress
                if progress.status == "error":
                    return
        finally:
            with suppress(OSError):
                manifest_path.unlink()

        await cls.ensure_episode_manifest(force_refresh=True, library_type=scoped_type)
        completion_warnings = list(skipped_warnings)
        if searcher_metadata.retry_warning and searcher_metadata.retry_warning not in completion_warnings:
            completion_warnings.append(searcher_metadata.retry_warning)
        yield IndexProgress(
            status="complete",
            message=f"Successfully updated {anime_name}",
            progress=1.0,
            total_files=total_files,
            completed_files=total_files,
            anime_name=anime_name,
            prepared_library_paths=[str(path) for path in prepared_files],
            warnings=completion_warnings or None,
            requested_batch_size=batch_size,
            effective_batch_size=searcher_metadata.effective_batch_size,
            effective_decode_backend=(
                searcher_metadata.effective_decode_backend or decode_backend
            ),
            retry_reason=searcher_metadata.retry_reason,
        )

    @classmethod
    async def remove_anime_files(
        cls,
        *,
        library_type: LibraryType | str | None = None,
        anime_name: str,
        library_paths: list[Path],
    ) -> AsyncIterator[IndexProgress]:
        """Remove a precise list of already imported library files from the index and library."""
        scoped_type = coerce_library_type(library_type)
        library_root = cls.get_library_path(scoped_type)
        searcher_path = cls.get_anime_searcher_path()

        yield IndexProgress(
            status="starting",
            message=f"Preparing removal for {anime_name}",
            anime_name=anime_name,
        )

        if not library_paths:
            yield IndexProgress(
                status="error",
                error="No library paths provided for removal.",
                anime_name=anime_name,
            )
            return

        normalized_paths: list[Path] = []
        seen_paths: set[str] = set()
        for raw_path in library_paths:
            candidate = raw_path if raw_path.is_absolute() else (library_root / raw_path)
            resolved = candidate.resolve()
            try:
                rel_path = resolved.relative_to(library_root.resolve())
            except ValueError:
                yield IndexProgress(
                    status="error",
                    error=f"Path is outside the library root: {raw_path}",
                    anime_name=anime_name,
                )
                return
            if len(rel_path.parts) < 2 or rel_path.parts[0] != anime_name:
                yield IndexProgress(
                    status="error",
                    error=f"Path does not belong to series '{anime_name}': {raw_path}",
                    anime_name=anime_name,
                )
                return
            rel_key = str(rel_path)
            if rel_key in seen_paths:
                continue
            seen_paths.add(rel_key)
            normalized_paths.append(library_root / rel_path)

        total_files = len(normalized_paths)
        manifest_path = await asyncio.to_thread(cls._write_temp_path_manifest, normalized_paths)
        try:
            yield IndexProgress(
                status="indexing",
                message=f"Removing {total_files} file(s) from {anime_name}",
                progress=0.2,
                total_files=total_files,
                anime_name=anime_name,
            )

            cmd = [
                "pixi", "run", "--locked",
                "python", "-m", "anime_searcher.cli",
                "remove", str(cls.get_library_root()),
                "--type", scoped_type.value,
                "--series", anime_name,
                "--manifest", str(manifest_path),
                "--progress-json",
            ]

            async for progress in cls._stream_searcher_command(
                cmd=cmd,
                cwd=searcher_path,
                total_files=total_files,
                status="indexing",
                progress_start=0.2,
                progress_span=0.5,
            ):
                progress.anime_name = anime_name
                yield progress
                if progress.status == "error":
                    return
        finally:
            with suppress(OSError):
                manifest_path.unlink()

        removed_paths: list[str] = []
        for i, prepared_path in enumerate(normalized_paths, start=1):
            await cls._delete_library_file_artifacts(prepared_path)
            removed_paths.append(str(prepared_path))
            yield IndexProgress(
                status="copying",
                message=f"Removed {prepared_path.name}",
                progress=0.7 + 0.25 * (i / max(total_files, 1)),
                current_file=prepared_path.name,
                total_files=total_files,
                completed_files=i,
                anime_name=anime_name,
            )

        await cls.ensure_episode_manifest(force_refresh=True, library_type=scoped_type)
        yield IndexProgress(
            status="complete",
            message=f"Successfully removed {len(removed_paths)} file(s) from {anime_name}",
            progress=1.0,
            total_files=total_files,
            completed_files=total_files,
            anime_name=anime_name,
            prepared_library_paths=removed_paths,
        )

    @classmethod
    async def search_frame(
        cls,
        image_path: Path,
        library_type: LibraryType | str | None = None,
        anime_name: str | None = None,
        flip: bool = True,
        top_n: int = 5,
    ) -> list[dict]:
        """
        Search for a frame in the indexed library.

        Args:
            image_path: Path to the query image.
            anime_name: Filter to specific anime (optional).
            flip: Also search flipped image.
            top_n: Number of results to return.

        Returns:
            List of search results.
        """
        scoped_type = coerce_library_type(library_type)
        library_path = cls.get_library_path(scoped_type)
        searcher_path = cls.get_anime_searcher_path()

        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "search", str(image_path),
            "--library", str(cls.get_library_root()),
            "--type", scoped_type.value,
            "--top-n", str(top_n),
            "--json",
        ]

        if flip:
            cmd.append("--flip")

        if anime_name:
            cmd.extend(["--series", anime_name])

        try:
            result = await run_command(
                cmd,
                cwd=searcher_path,
                timeout_seconds=cls.SEARCH_TIMEOUT_SECONDS,
            )
        except CommandTimeoutError as exc:
            raise RuntimeError(str(exc)) from exc

        if result.returncode != 0:
            raise RuntimeError(f"Search failed: {result.stderr.decode()}")

        try:
            payload = json.loads(result.stdout.decode())
            return payload.get("results", [])
        except json.JSONDecodeError:
            return []

    @classmethod
    def _get_source_details_sync(
        cls,
        library_type: LibraryType | str | None = None,
    ) -> list[dict]:
        """Collect per-source metadata for all series in a library type (sync)."""
        scoped_type = coerce_library_type(library_type)
        library_path = cls.get_library_path(scoped_type)

        if not library_path.exists():
            return []

        # Load the shared index manifest once
        index_dir = library_path / cls.INDEX_DIR_NAME
        manifest_path = index_dir / cls.MANIFEST_FILE
        manifest_series: dict = {}
        manifest_default_fps: float | None = None
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest_payload, dict):
                raw_series = manifest_payload.get("series", {})
                if isinstance(raw_series, dict):
                    manifest_series = raw_series
                config = manifest_payload.get("config", {})
                if isinstance(config, dict):
                    manifest_default_fps = cls._coerce_fps(config.get("default_fps"))
        except (OSError, json.JSONDecodeError):
            pass

        # Load state.json once (indexed file paths keyed by relative path)
        state_path = index_dir / cls.STATE_FILE
        state_files: dict = {}
        try:
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state_payload, dict):
                files = state_payload.get("files", {})
                if isinstance(files, dict):
                    state_files = files
        except (OSError, json.JSONDecodeError):
            pass

        results = []
        try:
            series_dirs = [
                entry
                for entry in library_path.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            ]
        except OSError:
            return []

        for series_dir in series_dirs:
            series_name = series_dir.name

            # Count video files on disk
            video_files_on_disk: list[Path] = []
            try:
                video_files_on_disk = [
                    f
                    for f in series_dir.iterdir()
                    if (
                        f.is_file()
                        and f.suffix.lower() in cls.VIDEO_EXTENSIONS
                        and not cls.is_transient_library_video_path(f)
                    )
                ]
            except OSError:
                pass

            episode_count_on_disk = len(video_files_on_disk)
            total_size_bytes = sum(
                f.stat().st_size for f in video_files_on_disk
                if f.exists()
            )

            # Count indexed episodes from state.json
            prefix = f"{series_name}/"
            indexed_episode_count = sum(
                1 for path in state_files
                if path == series_name or path.startswith(prefix)
            )

            missing_episodes = max(0, indexed_episode_count - episode_count_on_disk)

            # Get FPS from manifest
            fps: float = 0.0
            series_entry = manifest_series.get(series_name)
            if isinstance(series_entry, dict):
                fps_val = cls._coerce_fps(series_entry.get("fps"))
                if fps_val is not None:
                    fps = fps_val
            if fps == 0.0 and manifest_default_fps is not None:
                fps = manifest_default_fps

            # Get purge_protection from .atr_torrents.json (may not exist yet)
            purge_protected = False
            torrents_path = series_dir / ".atr_torrents.json"
            try:
                torrents_payload = json.loads(torrents_path.read_text(encoding="utf-8"))
                if isinstance(torrents_payload, dict):
                    purge_protected = bool(torrents_payload.get("purge_protection", False))
            except (OSError, json.JSONDecodeError):
                pass

            # Get original_index_path from first .atr_source.json found
            original_index_path: str | None = None
            try:
                for entry in series_dir.iterdir():
                    if entry.is_file() and entry.name.endswith(cls.SOURCE_IMPORT_MANIFEST_SUFFIX):
                        try:
                            source_payload = json.loads(entry.read_text(encoding="utf-8"))
                            if isinstance(source_payload, dict):
                                raw_source_path = source_payload.get("source_path")
                                if isinstance(raw_source_path, str) and raw_source_path:
                                    original_index_path = raw_source_path
                                    break
                        except (OSError, json.JSONDecodeError):
                            continue
            except OSError:
                pass

            results.append({
                "name": series_name,
                "episode_count": indexed_episode_count,
                "total_size_bytes": total_size_bytes,
                "fps": fps,
                "missing_episodes": missing_episodes,
                "purge_protected": purge_protected,
                "original_index_path": original_index_path,
            })

        return results

    @classmethod
    async def get_source_details(
        cls,
        *,
        library_type: LibraryType | str | None = None,
    ) -> list[dict]:
        """Get detailed metadata for all sources in a library type."""
        return await asyncio.to_thread(cls._get_source_details_sync, library_type)

    # ------------------------------------------------------------------
    # Purge system
    # ------------------------------------------------------------------

    @classmethod
    def _purge_library_sync(
        cls,
        library_types: list[LibraryType],
    ) -> dict:
        """Delete video files from library sources, preserving indexes and metadata.

        Respects purge protection flags in .atr_torrents.json.
        Returns dict with purged_sources, freed_bytes, skipped_protected.
        """
        import shutil
        from .torrent_linker import TorrentLinkerService
        from ..config import settings as app_settings

        purged_sources: list[str] = []
        freed_bytes = 0
        skipped_protected: list[str] = []

        for lt in library_types:
            library_root = cls.get_library_path(library_type=lt)
            if not library_root.exists():
                continue
            for source_dir in library_root.iterdir():
                if not source_dir.is_dir() or source_dir.name.startswith("."):
                    continue
                metadata = TorrentLinkerService.load_metadata(source_dir)
                if metadata and metadata.purge_protection:
                    skipped_protected.append(source_dir.name)
                    continue
                for f in source_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in cls.VIDEO_EXTENSIONS:
                        freed_bytes += f.stat().st_size
                        f.unlink()
                purged_sources.append(source_dir.name)

        # Clear caches
        cache_dir = app_settings.cache_dir
        for lt in library_types:
            manifest = cache_dir / f"episodes_manifest__{lt.value}.json"
            if manifest.exists():
                manifest.unlink()
        for subdir in ["source_previews", "source_stream_chunks_v1"]:
            cache_subdir = cache_dir / subdir
            if cache_subdir.exists():
                shutil.rmtree(cache_subdir)
        default_manifest = cache_dir / "episodes_manifest.json"
        if default_manifest.exists():
            default_manifest.unlink()

        return {
            "purged_sources": purged_sources,
            "freed_bytes": freed_bytes,
            "skipped_protected": skipped_protected,
        }

    @classmethod
    async def purge_library(
        cls,
        library_types: list[LibraryType],
    ) -> dict:
        """Async wrapper for purge."""
        return await asyncio.to_thread(cls._purge_library_sync, library_types)

    @classmethod
    def _estimate_purge_size_sync(
        cls,
        library_types: list[LibraryType],
    ) -> dict:
        """Estimate space freed by purging (respects protection)."""
        from .torrent_linker import TorrentLinkerService

        total_bytes = 0
        source_count = 0

        for lt in library_types:
            library_root = cls.get_library_path(library_type=lt)
            if not library_root.exists():
                continue
            for source_dir in library_root.iterdir():
                if not source_dir.is_dir() or source_dir.name.startswith("."):
                    continue
                metadata = TorrentLinkerService.load_metadata(source_dir)
                if metadata and metadata.purge_protection:
                    continue
                has_videos = False
                for f in source_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in cls.VIDEO_EXTENSIONS:
                        total_bytes += f.stat().st_size
                        has_videos = True
                if has_videos:
                    source_count += 1

        return {"estimated_bytes": total_bytes, "source_count": source_count}

    @classmethod
    async def estimate_purge_size(
        cls,
        library_types: list[LibraryType],
    ) -> dict:
        """Async wrapper for purge estimate."""
        return await asyncio.to_thread(cls._estimate_purge_size_sync, library_types)
