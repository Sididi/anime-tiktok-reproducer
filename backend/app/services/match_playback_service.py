from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal

from ..config import settings
from ..models import MatchList, Project, SceneList
from ..utils.media_binaries import get_media_subprocess_env, rewrite_media_command
from .anime_library import AnimeLibraryService
from .project_service import ProjectService

ClipTrack = Literal["tiktok", "source"]


@dataclass
class PlaybackPrepareProgress:
    status: str
    progress: float
    message: str
    scene_index: int | None = None
    total_scenes: int | None = None
    error: str | None = None
    manifest: dict | None = None
    cached: bool = False
    track: ClipTrack | None = None
    scene_asset: dict | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "scene_index": self.scene_index,
            "total_scenes": self.total_scenes,
            "error": self.error,
            "manifest": self.manifest,
            "cached": self.cached,
            "track": self.track,
            "scene_asset": self.scene_asset,
        }


@dataclass(frozen=True)
class _ClipProfile:
    key: str
    width: int
    height: int
    fps: int
    crf: int


@dataclass
class _ClipPlan:
    scene_index: int
    track: ClipTrack
    input_path: Path
    start_time: float
    end_time: float
    profile: str
    clip_id: str
    source_key: str | None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end_time) - float(self.start_time))


@dataclass
class _ScenePlan:
    scene_index: int
    has_match: bool
    tiktok: _ClipPlan
    source: _ClipPlan | None


@dataclass
class _ClipJobResult:
    plan: _ClipPlan
    encoded: bool
    duration: float | None
    error: str | None = None


