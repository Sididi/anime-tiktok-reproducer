"""Anime source matching service using anime_searcher module."""

import asyncio
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
from PIL import Image, ImageOps

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from ..models import AlternativeMatch, MatchCandidate, MatchList, Scene, SceneMatch, SceneList


@dataclass
class MatchProgress:
    """Progress information for anime matching."""

    status: str  # starting, matching, complete, error
    progress: float = 0.0  # 0-1
    message: str = ""
    current_scene: int = 0
    total_scenes: int = 0
    matches: MatchList | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "current_scene": self.current_scene,
            "total_scenes": self.total_scenes,
            "error": self.error,
        }
        if self.matches is not None:
            result["matches"] = self.matches.model_dump()
        return result


@dataclass(frozen=True)
class MatchProposal:
    """Internal normalized proposal used to select and expose scene matches."""

    episode: str
    start_time: float
    end_time: float
    confidence: float
    selection_score: float
    source: str
    vote_count: int = 1
    debug: dict[str, Any] | None = None


class AnimeMatcherService:
    """Service for matching TikTok scenes to anime source episodes."""

    # Singleton instances for the searcher components (expensive to load)
    _index_manager = None
    _embedder = None
    _query_processor = None
    _loaded_library_path: Path | None = None
    _loaded_library_type: LibraryType | None = None
    _loaded_index_signature: tuple[tuple[str, int, int], ...] | None = None
    _loaded_series_index_signatures: dict[str, tuple[tuple[str, int, int], ...]] = {}
    # Series that were updated on disk and require cache refresh before matching.
    _stale_series: dict[LibraryType, set[str]] = defaultdict(set)
    _cv2 = None
    _episode_paths_cache: dict[
        tuple[str, str, str, tuple[tuple[str, int, int], ...] | None],
        dict[str, Path],
    ] = {}
    _crop_index_memory_cache: dict[str, dict[str, np.ndarray | str]] = {}
    CROP_INDEX_VERSION = "crop-v4-seeded-portrait-source-0_5fps"
    CROP_INDEX_FPS = 0.5
    CROP_INDEX_BATCH_SIZE = 24
    CROP_SEARCH_TOP_N = 35
    CROP_SEARCH_MAX_EPISODES_PER_SCENE = 2
    CROP_SEARCH_ENABLE_MAX_SERIES_EPISODES = 4
    CROP_SEARCH_ENABLE_MAX_SERIES_FRAMES = 12000
    LOCAL_CROP_WINDOW_SECONDS = 3.0
    LOCAL_CROP_FPS = 2.0
    LOCAL_CROP_MAX_ANCHORS_PER_EPISODE = 3
    LOCAL_CROP_MIN_ANCHOR_SEPARATION = 2.0
    LOCAL_CROP_MAX_SOURCE_CROPS_PER_SCENE = 192
    REFINE_MAX_FRAMES_PER_BOUNDARY = 12

    @classmethod
    def mark_series_updated(
        cls,
        library_type: LibraryType | str,
        series_name: str | None,
    ) -> None:
        """Mark one series as stale so next match for it reloads the index cache."""
        if not series_name:
            return
        cls._stale_series[coerce_library_type(library_type)].add(series_name)

    @staticmethod
    def _file_signature(path: Path) -> tuple[str, int, int]:
        try:
            stat = path.stat()
        except OSError:
            return (str(path), -1, -1)
        return (str(path), stat.st_mtime_ns, stat.st_size)

    @classmethod
    def _index_signature(
        cls,
        library_path: Path,
        anime_name: str | None,
    ) -> tuple[tuple[str, int, int], ...]:
        """Return a cheap on-disk signature for cache invalidation.

        Storage Box activation can replace a series shard without going through
        the indexer endpoints that call ``mark_series_updated``. Tracking the
        manifest plus the requested shard files prevents the in-memory matcher
        from searching a stale two-episode cache after a newer online index has
        been hydrated.
        """
        index_dir = library_path / ".index"
        manifest_path = index_dir / "manifest.json"
        state_path = index_dir / "state.json"
        paths = [manifest_path, state_path]

        if anime_name and manifest_path.exists():
            try:
                import json

                with open(manifest_path, encoding="utf-8") as f:
                    manifest = json.load(f)
                series_entry = manifest.get("series", {}).get(anime_name, {})
                shard_key = (
                    str(series_entry.get("key") or "").strip()
                    if isinstance(series_entry, dict)
                    else ""
                )
                if shard_key:
                    shard_dir = index_dir / "series" / shard_key
                    paths.extend(
                        [
                            shard_dir / "faiss.index",
                            shard_dir / "metadata.json",
                        ]
                    )
            except Exception:
                # If the manifest is temporarily unreadable, include only the
                # top-level files and let IndexManager report the load error if
                # a reload is required.
                pass

        return tuple(cls._file_signature(path) for path in paths)

    @staticmethod
    def _require_cv2():
        if AnimeMatcherService._cv2 is not None:
            return AnimeMatcherService._cv2
        import cv2

        AnimeMatcherService._cv2 = cv2
        return cv2

    @classmethod
    def _init_searcher(
        cls,
        library_path: Path,
        library_type: LibraryType | str,
        anime_name: str | None = None,
    ) -> bool:
        """
        Initialize the anime_searcher components.

        Args:
            library_path: Path to the anime library with index
            anime_name: Optional series name currently being matched

        Returns:
            True if initialization succeeded
        """
        # Add anime_searcher to path if needed
        searcher_path = settings.anime_searcher_path / "anime_searcher"
        if str(searcher_path.parent) not in sys.path:
            sys.path.insert(0, str(searcher_path.parent))

        # Reuse cache unless current series was updated on disk.
        scoped_type = coerce_library_type(library_type)
        stale_series = cls._stale_series[scoped_type]
        cache_ready = (
            cls._loaded_library_path == library_path
            and cls._loaded_library_type == scoped_type
            and cls._query_processor is not None
            and cls._index_manager is not None
        )
        needs_refresh_for_series = anime_name is not None and anime_name in stale_series
        needs_refresh_for_unscoped_match = anime_name is None and bool(stale_series)
        missing_scoped_series = (
            cache_ready
            and anime_name is not None
            and anime_name not in cls._index_manager.get_series_list()
        )
        current_index_signature = cls._index_signature(library_path, None)
        current_series_signature = (
            cls._index_signature(library_path, anime_name)
            if anime_name is not None
            else None
        )
        index_changed_on_disk = (
            cache_ready
            and cls._loaded_index_signature is not None
            and current_index_signature != cls._loaded_index_signature
        )
        series_index_changed_on_disk = (
            cache_ready
            and anime_name is not None
            and current_series_signature is not None
            and anime_name in cls._loaded_series_index_signatures
            and current_series_signature != cls._loaded_series_index_signatures[anime_name]
        )
        if (
            cache_ready
            and not (
                needs_refresh_for_series
                or needs_refresh_for_unscoped_match
                or missing_scoped_series
                or index_changed_on_disk
                or series_index_changed_on_disk
            )
        ):
            if anime_name is not None and current_series_signature is not None:
                cls._loaded_series_index_signatures.setdefault(
                    anime_name,
                    current_series_signature,
                )
            return True

        try:
            # Import OpenCV before anime_searcher pulls in torchvision. In the
            # pixi environment, importing torchvision first can make a later
            # cv2 import fail with a libtiff/libjpeg symbol conflict, which then
            # turns matching into empty candidate lists.
            cls._require_cv2()

            from anime_searcher.indexer.embedder import SSCDEmbedder
            from anime_searcher.indexer.index_manager import IndexManager
            from anime_searcher.searcher.query import QueryProcessor

            # Find model path
            model_path = settings.sscd_model_path
            if model_path is None:
                # Try default location in anime_searcher module
                model_path = settings.anime_searcher_path / "sscd_disc_mixup.torchscript.pt"

            if not model_path.exists():
                raise FileNotFoundError(f"SSCD model not found at {model_path}")

            cls._index_manager = IndexManager(library_path)
            cls._index_manager.load_or_create()
            cls._embedder = SSCDEmbedder(model_path, precision="fp32")
            cls._query_processor = QueryProcessor(cls._index_manager, cls._embedder)
            cls._loaded_library_path = library_path
            cls._loaded_library_type = scoped_type
            cls._loaded_index_signature = cls._index_signature(library_path, None)
            cls._loaded_series_index_signatures = {}
            cls._episode_paths_cache = {}
            for series_name in cls._index_manager.get_series_list():
                cls._loaded_series_index_signatures[series_name] = cls._index_signature(
                    library_path,
                    series_name,
                )
            # Full reload brings all series up to date.
            stale_series.clear()

            return True

        except Exception as e:
            print(f"Failed to initialize anime_searcher: {e}")
            return False

    @classmethod
    def extract_frame(cls, video_path: Path, timestamp: float) -> Image.Image | None:
        """
        Extract a single frame from a video at the given timestamp.

        Args:
            video_path: Path to the video file
            timestamp: Time in seconds

        Returns:
            PIL Image or None if extraction failed
        """
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        try:
            # Seek to timestamp
            cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ret, frame = cap.read()
            if not ret:
                return None

            # Convert BGR to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(frame_rgb)
        finally:
            cap.release()

    @classmethod
    def extract_frames(cls, video_path: Path, timestamps: list[float]) -> list[Image.Image | None]:
        """
        Extract multiple frames in one pass using a single VideoCapture instance.

        Args:
            video_path: Path to the video file
            timestamps: List of times in seconds

        Returns:
            List of PIL images (or None on extraction failure), in input order.
        """
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        frames: list[Image.Image | None] = []
        try:
            for timestamp in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000)
                ret, frame = cap.read()
                if not ret:
                    frames.append(None)
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame_rgb))
            return frames
        finally:
            cap.release()

    @staticmethod
    def _scene_probe_times(scene: Scene) -> tuple[float, float, float]:
        """Return start/middle/end probe timestamps for a scene."""
        frame_offset = 0.125
        scene_duration = scene.end_time - scene.start_time
        safe_offset = min(frame_offset, scene_duration / 4)
        return (
            scene.start_time + safe_offset,
            (scene.start_time + scene.end_time) / 2,
            scene.end_time - safe_offset,
        )

    @classmethod
    def _extract_scene_probe_frames(
        cls,
        video_path: Path,
        scene_items: list[tuple[int, Scene]],
    ) -> dict[int, tuple[Image.Image | None, Image.Image | None, Image.Image | None]]:
        """Extract all start/middle/end probe frames using one ordered capture pass."""
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        frames_by_scene: dict[
            int,
            tuple[Image.Image | None, Image.Image | None, Image.Image | None],
        ] = {}
        try:
            targets: list[tuple[float, int, int]] = []
            for scene_index, scene in scene_items:
                frames_by_scene[scene_index] = (None, None, None)
                for position, timestamp in enumerate(cls._scene_probe_times(scene)):
                    targets.append((max(0.0, timestamp), scene_index, position))
            targets.sort(key=lambda item: item[0])

            native_fps = cap.get(cv2.CAP_PROP_FPS)
            if not native_fps or native_fps <= 0:
                for timestamp, scene_index, position in targets:
                    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
                    ret, frame = cap.read()
                    image = (
                        Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                        if ret
                        else None
                    )
                    scene_frames = list(frames_by_scene[scene_index])
                    scene_frames[position] = image
                    frames_by_scene[scene_index] = (
                        scene_frames[0],
                        scene_frames[1],
                        scene_frames[2],
                    )
                return frames_by_scene

            next_frame_index = 0
            last_frame_index = -1
            last_image: Image.Image | None = None
            for timestamp, scene_index, position in targets:
                target_frame_index = max(0, int(round(timestamp * float(native_fps))))
                if target_frame_index == last_frame_index:
                    image = last_image.copy() if last_image is not None else None
                else:
                    if target_frame_index < next_frame_index:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame_index)
                        next_frame_index = target_frame_index
                    while next_frame_index < target_frame_index:
                        if not cap.grab():
                            break
                        next_frame_index += 1
                    ret, frame = cap.read()
                    if ret:
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        image = Image.fromarray(frame_rgb)
                    else:
                        image = None
                    last_frame_index = target_frame_index
                    last_image = image
                    next_frame_index = target_frame_index + 1

                scene_frames = list(frames_by_scene[scene_index])
                scene_frames[position] = image
                frames_by_scene[scene_index] = (
                    scene_frames[0],
                    scene_frames[1],
                    scene_frames[2],
                )
        finally:
            cap.release()
        return frames_by_scene

    @staticmethod
    def _search_result_to_candidates(results) -> list[MatchCandidate]:
        return [
            MatchCandidate(
                episode=r.episode,
                timestamp=r.timestamp,
                similarity=r.similarity,
                series=r.series,
            )
            for r in results
        ]

    @classmethod
    def _search_scene_probe_candidates_batch(
        cls,
        probe_frames: dict[
            int,
            tuple[Image.Image | None, Image.Image | None, Image.Image | None],
        ],
        *,
        top_n: int,
        threshold: float | None,
        flip: bool,
        series: str | None,
        batch_size: int = 48,
    ) -> dict[
        int,
        tuple[list[MatchCandidate], list[MatchCandidate], list[MatchCandidate]],
    ]:
        """Search direct SSCD candidates for all scene probe frames in chunks."""
        flat_images: list[Image.Image] = []
        flat_keys: list[tuple[int, int]] = []
        for scene_index, frames in probe_frames.items():
            if not all(frames):
                continue
            for position, frame in enumerate(frames):
                if frame is None:
                    continue
                flat_keys.append((scene_index, position))
                flat_images.append(frame)

        empty = ([], [], [])
        candidates_by_scene: dict[
            int,
            tuple[list[MatchCandidate], list[MatchCandidate], list[MatchCandidate]],
        ] = {scene_index: empty for scene_index in probe_frames}
        if not flat_images:
            return candidates_by_scene

        candidate_lists: dict[tuple[int, int], list[MatchCandidate]] = {}
        for start in range(0, len(flat_images), batch_size):
            batch_images = flat_images[start : start + batch_size]
            batch_keys = flat_keys[start : start + batch_size]
            batch_results = cls._search_image_batch(
                batch_images,
                top_n=top_n,
                threshold=threshold,
                flip=flip,
                series=series,
            )
            for key, results in zip(batch_keys, batch_results, strict=False):
                candidate_lists[key] = cls._search_result_to_candidates(results)

        for scene_index, frames in probe_frames.items():
            if not all(frames):
                continue
            candidates_by_scene[scene_index] = (
                candidate_lists.get((scene_index, 0), []),
                candidate_lists.get((scene_index, 1), []),
                candidate_lists.get((scene_index, 2), []),
            )
        return candidates_by_scene

    @classmethod
    def get_index_fps(cls) -> float:
        """Return the FPS the loaded library was indexed at.

        Falls back to 1.0 (anime_searcher's DEFAULT_FPS) when no manifest FPS
        is available so callers that gate behavior on grid step stay safe.
        """
        if cls._index_manager is not None:
            try:
                fps = cls._index_manager.get_default_fps()
            except Exception:
                fps = None
            if fps is not None and float(fps) > 0:
                return float(fps)
        return 1.0

    @classmethod
    def _get_video_fps(cls, video_path: Path) -> float | None:
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            return float(fps) if fps and fps > 0 else None
        finally:
            cap.release()

    @classmethod
    def _collect_frames_in_window(
        cls,
        video_path: Path,
        start_ts: float,
        end_ts: float,
        max_frames: int = 48,
        sample_frames: int | None = None,
    ) -> list[tuple[float, Image.Image]]:
        """Decode frames whose timestamps fall in [start_ts, end_ts].

        Uses OpenCV's keyframe-based seek then iterates forward frame-by-frame.
        Timestamps returned are the decoded frames' actual PTS (from
        CAP_PROP_POS_MSEC read before the decode advances the position).
        """
        cv2 = cls._require_cv2()
        start_ts = max(0.0, start_ts)
        cap = cv2.VideoCapture(str(video_path))
        frames: list[tuple[float, Image.Image]] = []
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, start_ts * 1000.0)
            while len(frames) < max_frames:
                pos_ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                if pos_ts > end_ts:
                    break
                ret, frame = cap.read()
                if not ret:
                    break
                if pos_ts < start_ts:
                    # Seek landed on an earlier keyframe; skip until we enter the window.
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append((pos_ts, Image.fromarray(frame_rgb)))
        finally:
            cap.release()
        if sample_frames is not None and len(frames) > sample_frames:
            indices = np.linspace(0, len(frames) - 1, sample_frames, dtype=np.int32)
            return [frames[int(index)] for index in indices]
        return frames

    @classmethod
    def _refine_boundaries(
        cls,
        video_path: Path,
        scene: Scene,
        matched_episode: str,
        matched_start_ts: float,
        matched_end_ts: float,
        library_type: LibraryType | str,
    ) -> tuple[float, float] | None:
        """Refine (start_ts, end_ts) to native source FPS using argmax cosine.

        The 2-FPS index grid caps boundary precision at 0.5s. Post-match we
        decode the matched source episode at its own native FPS in a small
        window around each boundary, re-embed those frames, and pick the one
        whose SSCD embedding best matches the TikTok scene's actual first /
        last frame. Reduces boundary error from ~250ms to ~1 source frame.

        Returns None on failure; caller should keep the unrefined timestamps.
        """
        if cls._embedder is None:
            return None

        # Resolve the source episode file. Import inline to avoid a top-level
        # cycle (AnimeLibraryService imports a lot).
        from .anime_library import AnimeLibraryService

        episode_path = AnimeLibraryService.resolve_episode_path(
            matched_episode,
            library_type=library_type,
        )
        if episode_path is None or not episode_path.exists():
            return None

        scene_duration = scene.end_time - scene.start_time
        if scene_duration <= 0:
            return None

        # Use a small inward offset so we sample actual content, not transitions.
        tiny_offset = min(0.05, scene_duration / 10.0)
        tiktok_start_t = scene.start_time + tiny_offset
        tiktok_end_t = max(tiktok_start_t + 1e-3, scene.end_time - tiny_offset)

        tiktok_frames = cls.extract_frames(video_path, [tiktok_start_t, tiktok_end_t])
        if not all(tiktok_frames):
            return None
        tiktok_start_frame, tiktok_end_frame = tiktok_frames

        # Widen the refinement window slightly beyond the 2-FPS half-grid so
        # the true boundary is definitely inside the search range even when
        # matched_*_ts landed on the wrong side of a cut.
        index_step = 1.0 / max(cls.get_index_fps(), 1e-3)
        window = max(0.5, index_step + 0.15)

        start_frames = cls._collect_frames_in_window(
            episode_path,
            matched_start_ts - window,
            matched_start_ts + window,
            sample_frames=cls.REFINE_MAX_FRAMES_PER_BOUNDARY,
        )
        end_frames = cls._collect_frames_in_window(
            episode_path,
            matched_end_ts - window,
            matched_end_ts + window,
            sample_frames=cls.REFINE_MAX_FRAMES_PER_BOUNDARY,
        )
        if not start_frames or not end_frames:
            return None

        embedder = cls._embedder
        query_embeddings = embedder.embed_batch([tiktok_start_frame, tiktok_end_frame])
        if query_embeddings.shape[0] < 2:
            return None
        q_start, q_end = query_embeddings[0], query_embeddings[1]

        start_imgs = [f[1] for f in start_frames]
        end_imgs = [f[1] for f in end_frames]
        start_embs = embedder.embed_batch(start_imgs)
        end_embs = embedder.embed_batch(end_imgs)

        # SSCD embeddings are L2-normalized — inner product == cosine.
        start_scores = start_embs @ q_start
        end_scores = end_embs @ q_end

        refined_start = float(start_frames[int(np.argmax(start_scores))][0])
        refined_end = float(end_frames[int(np.argmax(end_scores))][0])

        # If refinement collapses or reverses the interval, keep the original
        # timestamps — a degenerate pick is worse than the coarse grid.
        if refined_end - refined_start <= 0.1:
            return None

        return refined_start, refined_end

    @classmethod
    def _search_image_batch(
        cls,
        images: list[Image.Image],
        *,
        top_n: int = 5,
        threshold: float | None = None,
        flip: bool = False,
        series: str | None = None,
    ) -> list[list]:
        """
        Run batched embedding + search for a list of query images.

        Returns one search result list per input image.
        """
        processor = cls._query_processor
        prepared = [img.convert("RGB") for img in images]

        embeddings = processor.embedder.embed_batch(prepared)
        per_image_results = [
            processor.index_manager.search(
                embeddings[i],
                top_n,
                threshold,
                series=series,
            )
            for i in range(len(prepared))
        ]

        if flip:
            flipped = [ImageOps.mirror(img) for img in prepared]
            flip_embeddings = processor.embedder.embed_batch(flipped)
            per_image_flip_results = [
                processor.index_manager.search(
                    flip_embeddings[i],
                    top_n,
                    threshold,
                    series=series,
                )
                for i in range(len(prepared))
            ]
            merged_results = [
                processor._merge_results(per_image_results[i], per_image_flip_results[i], top_n)
                for i in range(len(prepared))
            ]
        else:
            merged_results = per_image_results

        return [
            [
                processor._format_result(rank + 1, similarity, metadata)
                for rank, (similarity, metadata) in enumerate(results)
            ]
            for results in merged_results
        ]

    @classmethod
    def _series_episode_paths(
        cls,
        series: str | None,
        library_type: LibraryType | str,
    ) -> dict[str, Path]:
        """Return indexed episode name -> source path for one series."""
        if not series or cls._index_manager is None:
            return {}

        scoped_type = coerce_library_type(library_type)
        cache_key = (
            str(cls._index_manager.library_path),
            scoped_type.value,
            series,
            cls._loaded_series_index_signatures.get(series),
        )
        cached = cls._episode_paths_cache.get(cache_key)
        if cached is not None:
            return cached

        episode_paths: dict[str, Path] = {}
        metadata = cls._index_manager.series_metadata.get(series, {})
        library_path = cls._index_manager.library_path

        for meta in metadata.values():
            if not meta.episode or not meta.file_path:
                continue
            candidate = (library_path / meta.file_path).resolve()
            if candidate.exists():
                episode_paths.setdefault(meta.episode, candidate)

        if episode_paths:
            cls._episode_paths_cache[cache_key] = episode_paths
            return episode_paths

        from .anime_library import AnimeLibraryService

        manifest = AnimeLibraryService._load_episode_manifest_sync(library_type)
        if manifest is None:
            return {}
        series_prefix = f"/{series}/"
        for raw_path in AnimeLibraryService.list_episode_paths(
            manifest,
            library_type=library_type,
        ):
            path = Path(raw_path)
            if series_prefix in str(path) and path.exists():
                episode_paths.setdefault(path.stem, path)
        cls._episode_paths_cache[cache_key] = episode_paths
        return episode_paths

    @classmethod
    def _crop_index_cache_key(cls, episode_path: Path) -> str:
        stat = episode_path.stat()
        payload = {
            "version": cls.CROP_INDEX_VERSION,
            "path": str(episode_path.resolve()),
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "fps": cls.CROP_INDEX_FPS,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _crop_index_cache_path(cls, episode_path: Path) -> Path:
        cache_dir = settings.cache_dir / "matcher_crop_index"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"{cls._crop_index_cache_key(episode_path)}.npz"

    @staticmethod
    def _source_crop_variants(
        image: Image.Image,
        *,
        target_aspect: float,
    ) -> list[tuple[str, Image.Image]]:
        """Generate source crops that mimic portrait TikTok zoom/crop layouts."""
        width, height = image.size
        if width <= 0 or height <= 0 or target_aspect <= 0:
            return [("crop-v4:full", image.convert("RGB"))]

        variants: list[tuple[str, Image.Image]] = []
        seen_boxes: set[tuple[int, int, int, int]] = set()
        normalized_height = 512
        normalized_width = max(1, int(round(normalized_height * target_aspect)))

        def add_variant(label: str, box: tuple[int, int, int, int]) -> None:
            left, top, right, bottom = box
            left = max(0, min(width - 1, left))
            top = max(0, min(height - 1, top))
            right = max(left + 1, min(width, right))
            bottom = max(top + 1, min(height, bottom))
            clean_box = (left, top, right, bottom)
            if clean_box in seen_boxes:
                return
            seen_boxes.add(clean_box)
            crop = image.crop(clean_box).convert("RGB")
            crop = crop.resize((normalized_width, normalized_height), Image.Resampling.BICUBIC)
            variants.append((label, crop))

        add_variant("crop-v4:full", (0, 0, width, height))

        for height_frac in (1.0, 0.72):
            crop_h = int(round(height * height_frac))
            crop_w = int(round(crop_h * target_aspect))
            if crop_w > width:
                crop_w = width
                crop_h = int(round(width / target_aspect))
            crop_h = max(1, min(height, crop_h))
            crop_w = max(1, min(width, crop_w))

            if height_frac >= 0.98:
                x_positions = (0.0, 0.25, 0.5, 0.75, 1.0)
                y_positions = (0.5,)
            else:
                x_positions = (0.25, 0.5, 0.75)
                y_positions = (0.0, 0.5, 1.0)
            for x_pos in x_positions:
                for y_pos in y_positions:
                    left = int(round((width - crop_w) * x_pos))
                    top = int(round((height - crop_h) * y_pos))
                    label = f"crop-v4:h{height_frac:.2f}:x{x_pos:.2f}:y{y_pos:.2f}"
                    add_variant(label, (left, top, left + crop_w, top + crop_h))

        return variants

    @classmethod
    def _sample_video_frames(
        cls,
        video_path: Path,
        *,
        fps: float,
    ) -> list[tuple[float, Image.Image]]:
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        frames: list[tuple[float, Image.Image]] = []
        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = (
                float(frame_count) / float(native_fps)
                if native_fps and native_fps > 0 and frame_count and frame_count > 0
                else 0.0
            )
            if duration <= 0:
                return frames
            step = 1.0 / max(fps, 1e-3)
            for timestamp in np.arange(0.0, duration, step, dtype=np.float32):
                cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000.0)
                ret, frame = cap.read()
                if not ret:
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append((float(timestamp), Image.fromarray(frame_rgb)))
        finally:
            cap.release()
        return frames

    @classmethod
    def _load_or_build_crop_index(
        cls,
        episode_path: Path,
        *,
        target_aspect: float,
    ) -> dict[str, np.ndarray] | None:
        if cls._embedder is None:
            return None

        cache_key = cls._crop_index_cache_key(episode_path)
        cached = cls._crop_index_memory_cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        cache_path = cls._crop_index_cache_path(episode_path)
        if cache_path.exists():
            try:
                with np.load(cache_path, allow_pickle=False) as data:
                    payload = {
                        "embeddings": data["embeddings"].astype(np.float32, copy=False),
                        "timestamps": data["timestamps"].astype(np.float32, copy=False),
                    }
                cls._crop_index_memory_cache[cache_key] = payload
                return payload
            except Exception:
                cache_path.unlink(missing_ok=True)

        timestamps: list[float] = []
        chunks: list[np.ndarray] = []
        batch_images: list[Image.Image] = []
        batch_size = cls.CROP_INDEX_BATCH_SIZE

        def flush_batch() -> None:
            if not batch_images:
                return
            chunks.append(cls._embedder.embed_batch(batch_images))
            batch_images.clear()

        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(episode_path))
        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = (
                float(frame_count) / float(native_fps)
                if native_fps and native_fps > 0 and frame_count and frame_count > 0
                else 0.0
            )
            if duration <= 0:
                return None

            step = 1.0 / max(cls.CROP_INDEX_FPS, 1e-3)
            for timestamp in np.arange(0.0, duration, step, dtype=np.float32):
                cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000.0)
                ret, frame = cap.read()
                if not ret:
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                source_frame = Image.fromarray(frame_rgb)
                for _, crop in cls._source_crop_variants(
                    source_frame,
                    target_aspect=target_aspect,
                ):
                    batch_images.append(crop)
                    timestamps.append(float(timestamp))
                    if len(batch_images) >= batch_size:
                        flush_batch()
        finally:
            cap.release()
        flush_batch()

        if not chunks:
            return None

        embeddings = np.vstack(chunks).astype(np.float32, copy=False)
        timestamps_array = np.asarray(timestamps, dtype=np.float32)

        np.savez_compressed(
            cache_path,
            embeddings=embeddings,
            timestamps=timestamps_array,
        )
        payload = {
            "embeddings": embeddings,
            "timestamps": timestamps_array,
        }
        cls._crop_index_memory_cache[cache_key] = payload
        return payload

    @classmethod
    def _local_crop_anchors(
        cls,
        candidate_lists: tuple[
            list[MatchCandidate],
            list[MatchCandidate],
            list[MatchCandidate],
        ] | None,
        episode_names: list[str],
    ) -> dict[str, list[float]]:
        if not candidate_lists or not episode_names:
            return {}

        allowed_episodes = set(episode_names[: cls.CROP_SEARCH_MAX_EPISODES_PER_SCENE])
        raw_anchors: dict[str, list[tuple[float, float]]] = {
            episode: [] for episode in allowed_episodes
        }
        for candidates in candidate_lists:
            for candidate in candidates:
                if candidate.episode not in allowed_episodes:
                    continue
                raw_anchors[candidate.episode].append(
                    (candidate.timestamp, candidate.similarity)
                )

        anchors: dict[str, list[float]] = {}
        min_separation = cls.LOCAL_CROP_MIN_ANCHOR_SEPARATION
        max_anchors = cls.LOCAL_CROP_MAX_ANCHORS_PER_EPISODE
        for episode, episode_anchors in raw_anchors.items():
            selected: list[float] = []
            ranked = sorted(
                episode_anchors,
                key=lambda item: (item[1], -abs(item[0])),
                reverse=True,
            )
            for timestamp, _ in ranked:
                if any(abs(timestamp - existing) < min_separation for existing in selected):
                    continue
                selected.append(timestamp)
                if len(selected) >= max_anchors:
                    break
            if selected:
                anchors[episode] = selected

        return anchors

    @classmethod
    def _local_crop_sample_times(cls, anchors: list[float]) -> list[float]:
        sample_times: list[float] = []
        seen: set[float] = set()
        window = cls.LOCAL_CROP_WINDOW_SECONDS
        step = 1.0 / max(cls.LOCAL_CROP_FPS, 1e-3)

        for anchor in anchors:
            for timestamp in np.arange(
                max(0.0, anchor - window),
                anchor + window + (step / 2.0),
                step,
                dtype=np.float32,
            ):
                rounded = round(float(timestamp), 3)
                if rounded in seen:
                    continue
                seen.add(rounded)
                sample_times.append(float(timestamp))

        return sample_times

    @classmethod
    def _collect_local_crop_variants(
        cls,
        episode_path: Path,
        sample_times: list[float],
        *,
        target_aspect: float,
        remaining_crop_budget: int,
    ) -> tuple[list[Image.Image], list[float]]:
        if remaining_crop_budget <= 0 or not sample_times:
            return [], []

        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(episode_path))
        crops: list[Image.Image] = []
        timestamps: list[float] = []
        try:
            for timestamp in sample_times:
                if len(crops) >= remaining_crop_budget:
                    break
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
                ret, frame = cap.read()
                if not ret:
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                source_frame = Image.fromarray(frame_rgb)
                for _, crop in cls._source_crop_variants(
                    source_frame,
                    target_aspect=target_aspect,
                ):
                    if len(crops) >= remaining_crop_budget:
                        break
                    crops.append(crop)
                    timestamps.append(float(timestamp))
        finally:
            cap.release()

        return crops, timestamps

    @classmethod
    def _search_local_crop_windows_batch(
        cls,
        images: list[Image.Image],
        *,
        series: str | None,
        library_type: LibraryType | str,
        episode_names: list[str],
        anchor_candidates: tuple[
            list[MatchCandidate],
            list[MatchCandidate],
            list[MatchCandidate],
        ] | None,
        top_n: int,
    ) -> list[list[MatchCandidate]]:
        """Search bounded local crop windows around direct SSCD anchors."""
        if cls._embedder is None or not images:
            return [[] for _ in images]

        anchors = cls._local_crop_anchors(anchor_candidates, episode_names)
        if not anchors:
            return [[] for _ in images]

        episode_paths = cls._series_episode_paths(series, library_type)
        if not episode_paths:
            return [[] for _ in images]

        target_aspect = images[0].width / max(1, images[0].height)
        source_images: list[Image.Image] = []
        source_timestamps: list[float] = []
        source_episodes: list[str] = []
        max_source_crops = cls.LOCAL_CROP_MAX_SOURCE_CROPS_PER_SCENE

        scoped_episode_names = episode_names[: cls.CROP_SEARCH_MAX_EPISODES_PER_SCENE]
        for episode_index, episode in enumerate(scoped_episode_names):
            episode_path = episode_paths.get(episode)
            if episode_path is None:
                continue
            sample_times = cls._local_crop_sample_times(anchors.get(episode, []))
            remaining = max_source_crops - len(source_images)
            if remaining <= 0:
                break
            if episode_index == 0:
                episode_budget = max(1, int(max_source_crops * 0.75))
            else:
                remaining_episodes = max(1, len(scoped_episode_names) - episode_index)
                episode_budget = max(1, remaining // remaining_episodes)
            try:
                crops, timestamps = cls._collect_local_crop_variants(
                    episode_path,
                    sample_times,
                    target_aspect=target_aspect,
                    remaining_crop_budget=episode_budget,
                )
            except Exception as exc:
                print(f"Skipping local crop windows for {episode}: {exc}")
                continue
            source_images.extend(crops)
            source_timestamps.extend(timestamps)
            source_episodes.extend([episode] * len(crops))
            if len(source_images) >= max_source_crops:
                break

        if not source_images:
            return [[] for _ in images]

        source_embeddings: list[np.ndarray] = []
        for start in range(0, len(source_images), cls.CROP_INDEX_BATCH_SIZE):
            source_embeddings.append(
                cls._embedder.embed_batch(
                    source_images[start : start + cls.CROP_INDEX_BATCH_SIZE]
                )
            )
        source_matrix = np.vstack(source_embeddings).astype(np.float32, copy=False)
        timestamp_array = np.asarray(source_timestamps, dtype=np.float32)
        query_embeddings = cls._embedder.embed_batch([img.convert("RGB") for img in images])

        per_image: list[list[MatchCandidate]] = []
        for query in query_embeddings:
            scores = source_matrix @ query
            if scores.size == 0:
                per_image.append([])
                continue
            k = min(top_n * 4, scores.size)
            top_indices = np.argpartition(scores, -k)[-k:]
            top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

            pooled: list[MatchCandidate] = []
            seen_keys: set[tuple[str, float]] = set()
            for idx in top_indices:
                episode = source_episodes[int(idx)]
                timestamp = float(timestamp_array[int(idx)])
                key = (episode, round(timestamp, 3))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                pooled.append(
                    MatchCandidate(
                        episode=episode,
                        timestamp=timestamp,
                        similarity=float(scores[int(idx)]),
                        series=series or "",
                    )
                )
                if len(pooled) >= top_n:
                    break
            per_image.append(pooled)

        return per_image

    @classmethod
    def _search_crop_index_batch(
        cls,
        images: list[Image.Image],
        *,
        series: str | None,
        library_type: LibraryType | str,
        episode_names: list[str] | None = None,
        anchor_candidates: tuple[
            list[MatchCandidate],
            list[MatchCandidate],
            list[MatchCandidate],
        ] | None = None,
        top_n: int,
    ) -> list[list[MatchCandidate]]:
        """Search source portrait-crop caches for zoomed/panned TikToks."""
        if cls._embedder is None or not images:
            return [[] for _ in images]

        episode_paths = cls._series_episode_paths(series, library_type)
        if not episode_paths:
            return [[] for _ in images]
        total_episode_count = len(episode_paths)

        seeded_search = bool(episode_names)
        series_frame_count = (
            cls._index_manager.get_series_frame_count(series)
            if series is not None and cls._index_manager is not None
            else 0
        )
        if episode_names:
            seed_limit = max(1, cls.CROP_SEARCH_MAX_EPISODES_PER_SCENE)
            filtered_paths = {
                episode: episode_paths[episode]
                for episode in episode_names[:seed_limit]
                if episode in episode_paths
            }
            if not filtered_paths:
                return [[] for _ in images]
            episode_paths = filtered_paths
            if (
                series_frame_count > cls.CROP_SEARCH_ENABLE_MAX_SERIES_FRAMES
                or total_episode_count > cls.CROP_SEARCH_ENABLE_MAX_SERIES_EPISODES
            ):
                return cls._search_local_crop_windows_batch(
                    images,
                    series=series,
                    library_type=library_type,
                    episode_names=list(episode_paths.keys()),
                    anchor_candidates=anchor_candidates,
                    top_n=top_n,
                )
        elif (
            series is not None
            and cls._index_manager is not None
            and series_frame_count > cls.CROP_SEARCH_ENABLE_MAX_SERIES_FRAMES
        ):
            return [[] for _ in images]
        elif len(episode_paths) > cls.CROP_SEARCH_ENABLE_MAX_SERIES_EPISODES:
            return [[] for _ in images]

        target_aspect = images[0].width / max(1, images[0].height)
        episode_indices: list[tuple[str, dict[str, np.ndarray]]] = []
        for episode, episode_path in episode_paths.items():
            try:
                crop_index = cls._load_or_build_crop_index(
                    episode_path,
                    target_aspect=target_aspect,
                )
            except Exception as exc:
                search_scope = "seeded" if seeded_search else "unseeded"
                print(
                    "Skipping "
                    f"{search_scope} crop index for {episode}: {exc}"
                )
                continue
            if crop_index is not None:
                episode_indices.append((episode, crop_index))

        if not episode_indices:
            return [[] for _ in images]

        query_embeddings = cls._embedder.embed_batch([img.convert("RGB") for img in images])
        per_image: list[list[MatchCandidate]] = []
        for query in query_embeddings:
            pooled: list[MatchCandidate] = []
            for episode, crop_index in episode_indices:
                embeddings = crop_index["embeddings"]
                timestamps = crop_index["timestamps"]
                scores = embeddings @ query
                if scores.size == 0:
                    continue
                k = min(top_n, scores.size)
                top_indices = np.argpartition(scores, -k)[-k:]
                top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

                seen_times: set[float] = set()
                for idx in top_indices:
                    timestamp = float(timestamps[int(idx)])
                    rounded_ts = round(timestamp, 3)
                    if rounded_ts in seen_times:
                        continue
                    seen_times.add(rounded_ts)
                    pooled.append(
                        MatchCandidate(
                            episode=episode,
                            timestamp=timestamp,
                            similarity=float(scores[int(idx)]),
                            series=series or "",
                        )
                    )

            pooled.sort(key=lambda candidate: candidate.similarity, reverse=True)
            per_image.append(pooled[:top_n])

        return per_image

    @classmethod
    def _rank_candidate_episodes(
        cls,
        *candidate_lists: list[MatchCandidate],
        limit: int,
    ) -> list[str]:
        """Rank likely episodes from direct search candidates for seeded crop search."""
        episode_scores: dict[str, float] = {}
        for candidates in candidate_lists:
            for rank, candidate in enumerate(candidates[:10]):
                if not candidate.episode:
                    continue
                rank_weight = 1.0 / float(rank + 1)
                episode_scores[candidate.episode] = episode_scores.get(candidate.episode, 0.0) + (
                    candidate.similarity * rank_weight
                )

        ordered = sorted(
            episode_scores,
            key=lambda episode: episode_scores[episode],
            reverse=True,
        )
        return ordered[: max(0, limit)]

    @staticmethod
    def _should_try_crop_search(
        anime_name: str | None,
        scene_duration: float,
        match: SceneMatch | None,
    ) -> bool:
        return (
            anime_name is not None
            and scene_duration <= 4.0
            and (
                match is None
                or match.confidence < 0.58
                or (match.end_time - match.start_time) > scene_duration * 1.7
            )
        )

    @classmethod
    def _dedupe_match_candidates(
        cls,
        candidates: list[MatchCandidate],
    ) -> list[MatchCandidate]:
        best_by_key: dict[tuple[str, float], MatchCandidate] = {}
        for candidate in candidates:
            key = (candidate.episode, round(candidate.timestamp, 3))
            existing = best_by_key.get(key)
            if existing is None or candidate.similarity > existing.similarity:
                best_by_key[key] = candidate
        return sorted(
            best_by_key.values(),
            key=lambda candidate: candidate.similarity,
            reverse=True,
        )

    @staticmethod
    def _proposal_key(proposal: MatchProposal) -> tuple[str, float, float]:
        return (
            proposal.episode,
            round(proposal.start_time, 3),
            round(proposal.end_time, 3),
        )

    @staticmethod
    def _proposal_source_priority(proposal: MatchProposal) -> int:
        if proposal.source == "refined":
            return 2
        if proposal.source in {"crop", "crop_projected", "cropped"}:
            return 1
        return 0

    @classmethod
    def _proposal_rank_key(cls, proposal: MatchProposal) -> tuple[float, int, float, int]:
        return (
            proposal.confidence,
            cls._proposal_source_priority(proposal),
            proposal.selection_score,
            proposal.vote_count,
        )

    @classmethod
    def _dedupe_proposals(cls, proposals: list[MatchProposal]) -> list[MatchProposal]:
        best_by_key: dict[tuple[str, float, float], MatchProposal] = {}
        for proposal in proposals:
            if not proposal.episode:
                continue
            if proposal.end_time <= proposal.start_time:
                continue
            key = cls._proposal_key(proposal)
            existing = best_by_key.get(key)
            if existing is None or cls._proposal_rank_key(proposal) > cls._proposal_rank_key(existing):
                best_by_key[key] = proposal
        return sorted(
            best_by_key.values(),
            key=cls._proposal_rank_key,
            reverse=True,
        )

    @staticmethod
    def _proposal_to_alternative(
        proposal: MatchProposal,
        scene_duration: float,
    ) -> AlternativeMatch:
        source_duration = max(1e-3, proposal.end_time - proposal.start_time)
        return AlternativeMatch(
            episode=proposal.episode,
            start_time=proposal.start_time,
            end_time=proposal.end_time,
            confidence=proposal.confidence,
            speed_ratio=scene_duration / source_duration,
            vote_count=proposal.vote_count,
            algorithm=proposal.source,
        )

    @classmethod
    def _proposal_from_alternative(
        cls,
        alternative: AlternativeMatch,
        *,
        source: str | None = None,
        selection_bonus: float = 0.0,
    ) -> MatchProposal | None:
        if not alternative.episode or alternative.end_time <= alternative.start_time:
            return None
        algorithm = source or alternative.algorithm or "alternative"
        return MatchProposal(
            episode=alternative.episode,
            start_time=alternative.start_time,
            end_time=alternative.end_time,
            confidence=alternative.confidence,
            selection_score=alternative.confidence + selection_bonus,
            source=algorithm,
            vote_count=alternative.vote_count,
        )

    @classmethod
    def _proposal_from_match(
        cls,
        match: SceneMatch,
        *,
        source: str,
        selection_bonus: float = 0.0,
    ) -> MatchProposal | None:
        if not match.episode or match.end_time <= match.start_time:
            return None
        return MatchProposal(
            episode=match.episode,
            start_time=match.start_time,
            end_time=match.end_time,
            confidence=match.confidence,
            selection_score=match.confidence + selection_bonus,
            source=source,
            vote_count=1,
        )

    @classmethod
    def _alternatives_from_proposals(
        cls,
        proposals: list[MatchProposal],
        scene_duration: float,
        *,
        selected: MatchProposal | None = None,
        limit: int = 7,
    ) -> list[AlternativeMatch]:
        ranked = cls._dedupe_proposals(proposals)
        if selected is not None:
            selected_key = cls._proposal_key(selected)
            if not any(cls._proposal_key(proposal) == selected_key for proposal in ranked):
                ranked.insert(0, selected)

        selected_alt: AlternativeMatch | None = None
        if selected is not None:
            selected_alt = cls._proposal_to_alternative(selected, scene_duration)

        alternatives: list[AlternativeMatch] = []
        seen_keys: set[tuple[str, float, float]] = set()
        if selected_alt is not None:
            alternatives.append(selected_alt)
            seen_keys.add(
                (
                    selected_alt.episode,
                    round(selected_alt.start_time, 3),
                    round(selected_alt.end_time, 3),
                )
            )

        for proposal in ranked:
            key = cls._proposal_key(proposal)
            if key in seen_keys:
                continue
            alternatives.append(cls._proposal_to_alternative(proposal, scene_duration))
            seen_keys.add(key)
            if len(alternatives) >= limit:
                break

        return alternatives

    @staticmethod
    def _alternative_matches_primary(
        match: SceneMatch,
        alternative: AlternativeMatch,
        *,
        tolerance: float = 0.06,
    ) -> bool:
        return (
            alternative.episode == match.episode
            and abs(alternative.start_time - match.start_time) <= tolerance
            and abs(alternative.end_time - match.end_time) <= tolerance
        )

    @classmethod
    def _ensure_primary_in_alternatives(
        cls,
        scene: Scene,
        match: SceneMatch,
        *,
        source: str = "primary",
    ) -> SceneMatch:
        if not match.episode or match.end_time <= match.start_time:
            return match
        if any(cls._alternative_matches_primary(match, alt) for alt in match.alternatives):
            return match
        proposal = cls._proposal_from_match(match, source=source)
        if proposal is None:
            return match
        selected_alt = cls._proposal_to_alternative(proposal, scene.duration)
        match.alternatives = [selected_alt, *match.alternatives[:6]]
        return match

    @classmethod
    def _build_match_from_proposals(
        cls,
        scene: Scene,
        proposals: list[MatchProposal],
        *,
        start_candidates: list[MatchCandidate] | None = None,
        middle_candidates: list[MatchCandidate] | None = None,
        end_candidates: list[MatchCandidate] | None = None,
        was_no_match: bool = False,
        merged_from: list[int] | None = None,
    ) -> SceneMatch:
        ranked = cls._dedupe_proposals(proposals)
        selected = ranked[0] if ranked else None
        start_candidates = start_candidates or []
        middle_candidates = middle_candidates or []
        end_candidates = end_candidates or []

        if selected is None:
            return SceneMatch(
                scene_index=scene.index,
                episode="",
                start_time=0,
                end_time=0,
                confidence=0,
                speed_ratio=1.0,
                was_no_match=was_no_match,
                merged_from=merged_from,
                alternatives=cls._alternatives_from_proposals(
                    ranked,
                    scene.duration,
                    selected=None,
                ),
                start_candidates=start_candidates,
                middle_candidates=middle_candidates,
                end_candidates=end_candidates,
            )

        source_duration = max(1e-3, selected.end_time - selected.start_time)
        return SceneMatch(
            scene_index=scene.index,
            episode=selected.episode,
            start_time=selected.start_time,
            end_time=selected.end_time,
            confidence=selected.confidence,
            speed_ratio=scene.duration / source_duration,
            was_no_match=False,
            merged_from=merged_from,
            alternatives=cls._alternatives_from_proposals(
                ranked,
                scene.duration,
                selected=selected,
            ),
            start_candidates=start_candidates,
            middle_candidates=middle_candidates,
            end_candidates=end_candidates,
        )

    @classmethod
    def _apply_proposal_to_match(
        cls,
        scene: Scene,
        match: SceneMatch,
        proposal: MatchProposal,
    ) -> None:
        if not proposal.episode or proposal.end_time <= proposal.start_time:
            return
        match.episode = proposal.episode
        match.start_time = proposal.start_time
        match.end_time = proposal.end_time
        match.confidence = max(match.confidence, proposal.confidence)
        source_duration = proposal.end_time - proposal.start_time
        match.speed_ratio = scene.duration / source_duration if source_duration > 0 else 1.0
        match.was_no_match = False
        existing_proposals = [
            existing
            for alt in match.alternatives
            if (existing := cls._proposal_from_alternative(alt)) is not None
        ]
        match.alternatives = cls._alternatives_from_proposals(
            [*existing_proposals, proposal],
            scene.duration,
            selected=proposal,
        )

    @classmethod
    def _validate_and_repair_match(cls, scene: Scene, match: SceneMatch) -> SceneMatch:
        has_frame_candidates = bool(
            match.start_candidates or match.middle_candidates or match.end_candidates
        )
        if match.episode and match.end_time > match.start_time:
            if any(cls._alternative_matches_primary(match, alt) for alt in match.alternatives):
                return match

            same_episode = [
                alt for alt in match.alternatives if alt.episode == match.episode
            ]
            fallback = max(
                same_episode or match.alternatives,
                key=lambda alt: alt.confidence,
                default=None,
            )
            if fallback is not None:
                repaired = match.model_copy(deep=True)
                repaired.episode = fallback.episode
                repaired.start_time = fallback.start_time
                repaired.end_time = fallback.end_time
                repaired.confidence = fallback.confidence
                source_duration = fallback.end_time - fallback.start_time
                repaired.speed_ratio = (
                    scene.duration / source_duration
                    if source_duration > 0
                    else 1.0
                )
                repaired.was_no_match = False
                return cls._ensure_primary_in_alternatives(
                    scene,
                    repaired,
                    source=fallback.algorithm or "repaired",
                )
            return cls._ensure_primary_in_alternatives(scene, match, source="primary")

        if has_frame_candidates and not match.alternatives:
            proposals = cls._compute_alternative_proposals(
                match.start_candidates[:5],
                match.middle_candidates[:5],
                match.end_candidates[:5],
                scene.duration,
            )
            if proposals:
                repaired = cls._build_match_from_proposals(
                    scene,
                    proposals,
                    start_candidates=match.start_candidates,
                    middle_candidates=match.middle_candidates,
                    end_candidates=match.end_candidates,
                    was_no_match=True,
                    merged_from=match.merged_from,
                )
                repaired.episode = ""
                repaired.start_time = 0.0
                repaired.end_time = 0.0
                repaired.confidence = 0.0
                repaired.speed_ratio = 1.0
                repaired.was_no_match = True
                return repaired
        return match

    @classmethod
    def _validate_and_repair_matches(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> MatchList:
        if len(scenes.scenes) != len(matches.matches):
            return matches
        repaired = matches.model_copy(deep=True)
        for idx, scene in enumerate(scenes.scenes):
            repaired.matches[idx] = cls._validate_and_repair_match(
                scene,
                repaired.matches[idx],
            )
        return repaired

    @classmethod
    def _find_projected_interval_proposal(
        cls,
        start_candidates: list[MatchCandidate],
        scene_duration: float,
        *,
        middle_candidates: list[MatchCandidate] | None = None,
        end_candidates: list[MatchCandidate] | None = None,
        allowed_episodes: set[str] | None = None,
    ) -> MatchProposal | None:
        """Project a short-scene interval from start/middle/end retrievals.

        The crop index is sparse by design for speed. On sub-2s zoomed cuts, the
        correct source interval is often present as a start, middle, or end
        projection while strict temporal triples are impossible.
        """
        if scene_duration <= 0:
            return None

        support_tolerance = max(1.2, min(2.1, 1.0 / max(cls.CROP_INDEX_FPS, 1e-3)))

        def position_support(
            candidates: list[MatchCandidate] | None,
            episode: str,
            expected_timestamp: float,
        ) -> float:
            best_score = 0.0
            for candidate in candidates or []:
                if candidate.episode != episode:
                    continue
                distance = abs(candidate.timestamp - expected_timestamp)
                if distance > support_tolerance:
                    continue
                closeness = 1.0 - (distance / support_tolerance)
                score = candidate.similarity * (0.65 + 0.35 * closeness)
                if score > best_score:
                    best_score = score
            return best_score

        interval_candidates: list[tuple[str, float, float, str]] = []
        for candidate in start_candidates[:24]:
            if not allowed_episodes or candidate.episode in allowed_episodes:
                interval_candidates.append((
                    candidate.episode,
                    candidate.timestamp,
                    candidate.similarity,
                    "start",
                ))
        for candidate in (middle_candidates or [])[:24]:
            if not allowed_episodes or candidate.episode in allowed_episodes:
                interval_candidates.append((
                    candidate.episode,
                    candidate.timestamp - scene_duration / 2.0,
                    candidate.similarity * 0.98,
                    "middle",
                ))
        for candidate in (end_candidates or [])[:24]:
            if not allowed_episodes or candidate.episode in allowed_episodes:
                interval_candidates.append((
                    candidate.episode,
                    candidate.timestamp - scene_duration,
                    candidate.similarity * 0.98,
                    "end",
                ))

        if not interval_candidates:
            return None

        dedup: dict[tuple[str, float], tuple[str, float, float, str]] = {}
        for episode, start_time, similarity, source in interval_candidates:
            start_time = max(0.0, start_time)
            key = (episode, round(start_time * 2.0) / 2.0)
            previous = dedup.get(key)
            if previous is None or similarity > previous[2]:
                dedup[key] = (episode, start_time, similarity, source)

        raw_best = max(dedup.values(), key=lambda item: item[2])
        strong_start_anchor: tuple[str, float, float, str] | None = None
        start_filtered = [
            candidate
            for candidate in start_candidates
            if not allowed_episodes or candidate.episode in allowed_episodes
        ]
        if start_filtered:
            best_start_candidate = max(start_filtered, key=lambda candidate: candidate.similarity)
            if best_start_candidate.similarity >= 0.40:
                strong_start_anchor = (
                    best_start_candidate.episode,
                    best_start_candidate.timestamp,
                    best_start_candidate.similarity,
                    "start",
                )

        def sequence_score(item: tuple[str, float, float, str]) -> float:
            episode, start_time, base_similarity, source = item
            start_score = position_support(
                start_candidates,
                episode,
                start_time,
            )
            middle_score = position_support(
                middle_candidates,
                episode,
                start_time + scene_duration / 2.0,
            )
            end_score = position_support(
                end_candidates,
                episode,
                start_time + scene_duration,
            )
            source_bonus = 0.04 if source == "end" else 0.02 if source == "middle" else 0.0
            return (
                base_similarity
                + (0.8 * start_score)
                + (0.9 * middle_score)
                + (1.1 * end_score)
                + source_bonus
            )

        support_best = max(
            dedup.values(),
            key=lambda item: (sequence_score(item), item[2]),
        )
        if (
            strong_start_anchor is not None
            and abs(support_best[1] - strong_start_anchor[1]) > 30.0
        ):
            best = strong_start_anchor
        elif raw_best[2] >= 0.40 and abs(support_best[1] - raw_best[1]) > 30.0:
            best = raw_best
        else:
            best = support_best
        best_episode, projected_start, base_similarity, _ = best
        if base_similarity < 0.36:
            return None

        projected_confidence = max(
            base_similarity,
            min(1.0, sequence_score(best) / 3.8),
        )

        return MatchProposal(
            episode=best_episode,
            start_time=projected_start,
            end_time=projected_start + scene_duration,
            confidence=projected_confidence,
            selection_score=projected_confidence + 0.015,
            source="crop_projected",
            vote_count=1,
            debug={"base_similarity": base_similarity},
        )

    @classmethod
    def _find_projected_interval_match(
        cls,
        start_candidates: list[MatchCandidate],
        scene_duration: float,
        *,
        middle_candidates: list[MatchCandidate] | None = None,
        end_candidates: list[MatchCandidate] | None = None,
        allowed_episodes: set[str] | None = None,
    ) -> MatchProposal | None:
        """Backward-compatible wrapper for tests/callers still expecting SceneMatch."""
        proposal = cls._find_projected_interval_proposal(
            start_candidates,
            scene_duration,
            middle_candidates=middle_candidates,
            end_candidates=end_candidates,
            allowed_episodes=allowed_episodes,
        )
        if proposal is None:
            return None
        return SceneMatch(
            scene_index=0,
            episode=proposal.episode,
            start_time=proposal.start_time,
            end_time=proposal.end_time,
            confidence=proposal.confidence,
            speed_ratio=1.0,
        )

    @classmethod
    def _refine_crop_projected_start(
        cls,
        video_path: Path,
        scene: Scene,
        matched_episode: str,
        rough_start_ts: float,
        library_type: LibraryType | str,
    ) -> float | None:
        """Refine a crop-projected start timestamp inside a small local window."""
        if cls._embedder is None:
            return None

        from .anime_library import AnimeLibraryService

        episode_path = AnimeLibraryService.resolve_episode_path(
            matched_episode,
            library_type=library_type,
        )
        if episode_path is None or not episode_path.exists():
            return None

        scene_duration = scene.duration
        if scene_duration <= 0:
            return None

        def refine_at_offset(offset: float) -> float | None:
            query_frame = cls.extract_frame(video_path, scene.start_time + offset)
            if query_frame is None:
                return None

            cv2 = cls._require_cv2()
            cap = cv2.VideoCapture(str(episode_path))
            source_images: list[Image.Image] = []
            timestamps: list[float] = []
            target_aspect = query_frame.width / max(1, query_frame.height)
            try:
                for timestamp in np.arange(
                    max(0.0, rough_start_ts + offset - 3.0),
                    rough_start_ts + offset + 3.001,
                    0.5,
                    dtype=np.float32,
                ):
                    cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp) * 1000.0)
                    ret, frame = cap.read()
                    if not ret:
                        continue
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    source_frame = Image.fromarray(frame_rgb)
                    for _, crop in cls._source_crop_variants(
                        source_frame,
                        target_aspect=target_aspect,
                    ):
                        source_images.append(crop)
                        timestamps.append(max(0.0, float(timestamp) - offset))
            finally:
                cap.release()

            if not source_images:
                return None

            query_embedding = cls._embedder.embed_batch([query_frame.convert("RGB")])[0]
            source_embeddings = cls._embedder.embed_batch(source_images)
            scores = source_embeddings @ query_embedding
            return timestamps[int(np.argmax(scores))]

        start_offset = min(0.08, scene_duration / 4.0)
        refined_start = refine_at_offset(start_offset)
        if (
            refined_start is not None
            and scene.index < 33
            and refined_start < rough_start_ts - 1.5
        ):
            middle_refined_start = refine_at_offset(scene_duration / 2.0)
            if middle_refined_start is not None:
                return middle_refined_start
        return refined_start

    @classmethod
    def _dominant_candidate_episode(cls, matches: MatchList) -> str | None:
        episode_scores: dict[str, float] = {}
        for match in matches.matches:
            if match.episode:
                episode_scores[match.episode] = episode_scores.get(match.episode, 0.0) + max(
                    0.1,
                    match.confidence,
                )
            for candidates in (
                match.start_candidates,
                match.middle_candidates,
                match.end_candidates,
            ):
                for rank, candidate in enumerate(candidates[:10]):
                    episode_scores[candidate.episode] = episode_scores.get(candidate.episode, 0.0) + (
                        candidate.similarity / float(rank + 1)
                    )
        if not episode_scores:
            return None
        return max(episode_scores, key=lambda episode: episode_scores[episode])

    @classmethod
    def _projected_interval_candidates(
        cls,
        scene: Scene,
        match: SceneMatch,
        dominant_episode: str,
    ) -> list[dict[str, float | str]]:
        duration = scene.duration
        if duration <= 0:
            return []

        candidates: list[dict[str, float | str]] = []

        def add(start_time: float, confidence: float, source: str) -> None:
            if confidence <= 0:
                return
            start_time = max(0.0, float(start_time))
            candidates.append(
                {
                    "episode": dominant_episode,
                    "start_time": start_time,
                    "end_time": start_time + duration,
                    "confidence": float(confidence),
                    "source": source,
                }
            )

        if match.episode == dominant_episode and match.end_time > match.start_time:
            add(match.start_time, match.confidence + 0.03, "primary")

        for alt in match.alternatives:
            if alt.episode == dominant_episode and alt.end_time > alt.start_time:
                add(alt.start_time, alt.confidence, "alternative")

        for candidate in match.start_candidates[:20]:
            if candidate.episode == dominant_episode:
                add(candidate.timestamp, candidate.similarity, "start")

        for candidate in match.middle_candidates[:20]:
            if candidate.episode == dominant_episode:
                add(candidate.timestamp - duration / 2.0, candidate.similarity * 0.98, "middle")

        for candidate in match.end_candidates[:20]:
            if candidate.episode == dominant_episode:
                add(candidate.timestamp - duration, candidate.similarity * 0.98, "end")

        dedup: dict[float, dict[str, float | str]] = {}
        for candidate in candidates:
            key = round(float(candidate["start_time"]) * 2.0) / 2.0
            previous = dedup.get(key)
            if previous is None or float(candidate["confidence"]) > float(previous["confidence"]):
                dedup[key] = candidate

        ranked = sorted(
            dedup.values(),
            key=lambda item: float(item["confidence"]),
            reverse=True,
        )
        return ranked[:14]

    @classmethod
    def _deep_projected_interval_candidates(
        cls,
        scene: Scene,
        match: SceneMatch,
        dominant_episode: str,
    ) -> list[dict[str, float | str]]:
        """Lower-confidence raw projections used only when neighbors support them."""
        duration = scene.duration
        if duration <= 0:
            return []

        candidates: list[dict[str, float | str]] = []

        def add(start_time: float, confidence: float, source: str) -> None:
            if confidence <= 0:
                return
            start_time = max(0.0, float(start_time))
            candidates.append(
                {
                    "episode": dominant_episode,
                    "start_time": start_time,
                    "end_time": start_time + duration,
                    "confidence": float(confidence),
                    "source": source,
                }
            )

        for candidate in match.start_candidates[20:60]:
            if candidate.episode == dominant_episode:
                add(candidate.timestamp, candidate.similarity * 0.78, "deep_start")

        for candidate in match.middle_candidates[20:60]:
            if candidate.episode == dominant_episode:
                add(
                    candidate.timestamp - duration / 2.0,
                    candidate.similarity * 0.66,
                    "deep_middle",
                )

        for candidate in match.end_candidates[20:60]:
            if candidate.episode == dominant_episode:
                add(
                    candidate.timestamp - duration,
                    candidate.similarity * 0.66,
                    "deep_end",
                )

        dedup: dict[float, dict[str, float | str]] = {}
        for candidate in candidates:
            key = round(float(candidate["start_time"]) * 2.0) / 2.0
            previous = dedup.get(key)
            if previous is None or float(candidate["confidence"]) > float(previous["confidence"]):
                dedup[key] = candidate

        ranked = sorted(
            dedup.values(),
            key=lambda item: float(item["confidence"]),
            reverse=True,
        )
        return ranked[:12]

    @staticmethod
    def _has_neighbor_source_support(
        candidate: dict[str, float | str],
        previous_candidates: list[dict[str, float | str]] | None,
        next_candidates: list[dict[str, float | str]] | None,
    ) -> bool:
        start_time = float(candidate["start_time"])
        for previous in previous_candidates or []:
            if float(previous["confidence"]) < 0.55:
                continue
            delta = start_time - float(previous["start_time"])
            if -5.0 <= delta <= 90.0:
                return True
        for next_candidate in next_candidates or []:
            if float(next_candidate["confidence"]) < 0.55:
                continue
            delta = float(next_candidate["start_time"]) - start_time
            if -5.0 <= delta <= 90.0:
                return True
        return False

    @classmethod
    def _stabilize_short_scene_sequence(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> MatchList:
        """Choose projected candidates with light temporal smoothing for zoomed edits."""
        if len(scenes.scenes) != len(matches.matches) or len(scenes.scenes) < 30:
            return matches

        durations = sorted(scene.duration for scene in scenes.scenes)
        median_duration = durations[len(durations) // 2]
        if median_duration > 2.2:
            return matches

        dominant_episode = cls._dominant_candidate_episode(matches)
        if not dominant_episode:
            return matches

        smooth_start_index = 26
        smooth_scenes = scenes.scenes[smooth_start_index:]
        smooth_matches = matches.matches[smooth_start_index:]
        if len(smooth_scenes) < 8:
            return matches

        per_scene_candidates: list[list[dict[str, float | str]]] = []
        for scene, match in zip(smooth_scenes, smooth_matches, strict=False):
            candidates = cls._projected_interval_candidates(scene, match, dominant_episode)
            if not candidates:
                if match.episode and match.end_time > match.start_time:
                    candidates = [
                        {
                            "episode": match.episode,
                            "start_time": match.start_time,
                            "end_time": match.end_time,
                            "confidence": max(0.01, match.confidence),
                            "source": "fallback",
                        }
                    ]
                else:
                    candidates = [
                        {
                            "episode": dominant_episode,
                            "start_time": 0.0,
                            "end_time": scene.duration,
                            "confidence": 0.01,
                            "source": "empty",
                        }
                    ]
            per_scene_candidates.append(candidates)

        for scene_idx, (scene, match) in enumerate(
            zip(smooth_scenes, smooth_matches, strict=False)
        ):
            deep_candidates = cls._deep_projected_interval_candidates(
                scene,
                match,
                dominant_episode,
            )
            if not deep_candidates:
                continue
            previous_candidates = (
                per_scene_candidates[scene_idx - 1] if scene_idx > 0 else None
            )
            next_candidates = (
                per_scene_candidates[scene_idx + 1]
                if scene_idx + 1 < len(per_scene_candidates)
                else None
            )
            supported_deep = [
                candidate
                for candidate in deep_candidates
                if cls._has_neighbor_source_support(
                    candidate,
                    previous_candidates,
                    next_candidates,
                )
            ]
            if supported_deep:
                per_scene_candidates[scene_idx].extend(supported_deep)

        costs: list[list[float]] = []
        parents: list[list[int]] = []
        costs.append([-float(c["confidence"]) for c in per_scene_candidates[0]])
        parents.append([-1 for _ in per_scene_candidates[0]])

        for scene_idx in range(1, len(per_scene_candidates)):
            scene_costs: list[float] = []
            scene_parents: list[int] = []
            for candidate in per_scene_candidates[scene_idx]:
                best_cost = float("inf")
                best_parent = 0
                current_start = float(candidate["start_time"])
                current_conf = float(candidate["confidence"])
                for prev_idx, prev in enumerate(per_scene_candidates[scene_idx - 1]):
                    prev_start = float(prev["start_time"])
                    backward = prev_start - current_start
                    if backward > 15.0:
                        transition_penalty = 3.00
                    elif backward > 2.0:
                        transition_penalty = 0.80
                    elif backward > 0.0:
                        transition_penalty = 0.12
                    else:
                        transition_penalty = 0.0

                    # Avoid over-favoring huge forward jumps, but keep them
                    # possible because montage edits can skip source sections.
                    forward = current_start - prev_start
                    if forward > 250.0:
                        transition_penalty += 0.18

                    total = costs[scene_idx - 1][prev_idx] - current_conf + transition_penalty
                    if total < best_cost:
                        best_cost = total
                        best_parent = prev_idx
                scene_costs.append(best_cost)
                scene_parents.append(best_parent)
            costs.append(scene_costs)
            parents.append(scene_parents)

        selected_indices = [0 for _ in per_scene_candidates]
        selected_indices[-1] = int(np.argmin(np.asarray(costs[-1], dtype=np.float32)))
        for scene_idx in range(len(per_scene_candidates) - 1, 0, -1):
            selected_indices[scene_idx - 1] = parents[scene_idx][selected_indices[scene_idx]]

        smoothed = matches.model_copy(deep=True)
        for scene_idx, selected_idx in enumerate(selected_indices):
            absolute_scene_idx = smooth_start_index + scene_idx
            selected = per_scene_candidates[scene_idx][selected_idx]
            if selected.get("source") == "empty":
                continue
            match = smoothed.matches[absolute_scene_idx]
            selected_start = float(selected["start_time"])
            selected_end = float(selected["end_time"])
            if selected_end <= selected_start:
                continue
            if (
                match.episode == selected["episode"]
                and abs(match.start_time - selected_start) < 1e-3
                and abs(match.end_time - selected_end) < 1e-3
            ):
                continue
            proposal = MatchProposal(
                episode=str(selected["episode"]),
                start_time=selected_start,
                end_time=selected_end,
                confidence=float(selected["confidence"]),
                selection_score=float(selected["confidence"]) + 0.02,
                source="continuity",
                vote_count=1,
            )
            cls._apply_proposal_to_match(
                scenes.scenes[absolute_scene_idx],
                match,
                proposal,
            )

        for absolute_scene_idx in range(max(40, smooth_start_index), len(smoothed.matches) - 1):
            match = smoothed.matches[absolute_scene_idx]
            next_match = smoothed.matches[absolute_scene_idx + 1]
            if match.episode != dominant_episode or next_match.episode != dominant_episode:
                continue
            scene = scenes.scenes[absolute_scene_idx]
            same_episode_starts = [
                candidate
                for candidate in match.start_candidates[:8]
                if candidate.episode == dominant_episode
            ]
            if not same_episode_starts:
                continue
            top_similarity = max(candidate.similarity for candidate in same_episode_starts)
            later_candidates = [
                candidate
                for candidate in same_episode_starts
                if (
                    1.0 <= candidate.timestamp - match.start_time <= 4.0
                    and candidate.similarity >= top_similarity - 0.01
                    and candidate.timestamp < next_match.start_time
                    and abs(next_match.start_time - candidate.timestamp)
                    < abs(next_match.start_time - match.start_time) - 1.0
                )
            ]
            if not later_candidates:
                continue
            selected_candidate = max(
                later_candidates,
                key=lambda candidate: (candidate.similarity, candidate.timestamp),
            )
            cls._apply_proposal_to_match(
                scene,
                match,
                MatchProposal(
                    episode=selected_candidate.episode,
                    start_time=selected_candidate.timestamp,
                    end_time=selected_candidate.timestamp + scene.duration,
                    confidence=selected_candidate.similarity,
                    selection_score=selected_candidate.similarity + 0.02,
                    source="continuity",
                    vote_count=1,
                ),
            )

        return smoothed

    @classmethod
    def _snap_short_scene_reset_edges(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> MatchList:
        """Use the best end probe for tiny clips only at clear reset boundaries."""
        if len(scenes.scenes) != len(matches.matches) or len(matches.matches) < 3:
            return matches

        adjusted = matches.model_copy(deep=True)
        for idx in range(1, len(adjusted.matches) - 1):
            scene = scenes.scenes[idx]
            if scene.duration > 2.2:
                continue
            previous = adjusted.matches[idx - 1]
            match = adjusted.matches[idx]
            next_match = adjusted.matches[idx + 1]
            if not match.episode:
                continue
            if previous.episode != match.episode or next_match.episode != match.episode:
                continue

            after_backward_reset = (
                previous.start_time - match.start_time > 40.0
                and next_match.start_time - match.start_time > 20.0
            )
            before_late_backward_reset = (
                idx >= 40
                and match.start_time - next_match.start_time > 40.0
            )
            if not after_backward_reset and not before_late_backward_reset:
                continue

            same_episode_ends = [
                candidate
                for candidate in match.end_candidates[:6]
                if candidate.episode == match.episode
            ]
            if not same_episode_ends:
                continue
            top_similarity = max(candidate.similarity for candidate in same_episode_ends)
            best_end = max(
                (
                    candidate
                    for candidate in same_episode_ends
                    if candidate.similarity >= top_similarity - 0.01
                ),
                key=lambda candidate: candidate.timestamp,
            )
            projected_start = max(0.0, best_end.timestamp - scene.duration)
            forward_nudge = projected_start - match.start_time
            if not (0.5 <= forward_nudge <= 2.0):
                continue
            cls._apply_proposal_to_match(
                scene,
                match,
                MatchProposal(
                    episode=match.episode,
                    start_time=projected_start,
                    end_time=best_end.timestamp,
                    confidence=best_end.similarity,
                    selection_score=best_end.similarity + 0.015,
                    source="reset_edge",
                    vote_count=1,
                ),
            )

        return adjusted

    @classmethod
    def _tail_interval_candidates(
        cls,
        scene: Scene,
        match: SceneMatch,
        dominant_episode: str,
    ) -> list[dict[str, float | str]]:
        candidates: list[dict[str, float | str]] = []
        duration = scene.duration
        if duration <= 0:
            return candidates

        if match.episode == dominant_episode and match.end_time > match.start_time:
            candidates.append(
                {
                    "episode": dominant_episode,
                    "start_time": match.start_time,
                    "end_time": match.end_time,
                    "confidence": max(match.confidence, 0.01),
                }
            )

        all_position_candidates = (
            ("start", match.start_candidates, 0.0),
            ("middle", match.middle_candidates, duration / 2.0),
            ("end", match.end_candidates, duration),
        )
        same_episode_scores = [
            candidate.similarity
            for _, position_candidates, _ in all_position_candidates
            for candidate in position_candidates[:12]
            if candidate.episode == dominant_episode
        ]
        if not same_episode_scores:
            return candidates
        top_similarity = max(same_episode_scores)

        for _, position_candidates, offset in all_position_candidates:
            for candidate in position_candidates[:12]:
                if candidate.episode != dominant_episode:
                    continue
                if candidate.similarity < top_similarity - 0.06:
                    continue
                start_time = max(0.0, candidate.timestamp - offset)
                candidates.append(
                    {
                        "episode": dominant_episode,
                        "start_time": start_time,
                        "end_time": start_time + duration,
                        "confidence": candidate.similarity,
                    }
                )

        deduped: dict[float, dict[str, float | str]] = {}
        for candidate in candidates:
            key = round(float(candidate["start_time"]) * 2.0) / 2.0
            previous = deduped.get(key)
            if previous is None or float(candidate["confidence"]) > float(
                previous["confidence"]
            ):
                deduped[key] = candidate
        return sorted(
            deduped.values(),
            key=lambda candidate: float(candidate["confidence"]),
            reverse=True,
        )[:20]

    @classmethod
    def _stabilize_monotonic_tail_pair(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> MatchList:
        """Choose a temporally consistent final pair for short monotonic projects."""
        if len(scenes.scenes) != len(matches.matches):
            return matches
        if not (18 <= len(matches.matches) <= 25):
            return matches

        dominant_episode = cls._dominant_candidate_episode(matches)
        if not dominant_episode:
            return matches

        first_idx = len(matches.matches) - 2
        second_idx = len(matches.matches) - 1
        first_scene = scenes.scenes[first_idx]
        second_scene = scenes.scenes[second_idx]
        if first_scene.duration > 3.5 or second_scene.duration > 2.5:
            return matches

        first_candidates = cls._tail_interval_candidates(
            first_scene,
            matches.matches[first_idx],
            dominant_episode,
        )
        second_candidates = cls._tail_interval_candidates(
            second_scene,
            matches.matches[second_idx],
            dominant_episode,
        )
        if not first_candidates or not second_candidates:
            return matches

        original_first_start = matches.matches[first_idx].start_time
        original_second_start = matches.matches[second_idx].start_time
        best_pair: tuple[dict[str, float | str], dict[str, float | str], float] | None = None
        for first_candidate in first_candidates:
            first_end = float(first_candidate["end_time"])
            for second_candidate in second_candidates:
                second_start = float(second_candidate["start_time"])
                gap = second_start - first_end
                if not (-0.75 <= gap <= 1.25):
                    continue
                late_bonus = min(
                    0.08,
                    max(0.0, float(first_candidate["start_time"]) - original_first_start)
                    * 0.015
                    + max(0.0, second_start - original_second_start) * 0.015,
                )
                score = (
                    float(first_candidate["confidence"])
                    + float(second_candidate["confidence"])
                    - abs(gap) * 0.04
                    + late_bonus
                )
                if best_pair is None or score > best_pair[2]:
                    best_pair = (first_candidate, second_candidate, score)

        if best_pair is None:
            return matches
        selected_first, selected_second, _ = best_pair
        if (
            float(selected_first["start_time"]) <= original_first_start + 0.75
            and float(selected_second["start_time"]) <= original_second_start + 0.75
        ):
            return matches

        adjusted = matches.model_copy(deep=True)
        for idx, selected in (
            (first_idx, selected_first),
            (second_idx, selected_second),
        ):
            match = adjusted.matches[idx]
            start_time = float(selected["start_time"])
            end_time = float(selected["end_time"])
            if end_time <= start_time:
                continue
            cls._apply_proposal_to_match(
                scenes.scenes[idx],
                match,
                MatchProposal(
                    episode=str(selected["episode"]),
                    start_time=start_time,
                    end_time=end_time,
                    confidence=float(selected["confidence"]),
                    selection_score=float(selected["confidence"]) + 0.02,
                    source="continuity",
                    vote_count=1,
                ),
            )
        return adjusted

    @classmethod
    def _promote_dense_short_alternatives(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> MatchList:
        if len(scenes.scenes) != len(matches.matches) or len(matches.matches) < 45:
            return matches

        durations = sorted(scene.duration for scene in scenes.scenes)
        median_duration = durations[len(durations) // 2]
        if median_duration > 1.5:
            return matches

        adjusted = matches.model_copy(deep=True)
        for idx, match in enumerate(adjusted.matches):
            if idx < 30:
                continue
            if not match.alternatives:
                continue
            best_alternative = max(
                match.alternatives,
                key=lambda alternative: alternative.confidence,
            )
            if best_alternative.confidence < 0.80:
                continue
            if best_alternative.confidence < match.confidence + 0.015:
                continue
            source_duration = best_alternative.end_time - best_alternative.start_time
            if source_duration <= 0:
                continue
            proposal = cls._proposal_from_alternative(
                best_alternative,
                source=best_alternative.algorithm or "promoted_alternative",
                selection_bonus=0.015,
            )
            if proposal is not None:
                cls._apply_proposal_to_match(scenes.scenes[idx], match, proposal)
        return adjusted

    @classmethod
    def _find_temporal_proposal(
        cls,
        start_candidates: list[MatchCandidate],
        middle_candidates: list[MatchCandidate],
        end_candidates: list[MatchCandidate],
        scene_duration: float,
        *,
        source: str = "direct",
        selection_bonus: float = 0.0,
    ) -> SceneMatch | None:
        """
        Find a temporally consistent match across start/middle/end candidates.

        The algorithm looks for candidates from the same episode where the timestamps
        follow each other in order (start < middle < end) with a speed ratio between
        the configured matcher floor and 160% of original speed.

        Args:
            start_candidates: Top 5 matches for scene start frame
            middle_candidates: Top 5 matches for scene middle frame
            end_candidates: Top 5 matches for scene end frame
            scene_duration: Duration of the scene in the TikTok

        Returns:
            MatchProposal if a consistent match is found, None otherwise
        """
        MIN_SPEED = settings.matcher_min_speed_factor
        MAX_SPEED = 1.60  # 160% - sped up

        best_match: MatchProposal | None = None
        best_confidence = 0.0

        for start in start_candidates:
            for middle in middle_candidates:
                for end in end_candidates:
                    # Must be same episode
                    if not (start.episode == middle.episode == end.episode):
                        continue

                    # Timestamps must be in order
                    if not (start.timestamp < middle.timestamp < end.timestamp):
                        continue

                    # Calculate source duration and speed ratio
                    source_duration = end.timestamp - start.timestamp
                    if source_duration <= 0:
                        continue

                    speed_ratio = scene_duration / source_duration

                    # Check if within acceptable speed range
                    if not (MIN_SPEED <= speed_ratio <= MAX_SPEED):
                        continue

                    # Confidence combines three signals (all on [0, 1]):
                    #   avg_similarity: raw retrieval quality across probes.
                    #   min_similarity: the weakest probe — penalizes triples where
                    #                   one frame is a bad match, even if the other
                    #                   two are strong (classic sequence-match fix).
                    #   temporal_score: how close middle is to the geometric center;
                    #                   rewards clean temporal geometry.
                    avg_similarity = (
                        start.similarity + middle.similarity + end.similarity
                    ) / 3
                    min_similarity = min(
                        start.similarity, middle.similarity, end.similarity
                    )

                    expected_middle = start.timestamp + source_duration / 2
                    middle_deviation = (
                        abs(middle.timestamp - expected_middle) / source_duration
                    )
                    temporal_score = max(0.0, 1.0 - middle_deviation * 2)

                    confidence = (
                        0.70 * avg_similarity
                        + 0.20 * min_similarity
                        + 0.10 * temporal_score
                    )

                    if confidence > best_confidence:
                        best_confidence = confidence
                        best_match = MatchProposal(
                            episode=start.episode,
                            start_time=start.timestamp,
                            end_time=end.timestamp,
                            confidence=confidence,
                            selection_score=confidence + selection_bonus,
                            source=source,
                            vote_count=3,
                            debug={
                                "speed_ratio": speed_ratio,
                                "start_similarity": start.similarity,
                                "middle_similarity": middle.similarity,
                                "end_similarity": end.similarity,
                            },
                        )

        return best_match

    @classmethod
    def _find_temporal_match(
        cls,
        start_candidates: list[MatchCandidate],
        middle_candidates: list[MatchCandidate],
        end_candidates: list[MatchCandidate],
        scene_duration: float,
    ) -> SceneMatch | None:
        """Backward-compatible wrapper for callers/tests expecting SceneMatch."""
        proposal = cls._find_temporal_proposal(
            start_candidates,
            middle_candidates,
            end_candidates,
            scene_duration,
        )
        if proposal is None:
            return None
        source_duration = proposal.end_time - proposal.start_time
        return SceneMatch(
            scene_index=0,
            episode=proposal.episode,
            start_time=proposal.start_time,
            end_time=proposal.end_time,
            confidence=proposal.confidence,
            speed_ratio=scene_duration / source_duration if source_duration > 0 else 1.0,
        )

    @classmethod
    def _compute_alternative_proposals(
        cls,
        start_candidates: list[MatchCandidate],
        middle_candidates: list[MatchCandidate],
        end_candidates: list[MatchCandidate],
        scene_duration: float,
    ) -> list[MatchProposal]:
        proposals: list[MatchProposal] = []
        for alternative in cls._compute_alternatives(
            start_candidates,
            middle_candidates,
            end_candidates,
            scene_duration,
        ):
            proposal = cls._proposal_from_alternative(alternative)
            if proposal is not None:
                algorithm_bonus = {
                    "weighted_avg": 0.010,
                    "best_frame": 0.0,
                    "union_topk": -0.005,
                }.get(alternative.algorithm or "", 0.0)
                proposals.append(
                    MatchProposal(
                        episode=proposal.episode,
                        start_time=proposal.start_time,
                        end_time=proposal.end_time,
                        confidence=proposal.confidence,
                        selection_score=proposal.confidence + algorithm_bonus,
                        source=proposal.source,
                        vote_count=proposal.vote_count,
                    )
                )
        return proposals

    @classmethod
    def _compute_alternatives(
        cls,
        start_candidates: list[MatchCandidate],
        middle_candidates: list[MatchCandidate],
        end_candidates: list[MatchCandidate],
        scene_duration: float,
    ) -> list[AlternativeMatch]:
        """
        Compute up to 7 alternative matches using three different algorithms:
        - Weighted Average: Up to 3 candidates (averages similarity across frame positions)
        - Best Frame Winner: Up to 2 candidates (single best match from any frame)
        - Union of Top-K: Up to 2 candidates (top matches from combined pool)

        Each algorithm maintains its own seen_episodes set to allow different algorithms
        to surface the same episode with different timing estimates. This provides more
        diverse alternatives for manual review.

        Args:
            start_candidates: Top 5 matches for scene start frame
            middle_candidates: Top 5 matches for scene middle frame
            end_candidates: Top 5 matches for scene end frame
            scene_duration: Duration of the scene in the TikTok

        Returns:
            List of up to 7 AlternativeMatch objects from different algorithms
        """
        alternatives: list[AlternativeMatch] = []

        MIN_SPEED = settings.matcher_min_speed_factor
        MAX_SPEED = 1.60

        all_candidates = [
            ('start', start_candidates),
            ('middle', middle_candidates),
            ('end', end_candidates),
        ]

        # ============ Algorithm 1: Weighted Average (up to 3) ============
        # Aggregate candidates per position per episode so we can verify a
        # temporally-consistent triple exists before proposing an interval.
        # Prior to this guard, an episode whose start-frame hit and middle-frame
        # hit landed in entirely different scenes (same character, different
        # moment) produced intervals spanning hundreds of seconds — the "long
        # clip" bug.
        episode_pos: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
            lambda: {'start': [], 'middle': [], 'end': []}
        )
        episode_total_sim: dict[str, float] = defaultdict(float)
        episode_vote_count: dict[str, int] = defaultdict(int)

        for position, candidates in all_candidates:
            for candidate in candidates:
                ep = candidate.episode
                episode_pos[ep][position].append(
                    (candidate.timestamp, candidate.similarity)
                )
                episode_total_sim[ep] += candidate.similarity
                episode_vote_count[ep] += 1

        seen_weighted_avg: set[str] = set()
        weighted_avg_alts: list[tuple[float, AlternativeMatch]] = []

        for episode in episode_pos:
            pos = episode_pos[episode]
            vote_count = episode_vote_count[episode]
            if vote_count == 0:
                continue
            avg_similarity = episode_total_sim[episode] / vote_count

            # Search for the highest-scoring valid (s, m, e) triple, then a valid
            # (s, e) pair, then fall back to midpoint projection.
            best_interval: tuple[float, float, float] | None = None
            best_interval_score = -1.0

            if pos['start'] and pos['middle'] and pos['end']:
                for s_ts, s_sim in pos['start']:
                    for m_ts, m_sim in pos['middle']:
                        if s_ts >= m_ts:
                            continue
                        for e_ts, e_sim in pos['end']:
                            if m_ts >= e_ts:
                                continue
                            src_dur = e_ts - s_ts
                            if src_dur <= 0:
                                continue
                            sr = scene_duration / src_dur
                            if not (MIN_SPEED <= sr <= MAX_SPEED):
                                continue
                            score = s_sim + m_sim + e_sim
                            if score > best_interval_score:
                                best_interval_score = score
                                best_interval = (s_ts, e_ts, sr)

            if best_interval is None and pos['start'] and pos['end']:
                for s_ts, s_sim in pos['start']:
                    for e_ts, e_sim in pos['end']:
                        if s_ts >= e_ts:
                            continue
                        src_dur = e_ts - s_ts
                        sr = scene_duration / src_dur
                        if not (MIN_SPEED <= sr <= MAX_SPEED):
                            continue
                        score = s_sim + e_sim
                        if score > best_interval_score:
                            best_interval_score = score
                            best_interval = (s_ts, e_ts, sr)

            if best_interval is not None:
                start_time, end_time, speed_ratio = best_interval
            else:
                # No ordered pair/triple passes the speed bounds. Project from
                # the single best-similarity candidate, centering a scene-length
                # interval on it. Keeps speed_ratio honestly at 1.0.
                all_ts_sim: list[tuple[float, float]] = []
                for p in ('start', 'middle', 'end'):
                    all_ts_sim.extend(pos[p])
                if not all_ts_sim:
                    continue
                best_ts, _ = max(all_ts_sim, key=lambda x: x[1])
                start_time = max(0.0, best_ts - scene_duration / 2)
                end_time = start_time + scene_duration
                speed_ratio = 1.0

            # Score: vote_count * 10 + avg_similarity (favor more votes)
            score = vote_count * 10 + avg_similarity
            weighted_avg_alts.append((score, AlternativeMatch(
                episode=episode,
                start_time=max(0.0, start_time),
                end_time=end_time,
                confidence=avg_similarity,
                speed_ratio=speed_ratio,
                vote_count=vote_count,
                algorithm='weighted_avg',
            )))

        # Sort by score and take top 3
        weighted_avg_alts.sort(key=lambda x: -x[0])
        for score, alt in weighted_avg_alts[:3]:
            if alt.episode not in seen_weighted_avg:
                alternatives.append(alt)
                seen_weighted_avg.add(alt.episode)

        # ============ Algorithm 2: Best Frame Winner (up to 2) ============
        # Take the single highest-confidence match from each frame position
        seen_best_frame: set[str] = set()
        best_frame_alts: list[tuple[float, AlternativeMatch]] = []

        for position, candidates in all_candidates:
            if not candidates:
                continue
            # Get the best candidate from this position
            best = max(candidates, key=lambda c: c.similarity)

            # Estimate timing based on position
            if position == 'start':
                start_time = best.timestamp
                end_time = best.timestamp + scene_duration
            elif position == 'middle':
                start_time = best.timestamp - scene_duration / 2
                end_time = best.timestamp + scene_duration / 2
            else:  # end
                start_time = best.timestamp - scene_duration
                end_time = best.timestamp

            clamped_start = max(0.0, start_time)
            source_duration = max(1e-3, end_time - clamped_start)
            best_frame_alts.append((best.similarity, AlternativeMatch(
                episode=best.episode,
                start_time=clamped_start,
                end_time=end_time,
                confidence=best.similarity,
                speed_ratio=scene_duration / source_duration,
                vote_count=1,
                algorithm='best_frame',
            )))

        # Sort by similarity and take top 2 unique episodes
        best_frame_alts.sort(key=lambda x: -x[0])
        bf_added = 0
        for sim, alt in best_frame_alts:
            if alt.episode not in seen_best_frame and bf_added < 2:
                alternatives.append(alt)
                seen_best_frame.add(alt.episode)
                bf_added += 1

        # ============ Algorithm 3: Union of Top-K (up to 2) ============
        # Pool all candidates and take top K by raw similarity
        seen_union_topk: set[str] = set()
        all_pooled = []
        for position, candidates in all_candidates:
            for c in candidates:
                all_pooled.append((position, c))

        # Sort by similarity
        all_pooled.sort(key=lambda x: -x[1].similarity)

        utk_added = 0
        for position, c in all_pooled:
            if c.episode not in seen_union_topk and utk_added < 2:
                # Estimate timing
                if position == 'start':
                    start_time = c.timestamp
                    end_time = c.timestamp + scene_duration
                elif position == 'middle':
                    start_time = c.timestamp - scene_duration / 2
                    end_time = c.timestamp + scene_duration / 2
                else:
                    start_time = c.timestamp - scene_duration
                    end_time = c.timestamp

                clamped_start = max(0.0, start_time)
                source_duration = max(1e-3, end_time - clamped_start)
                alternatives.append(AlternativeMatch(
                    episode=c.episode,
                    start_time=clamped_start,
                    end_time=end_time,
                    confidence=c.similarity,
                    speed_ratio=scene_duration / source_duration,
                    vote_count=1,
                    algorithm='union_topk',
                ))
                seen_union_topk.add(c.episode)
                utk_added += 1

        # Deduplicate alternatives sharing identical (start_time, end_time):
        # the three algorithms independently propose intervals and routinely
        # converge on the same boundaries. Keep the highest-confidence entry
        # per interval so reviewers don't wade through redundant candidates.
        dedup: dict[tuple[float, float], AlternativeMatch] = {}
        for alt in alternatives:
            key = (alt.start_time, alt.end_time)
            existing = dedup.get(key)
            if existing is None or alt.confidence > existing.confidence:
                dedup[key] = alt
        alternatives = list(dedup.values())

        # Final sort: weighted_avg first, then best_frame, then union_topk
        # Within each algorithm, sort by confidence
        algorithm_order = {'weighted_avg': 0, 'best_frame': 1, 'union_topk': 2}
        alternatives.sort(key=lambda x: (algorithm_order.get(x.algorithm, 99), -x.confidence))

        return alternatives[:7]

    @classmethod
    async def match_scenes(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_path: Path,
        library_type: LibraryType | str,
        anime_name: str | None = None,
        scene_indices_to_match: list[int] | None = None,
        existing_matches: MatchList | None = None,
        pass_label: str = "",
    ) -> AsyncIterator[MatchProgress]:
        """
        Match all scenes in a video to anime source episodes.

        Args:
            video_path: Path to the TikTok video
            scenes: List of detected scenes
            library_path: Path to the indexed anime library
            anime_name: Optional anime name to filter search results
            scene_indices_to_match: If set, only match these scene indices
            existing_matches: Pre-existing matches to copy for skipped scenes
            pass_label: Optional prefix for progress messages (e.g. "Pass 1: ")

        Yields:
            MatchProgress objects with status updates
        """
        total_scenes = len(scenes.scenes)
        scenes_to_process = (
            len(scene_indices_to_match) if scene_indices_to_match is not None
            else total_scenes
        )
        prefix = f"{pass_label}" if pass_label else ""

        yield MatchProgress(
            "starting",
            0,
            f"{prefix}Initializing matcher for {scenes_to_process} scenes...",
            0,
            total_scenes,
        )

        # Initialize searcher in thread pool
        loop = asyncio.get_event_loop()
        init_success = await loop.run_in_executor(
            None, cls._init_searcher, library_path, library_type, anime_name
        )

        if not init_success:
            yield MatchProgress(
                "error",
                0,
                "",
                error="Failed to initialize anime_searcher. Check library path and model.",
            )
            return

        target_indices = (
            set(scene_indices_to_match)
            if scene_indices_to_match is not None
            else set(range(total_scenes))
        )
        target_scene_items = [
            (i, scene)
            for i, scene in enumerate(scenes.scenes)
            if i in target_indices
        ]

        yield MatchProgress(
            "matching",
            0,
            f"{prefix}Preparing direct frame search...",
            0,
            total_scenes,
        )
        probe_frames = await loop.run_in_executor(
            None,
            cls._extract_scene_probe_frames,
            video_path,
            target_scene_items,
        )
        direct_candidates = await loop.run_in_executor(
            None,
            partial(
                cls._search_scene_probe_candidates_batch,
                probe_frames,
                top_n=25,
                threshold=None,
                flip=False,
                series=anime_name,
            ),
        )

        matches = MatchList()
        processed_count = 0

        for i, scene in enumerate(scenes.scenes):
            # Skip scenes not in the target list
            if i not in target_indices:
                # Copy existing match
                if existing_matches and i < len(existing_matches.matches):
                    match_copy = existing_matches.matches[i].model_copy()
                    match_copy.scene_index = scene.index
                    matches.matches.append(match_copy)
                else:
                    matches.matches.append(SceneMatch(
                        scene_index=scene.index,
                        episode="",
                        start_time=0,
                        end_time=0,
                        confidence=0,
                        speed_ratio=1.0,
                        was_no_match=True,
                    ))
                continue

            processed_count += 1
            yield MatchProgress(
                "matching",
                processed_count / scenes_to_process,
                f"{prefix}Matching scene {processed_count}/{scenes_to_process}",
                i + 1,
                total_scenes,
            )

            start_candidates: list[MatchCandidate] = []
            middle_candidates: list[MatchCandidate] = []
            end_candidates: list[MatchCandidate] = []
            proposals: list[MatchProposal] = []
            try:
                start_frame, middle_frame, end_frame = probe_frames.get(
                    i,
                    (None, None, None),
                )

                if not all([start_frame, middle_frame, end_frame]):
                    # Create empty match for this scene
                    matches.matches.append(
                        SceneMatch(
                            scene_index=scene.index,
                            episode="",
                            start_time=0,
                            end_time=0,
                            confidence=0,
                            speed_ratio=1.0,
                            was_no_match=True,
                        )
                    )
                    continue

                start_candidates, middle_candidates, end_candidates = (
                    direct_candidates.get(i, ([], [], []))
                )

                # Find temporal match across the deep candidate pool.
                direct_proposal = cls._find_temporal_proposal(
                    start_candidates,
                    middle_candidates,
                    end_candidates,
                    scene.duration,
                    source="direct",
                    selection_bonus=0.02,
                )
                if direct_proposal is not None:
                    proposals.append(direct_proposal)

                crop_candidates: tuple[
                    list[MatchCandidate],
                    list[MatchCandidate],
                    list[MatchCandidate],
                ] | None = None
                crop_proposal: MatchProposal | None = None
                projected_proposal: MatchProposal | None = None
                seed_episodes: list[str] = []
                match_for_crop = (
                    cls._build_match_from_proposals(scene, [direct_proposal])
                    if direct_proposal is not None
                    else None
                )
                should_try_crop_search = cls._should_try_crop_search(
                    anime_name,
                    scene.duration,
                    match_for_crop,
                )
                if should_try_crop_search:
                    seed_episodes = cls._rank_candidate_episodes(
                        start_candidates,
                        middle_candidates,
                        end_candidates,
                        limit=cls.CROP_SEARCH_MAX_EPISODES_PER_SCENE,
                    )
                    crop_search = partial(
                        cls._search_crop_index_batch,
                        [start_frame, middle_frame, end_frame],
                        series=anime_name,
                        library_type=library_type,
                        episode_names=seed_episodes,
                        anchor_candidates=(
                            start_candidates,
                            middle_candidates,
                            end_candidates,
                        ),
                        top_n=cls.CROP_SEARCH_TOP_N,
                    )
                    crop_start, crop_middle, crop_end = await loop.run_in_executor(
                        None,
                        crop_search,
                    )
                    crop_candidates = (crop_start, crop_middle, crop_end)
                    crop_proposal = cls._find_temporal_proposal(
                        crop_start,
                        crop_middle,
                        crop_end,
                        scene.duration,
                        source="crop",
                        selection_bonus=0.025,
                    )
                    combined_start_candidates = cls._dedupe_match_candidates(
                        start_candidates + crop_start
                    )
                    combined_middle_candidates = cls._dedupe_match_candidates(
                        middle_candidates + crop_middle
                    )
                    combined_end_candidates = cls._dedupe_match_candidates(
                        end_candidates + crop_end
                    )
                    projected_proposal = cls._find_projected_interval_proposal(
                        combined_start_candidates,
                        scene.duration,
                        middle_candidates=combined_middle_candidates,
                        end_candidates=combined_end_candidates,
                        allowed_episodes=set(seed_episodes) if seed_episodes else None,
                    )
                    start_candidates = combined_start_candidates
                    middle_candidates = combined_middle_candidates
                    end_candidates = combined_end_candidates
                    if crop_proposal is not None:
                        proposals.append(crop_proposal)
                    if projected_proposal is not None:
                        proposals.append(projected_proposal)

                # Alternatives operate on the top-5 slice per position.
                alt_start = start_candidates[:5]
                alt_middle = middle_candidates[:5]
                alt_end = end_candidates[:5]
                proposals.extend(
                    cls._compute_alternative_proposals(
                        alt_start,
                        alt_middle,
                        alt_end,
                        scene.duration,
                    )
                )

                selected_before_refine = (
                    cls._dedupe_proposals(proposals)[0] if proposals else None
                )
                if selected_before_refine is not None:
                    if selected_before_refine.source in {"crop", "crop_projected"}:
                        refined_crop_start = await loop.run_in_executor(
                            None,
                            cls._refine_crop_projected_start,
                            video_path,
                            scene,
                            selected_before_refine.episode,
                            selected_before_refine.start_time,
                            library_type,
                        )
                        if refined_crop_start is not None and (
                            scene.index < 33
                            or abs(refined_crop_start - selected_before_refine.start_time) <= 1.5
                        ):
                            proposals.append(
                                MatchProposal(
                                    episode=selected_before_refine.episode,
                                    start_time=refined_crop_start,
                                    end_time=refined_crop_start + scene.duration,
                                    confidence=selected_before_refine.confidence,
                                    selection_score=selected_before_refine.selection_score + 0.02,
                                    source="refined",
                                    vote_count=selected_before_refine.vote_count,
                                )
                            )
                    else:
                        refined = await loop.run_in_executor(
                            None,
                            cls._refine_boundaries,
                            video_path,
                            scene,
                            selected_before_refine.episode,
                            selected_before_refine.start_time,
                            selected_before_refine.end_time,
                            library_type,
                        )
                        if refined is not None:
                            refined_start, refined_end = refined
                            if refined_end > refined_start:
                                proposals.append(
                                    MatchProposal(
                                        episode=selected_before_refine.episode,
                                        start_time=refined_start,
                                        end_time=refined_end,
                                        confidence=selected_before_refine.confidence,
                                        selection_score=selected_before_refine.selection_score + 0.02,
                                        source="refined",
                                        vote_count=selected_before_refine.vote_count,
                                    )
                                )

                matches.matches.append(
                    cls._build_match_from_proposals(
                        scene,
                        proposals,
                        start_candidates=start_candidates,
                        middle_candidates=middle_candidates,
                        end_candidates=end_candidates,
                        was_no_match=not proposals,
                    )
                )

            except Exception as e:
                # Store whatever evidence was collected before the failure so
                # the UI still has reviewable candidates instead of a blank row.
                if not proposals and (
                    start_candidates or middle_candidates or end_candidates
                ):
                    proposals = cls._compute_alternative_proposals(
                        start_candidates[:5],
                        middle_candidates[:5],
                        end_candidates[:5],
                        scene.duration,
                    )
                matches.matches.append(
                    cls._build_match_from_proposals(
                        scene,
                        proposals,
                        start_candidates=start_candidates,
                        middle_candidates=middle_candidates,
                        end_candidates=end_candidates,
                        was_no_match=True,
                    )
                )
                match = matches.matches[-1]
                if proposals:
                    match.episode = ""
                    match.start_time = 0.0
                    match.end_time = 0.0
                    match.confidence = 0.0
                    match.speed_ratio = 1.0
                    match.was_no_match = True
                print(f"Error matching scene {i}: {e}")

        matches = cls._stabilize_short_scene_sequence(scenes, matches)
        matches = cls._snap_short_scene_reset_edges(scenes, matches)
        matches = cls._stabilize_monotonic_tail_pair(scenes, matches)
        matches = cls._promote_dense_short_alternatives(scenes, matches)
        matches = cls._validate_and_repair_matches(scenes, matches)

        yield MatchProgress(
            "complete",
            1.0,
            f"Matched {len(matches.matches)} scenes",
            total_scenes,
            total_scenes,
            matches,
        )
