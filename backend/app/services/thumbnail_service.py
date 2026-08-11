"""Thumbnail candidate computation and frame extraction for upload covers.

Candidates are timestamps in the FINAL rendered video, computed from the
authoritative playback timeline (output/transcription_timing.json). Preview
JPEGs are extracted from the shared upload_source cache in one decode pass.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from ..models.transcription import Transcription
from .anime_matcher import AnimeMatcherService
from .export_service import ExportService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailCandidate:
    index: int
    label: str
    timestamp_seconds: float

    @property
    def timestamp_ms(self) -> int:
        return int(round(self.timestamp_seconds * 1000))


class ThumbnailService:
    # 3 frames at the 60fps TikTok timeline — absorbs off-by-a-frame scene cuts.
    _SHIFT_SECONDS = 3.0 / 60.0
    _JPEG_QUALITY = 90
    _THUMBS_CACHE_DIR = settings.cache_dir / "upload_thumbs"

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

    @classmethod
    def compute_candidates(cls, transcription: Transcription) -> list[ThumbnailCandidate]:
        scenes = [s for s in transcription.scenes if s.end_time > s.start_time]
        if not scenes:
            return []
        shift = cls._SHIFT_SECONDS
        first = scenes[0]
        mid = (first.start_time + first.end_time) / 2
        spots: list[tuple[str, float]] = [
            ("Scène 1 · début", min(first.start_time + shift, mid)),
            ("Scène 1 · milieu", mid),
            ("Scène 1 · fin", max(first.end_time - shift, mid)),
        ]
        for ordinal, scene in enumerate(scenes[1:3], start=2):
            scene_mid = (scene.start_time + scene.end_time) / 2
            spots.append(
                (f"Scène {ordinal} · début", min(scene.start_time + shift, scene_mid))
            )
        return [
            ThumbnailCandidate(index=i, label=label, timestamp_seconds=round(ts, 3))
            for i, (label, ts) in enumerate(spots)
        ]

    @classmethod
    def _project_thumbs_dir(cls, project_id: str) -> Path:
        return cls._THUMBS_CACHE_DIR / project_id

    @classmethod
    def build_candidates_payload(
        cls, project_id: str, video_path: Path
    ) -> dict[str, Any]:
        """Compute candidates, extract+cache JPEGs, return the API payload."""
        transcription = cls.load_final_timeline(project_id)
        if transcription is None:
            return {
                "state": "error",
                "detail": "Timeline finale introuvable (transcription_timing.json)",
            }
        candidates = cls.compute_candidates(transcription)
        if not candidates:
            return {
                "state": "error",
                "detail": "Aucune scène exploitable dans la timeline finale",
            }

        stat = video_path.stat()
        version = f"{stat.st_mtime_ns}-{stat.st_size}"
        cache_dir = cls._project_thumbs_dir(project_id) / version
        if not all(
            (cache_dir / f"cand_{c.index}.jpg").exists() for c in candidates
        ):
            # Source video changed (or first call): rebuild from scratch.
            shutil.rmtree(cls._project_thumbs_dir(project_id), ignore_errors=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            images = AnimeMatcherService.extract_frames(
                video_path, [c.timestamp_seconds for c in candidates]
            )
            for candidate, image in zip(candidates, images):
                if image is None:
                    logger.warning(
                        "Thumbnail frame extraction failed: project=%s index=%d t=%.3f",
                        project_id, candidate.index, candidate.timestamp_seconds,
                    )
                    continue
                image.convert("RGB").save(
                    cache_dir / f"cand_{candidate.index}.jpg",
                    "JPEG",
                    quality=cls._JPEG_QUALITY,
                )

        payload = [
            {
                "index": c.index,
                "label": c.label,
                "timestamp_ms": c.timestamp_ms,
                "image_url": (
                    f"/project-manager/projects/{project_id}"
                    f"/thumbnail-frame/{c.index}?v={version}"
                ),
            }
            for c in candidates
            if (cache_dir / f"cand_{c.index}.jpg").exists()
        ]
        if not payload:
            return {
                "state": "error",
                "detail": "Extraction des miniatures impossible depuis la vidéo finale",
            }
        return {"state": "ready", "version": version, "candidates": payload}

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
