"""Service for managing the anime library (indexing, listing, copying)."""

import asyncio
import hashlib
import json
import logging
import subprocess
import shutil
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from PIL import Image

from ..config import settings
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

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "current_file": self.current_file,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "error": self.error,
        }


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


@dataclass(frozen=True)
class SourceNormalizationPlan:
    """Chosen compatibility action for one source episode."""

    action: str
    source_path: Path
    target_path: Path
    probe: SourceMediaProbe


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
    INDEX_DIR_NAME = ".index"
    MANIFEST_FILE = "manifest.json"
    LEGACY_METADATA_FILE = "metadata.json"
    STATE_FILE = "state.json"
    LIST_TIMEOUT_SECONDS = 120.0
    SEARCH_TIMEOUT_SECONDS = 120.0
    REMUX_TIMEOUT_SECONDS = 600.0
    INDEX_TIMEOUT_SECONDS = 7200.0
    PREVIEW_PROXY_TIMEOUT_SECONDS = 3600.0
    SOURCE_NORMALIZATION_TIMEOUT_SECONDS = 7200.0
    SUBTITLE_EXTRACTION_TIMEOUT_SECONDS = 1800.0
    FFPROBE_TIMEOUT_SECONDS = 30.0
    SOURCE_NORMALIZATION_AUDIO_BITRATE = "192k"
    SOURCE_NORMALIZATION_AUDIO_RATE = "48000"
    SOURCE_NORMALIZATION_PROFILE_H264_MP4_AAC = "h264_mp4_aac"
    GPU_HWACCEL = "cuda"
    GPU_H264_ENCODER = "h264_nvenc"

    _episode_manifest_cache: dict | None = None
    _episode_manifest_lock: asyncio.Lock | None = None
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
    }

    @staticmethod
    def get_library_path() -> Path:
        """Get the anime library path from settings."""
        return settings.anime_library_path

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
    def _get_indexed_series_fps_sync(cls, anime_name: str) -> float | None:
        """Read FPS for an already indexed series from index metadata."""
        index_dir = cls.get_library_path() / cls.INDEX_DIR_NAME
        manifest_path = index_dir / cls.MANIFEST_FILE
        if manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = None

            if isinstance(payload, dict):
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
                                config.get("default_fps", config.get("fps"))
                            )

        # Legacy fallback: single global FPS in metadata config.
        legacy_metadata_path = index_dir / cls.LEGACY_METADATA_FILE
        if not legacy_metadata_path.exists():
            return None

        # Legacy index is global; ensure this series actually exists in state.
        state_path = index_dir / cls.STATE_FILE
        if state_path.exists():
            try:
                state_payload = json.loads(state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state_payload = {}
            files = state_payload.get("files", {}) if isinstance(state_payload, dict) else {}
            if isinstance(files, dict):
                has_series_state = any(
                    path == anime_name or path.startswith(f"{anime_name}/")
                    for path in files
                )
                if not has_series_state:
                    return None

        try:
            legacy_payload = json.loads(legacy_metadata_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(legacy_payload, dict):
            return None
        config = legacy_payload.get("config", {})
        if not isinstance(config, dict):
            return None
        return cls._coerce_fps(config.get("fps"))

    @classmethod
    def get_episode_manifest_path(cls) -> Path:
        """Get path for cached episode index manifest."""
        return settings.cache_dir / "episodes_manifest.json"

    @classmethod
    def _get_manifest_lock(cls) -> asyncio.Lock:
        if cls._episode_manifest_lock is None:
            cls._episode_manifest_lock = asyncio.Lock()
        return cls._episode_manifest_lock

    @classmethod
    def _get_preview_generation_lock(cls) -> asyncio.Lock:
        if cls._preview_generation_lock is None:
            cls._preview_generation_lock = asyncio.Lock()
        return cls._preview_generation_lock

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
    def _scan_library_episodes_sync(cls) -> dict:
        """Scan library once and build fast stem -> path index."""
        library_path = cls.get_library_path()
        episodes: list[str] = []
        by_stem: dict[str, list[str]] = {}

        if library_path.exists():
            for entry in library_path.rglob("*"):
                if not entry.is_file() or entry.suffix.lower() not in cls.VIDEO_EXTENSIONS:
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

        manifest_path = cls.get_episode_manifest_path()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        cls._episode_manifest_cache = manifest
        return manifest

    @classmethod
    def _load_episode_manifest_sync(cls) -> dict | None:
        """Load cached episode manifest if present."""
        if cls._episode_manifest_cache is not None:
            return cls._episode_manifest_cache

        manifest_path = cls.get_episode_manifest_path()
        if not manifest_path.exists():
            return None

        try:
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest.get("by_stem"), dict):
                return None
            cls._episode_manifest_cache = manifest
            return manifest
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    async def ensure_episode_manifest(cls, *, force_refresh: bool = False) -> dict:
        """Ensure episode manifest exists, rebuilding if needed."""
        if not force_refresh:
            manifest = await asyncio.to_thread(cls._load_episode_manifest_sync)
            if manifest is not None:
                return manifest

        async with cls._get_manifest_lock():
            if not force_refresh:
                manifest = await asyncio.to_thread(cls._load_episode_manifest_sync)
                if manifest is not None:
                    return manifest
            return await asyncio.to_thread(cls._scan_library_episodes_sync)

    @classmethod
    def resolve_episode_path(cls, episode_name: str, manifest: dict | None = None) -> Path | None:
        """Resolve an episode path using cached manifest (no recursive scan)."""
        candidate = Path(episode_name)
        if candidate.is_absolute() and candidate.exists():
            return candidate

        library_path = cls.get_library_path()
        if candidate.suffix and not candidate.is_absolute():
            full = (library_path / candidate).resolve()
            if full.exists():
                return full

        manifest_data = manifest or cls._load_episode_manifest_sync()
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
    def list_episode_paths(cls, manifest: dict | None = None) -> list[str]:
        """Return known episode absolute paths from manifest."""
        manifest_data = manifest or cls._load_episode_manifest_sync()
        if manifest_data is None:
            return []
        return [p for p in manifest_data.get("episodes", []) if Path(p).exists()]

    @classmethod
    def _get_source_normalization_profile(cls) -> str:
        profile = str(settings.source_normalization_profile or "").strip().lower()
        return profile or cls.SOURCE_NORMALIZATION_PROFILE_H264_MP4_AAC

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
            mapped = cls._LANGUAGE_ALIASES.get(raw)
            if mapped:
                return mapped
            if "-" in raw:
                mapped = cls._LANGUAGE_ALIASES.get(raw.split("-", 1)[0])
                if mapped:
                    return mapped
            if "_" in raw:
                mapped = cls._LANGUAGE_ALIASES.get(raw.split("_", 1)[0])
                if mapped:
                    return mapped

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
        ):
            if needle.strip() in haystack:
                return normalized
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
        )

    @classmethod
    def select_preferred_audio_stream(
        cls,
        probe: SourceMediaProbe,
    ) -> SourceMediaStream | None:
        """Select the single audio stream kept in normalized sources."""
        if not probe.audio_streams:
            return None

        for preferred_lang in ("ja", "en"):
            for stream in probe.audio_streams:
                if stream.language == preferred_lang:
                    return stream
        return min(probe.audio_streams, key=lambda stream: stream.index)

    @classmethod
    def get_subtitle_sidecar_dir(cls, source_path: Path) -> Path:
        """Return the deterministic sidecar directory for one normalized source."""
        return source_path.with_name(f"{source_path.stem}{cls.SUBTITLE_SIDECAR_SUFFIX}")

    @classmethod
    def get_subtitle_sidecar_manifest_path(cls, source_path: Path) -> Path:
        return cls.get_subtitle_sidecar_dir(source_path) / "manifest.json"

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
                    "stream=index,codec_type,codec_name,pix_fmt,avg_frame_rate,r_frame_rate:"
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

        raw_duration = str(format_payload.get("duration", "")).strip()
        duration: float | None = None
        if raw_duration:
            try:
                parsed_duration = float(raw_duration)
            except ValueError:
                parsed_duration = 0.0
            duration = parsed_duration if parsed_duration > 0 else None

        fps = cls._parse_ffprobe_rate(video_stream.get("avg_frame_rate"))
        if fps is None:
            fps = cls._parse_ffprobe_rate(video_stream.get("r_frame_rate"))

        format_name = str(format_payload.get("format_name", "")).strip().lower() or None
        video_codec = str(video_stream.get("codec_name", "")).strip().lower() or None
        preferred_audio_stream = cls.select_preferred_audio_stream(
            SourceMediaProbe(
                source_path=source_path,
                container_suffix=source_path.suffix.lower(),
                format_name=format_name,
                video_codec=video_codec,
                audio_codec=None,
                pix_fmt=str(video_stream.get("pix_fmt", "")).strip().lower() or None,
                fps=fps,
                duration=duration,
                has_audio=bool(audio_streams),
                audio_streams=tuple(audio_streams),
                subtitle_streams=tuple(subtitle_streams),
                data_streams=tuple(data_streams),
            )
        )
        audio_codec = None
        selected_audio_stream_index = None
        if preferred_audio_stream is not None:
            audio_codec = preferred_audio_stream.codec_name
            selected_audio_stream_index = preferred_audio_stream.index

        return SourceMediaProbe(
            source_path=source_path,
            container_suffix=source_path.suffix.lower(),
            format_name=format_name,
            video_codec=video_codec,
            audio_codec=audio_codec,
            pix_fmt=str(video_stream.get("pix_fmt", "")).strip().lower() or None,
            fps=fps,
            duration=duration,
            has_audio=bool(audio_streams),
            audio_streams=tuple(audio_streams),
            subtitle_streams=tuple(subtitle_streams),
            data_streams=tuple(data_streams),
            selected_audio_stream_index=selected_audio_stream_index,
        )

    @classmethod
    def probe_source_media_sync(cls, source_path: Path) -> SourceMediaProbe | None:
        """Public sync probe wrapper used by normalization and tests."""
        return cls._probe_media_sync(source_path)

    @classmethod
    def classify_source_normalization(cls, probe: SourceMediaProbe) -> str:
        """Classify the cheapest compatibility action for Premiere-safe output."""
        profile = cls._get_source_normalization_profile()
        if profile != cls.SOURCE_NORMALIZATION_PROFILE_H264_MP4_AAC:
            raise RuntimeError(f"Unsupported source normalization profile: {profile}")

        video_codec = (probe.video_codec or "").strip().lower()
        audio_codec = (probe.audio_codec or "").strip().lower()
        has_extra_audio_streams = len(probe.audio_streams) > 1
        has_subtitle_streams = len(probe.subtitle_streams) > 0
        has_data_streams = len(probe.data_streams) > 0

        if (
            video_codec == "h264"
            and probe.container_suffix == ".mp4"
            and not has_extra_audio_streams
            and not has_subtitle_streams
            and not has_data_streams
            and (not probe.has_audio or audio_codec == "aac")
        ):
            return "noop"

        if video_codec != "h264":
            return "full_h264_aac_transcode"

        if probe.has_audio and audio_codec != "aac":
            return "audio_to_aac"

        return "remux_to_mp4"

    @staticmethod
    def _normalized_target_path(source_path: Path) -> Path:
        if source_path.suffix.lower() == ".mp4":
            return source_path
        return source_path.with_suffix(".mp4")

    @classmethod
    def _build_source_normalization_plan_sync(
        cls,
        source_path: Path,
    ) -> SourceNormalizationPlan:
        probe = cls._probe_media_sync(source_path)
        if probe is None:
            raise RuntimeError(f"Unable to probe source media: {source_path}")
        if probe.duration is None:
            raise RuntimeError(f"Unable to determine source duration: {source_path}")
        if probe.video_codec is None:
            raise RuntimeError(f"Unable to determine source video codec: {source_path}")
        return SourceNormalizationPlan(
            action=cls.classify_source_normalization(probe),
            source_path=source_path,
            target_path=cls._normalized_target_path(source_path),
            probe=probe,
        )

    @classmethod
    def _normalization_duration_tolerance_seconds(cls, probe: SourceMediaProbe) -> float:
        fps = probe.fps or 24.0
        return max(0.25, min(1.0, 3.0 / fps))

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
        if (normalized_probe.video_codec or "").strip().lower() != "h264":
            return False
        if normalized_probe.duration is None:
            return False
        if normalized_probe.subtitle_streams:
            return False
        if normalized_probe.data_streams:
            return False

        normalized_audio = (normalized_probe.audio_codec or "").strip().lower()
        if reference_probe.has_audio:
            if (
                not normalized_probe.has_audio
                or normalized_audio != "aac"
                or len(normalized_probe.audio_streams) != 1
            ):
                return False
        elif normalized_probe.has_audio:
            return False

        if reference_probe.duration is not None and normalized_probe.duration is not None:
            tolerance = cls._normalization_duration_tolerance_seconds(reference_probe)
            if abs(reference_probe.duration - normalized_probe.duration) > tolerance:
                return False

        return normalized_probe.source_path.exists() and normalized_probe.source_path.stat().st_size > 0

    @classmethod
    def _build_selected_audio_map_args(cls, probe: SourceMediaProbe) -> list[str]:
        if probe.selected_audio_stream_index is None:
            return []
        return ["-map", f"0:{probe.selected_audio_stream_index}"]

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
        ] + cls._build_audio_args(has_audio=probe.has_audio) + [str(output_path)]

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
        ] + cls._build_audio_args(has_audio=probe.has_audio) + [str(output_path)]

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
        return detail[:500]

    @classmethod
    async def _run_normalization_command(
        cls,
        cmd: list[str],
        *,
        timeout_seconds: float,
    ):
        return await run_command(cmd, timeout_seconds=timeout_seconds)

    @classmethod
    def get_subtitle_sidecar_asset_path(
        cls,
        source_path: Path,
        entry: SubtitleSidecarEntry,
    ) -> Path | None:
        if not entry.asset_filename:
            return None
        return cls.get_subtitle_sidecar_dir(source_path) / entry.asset_filename

    @classmethod
    def get_subtitle_sidecar_cue_manifest_path(
        cls,
        source_path: Path,
        entry: SubtitleSidecarEntry,
    ) -> Path | None:
        if not entry.cue_manifest_filename:
            return None
        return cls.get_subtitle_sidecar_dir(source_path) / entry.cue_manifest_filename

    @classmethod
    def load_subtitle_sidecar_entries(
        cls,
        source_path: Path,
    ) -> list[SubtitleSidecarEntry]:
        manifest_path = cls.get_subtitle_sidecar_manifest_path(source_path)
        if not manifest_path.exists():
            return []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

        raw_entries = payload.get("subtitle_streams", [])
        if not isinstance(raw_entries, list):
            return []

        entries: list[SubtitleSidecarEntry] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            try:
                entries.append(
                    SubtitleSidecarEntry(
                        stream_index=int(raw_entry.get("stream_index")),
                        stream_position=int(raw_entry.get("stream_position")),
                        codec_name=str(raw_entry.get("codec_name", "")).strip().lower() or None,
                        language=str(raw_entry.get("language", "")).strip().lower() or None,
                        raw_language=str(raw_entry.get("raw_language", "")).strip() or None,
                        title=str(raw_entry.get("title", "")).strip() or None,
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
        if "full" in title:
            return 0
        if "sign" in title or "song" in title:
            return 1
        return 2

    @classmethod
    def select_preferred_subtitle_entry(
        cls,
        entries: list[SubtitleSidecarEntry],
        *,
        target_language: str | None,
    ) -> SubtitleSidecarEntry | None:
        normalized_target = cls.normalize_stream_language(target_language)
        allowed_languages = [lang for lang in (normalized_target, "en") if lang]
        seen_languages: set[str] = set()
        ordered_languages: list[str] = []
        for language in allowed_languages:
            if language in seen_languages:
                continue
            seen_languages.add(language)
            ordered_languages.append(language)

        candidates = [
            entry
            for entry in entries
            if entry.status == "ok"
            and entry.kind in {"text", "image"}
            and entry.language in ordered_languages
            and entry.asset_filename
        ]
        if not candidates:
            return None

        def _rank(entry: SubtitleSidecarEntry) -> tuple[int, int, int, int]:
            language_rank = ordered_languages.index(entry.language or "en")
            kind_rank = 0 if entry.kind == "text" else 1
            english_title_rank = (
                cls._subtitle_entry_title_rank(entry) if entry.language == "en" else 0
            )
            return (
                language_rank,
                kind_rank,
                english_title_rank,
                entry.stream_position,
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
    ) -> None:
        if not probe.subtitle_streams:
            return

        sidecar_dir = cls.get_subtitle_sidecar_dir(normalized_target_path)
        tmp_dir = sidecar_dir.with_name(f"{sidecar_dir.name}.tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        manifest_entries: list[dict[str, Any]] = []
        for stream in probe.subtitle_streams:
            kind = cls._subtitle_kind_for_codec(stream.codec_name)
            entry_payload: dict[str, Any] = {
                "stream_index": stream.index,
                "stream_position": stream.stream_position,
                "codec_name": stream.codec_name,
                "language": stream.language,
                "raw_language": stream.raw_language,
                "title": stream.title,
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
                        cue_dir_name = asset_path.stem + "_cues"
                        cue_dir = tmp_dir / cue_dir_name
                        cue_dir.mkdir(parents=True, exist_ok=True)
                        cue_manifest_name = asset_path.stem + ".cues.json"
                        cue_manifest_entries: list[dict[str, Any]] = []
                        for cue_idx, cue in enumerate(cues, start=1):
                            cue_png_name = f"cue_{cue_idx:04d}.png"
                            cue_png_path = cue_dir / cue_png_name
                            rendered = await cls._render_pgs_cue_png_from_source(
                                source_path=source_path,
                                stream_position=stream.stream_position,
                                cue_start=float(cue["start"]),
                                cue_end=float(cue["end"]),
                                output_path=cue_png_path,
                            )
                            if not rendered:
                                continue
                            cue_manifest_entries.append(
                                {
                                    "start": float(cue["start"]),
                                    "end": float(cue["end"]),
                                    "asset_filename": f"{cue_dir_name}/{cue_png_name}",
                                }
                            )
                        if cue_manifest_entries:
                            cue_manifest_path = tmp_dir / cue_manifest_name
                            cue_manifest_path.write_text(
                                json.dumps({"cues": cue_manifest_entries}, indent=2),
                                encoding="utf-8",
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
        )

    @classmethod
    def _series_name_for_library_path(cls, source_path: Path) -> str | None:
        library_path = cls.get_library_path()
        try:
            rel = source_path.resolve().relative_to(library_path.resolve())
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) < 2:
            return None
        return parts[0]

    @classmethod
    async def _postprocess_source_normalization_commit(cls, normalized_path: Path) -> None:
        series_name = cls._series_name_for_library_path(normalized_path)
        if series_name is None:
            return
        await cls.ensure_episode_manifest(force_refresh=True)
        from .anime_matcher import AnimeMatcherService

        AnimeMatcherService.mark_series_updated(series_name)

    @classmethod
    async def normalize_source_for_processing(
        cls,
        source_path: Path,
    ) -> SourceNormalizationResult:
        """Normalize one source episode to Premiere-safe H.264 MP4 + AAC when needed."""
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        plan = await asyncio.to_thread(cls._build_source_normalization_plan_sync, source_path)
        if plan.action == "noop":
            return SourceNormalizationResult(
                action=plan.action,
                source_path=plan.source_path,
                normalized_path=plan.source_path,
                changed=False,
            )

        target_path = plan.target_path
        tmp_path = target_path.with_name(f"{target_path.stem}.normalize.tmp.mp4")

        if target_path != source_path and target_path.exists():
            existing_probe = await asyncio.to_thread(cls._probe_media_sync, target_path)
            if cls._is_valid_normalized_probe(existing_probe, reference_probe=plan.probe):
                try:
                    await cls._write_subtitle_sidecar(
                        source_path=plan.source_path,
                        normalized_target_path=target_path,
                        probe=plan.probe,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to preserve subtitle sidecar for %s: %s",
                        plan.source_path,
                        exc,
                    )
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
            elif plan.action == "audio_to_aac":
                try:
                    result = await cls._run_normalization_command(
                        cls._build_audio_to_aac_cmd(
                            plan.source_path,
                            tmp_path,
                            probe=plan.probe,
                        ),
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
                        gpu_error = None
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

            try:
                await cls._write_subtitle_sidecar(
                    source_path=plan.source_path,
                    normalized_target_path=target_path,
                    probe=plan.probe,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to preserve subtitle sidecar for %s: %s",
                    plan.source_path,
                    exc,
                )
            await asyncio.to_thread(tmp_path.replace, target_path)
            if target_path != source_path and source_path.exists():
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
    async def list_indexed_anime(cls) -> list[str]:
        """
        List all indexed anime in the library.

        Returns:
            Sorted list of anime series names.
        """
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        # Call anime_searcher directly via pixi to avoid task-shell quoting issues.
        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "list", str(library_path), "--json",
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
    async def index_anime(
        cls,
        source_folder: Path,
        anime_name: str | None = None,
        fps: float = 2.0,
        batch_size: int = 64,
        prefetch_batches: int = 3,
        transform_workers: int = 4,
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
            require_gpu: Fail if CUDA is unavailable.

        Yields:
            Progress updates during copying and indexing.
        """
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        # Ensure library directory exists
        library_path.mkdir(parents=True, exist_ok=True)

        # Determine anime name
        if anime_name is None:
            anime_name = source_folder.name

        requested_fps = fps
        effective_fps = fps
        existing_series_fps = await asyncio.to_thread(
            cls._get_indexed_series_fps_sync,
            anime_name,
        )
        is_existing_series = existing_series_fps is not None
        if existing_series_fps is not None:
            effective_fps = existing_series_fps

        dest_path = library_path / anime_name

        yield IndexProgress(
            status="starting",
            message=f"Preparing to index {anime_name}",
        )

        # Check if source folder exists
        if not source_folder.exists():
            yield IndexProgress(
                status="error",
                error=f"Source folder not found: {source_folder}",
            )
            return

        # Count video files for progress
        def _collect_video_files() -> list[Path]:
            return [
                f for f in source_folder.iterdir()
                if f.is_file() and f.suffix.lower() in cls.VIDEO_EXTENSIONS
            ]

        video_files = await asyncio.to_thread(_collect_video_files)

        if not video_files:
            yield IndexProgress(
                status="error",
                error=f"No video files found in {source_folder}",
            )
            return

        total_files = len(video_files)
        prepared_files: list[Path] = []

        if dest_path != source_folder:
            yield IndexProgress(
                status="copying",
                message=f"Preparing {total_files} files for library import",
                total_files=total_files,
            )
            dest_path.mkdir(parents=True, exist_ok=True)
        else:
            yield IndexProgress(
                status="copying",
                message=f"Normalizing {total_files} files in library",
                total_files=total_files,
            )

        # Copy or remux each file depending on the source container/codec.
        for i, video_file in enumerate(video_files):
            source_codec = await asyncio.to_thread(cls.get_primary_video_codec_sync, video_file)
            is_av1 = source_codec == "av1"
            should_remux_mkv = video_file.suffix.lower() == ".mkv" and not is_av1
            preferred_dest = dest_path / (
                video_file.stem + ".mp4" if should_remux_mkv else video_file.name
            )
            actual_dest = preferred_dest

            existing_ready = preferred_dest.exists()

            action = "Copying"
            if is_av1:
                action = "Copying AV1 source"
            elif should_remux_mkv:
                action = "Remuxing"

            if not existing_ready:
                yield IndexProgress(
                    status="copying",
                    message=f"{action} {video_file.name}",
                    progress=(i + 0.5) / total_files * 0.3,
                    current_file=video_file.name,
                    total_files=total_files,
                    completed_files=i,
                )

                if should_remux_mkv:
                    try:
                        remux_result = await run_command(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(video_file),
                                "-c",
                                "copy",
                                "-movflags",
                                "+faststart",
                                str(preferred_dest),
                            ],
                            timeout_seconds=cls.REMUX_TIMEOUT_SECONDS,
                        )
                        if remux_result.returncode != 0:
                            import sys
                            print(
                                f"[WARNING] ffmpeg remux failed for {video_file.name}: "
                                f"{remux_result.stderr.decode()[:200]}",
                                file=sys.stderr,
                            )
                            fallback_dest = dest_path / video_file.name
                            if fallback_dest != video_file and not fallback_dest.exists():
                                await asyncio.to_thread(shutil.copy2, video_file, fallback_dest)
                            actual_dest = fallback_dest
                    except CommandTimeoutError:
                        import sys
                        print(
                            f"[WARNING] ffmpeg remux timed out for {video_file.name}, "
                            "falling back to copy",
                            file=sys.stderr,
                        )
                        fallback_dest = dest_path / video_file.name
                        if fallback_dest != video_file and not fallback_dest.exists():
                            await asyncio.to_thread(shutil.copy2, video_file, fallback_dest)
                        actual_dest = fallback_dest
                    except FileNotFoundError as exc:
                        if is_media_binary_override_error(exc):
                            raise
                        import sys
                        print("[WARNING] ffmpeg not found, falling back to copy", file=sys.stderr)
                        fallback_dest = dest_path / video_file.name
                        if fallback_dest != video_file and not fallback_dest.exists():
                            await asyncio.to_thread(shutil.copy2, video_file, fallback_dest)
                        actual_dest = fallback_dest
                elif preferred_dest != video_file:
                    await asyncio.to_thread(shutil.copy2, video_file, preferred_dest)
            else:
                action = "Using existing"

            # When normalizing in-place, drop old non-MP4 source if replacement succeeded.
            if (
                dest_path == source_folder
                and actual_dest == preferred_dest
                and preferred_dest != video_file
                and preferred_dest.exists()
            ):
                with suppress(OSError):
                    await asyncio.to_thread(video_file.unlink)

            if actual_dest not in prepared_files:
                prepared_files.append(actual_dest)

            yield IndexProgress(
                status="copying",
                message=f"{action} {video_file.name}",
                progress=(i + 1) / total_files * 0.3,
                current_file=video_file.name,
                total_files=total_files,
                completed_files=i + 1,
            )

        # Verify all prepared files are accessible and complete before indexing.
        yield IndexProgress(
            status="copying",
            message="Verifying prepared files before indexing...",
            progress=0.3,
            total_files=total_files,
            completed_files=total_files,
        )

        # Small delay to ensure filesystem has flushed all writes.
        await asyncio.sleep(1.0)

        for dest_file in prepared_files:
            if not dest_file.exists():
                yield IndexProgress(
                    status="error",
                    error=f"Prepared file missing after import: {dest_file.name}",
                )
                return

            file_stat = await asyncio.to_thread(dest_file.stat)
            if file_stat.st_size == 0:
                yield IndexProgress(
                    status="error",
                    error=f"Prepared file is empty: {dest_file.name}",
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
            )

        # Run indexing with pixi run
        yield IndexProgress(
            status="indexing",
            message=f"Indexing {anime_name} at {effective_fps:g} fps",
            progress=0.35,
            total_files=total_files,
        )

        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "index", str(library_path),
            "--fps", str(effective_fps),
            "--series", anime_name,
            "--batch-size", str(batch_size),
            "--prefetch-batches", str(prefetch_batches),
            "--transform-workers", str(transform_workers),
        ]
        if require_gpu:
            cmd.append("--require-gpu")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(searcher_path),
            env=get_media_subprocess_env(cmd),
        )

        # Read output progressively
        stdout_lines = []
        stderr_task = asyncio.create_task(
            process.stderr.read() if process.stderr is not None else asyncio.sleep(0, result=b"")
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cls.INDEX_TIMEOUT_SECONDS
        aborted = False

        try:
            assert process.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError

                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                if not line:
                    break
                stdout_lines.append(line.decode())

                # Try to parse progress from output
                line_str = line.decode().strip()
                if line_str:
                    # Estimate progress based on output
                    yield IndexProgress(
                        status="indexing",
                        message=line_str[:100],  # Truncate long messages
                        progress=0.35 + 0.60 * (len(stdout_lines) / (total_files * 100)),  # Rough estimate
                        total_files=total_files,
                    )

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)
        except asyncio.CancelledError:
            aborted = True
            await terminate_process(process)
            raise
        except asyncio.TimeoutError:
            aborted = True
            await terminate_process(process)
            yield IndexProgress(
                status="error",
                error=(
                    f"Indexing timed out after {int(cls.INDEX_TIMEOUT_SECONDS)} seconds. "
                    "Try reducing library size or retrying."
                ),
            )
            return
        finally:
            if aborted and not stderr_task.done():
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task

        stderr = await stderr_task
        if process.returncode != 0:
            yield IndexProgress(
                status="error",
                error=f"Indexing failed: {stderr.decode()}",
            )
            return

        await cls.ensure_episode_manifest(force_refresh=True)

        yield IndexProgress(
            status="complete",
            message=f"Successfully indexed {anime_name}",
            progress=1.0,
            total_files=total_files,
            completed_files=total_files,
        )

    @classmethod
    async def search_frame(
        cls,
        image_path: Path,
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
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "search", str(image_path),
            "--library", str(library_path),
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
