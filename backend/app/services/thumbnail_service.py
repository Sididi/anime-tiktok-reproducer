"""Thumbnail candidate computation and frame extraction for upload covers.

Candidates are timestamps in the FINAL rendered video, computed from the
authoritative playback timeline (output/transcription_timing.json). A
background progressive builder resolves each candidate's composed 1080x1920
cover from the cleanest source available: a local clean-library frame, a
Drive range-fetch of the same, or (once cached) a frame from the rendered
output video — see _run_candidates_build for the meta.json state machine.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import settings
from ..models.match import MatchList, SceneMatch
from ..models.transcription import Transcription
from ..utils.media_binaries import get_media_subprocess_env, rewrite_media_command
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService
from .export_service import ExportService
from .google_drive_service import GoogleDriveService
from .project_service import ProjectService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailCandidate:
    index: int
    label: str
    timestamp_seconds: float
    scene_index: int
    position: str  # "start" | "mid" | "end"
    episode: str | None = None
    source_timestamp_seconds: float | None = None

    @property
    def timestamp_ms(self) -> int:
        return int(round(self.timestamp_seconds * 1000))


class ThumbnailService:
    # 3 frames at the 60fps TikTok timeline — absorbs off-by-a-frame scene cuts.
    _SHIFT_SECONDS = 3.0 / 60.0
    _JPEG_QUALITY = 90
    _THUMBS_CACHE_DIR = settings.cache_dir / "upload_thumbs"

    # Per-project locks guarding the rebuild critical section (rmtree + write
    # + progressive meta updates) in _run_candidates_build, same pattern as
    # UploadPhaseService._source_locks / _source_lock.
    _build_lock_guard = threading.Lock()
    _build_locks: dict[str, threading.Lock] = {}

    @classmethod
    def _build_lock(cls, project_id: str) -> threading.Lock:
        with cls._build_lock_guard:
            return cls._build_locks.setdefault(project_id, threading.Lock())

    @classmethod
    def load_final_timeline(cls, project_id: str) -> Transcription | None:
        path = ExportService.get_output_dir(project_id) / "transcription_timing.json"
        if not path.exists():
            return None
        try:
            return Transcription.model_validate(json.loads(path.read_text()))
        except Exception:
            logger.warning(
                "Unreadable transcription_timing.json for project %s", project_id,
                exc_info=True,
            )
            return None

    _LAST_END_LABELS = (
        "Dernière scène · fin",
        "Avant-dernière scène · fin",
        "Avant-avant-dernière scène · fin",
    )

    @classmethod
    def compute_candidates(
        cls,
        transcription: Transcription,
        matches: "MatchList | None",
    ) -> list[ThumbnailCandidate]:
        scenes = [s for s in transcription.scenes if s.end_time > s.start_time]
        if not scenes:
            return []
        shift = cls._SHIFT_SECONDS

        def output_ts(scene, position: str) -> float:
            mid = (scene.start_time + scene.end_time) / 2
            if position == "start":
                return min(scene.start_time + shift, mid)
            if position == "end":
                return max(scene.end_time - shift, mid)
            return mid

        match_by_scene: dict[int, "SceneMatch"] = {}
        if matches is not None:
            for match in matches.matches:
                if match.episode:
                    match_by_scene[match.scene_index] = match

        def source_coord(scene, position: str) -> tuple[str | None, float | None]:
            match = match_by_scene.get(scene.scene_index)
            if match is None or match.end_time <= match.start_time:
                return None, None
            ratio = match.speed_ratio if match.speed_ratio and match.speed_ratio > 0 else 1.0
            src_shift = shift / ratio
            src_mid = (match.start_time + match.end_time) / 2
            if position == "start":
                return match.episode, min(match.start_time + src_shift, src_mid)
            if position == "end":
                return match.episode, max(match.end_time - src_shift, src_mid)
            return match.episode, src_mid

        spots: list[tuple[object, str, str]] = [
            (scenes[0], "start", "Scène 1 · début"),
            (scenes[0], "mid", "Scène 1 · milieu"),
            (scenes[0], "end", "Scène 1 · fin"),
        ]
        for ordinal, scene in enumerate(scenes[1:6], start=2):
            spots.append((scene, "start", f"Scène {ordinal} · début"))
        for offset, label in enumerate(cls._LAST_END_LABELS):
            pos = len(scenes) - 1 - offset
            if pos < 0:
                break
            spots.append((scenes[pos], "end", label))

        candidates: list[ThumbnailCandidate] = []
        seen: set[tuple[int, str]] = set()
        for scene, position, label in spots:
            key = (scene.scene_index, position)
            if key in seen:
                continue
            seen.add(key)
            episode, src_ts = source_coord(scene, position)
            candidates.append(ThumbnailCandidate(
                index=len(candidates),
                label=label,
                timestamp_seconds=round(output_ts(scene, position), 3),
                scene_index=scene.scene_index,
                position=position,
                episode=episode,
                source_timestamp_seconds=round(src_ts, 3) if src_ts is not None else None,
            ))
        return candidates

    @classmethod
    def _project_thumbs_dir(cls, project_id: str) -> Path:
        return cls._THUMBS_CACHE_DIR / project_id

    # ── Progressive builder (meta.json state machine) ─────────────────────
    #
    # Cache layout: upload_thumbs/<project>/<version>/cand_<i>.jpg (composed
    # covers) + meta.json {"version", "candidates": [{"index", "label",
    # "timestamp_ms", "scene_index", "position", "source"}]}, source one of
    # "clean" | "output" | "pending". Version tracks transcription_timing.json
    # + matches.json mtimes so a rematch or a re-render invalidates the cache.

    @classmethod
    def _candidates_version(cls, project_id: str) -> str | None:
        tt_path = ExportService.get_output_dir(project_id) / "transcription_timing.json"
        if not tt_path.exists():
            return None
        tt_mtime_ns = tt_path.stat().st_mtime_ns
        matches_path = ProjectService.get_matches_file(project_id)
        mm_mtime_ns = matches_path.stat().st_mtime_ns if matches_path.exists() else 0
        return f"{tt_mtime_ns}-{mm_mtime_ns}"

    @classmethod
    def _meta_path(cls, project_id: str, version: str) -> Path:
        return cls._project_thumbs_dir(project_id) / version / "meta.json"

    @classmethod
    def _read_meta(cls, project_id: str, version: str) -> dict[str, Any] | None:
        path = cls._meta_path(project_id, version)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            logger.warning(
                "Unreadable thumbnail meta.json: project=%s version=%s",
                project_id, version, exc_info=True,
            )
            return None

    @classmethod
    def _write_meta(cls, project_id: str, version: str, meta: dict[str, Any]) -> None:
        path = cls._meta_path(project_id, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(path)

    @classmethod
    def candidates_status(cls, project_id: str) -> dict[str, Any]:
        """Snapshot from meta.json; safe to call from any thread without the
        build lock (readers never mutate)."""
        version = cls._candidates_version(project_id)
        if version is None:
            return {
                "state": "error",
                "detail": "Timeline finale introuvable (transcription_timing.json)",
            }
        meta = cls._read_meta(project_id, version)
        if meta is None:
            return {"state": "in_progress", "version": version, "pending": 0, "candidates": []}

        payload = []
        pending = 0
        for c in meta.get("candidates", []):
            source = c.get("source")
            entry = {
                "index": c["index"],
                "label": c["label"],
                "timestamp_ms": c["timestamp_ms"],
                "source": source,
            }
            if source == "pending":
                pending += 1
            else:
                entry["image_url"] = (
                    f"/project-manager/projects/{project_id}"
                    f"/thumbnail-frame/{c['index']}?v={version}"
                )
            payload.append(entry)
        state = "ready" if pending == 0 else "partial"
        return {"state": state, "version": version, "pending": pending, "candidates": payload}

    @classmethod
    def _run_candidates_build(cls, project_id: str) -> None:
        """Synchronous builder body; callers get progress via candidates_status.
        Safe to call directly from tests (bypasses the background thread)."""
        with cls._build_lock(project_id):
            version = cls._candidates_version(project_id)
            if version is None:
                return  # candidates_status reports the "error" state

            project_thumbs_dir = cls._project_thumbs_dir(project_id)
            version_dir = project_thumbs_dir / version
            existing_meta = cls._read_meta(project_id, version)
            resuming = version_dir.exists() and existing_meta is not None
            if resuming:
                pending = sum(
                    1 for c in existing_meta.get("candidates", [])
                    if c.get("source") == "pending"
                )
                if pending == 0:
                    return  # cache hit: nothing left to resolve

            # Fresh build: version changed -> wipe stale versions. Resume
            # (version dir + meta already present, pending > 0) keeps the
            # existing meta/JPEGs as-is: already-resolved candidates must
            # not flicker back to "pending" for concurrent status readers,
            # nor get re-extracted.
            if not version_dir.exists():
                shutil.rmtree(project_thumbs_dir, ignore_errors=True)
            version_dir.mkdir(parents=True, exist_ok=True)

            transcription = cls.load_final_timeline(project_id)
            if transcription is None:
                return
            matches = ProjectService.load_matches(project_id)
            candidates = cls.compute_candidates(transcription, matches)
            if not candidates:
                return
            project = ProjectService.load(project_id)
            library_type = project.library_type if project else None
            drive_folder_id = project.drive_folder_id if project else None

            by_index = {c.index: c for c in candidates}

            if resuming:
                meta = existing_meta
                meta_candidates = meta.setdefault("candidates", [])
            else:
                meta_candidates = [
                    {
                        "index": c.index,
                        "label": c.label,
                        "timestamp_ms": c.timestamp_ms,
                        "scene_index": c.scene_index,
                        "position": c.position,
                        "source": "pending",
                    }
                    for c in candidates
                ]
                meta = {"version": version, "candidates": meta_candidates}
                cls._write_meta(project_id, version, meta)

            # Step 1: local clean frame, else Drive clean frame. Only
            # candidates still "pending" (fresh build: all of them; resume:
            # whatever didn't resolve last time) are attempted.
            for entry in meta_candidates:
                if entry.get("source") != "pending":
                    continue
                candidate = by_index[entry["index"]]
                if candidate.episode is None or candidate.source_timestamp_seconds is None:
                    continue
                image = cls._extract_local_clean_frame(candidate, library_type)
                if image is None and drive_folder_id:
                    image = cls._extract_drive_clean_frame(candidate, drive_folder_id)
                if image is None:
                    continue
                cover = cls.compose_vertical_cover(image)
                cover.save(
                    version_dir / f"cand_{candidate.index}.jpg",
                    "JPEG",
                    quality=cls._JPEG_QUALITY,
                )
                entry["source"] = "clean"
                cls._write_meta(project_id, version, meta)  # progressive visibility

            # Step 2: rendered-output fallback for whatever is still pending.
            still_pending = [e for e in meta_candidates if e["source"] == "pending"]
            if still_pending:
                from .upload_phase import UploadPhaseService  # lazy: import cycle

                video_path = UploadPhaseService.cached_source_video(project_id)
                if video_path is not None:
                    timestamps = [
                        by_index[e["index"]].timestamp_seconds for e in still_pending
                    ]
                    images = AnimeMatcherService.extract_frames(video_path, timestamps)
                    for entry, image in zip(still_pending, images):
                        if image is None:
                            logger.warning(
                                "Thumbnail output-fallback extraction failed: "
                                "project=%s index=%d",
                                project_id, entry["index"],
                            )
                            continue
                        cover = cls.compose_vertical_cover(image)
                        cover.save(
                            version_dir / f"cand_{entry['index']}.jpg",
                            "JPEG",
                            quality=cls._JPEG_QUALITY,
                        )
                        entry["source"] = "output"
                    cls._write_meta(project_id, version, meta)

                    # Candidates the output fallback also failed on: drop
                    # from meta entirely (mirrors v1 dropped-frame behavior).
                    meta["candidates"] = [
                        e for e in meta["candidates"] if e["source"] != "pending"
                    ]
                    cls._write_meta(project_id, version, meta)

    # Per-project in-flight registry for the background builder, mirroring
    # UploadPhaseService._source_downloads_in_flight.
    _builds_in_flight_guard = threading.Lock()
    _builds_in_flight: set[str] = set()

    @classmethod
    def start_candidates_build(cls, project_id: str) -> dict[str, Any]:
        """Kick the background builder when needed; return the current snapshot."""
        with cls._builds_in_flight_guard:
            if project_id in cls._builds_in_flight:
                return cls.candidates_status(project_id)

        status = cls.candidates_status(project_id)
        if status["state"] in ("ready", "error"):
            return status

        with cls._builds_in_flight_guard:
            if project_id in cls._builds_in_flight:
                return cls.candidates_status(project_id)
            cls._builds_in_flight.add(project_id)

        def _worker() -> None:
            try:
                cls._run_candidates_build(project_id)
            except Exception:
                logger.warning(
                    "Thumbnail candidates build failed for %s", project_id, exc_info=True,
                )
            finally:
                with cls._builds_in_flight_guard:
                    cls._builds_in_flight.discard(project_id)

        threading.Thread(
            target=_worker, name=f"thumb-candidates-{project_id}", daemon=True
        ).start()
        return cls.candidates_status(project_id)

    @classmethod
    def cached_frame_path(cls, project_id: str, index: int) -> Path | None:
        base = cls._project_thumbs_dir(project_id)
        if not base.exists():
            return None
        for version_dir in sorted(base.iterdir(), reverse=True):
            candidate = version_dir / f"cand_{index}.jpg"
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def extract_frame_image(
        cls, video_path: Path, timestamp_seconds: float, dest_path: Path
    ) -> Path | None:
        """Single-frame JPEG for image-native platforms (YouTube/Facebook)."""
        try:
            image = AnimeMatcherService.extract_frame(video_path, timestamp_seconds)
            if image is None:
                return None
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            image.convert("RGB").save(dest_path, "JPEG", quality=cls._JPEG_QUALITY)
            return dest_path
        except Exception:
            logger.warning(
                "Thumbnail image extraction failed: %s t=%.3f",
                video_path, timestamp_seconds, exc_info=True,
            )
            return None

    _COVER_SIZE = (1080, 1920)

    @classmethod
    def compose_vertical_cover(cls, image: Image.Image) -> Image.Image:
        """1080×1920 blurred-extend: frame full-width centered over a blurred,
        darkened self-fill background (the rendered videos' look, minus text)."""
        from PIL import ImageEnhance, ImageFilter, ImageOps  # local: PIL submodules

        target_w, target_h = cls._COVER_SIZE
        src = image.convert("RGB")
        if src.height >= src.width:  # already portrait-ish: plain fit
            return ImageOps.fit(src, cls._COVER_SIZE, Image.LANCZOS)
        background = ImageOps.fit(src, cls._COVER_SIZE, Image.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=40))
        background = ImageEnhance.Brightness(background).enhance(0.45)
        fg_h = int(round(target_w * src.height / src.width))
        foreground = src.resize((target_w, fg_h), Image.LANCZOS)
        background.paste(foreground, (0, (target_h - fg_h) // 2))
        return background

    @classmethod
    def _resolve_local_episode(cls, episode: str, library_type) -> Path | None:
        try:
            resolved = AnimeLibraryService.resolve_episode_path(
                episode, library_type=library_type
            )
        except Exception:
            logger.warning("Episode resolution failed for %r", episode, exc_info=True)
            return None
        if resolved is not None and resolved.exists():
            return resolved
        return None

    @classmethod
    def _extract_local_clean_frame(
        cls, candidate: ThumbnailCandidate, library_type
    ) -> Image.Image | None:
        if not candidate.episode or candidate.source_timestamp_seconds is None:
            return None
        path = cls._resolve_local_episode(candidate.episode, library_type)
        if path is None:
            return None
        try:
            return AnimeMatcherService.extract_frame(
                path, candidate.source_timestamp_seconds
            )
        except Exception:
            logger.warning(
                "Local clean-frame extraction failed: %s t=%.3f",
                path, candidate.source_timestamp_seconds, exc_info=True,
            )
            return None

    _DRIVE_FETCH_TIMEOUT_SECONDS = 90

    @classmethod
    def _extract_drive_clean_frame(
        cls, candidate: ThumbnailCandidate, drive_folder_id: str
    ) -> Image.Image | None:
        """Single-frame ffmpeg range-fetch from the project's Drive sources/ bundle.

        ffmpeg seeks over HTTPS with Range requests: it reads the mp4 index
        then only the GOP around the target — a few MB, never the full file.
        """
        if not candidate.episode or candidate.source_timestamp_seconds is None:
            return None
        try:
            sources_id = GoogleDriveService.find_subfolder(drive_folder_id, "sources")
            if not sources_id:
                return None
            basename = Path(candidate.episode).name
            entries = GoogleDriveService.list_children_named(sources_id, basename)
            if not entries:
                return None
            file_id = str(entries[0]["id"])
            token = GoogleDriveService.credentials().token
            url = (
                "https://www.googleapis.com/drive/v3/files/"
                f"{file_id}?alt=media&supportsAllDrives=true"
            )
            with tempfile.TemporaryDirectory(prefix="atr-thumb-drive-") as tmp:
                out = Path(tmp) / "frame.jpg"
                cmd = [
                    "ffmpeg", "-y", "-v", "error",
                    "-headers", f"Authorization: Bearer {token}\r\n",
                    "-ss", f"{candidate.source_timestamp_seconds:.3f}",
                    "-i", url,
                    "-frames:v", "1", "-q:v", "2",
                    str(out),
                ]
                # Same synchronous-ffmpeg idiom as match_playback_service._probe_clip_sync /
                # video_cleanup_service._spawn_crop_reader / video_color.py: this classmethod
                # is plain sync (no event loop involved), so we use subprocess.run directly
                # (binary resolution + sanitized env via media_binaries, same as run_command)
                # instead of the async run_command used elsewhere in the codebase.
                cmd = rewrite_media_command(cmd)
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    check=False,
                    timeout=cls._DRIVE_FETCH_TIMEOUT_SECONDS,
                    env=get_media_subprocess_env(cmd),
                )
                if result.returncode != 0 or not out.exists():
                    logger.warning(
                        "Drive frame range-fetch failed: episode=%s t=%.3f rc=%s",
                        basename, candidate.source_timestamp_seconds,
                        result.returncode,
                    )
                    return None
                with Image.open(out) as img:
                    return img.convert("RGB").copy()
        except Exception:
            logger.warning(
                "Drive clean-frame extraction failed for %s", candidate.episode,
                exc_info=True,
            )
            return None