class MatchPlaybackService:
    """Prepare and serve browser-safe clips for /matches playback."""

    CACHE_DIR_NAME = "playback_cache_v3"
    ACTIVE_STATE_FILE = "active.json"
    MANIFESTS_DIR = "manifests"
    CLIP_STORE_DIR = "clip_store"
    MANIFEST_VERSION = "v3"
    ENCODE_PROFILE_VERSION = "v3|max_speed_profiles"
    FFMPEG_TIMEOUT_SECONDS = 300
    CLIP_ID_TIME_PRECISION = 3
    ULTRA_LONG_SOURCE_SECONDS = 60.0
    CLIP_STORE_MAX_BYTES = 8 * 1024 * 1024 * 1024
    CLIP_STORE_STALE_SECONDS = 7 * 24 * 3600
    _prepare_locks: dict[str, asyncio.Lock] = {}
    _nvenc_checked = False
    _nvenc_available = False

    _PROFILE_MAP: dict[str, _ClipProfile] = {
        "tiktok_fast": _ClipProfile(
            key="tiktok_fast",
            width=540,
            height=960,
            fps=24,
            crf=28,
        ),
        "source_fast": _ClipProfile(
            key="source_fast",
            width=640,
            height=360,
            fps=20,
            crf=30,
        ),
        "source_ultra_long": _ClipProfile(
            key="source_ultra_long",
            width=426,
            height=240,
            fps=12,
            crf=34,
        ),
    }

    @classmethod
    def _cache_root(cls, project_id: str) -> Path:
        return ProjectService.get_project_dir(project_id) / cls.CACHE_DIR_NAME

    @classmethod
    def _manifests_dir(cls, project_id: str) -> Path:
        return cls._cache_root(project_id) / cls.MANIFESTS_DIR

    @classmethod
    def _clip_store_dir(cls, project_id: str) -> Path:
        return cls._cache_root(project_id) / cls.CLIP_STORE_DIR

    @classmethod
    def _state_file(cls, project_id: str) -> Path:
        return cls._cache_root(project_id) / cls.ACTIVE_STATE_FILE

    @classmethod
    def _manifest_file(cls, project_id: str, fingerprint: str) -> Path:
        return cls._manifests_dir(project_id) / f"{fingerprint}.json"

    @classmethod
    def _clip_file(cls, project_id: str, clip_id: str) -> Path:
        return cls._clip_store_dir(project_id) / f"{clip_id}.mp4"

    @classmethod
    def _clip_meta_file(cls, project_id: str, clip_id: str) -> Path:
        return cls._clip_store_dir(project_id) / f"{clip_id}.json"

    @classmethod
    def _get_prepare_lock(cls, project_id: str) -> asyncio.Lock:
        lock = cls._prepare_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._prepare_locks[project_id] = lock
        return lock

    @classmethod
    def is_prepare_running(cls, project_id: str) -> bool:
        """Return whether a playback prepare job is currently running."""
        return cls._get_prepare_lock(project_id).locked()

    @staticmethod
    def _dump_json_stable(payload: object) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _looks_like_fingerprint(value: str) -> bool:
        if len(value) != 40:
            return False
        return all(ch in "0123456789abcdef" for ch in value)

    @classmethod
    def _is_path_allowed(cls, path: Path, source_dirs: list[Path]) -> bool:
        for src_path in source_dirs:
            if src_path.is_dir():
                if cls._is_under(path, src_path):
                    return True
            elif path.resolve() == src_path.resolve():
                return True
        return False

    @staticmethod
    def _search_episode_sync(
        episode_name: str,
        source_dirs: list[Path],
        video_extensions: set[str],
    ) -> Path | None:
        for src_path in source_dirs:
            if src_path.is_dir():
                for ext in video_extensions:
                    candidate = src_path / f"{episode_name}{ext}"
                    if candidate.exists():
                        return candidate
                for ext in video_extensions:
                    for match in src_path.rglob(f"*{ext}"):
                        if match.stem == episode_name:
                            return match
            elif src_path.is_file() and src_path.stem == episode_name:
                return src_path
        return None

    @classmethod
    async def _resolve_episode_path(
        cls,
        project: Project,
        episode_value: str,
    ) -> Path:
        decoded_path = episode_value
        source_path = Path(decoded_path)
        video_extensions = {".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v"}

        source_dirs: list[Path] = []
        if project.source_paths:
            source_dirs = [Path(src) for src in project.source_paths]
        elif settings.anime_library_path and settings.anime_library_path.exists():
            source_dirs = [settings.anime_library_path]

        if not source_dirs:
            raise RuntimeError("No source paths configured for project")

        if (
            source_path.is_absolute()
            and source_path.exists()
            and cls._is_path_allowed(source_path, source_dirs)
        ):
            return source_path

        found_path: Path | None = None
        if settings.anime_library_path and settings.anime_library_path.exists():
            manifest = await AnimeLibraryService.ensure_episode_manifest()
            candidate = AnimeLibraryService.resolve_episode_path(decoded_path, manifest)
            if candidate and cls._is_path_allowed(candidate, source_dirs):
                found_path = candidate

        if found_path is None:
            found_path = await asyncio.to_thread(
                cls._search_episode_sync,
                decoded_path,
                source_dirs,
                video_extensions,
            )

        if found_path is None or not found_path.exists():
            raise RuntimeError(f"Source episode not found: {decoded_path}")

        return found_path

    @classmethod
    def _load_active_fingerprint(cls, project_id: str) -> str | None:
        state_file = cls._state_file(project_id)
        if not state_file.exists():
            return None

        try:
            payload = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

        fingerprint = payload.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            return fingerprint
        return None

    @classmethod
    def _save_active_fingerprint(cls, project_id: str, fingerprint: str) -> None:
        cache_root = cls._cache_root(project_id)
        cache_root.mkdir(parents=True, exist_ok=True)
        cls._state_file(project_id).write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    @classmethod
    def _build_fingerprint(
        cls,
        project: Project,
        scenes: SceneList,
        matches: MatchList,
        source_by_episode: dict[str, Path],
    ) -> str:
        hasher = hashlib.sha1()
        hasher.update(f"{cls.MANIFEST_VERSION}|{cls.ENCODE_PROFILE_VERSION}".encode("utf-8"))

        scenes_payload = [scene.model_dump() for scene in scenes.scenes]
        matches_payload = [match.model_dump() for match in matches.matches]
        hasher.update(cls._dump_json_stable(scenes_payload).encode("utf-8"))
        hasher.update(cls._dump_json_stable(matches_payload).encode("utf-8"))

        if not project.video_path:
            raise RuntimeError("Project has no TikTok video path")
        video_path = Path(project.video_path)
        if not video_path.exists():
            raise RuntimeError("Project TikTok video file does not exist")

        video_stat = video_path.stat()
        hasher.update(
            cls._dump_json_stable(
                {
                    "path": str(video_path.resolve()),
                    "size": video_stat.st_size,
                    "mtime_ns": video_stat.st_mtime_ns,
                }
            ).encode("utf-8")
        )

        source_meta: list[dict[str, object]] = []
        for episode, source_path in sorted(source_by_episode.items()):
            stat = source_path.stat()
            source_meta.append(
                {
                    "episode": episode,
                    "path": str(source_path.resolve()),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
        hasher.update(cls._dump_json_stable(source_meta).encode("utf-8"))

        return hasher.hexdigest()

    @classmethod
    def _profile_for_plan(cls, track: ClipTrack, clip_duration: float) -> str:
        if track == "tiktok":
            return "tiktok_fast"
        if clip_duration > cls.ULTRA_LONG_SOURCE_SECONDS:
            return "source_ultra_long"
        return "source_fast"

    @classmethod
    def _build_clip_id(
        cls,
        *,
        input_path: Path,
        start_time: float,
        end_time: float,
        track: ClipTrack,
        profile: str,
    ) -> str:
        stat = input_path.stat()
        payload = {
            "version": cls.ENCODE_PROFILE_VERSION,
            "track": track,
            "profile": profile,
            "input_path": str(input_path.resolve()),
            "input_size": stat.st_size,
            "input_mtime_ns": stat.st_mtime_ns,
            "start_time": round(float(start_time), cls.CLIP_ID_TIME_PRECISION),
            "end_time": round(float(end_time), cls.CLIP_ID_TIME_PRECISION),
        }
        return hashlib.sha1(cls._dump_json_stable(payload).encode("utf-8")).hexdigest()

    @classmethod
    def _build_scene_plans(
        cls,
        *,
        project_video: Path,
        scenes: SceneList,
        matches: MatchList,
        source_by_episode: dict[str, Path],
    ) -> list[_ScenePlan]:
        valid_matches = {
            match.scene_index: match
            for match in matches.matches
            if match.confidence > 0 and bool(match.episode)
        }

        plans: list[_ScenePlan] = []
        for scene in scenes.scenes:
            tiktok_profile = cls._profile_for_plan("tiktok", scene.duration)
            tiktok_clip_id = cls._build_clip_id(
                input_path=project_video,
                start_time=scene.start_time,
                end_time=scene.end_time,
                track="tiktok",
                profile=tiktok_profile,
            )
            tiktok_plan = _ClipPlan(
                scene_index=scene.index,
                track="tiktok",
                input_path=project_video,
                start_time=float(scene.start_time),
                end_time=float(scene.end_time),
                profile=tiktok_profile,
                clip_id=tiktok_clip_id,
                source_key=None,
            )

            source_plan: _ClipPlan | None = None
            match = valid_matches.get(scene.index)
            if match is not None:
                source_input = source_by_episode.get(match.episode)
                if source_input is None:
                    raise RuntimeError(f"Missing source path for episode: {match.episode}")

                source_duration = float(match.end_time) - float(match.start_time)
                source_profile = cls._profile_for_plan("source", source_duration)
                source_clip_id = cls._build_clip_id(
                    input_path=source_input,
                    start_time=match.start_time,
                    end_time=match.end_time,
                    track="source",
                    profile=source_profile,
                )
                source_plan = _ClipPlan(
                    scene_index=scene.index,
                    track="source",
                    input_path=source_input,
                    start_time=float(match.start_time),
                    end_time=float(match.end_time),
                    profile=source_profile,
                    clip_id=source_clip_id,
                    source_key=match.episode,
                )

            plans.append(
                _ScenePlan(
                    scene_index=scene.index,
                    has_match=source_plan is not None,
                    tiktok=tiktok_plan,
                    source=source_plan,
                )
            )

        return plans

    @staticmethod
    def _probe_clip_sync(path: Path) -> dict:
        cmd = rewrite_media_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,pix_fmt",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ]
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=get_media_subprocess_env(cmd),
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed for {path.name}: {result.stderr.strip()}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid ffprobe output for {path.name}") from exc

    @classmethod
    def _validate_clip_sync(cls, path: Path) -> float:
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"Clip not created: {path}")

        payload = cls._probe_clip_sync(path)
        streams = payload.get("streams") or []
        if not streams:
            raise RuntimeError(f"No video stream in clip: {path.name}")

        stream0 = streams[0]
        codec = str(stream0.get("codec_name", "")).lower()
        pix_fmt = str(stream0.get("pix_fmt", "")).lower()
        if codec != "h264":
            raise RuntimeError(f"Unexpected codec for {path.name}: {codec}")
        if pix_fmt not in {"yuv420p", "yuvj420p"}:
            raise RuntimeError(f"Unexpected pixel format for {path.name}: {pix_fmt}")

        duration_raw = (payload.get("format") or {}).get("duration")
        try:
            duration = float(duration_raw)
        except (TypeError, ValueError):
            duration = 0.0

        if duration <= 0.01:
            raise RuntimeError(f"Clip duration too short for {path.name}: {duration}")
        return duration

    @classmethod
    def _write_clip_meta_sync(
        cls,
        project_id: str,
        *,
        clip_id: str,
        duration: float,
        profile: str,
    ) -> None:
        meta_path = cls._clip_meta_file(project_id, clip_id)
        meta_path.write_text(
            json.dumps(
                {
                    "clip_id": clip_id,
                    "duration": duration,
                    "profile": profile,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
        )

    @classmethod
    def _read_clip_meta_sync(cls, project_id: str, clip_id: str) -> float | None:
        meta_path = cls._clip_meta_file(project_id, clip_id)
        if not meta_path.exists():
            return None

        try:
            payload = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

        raw = payload.get("duration")
        if not isinstance(raw, (float, int)):
            return None
        duration = float(raw)
        return duration if duration > 0 else None

    @classmethod
    def _get_clip_duration_sync(cls, project_id: str, clip_id: str) -> float:
        clip_path = cls._clip_file(project_id, clip_id)
        if not clip_path.exists() or clip_path.stat().st_size == 0:
            raise RuntimeError(f"Clip file missing: {clip_id}")

        cached_duration = cls._read_clip_meta_sync(project_id, clip_id)
        if cached_duration is not None:
            return cached_duration

        duration = cls._validate_clip_sync(clip_path)
        cls._write_clip_meta_sync(
            project_id,
            clip_id=clip_id,
            duration=duration,
            profile="unknown",
        )
        return duration

    @classmethod
    def _is_nvenc_available_sync(cls) -> bool:
        if cls._nvenc_checked:
            return cls._nvenc_available

        cls._nvenc_checked = True
        cmd = rewrite_media_command(["ffmpeg", "-hide_banner", "-encoders"])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env=get_media_subprocess_env(cmd),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            cls._nvenc_available = False
            return False

        cls._nvenc_available = (
            result.returncode == 0 and "h264_nvenc" in result.stdout
        )
        return cls._nvenc_available

    @classmethod
    def _build_nvenc_command_sync(
        cls,
        *,
        plan: _ClipPlan,
        profile: _ClipProfile,
        duration: float,
        vf: str,
        output_path: Path,
    ) -> list[str]:
        return rewrite_media_command(
            [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{plan.start_time:.6f}",
            "-i",
            str(plan.input_path),
            "-t",
            f"{duration:.6f}",
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            vf,
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "constqp",
            "-qp",
            str(profile.crf),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
            ]
        )

    @classmethod
    def _build_cpu_command_sync(
        cls,
        *,
        plan: _ClipPlan,
        profile: _ClipProfile,
        duration: float,
        vf: str,
        output_path: Path,
    ) -> list[str]:
        return rewrite_media_command(
            [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{plan.start_time:.6f}",
            "-i",
            str(plan.input_path),
            "-t",
            f"{duration:.6f}",
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            str(profile.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
            ]
        )

    @classmethod
    def _encode_clip_sync(
        cls,
        *,
        project_id: str,
        plan: _ClipPlan,
    ) -> float:
        if plan.end_time <= plan.start_time:
            raise RuntimeError("Invalid clip timing: end_time <= start_time")

        profile = cls._PROFILE_MAP[plan.profile]
        output_path = cls._clip_file(project_id, plan.clip_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        duration = plan.end_time - plan.start_time
        tmp_path = output_path.with_suffix(".tmp.mp4")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

        vf = (
            f"scale=w={profile.width}:h={profile.height}:"
            f"force_original_aspect_ratio=decrease,fps={profile.fps}"
        )

        error_details: list[str] = []
        encoded = False

        if cls._is_nvenc_available_sync():
            nvenc_cmd = cls._build_nvenc_command_sync(
                plan=plan,
                profile=profile,
                duration=duration,
                vf=vf,
                output_path=tmp_path,
            )
            result = subprocess.run(
                nvenc_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=cls.FFMPEG_TIMEOUT_SECONDS,
                env=get_media_subprocess_env(nvenc_cmd),
            )
            if result.returncode == 0:
                encoded = True
            else:
                error_details.append(
                    f"nvenc: {result.stderr.strip() or 'unknown error'}"
                )
                tmp_path.unlink(missing_ok=True)

        if not encoded:
            cpu_cmd = cls._build_cpu_command_sync(
                plan=plan,
                profile=profile,
                duration=duration,
                vf=vf,
                output_path=tmp_path,
            )
            result = subprocess.run(
                cpu_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=cls.FFMPEG_TIMEOUT_SECONDS,
                env=get_media_subprocess_env(cpu_cmd),
            )
            if result.returncode != 0:
                error_details.append(
                    f"cpu: {result.stderr.strip() or 'unknown error'}"
                )
                raise RuntimeError(
                    f"ffmpeg failed for {plan.clip_id}: {' | '.join(error_details)}"
                )

        tmp_path.replace(output_path)
        validated_duration = cls._validate_clip_sync(output_path)
        cls._write_clip_meta_sync(
            project_id,
            clip_id=plan.clip_id,
            duration=validated_duration,
            profile=plan.profile,
        )
        return validated_duration

    @classmethod
    def _build_clip_url(
        cls,
        *,
        project_id: str,
        scene_index: int,
        track: ClipTrack,
        fingerprint: str,
    ) -> str:
        return (
            f"/api/projects/{project_id}/matches/playback/clip/{scene_index}/{track}"
            f"?fingerprint={fingerprint}"
        )

    @classmethod
    def _default_manifest(cls) -> dict:
        return {
            "ready": False,
            "manifest_version": cls.MANIFEST_VERSION,
            "fingerprint": None,
            "generated_at": None,
            "encode_profile": cls.ENCODE_PROFILE_VERSION,
            "clip_store_stats": {
                "reused_count": 0,
                "encoded_count": 0,
                "total_clips": 0,
            },
            "scene_status": {},
            "scenes": [],
        }

    @classmethod
    def _load_manifest_sync(cls, project_id: str, fingerprint: str) -> dict | None:
        manifest_path = cls._manifest_file(project_id, fingerprint)
        if not manifest_path.exists():
            return None

        try:
            return json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    @classmethod
    def _save_manifest_sync(cls, project_id: str, fingerprint: str, manifest: dict) -> None:
        manifests_dir = cls._manifests_dir(project_id)
        manifests_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = cls._manifest_file(project_id, fingerprint)
        manifest_path.write_text(json.dumps(manifest, indent=2))

    @classmethod
    def _validate_manifest_sync(cls, project_id: str, manifest: dict | None) -> bool:
        if not manifest:
            return False
        if not manifest.get("ready"):
            return False
        scenes = manifest.get("scenes")
        if not isinstance(scenes, list):
            return False

        for scene_entry in scenes:
            if not isinstance(scene_entry, dict):
                return False
            for track_name in ("tiktok", "source"):
                clip_entry = scene_entry.get(track_name)
                if not clip_entry:
                    continue
                clip_id = clip_entry.get("clip_id")
                if not isinstance(clip_id, str) or not clip_id:
                    return False
                clip_path = cls._clip_file(project_id, clip_id)
                if not clip_path.exists() or clip_path.stat().st_size == 0:
                    return False
        return True

    @classmethod
    def _collect_clip_ids_from_manifest(cls, manifest: dict | None) -> set[str]:
        if not manifest or not isinstance(manifest.get("scenes"), list):
            return set()

        clip_ids: set[str] = set()
        for scene_entry in manifest["scenes"]:
            if not isinstance(scene_entry, dict):
                continue
            for track_name in ("tiktok", "source"):
                clip_entry = scene_entry.get(track_name)
                if not isinstance(clip_entry, dict):
                    continue
                clip_id = clip_entry.get("clip_id")
                if isinstance(clip_id, str) and clip_id:
                    clip_ids.add(clip_id)
        return clip_ids

    @classmethod
    def _gc_cache_sync(cls, project_id: str, keep_fingerprints: set[str]) -> None:
        manifests_dir = cls._manifests_dir(project_id)
        clip_dir = cls._clip_store_dir(project_id)
        if not manifests_dir.exists() or not clip_dir.exists():
            return

        manifest_files = sorted(
            manifests_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )

        # Keep active/current plus one recent manifest for safer transition.
        keep_manifest_names: set[str] = {f"{fingerprint}.json" for fingerprint in keep_fingerprints}
        for path in manifest_files[:1]:
            keep_manifest_names.add(path.name)

        for path in manifest_files:
            if path.name in keep_manifest_names:
                continue
            path.unlink(missing_ok=True)

        referenced_clip_ids: set[str] = set()
        active_clip_ids: set[str] = set()
        for path in manifests_dir.glob("*.json"):
            try:
                manifest = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            manifest_clip_ids = cls._collect_clip_ids_from_manifest(manifest)
            referenced_clip_ids.update(manifest_clip_ids)
            if path.stem in keep_fingerprints:
                active_clip_ids.update(manifest_clip_ids)

        for clip_path in clip_dir.glob("*.mp4"):
            clip_id = clip_path.stem
            if clip_id in referenced_clip_ids:
                continue
            clip_path.unlink(missing_ok=True)
            meta_path = cls._clip_meta_file(project_id, clip_id)
            meta_path.unlink(missing_ok=True)

        remaining_entries: list[tuple[Path, os.stat_result]] = []
        total_size = 0
        for clip_path in clip_dir.glob("*.mp4"):
            try:
                stat = clip_path.stat()
            except OSError:
                continue
            remaining_entries.append((clip_path, stat))
            total_size += stat.st_size

        stale_cutoff = datetime.now(timezone.utc).timestamp() - cls.CLIP_STORE_STALE_SECONDS
        for clip_path, stat in list(remaining_entries):
            clip_id = clip_path.stem
            atime = stat.st_atime if stat.st_atime > 0 else stat.st_mtime
            if clip_id in active_clip_ids or atime >= stale_cutoff:
                continue
            clip_path.unlink(missing_ok=True)
            cls._clip_meta_file(project_id, clip_id).unlink(missing_ok=True)

        remaining_entries = []
        total_size = 0
        for clip_path in clip_dir.glob("*.mp4"):
            try:
                stat = clip_path.stat()
            except OSError:
                continue
            remaining_entries.append((clip_path, stat))
            total_size += stat.st_size

        if total_size <= cls.CLIP_STORE_MAX_BYTES:
            return

        # Evict least-recently-used non-active clips if cache exceeds cap.
        evictable_entries = [
            (clip_path, stat)
            for clip_path, stat in remaining_entries
            if clip_path.stem not in active_clip_ids
        ]
        evictable_entries.sort(key=lambda item: item[1].st_atime)
        for clip_path, stat in evictable_entries:
            if total_size <= cls.CLIP_STORE_MAX_BYTES:
                break
            clip_path.unlink(missing_ok=True)
            cls._clip_meta_file(project_id, clip_path.stem).unlink(missing_ok=True)
            total_size -= stat.st_size

    @classmethod
    async def _run_clip_jobs(
        cls,
        project_id: str,
        clip_plans: list[_ClipPlan],
        *,
        max_workers: int,
    ) -> AsyncIterator[_ClipJobResult]:
        if not clip_plans:
            return

        unique_plans: dict[str, _ClipPlan] = {}
        for plan in clip_plans:
            unique_plans.setdefault(plan.clip_id, plan)

        ordered_plans = sorted(
            unique_plans.values(),
            key=lambda plan: (
                0 if plan.track == "tiktok" else 1,
                plan.source_key or "",
                plan.scene_index,
            ),
        )

        global_sem = asyncio.Semaphore(max(1, max_workers))
        episode_sems: dict[str, asyncio.Semaphore] = {}
        per_episode_limit = max(1, int(settings.match_playback_max_workers_per_episode))

        async def run_one(plan: _ClipPlan) -> _ClipJobResult:
            output_path = cls._clip_file(project_id, plan.clip_id)

            async with global_sem:
                source_sem: asyncio.Semaphore | None = None
                if plan.track == "source" and plan.source_key:
                    source_sem = episode_sems.setdefault(
                        plan.source_key,
                        asyncio.Semaphore(per_episode_limit),
                    )

                if source_sem:
                    await source_sem.acquire()

                try:
                    if output_path.exists() and output_path.stat().st_size > 0:
                        duration = await asyncio.to_thread(
                            cls._get_clip_duration_sync,
                            project_id,
                            plan.clip_id,
                        )
                        return _ClipJobResult(plan=plan, encoded=False, duration=duration)

                    duration = await asyncio.to_thread(
                        cls._encode_clip_sync,
                        project_id=project_id,
                        plan=plan,
                    )
                    return _ClipJobResult(plan=plan, encoded=True, duration=duration)
                except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                    return _ClipJobResult(plan=plan, encoded=False, duration=None, error=str(exc))
                finally:
                    if source_sem:
                        source_sem.release()

        tasks = [asyncio.create_task(run_one(plan)) for plan in ordered_plans]
        for completed in asyncio.as_completed(tasks):
            yield await completed

    @classmethod
    def _build_scene_entry(
        cls,
        *,
        project_id: str,
        fingerprint: str,
        scene_plan: _ScenePlan,
        scene_status: str,
        scene_error: str | None = None,
    ) -> dict:
        tiktok_duration = cls._get_clip_duration_sync(project_id, scene_plan.tiktok.clip_id)
        tiktok_status = "ready" if scene_status == "ready" else scene_status
        scene_entry = {
            "scene_index": scene_plan.scene_index,
            "has_match": scene_plan.has_match,
            "status": scene_status,
            "error": scene_error,
            "tiktok": {
                "scene_index": scene_plan.scene_index,
                "track": "tiktok",
                "url": cls._build_clip_url(
                    project_id=project_id,
                    scene_index=scene_plan.scene_index,
                    track="tiktok",
                    fingerprint=fingerprint,
                ),
                "duration": tiktok_duration,
                "ready": tiktok_status == "ready",
                "clip_id": scene_plan.tiktok.clip_id,
                "status": tiktok_status,
                "profile": scene_plan.tiktok.profile,
            },
            "source": None,
        }

        if scene_plan.source is not None:
            source_duration = cls._get_clip_duration_sync(project_id, scene_plan.source.clip_id)
            source_status = "ready" if scene_status == "ready" else scene_status
            scene_entry["source"] = {
                "scene_index": scene_plan.scene_index,
                "track": "source",
                "url": cls._build_clip_url(
                    project_id=project_id,
                    scene_index=scene_plan.scene_index,
                    track="source",
                    fingerprint=fingerprint,
                ),
                "duration": source_duration,
                "ready": source_status == "ready",
                "clip_id": scene_plan.source.clip_id,
                "status": source_status,
                "profile": scene_plan.source.profile,
            }

        return scene_entry

    @classmethod
    def _build_manifest(
        cls,
        *,
        project_id: str,
        fingerprint: str,
        scene_plans: list[_ScenePlan],
        scene_status_map: dict[int, str],
        scene_error_map: dict[int, str | None],
        reused_count: int,
        encoded_count: int,
    ) -> dict:
        scenes_payload: list[dict] = []
        for scene_plan in scene_plans:
            status = scene_status_map.get(scene_plan.scene_index, "ready")
            error = scene_error_map.get(scene_plan.scene_index)
            scenes_payload.append(
                cls._build_scene_entry(
                    project_id=project_id,
                    fingerprint=fingerprint,
                    scene_plan=scene_plan,
                    scene_status=status,
                    scene_error=error,
                )
            )

        scene_status = {
            str(scene["scene_index"]): scene["status"]
            for scene in scenes_payload
        }

        return {
            "ready": all(status == "ready" for status in scene_status.values()),
            "manifest_version": cls.MANIFEST_VERSION,
            "fingerprint": fingerprint,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "encode_profile": cls.ENCODE_PROFILE_VERSION,
            "clip_store_stats": {
                "reused_count": reused_count,
                "encoded_count": encoded_count,
                "total_clips": reused_count + encoded_count,
            },
            "scene_status": scene_status,
            "scenes": scenes_payload,
        }

    @classmethod
    def _extract_scene_asset(cls, manifest: dict, scene_index: int) -> dict | None:
        scenes = manifest.get("scenes")
        if not isinstance(scenes, list):
            return None
        for scene in scenes:
            if isinstance(scene, dict) and scene.get("scene_index") == scene_index:
                return scene
        return None

    @classmethod
    async def prepare_playback(
        cls,
        project_id: str,
        *,
        force: bool = False,
    ) -> AsyncIterator[PlaybackPrepareProgress]:
        lock = cls._get_prepare_lock(project_id)
        async with lock:
            async for progress in cls._prepare_playback_locked(project_id, force=force):
                yield progress

    @classmethod
    async def _prepare_playback_locked(
        cls,
        project_id: str,
        *,
        force: bool = False,
    ) -> AsyncIterator[PlaybackPrepareProgress]:
        project = ProjectService.load(project_id)
        if not project:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Project not found",
                error="Project not found",
            )
            return

        scenes = ProjectService.load_scenes(project_id)
        matches = ProjectService.load_matches(project_id)
        if not scenes or not scenes.scenes:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="No scenes available",
                error="No scenes available",
            )
            return
        if not matches:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="No matches available",
                error="No matches available",
            )
            return

        if not project.video_path:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="No project video",
                error="No project video",
            )
            return

        project_video = Path(project.video_path)
        if not project_video.exists():
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Project video file missing",
                error="Project video file missing",
            )
            return

        yield PlaybackPrepareProgress(
            status="scanning",
            progress=0.02,
            message="Scanning scenes and matches",
            total_scenes=len(scenes.scenes),
        )

        valid_matches = [m for m in matches.matches if m.confidence > 0 and bool(m.episode)]
        source_by_episode: dict[str, Path] = {}
        episodes_to_resolve = sorted({m.episode for m in valid_matches})
        for idx, episode in enumerate(episodes_to_resolve):
            yield PlaybackPrepareProgress(
                status="resolving_sources",
                progress=0.02 + (0.08 * ((idx + 1) / max(len(episodes_to_resolve), 1))),
                message=f"Resolving source episode {idx + 1}/{max(len(episodes_to_resolve), 1)}",
                total_scenes=len(scenes.scenes),
            )
            try:
                source_by_episode[episode] = await cls._resolve_episode_path(project, episode)
            except RuntimeError as exc:
                yield PlaybackPrepareProgress(
                    status="error",
                    progress=0.0,
                    message="Failed to resolve source episode",
                    error=str(exc),
                )
                return

        fingerprint = cls._build_fingerprint(project, scenes, matches, source_by_episode)
        existing_manifest = await asyncio.to_thread(cls._load_manifest_sync, project_id, fingerprint)
        if not force and await asyncio.to_thread(cls._validate_manifest_sync, project_id, existing_manifest):
            await asyncio.to_thread(cls._save_active_fingerprint, project_id, fingerprint)
            yield PlaybackPrepareProgress(
                status="complete",
                progress=1.0,
                message="Playback clips are ready (cache hit)",
                total_scenes=len(scenes.scenes),
                manifest=existing_manifest,
                cached=True,
            )
            return

        clip_store = cls._clip_store_dir(project_id)
        clip_store.mkdir(parents=True, exist_ok=True)
        cls._manifests_dir(project_id).mkdir(parents=True, exist_ok=True)

        try:
            scene_plans = cls._build_scene_plans(
                project_video=project_video,
                scenes=scenes,
                matches=matches,
                source_by_episode=source_by_episode,
            )
        except RuntimeError as exc:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Failed to compute clip plans",
                error=str(exc),
            )
            return

        all_clip_plans: list[_ClipPlan] = []
        for scene_plan in scene_plans:
            all_clip_plans.append(scene_plan.tiktok)
            if scene_plan.source is not None:
                all_clip_plans.append(scene_plan.source)

        unique_clip_plans: dict[str, _ClipPlan] = {}
        for plan in all_clip_plans:
            unique_clip_plans.setdefault(plan.clip_id, plan)

        missing_plans: list[_ClipPlan] = []
        reused_count = 0
        for plan in unique_clip_plans.values():
            clip_path = cls._clip_file(project_id, plan.clip_id)
            if clip_path.exists() and clip_path.stat().st_size > 0:
                reused_count += 1
            else:
                missing_plans.append(plan)

        encoded_count = 0
        scene_status_map: dict[int, str] = {scene.index: "ready" for scene in scenes.scenes}
        scene_error_map: dict[int, str | None] = {scene.index: None for scene in scenes.scenes}

        total_missing = len(missing_plans)
        worker_count = min(
            max(1, int(settings.match_playback_max_workers)),
            max(total_missing, 1),
        )
        completed_missing = 0

        async for job in cls._run_clip_jobs(
            project_id,
            missing_plans,
            max_workers=worker_count,
        ):
            completed_missing += 1
            if job.error:
                scene_status_map[job.plan.scene_index] = "error"
                scene_error_map[job.plan.scene_index] = job.error
                yield PlaybackPrepareProgress(
                    status="error",
                    progress=0.0,
                    message=(
                        f"Failed encoding {job.plan.track} clip for scene {job.plan.scene_index + 1}"
                    ),
                    scene_index=job.plan.scene_index,
                    total_scenes=len(scenes.scenes),
                    error=job.error,
                    track=job.plan.track,
                )
                return

            if job.encoded:
                encoded_count += 1
            else:
                reused_count += 1

            progress_ratio = completed_missing / max(total_missing, 1)
            status = "encoding_tiktok" if job.plan.track == "tiktok" else "encoding_source"
            action = "Encoded" if job.encoded else "Reused"
            yield PlaybackPrepareProgress(
                status=status,
                progress=0.12 + (0.82 * progress_ratio),
                message=(
                    f"{action} {job.plan.track} clip for scene {job.plan.scene_index + 1} "
                    f"({job.plan.duration:.2f}s, {job.plan.profile})"
                ),
                scene_index=job.plan.scene_index,
                total_scenes=len(scenes.scenes),
                track=job.plan.track,
            )

        yield PlaybackPrepareProgress(
            status="finalizing",
            progress=0.96,
            message="Finalizing playback manifest",
            total_scenes=len(scenes.scenes),
        )

        try:
            manifest = await asyncio.to_thread(
                cls._build_manifest,
                project_id=project_id,
                fingerprint=fingerprint,
                scene_plans=scene_plans,
                scene_status_map=scene_status_map,
                scene_error_map=scene_error_map,
                reused_count=reused_count,
                encoded_count=encoded_count,
            )
        except RuntimeError as exc:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Failed building playback manifest",
                error=str(exc),
            )
            return

        await asyncio.to_thread(cls._save_manifest_sync, project_id, fingerprint, manifest)
        await asyncio.to_thread(cls._save_active_fingerprint, project_id, fingerprint)

        keep = {fingerprint}
        active = cls._load_active_fingerprint(project_id)
        if active:
            keep.add(active)
        await asyncio.to_thread(cls._gc_cache_sync, project_id, keep)

        yield PlaybackPrepareProgress(
            status="complete",
            progress=1.0,
            message="Playback clips ready",
            total_scenes=len(scenes.scenes),
            manifest=manifest,
            cached=False,
        )

    @classmethod
    async def prepare_scene_playback(
        cls,
        project_id: str,
        *,
        scene_index: int,
        force: bool = False,
    ) -> AsyncIterator[PlaybackPrepareProgress]:
        lock = cls._get_prepare_lock(project_id)
        async with lock:
            async for progress in cls._prepare_scene_playback_locked(
                project_id,
                scene_index=scene_index,
                force=force,
            ):
                yield progress

    @classmethod
    async def _prepare_scene_playback_locked(
        cls,
        project_id: str,
        *,
        scene_index: int,
        force: bool,
    ) -> AsyncIterator[PlaybackPrepareProgress]:
        project = ProjectService.load(project_id)
        if not project:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Project not found",
                error="Project not found",
                scene_index=scene_index,
            )
            return

        scenes = ProjectService.load_scenes(project_id)
        matches = ProjectService.load_matches(project_id)
        if not scenes or not matches:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Scenes or matches missing",
                error="Scenes or matches missing",
                scene_index=scene_index,
            )
            return

        target_scene = next((scene for scene in scenes.scenes if scene.index == scene_index), None)
        if target_scene is None:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Scene not found",
                error="Scene not found",
                scene_index=scene_index,
            )
            return

        if not project.video_path:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="No project video",
                error="No project video",
                scene_index=scene_index,
            )
            return

        project_video = Path(project.video_path)
        if not project_video.exists():
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Project video file missing",
                error="Project video file missing",
                scene_index=scene_index,
            )
            return

        yield PlaybackPrepareProgress(
            status="scanning",
            progress=0.05,
            message=f"Preparing scene {scene_index + 1}",
            scene_index=scene_index,
            total_scenes=len(scenes.scenes),
        )

        valid_matches = [m for m in matches.matches if m.confidence > 0 and bool(m.episode)]
        source_by_episode: dict[str, Path] = {}
        episodes_to_resolve = sorted({m.episode for m in valid_matches})

        for idx, episode in enumerate(episodes_to_resolve):
            yield PlaybackPrepareProgress(
                status="resolving_sources",
                progress=0.05 + (0.10 * ((idx + 1) / max(len(episodes_to_resolve), 1))),
                message=f"Resolving source episode {idx + 1}/{max(len(episodes_to_resolve), 1)}",
                scene_index=scene_index,
                total_scenes=len(scenes.scenes),
            )
            try:
                source_by_episode[episode] = await cls._resolve_episode_path(project, episode)
            except RuntimeError as exc:
                yield PlaybackPrepareProgress(
                    status="error",
                    progress=0.0,
                    message="Failed to resolve source episode",
                    scene_index=scene_index,
                    total_scenes=len(scenes.scenes),
                    error=str(exc),
                )
                return

        try:
            scene_plans = cls._build_scene_plans(
                project_video=project_video,
                scenes=scenes,
                matches=matches,
                source_by_episode=source_by_episode,
            )
        except RuntimeError as exc:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Failed to compute clip plans",
                scene_index=scene_index,
                total_scenes=len(scenes.scenes),
                error=str(exc),
            )
            return

        target_plan = next((plan for plan in scene_plans if plan.scene_index == scene_index), None)
        if target_plan is None:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Scene playback plan missing",
                scene_index=scene_index,
                total_scenes=len(scenes.scenes),
                error="Scene playback plan missing",
            )
            return

        fingerprint = cls._build_fingerprint(project, scenes, matches, source_by_episode)
        existing_manifest = await asyncio.to_thread(cls._load_manifest_sync, project_id, fingerprint)
        if (
            not force
            and await asyncio.to_thread(cls._validate_manifest_sync, project_id, existing_manifest)
            and isinstance(existing_manifest, dict)
        ):
            scene_asset = cls._extract_scene_asset(existing_manifest, scene_index)
            await asyncio.to_thread(cls._save_active_fingerprint, project_id, fingerprint)
            yield PlaybackPrepareProgress(
                status="complete",
                progress=1.0,
                message=f"Scene {scene_index + 1} clips are ready (cache hit)",
                scene_index=scene_index,
                total_scenes=len(scenes.scenes),
                manifest=existing_manifest,
                scene_asset=scene_asset,
                cached=True,
            )
            return

        clip_store = cls._clip_store_dir(project_id)
        clip_store.mkdir(parents=True, exist_ok=True)
        cls._manifests_dir(project_id).mkdir(parents=True, exist_ok=True)

        target_clips = [target_plan.tiktok]
        if target_plan.source is not None:
            target_clips.append(target_plan.source)

        missing_target: list[_ClipPlan] = []
        reused_count = 0
        encoded_count = 0
        for plan in target_clips:
            clip_path = cls._clip_file(project_id, plan.clip_id)
            if clip_path.exists() and clip_path.stat().st_size > 0:
                reused_count += 1
            else:
                missing_target.append(plan)

        scene_status_map: dict[int, str] = {scene.index: "ready" for scene in scenes.scenes}
        scene_error_map: dict[int, str | None] = {scene.index: None for scene in scenes.scenes}
        scene_status_map[scene_index] = "preparing"

        total_missing = len(missing_target)
        completed_missing = 0
        worker_count = min(
            max(1, int(settings.match_playback_max_workers)),
            max(total_missing, 1),
        )

        async for job in cls._run_clip_jobs(
            project_id,
            missing_target,
            max_workers=worker_count,
        ):
            completed_missing += 1
            if job.error:
                scene_status_map[scene_index] = "error"
                scene_error_map[scene_index] = job.error
                yield PlaybackPrepareProgress(
                    status="error",
                    progress=0.0,
                    message=f"Failed encoding {job.plan.track} clip for scene {scene_index + 1}",
                    scene_index=scene_index,
                    total_scenes=len(scenes.scenes),
                    error=job.error,
                    track=job.plan.track,
                )
                return

            if job.encoded:
                encoded_count += 1
            else:
                reused_count += 1

            progress_ratio = completed_missing / max(total_missing, 1)
            status = "encoding_tiktok" if job.plan.track == "tiktok" else "encoding_source"
            action = "Encoded" if job.encoded else "Reused"
            yield PlaybackPrepareProgress(
                status=status,
                progress=0.18 + (0.70 * progress_ratio),
                message=(
                    f"{action} {job.plan.track} clip for scene {scene_index + 1} "
                    f"({job.plan.duration:.2f}s, {job.plan.profile})"
                ),
                scene_index=scene_index,
                total_scenes=len(scenes.scenes),
                track=job.plan.track,
            )

        scene_status_map[scene_index] = "ready"

        # Count reused clips globally for scene status accounting.
        all_clip_plans: list[_ClipPlan] = []
        for scene_plan in scene_plans:
            all_clip_plans.append(scene_plan.tiktok)
            if scene_plan.source is not None:
                all_clip_plans.append(scene_plan.source)

        unique_clip_ids = {plan.clip_id for plan in all_clip_plans}
        # encoded_count already includes clips encoded for the scene operation.
        # For final manifest stats we count global clip availability.
        global_reused = 0
        for clip_id in unique_clip_ids:
            clip_path = cls._clip_file(project_id, clip_id)
            if clip_path.exists() and clip_path.stat().st_size > 0:
                global_reused += 1

        yield PlaybackPrepareProgress(
            status="finalizing",
            progress=0.94,
            message=f"Finalizing scene {scene_index + 1} playback update",
            scene_index=scene_index,
            total_scenes=len(scenes.scenes),
        )

        try:
            manifest = await asyncio.to_thread(
                cls._build_manifest,
                project_id=project_id,
                fingerprint=fingerprint,
                scene_plans=scene_plans,
                scene_status_map=scene_status_map,
                scene_error_map=scene_error_map,
                reused_count=global_reused - encoded_count,
                encoded_count=encoded_count,
            )
        except RuntimeError as exc:
            yield PlaybackPrepareProgress(
                status="error",
                progress=0.0,
                message="Failed building scene manifest",
                scene_index=scene_index,
                total_scenes=len(scenes.scenes),
                error=str(exc),
            )
            return

        await asyncio.to_thread(cls._save_manifest_sync, project_id, fingerprint, manifest)
        await asyncio.to_thread(cls._save_active_fingerprint, project_id, fingerprint)
        await asyncio.to_thread(cls._gc_cache_sync, project_id, {fingerprint})

        scene_asset = cls._extract_scene_asset(manifest, scene_index)
        yield PlaybackPrepareProgress(
            status="complete",
            progress=1.0,
            message=f"Scene {scene_index + 1} playback clips ready",
            scene_index=scene_index,
            total_scenes=len(scenes.scenes),
            manifest=manifest,
            scene_asset=scene_asset,
            cached=False,
        )

    @classmethod
    def get_manifest(cls, project_id: str) -> dict:
        fingerprint = cls._load_active_fingerprint(project_id)
        if not fingerprint:
            return cls._default_manifest()

        manifest = cls._load_manifest_sync(project_id, fingerprint)
        if not cls._validate_manifest_sync(project_id, manifest):
            return cls._default_manifest()

        assert manifest is not None
        return manifest

    @classmethod
    def _get_clip_path_for_fingerprint(
        cls,
        project_id: str,
        *,
        scene_index: int,
        track: ClipTrack,
        fingerprint: str,
    ) -> Path:
        manifest = cls._load_manifest_sync(project_id, fingerprint)
        if not manifest:
            raise FileNotFoundError("Playback manifest not found")

        scene_entry = next(
            (entry for entry in manifest.get("scenes", []) if entry.get("scene_index") == scene_index),
            None,
        )
        if not scene_entry:
            raise FileNotFoundError(f"Scene {scene_index} not found in playback manifest")

        clip_entry = scene_entry.get(track)
        if not isinstance(clip_entry, dict):
            raise FileNotFoundError(f"Track '{track}' not available for scene {scene_index}")

        clip_id = clip_entry.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id:
            raise FileNotFoundError("Invalid clip_id in playback manifest")

        clip_path = cls._clip_file(project_id, clip_id)
        if not clip_path.exists() or clip_path.stat().st_size == 0:
            raise FileNotFoundError("Prepared clip file missing")

        return clip_path

    @classmethod
    def get_clip_path(
        cls,
        project_id: str,
        *,
        scene_index: int,
        track: ClipTrack,
        fingerprint: str | None = None,
    ) -> Path:
        effective_fingerprint = fingerprint or cls._load_active_fingerprint(project_id)
        if not effective_fingerprint:
            raise FileNotFoundError("No prepared playback cache")

        try:
            return cls._get_clip_path_for_fingerprint(
                project_id,
                scene_index=scene_index,
                track=track,
                fingerprint=effective_fingerprint,
            )
        except FileNotFoundError as exc:
            if fingerprint and cls._looks_like_fingerprint(fingerprint):
                current_fingerprint = cls._load_active_fingerprint(project_id)
                if current_fingerprint and current_fingerprint != fingerprint:
                    try:
                        return cls._get_clip_path_for_fingerprint(
                            project_id,
                            scene_index=scene_index,
                            track=track,
                            fingerprint=current_fingerprint,
                        )
                    except FileNotFoundError:
                        pass
            raise exc
