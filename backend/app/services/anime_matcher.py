"""Anime source matching service using anime_searcher module."""

import asyncio
import hashlib
import json
import math
import sys
import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, AsyncIterator

import numpy as np
from PIL import Image, ImageOps

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from ..models import AlternativeMatch, MatchCandidate, MatchList, Scene, SceneMatch, SceneList
from .runtime_memory import release_unused_memory


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


@dataclass(frozen=True)
class DenseSourceCandidate:
    """Candidate interval with the evidence needed for dense montage re-ranking."""

    proposal: MatchProposal
    support: float
    support_count: int
    best_similarity: float
    move_from_base: float
    duration_error: float
    cut_bonus: float
    is_cut_aligned: bool


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
    # Per-(video signature, native frame index) LRU cache of SSCD embeddings for
    # TikTok frames. Reused across passes when scene re-matching lands on a
    # frame index that was already embedded in an earlier pass. Bounded so the
    # in-RAM embedding vectors do not accumulate unbounded across projects.
    _video_frame_embedding_cache: "OrderedDict[tuple[str, int, int, int], np.ndarray]" = (
        OrderedDict()
    )
    VIDEO_FRAME_EMBEDDING_CACHE_MAX = 8192
    # Cumulative per-run instrumentation (reset by reset_runtime_stats).
    _runtime_stats: dict[str, float] = defaultdict(float)
    REFINE_MAX_FRAMES_PER_BOUNDARY = 12
    MAX_SEQUENTIAL_GRAB_FRAMES = 90
    DENSE_SOURCE_CUT_THRESHOLDS = (27.0, 18.0, 12.0, 8.0, 5.0)
    DENSE_SOURCE_CUT_MIN_SCENE_LEN = 3
    DENSE_SOURCE_CUT_FRAME_SKIP = 0
    DENSE_SOURCE_CUT_MAX_EPISODES = 3
    DENSE_VISUAL_RERANK_MAX_SCENES = 10
    DENSE_VISUAL_RERANK_MAX_CANDIDATES = 6
    DENSE_VISUAL_RERANK_MARGIN = 0.012

    @classmethod
    def _clear_dependent_index_caches(cls) -> None:
        """Drop caches that contain data derived from the current FAISS manager."""
        try:
            from .scene_aligner import SceneAlignerService

            SceneAlignerService.clear_index_caches()
        except Exception:
            pass

    @classmethod
    def release_matching_resources(cls, *, reason: str = "matching_phase_exit") -> None:
        """Release SSCD, FAISS shards, decoder sessions, and matching caches."""
        cls._clear_dependent_index_caches()

        manager = cls._index_manager
        unloaded_series = 0
        if manager is not None:
            try:
                unloaded_series = manager.unload_all_series()
            except Exception:
                pass

        # QueryProcessor owns both manager and embedder, so break it first.
        cls._query_processor = None
        cls._index_manager = None
        cls._embedder = None
        cls._loaded_library_path = None
        cls._loaded_library_type = None
        cls._loaded_index_signature = None
        cls._loaded_series_index_signatures = {}
        cls._episode_paths_cache = {}
        cls._video_frame_embedding_cache.clear()
        cls.reset_runtime_stats()

        pynv_decode = sys.modules.get(f"{__package__}.pynv_decode")
        if pynv_decode is not None:
            try:
                pynv_decode.close_pool()
            except Exception:
                pass

        # Return freed native FAISS/model pages now, at the phase boundary,
        # rather than waiting until transcription eventually finishes.
        release_unused_memory(
            "matching_resources_released",
            reason=reason,
            unloaded_faiss_series=unloaded_series,
        )

    @classmethod
    def _get_cached_video_frame_embedding(
        cls, key: tuple[str, int, int, int],
    ) -> np.ndarray | None:
        """Fetch a cached probe embedding, refreshing its LRU recency on hit."""
        cache = cls._video_frame_embedding_cache
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value

    @classmethod
    def _store_video_frame_embedding(
        cls, key: tuple[str, int, int, int], embedding: np.ndarray,
    ) -> None:
        """Insert a probe embedding and evict the oldest entries past the bound."""
        cache = cls._video_frame_embedding_cache
        cache[key] = embedding
        cache.move_to_end(key)
        while len(cache) > cls.VIDEO_FRAME_EMBEDDING_CACHE_MAX:
            cache.popitem(last=False)

    @classmethod
    def reset_runtime_stats(cls) -> None:
        cls._runtime_stats = defaultdict(float)

    @classmethod
    def get_runtime_stats(cls) -> dict[str, float]:
        stats = dict(cls._runtime_stats)
        stats["video_frame_embedding_cache_size"] = float(
            len(cls._video_frame_embedding_cache)
        )
        return stats

    @classmethod
    def _record_runtime_stat(cls, name: str, value: float = 1.0) -> None:
        cls._runtime_stats[name] += float(value)

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

            new_index_manager = IndexManager(library_path)
            new_index_manager.load_or_create()
            # FAST MODE (F3): fp16 embedder + TF32 when the flag is on; exact
            # fp32 mainline otherwise. Numeric config is global-but-idempotent
            # and only touched inside the fast branch (flag-off byte-identity).
            from . import fast_matching

            fast_matching.configure_numerics()
            _precision = fast_matching.embedder_precision(default="fp32")
            # Index manifests may change while the 95 MiB SSCD model remains
            # identical. Reuse the model so a shard refresh does not create a
            # transient second CUDA model and another allocator burst.
            embedder = cls._embedder
            if embedder is None:
                embedder = SSCDEmbedder(model_path, precision=_precision)
            new_query_processor = QueryProcessor(new_index_manager, embedder)

            old_index_manager = cls._index_manager
            cls._clear_dependent_index_caches()
            cls._index_manager = new_index_manager
            cls._embedder = embedder
            cls._query_processor = new_query_processor
            cls._loaded_library_path = library_path
            cls._loaded_library_type = scoped_type
            cls._loaded_index_signature = cls._index_signature(library_path, None)
            cls._loaded_series_index_signatures = {}
            cls._episode_paths_cache = {}
            for series_name in new_index_manager.get_series_list():
                cls._loaded_series_index_signatures[series_name] = cls._index_signature(
                    library_path,
                    series_name,
                )
            if old_index_manager is not None and old_index_manager is not new_index_manager:
                try:
                    old_index_manager.unload_all_series()
                except Exception:
                    pass
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
        started_at = time.perf_counter()
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
            cls._record_runtime_stat(
                "frame_decode_single_seconds",
                time.perf_counter() - started_at,
            )
            cls._record_runtime_stat("frame_decode_single_calls")

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
        started_at = time.perf_counter()
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        try:
            return cls._extract_frames_from_capture(cap, timestamps)
        finally:
            cap.release()
            cls._record_runtime_stat(
                "frame_decode_batch_seconds",
                time.perf_counter() - started_at,
            )
            cls._record_runtime_stat("frame_decode_batch_calls")
            cls._record_runtime_stat("frame_decode_batch_targets", len(timestamps))

    @classmethod
    def _extract_frames_from_capture(
        cls,
        cap,
        timestamps: list[float],
    ) -> list[Image.Image | None]:
        """Decode frames nearest the requested presentation timestamps."""
        frames, _ = cls._extract_frames_with_indices_from_capture(cap, timestamps)
        return frames

    @classmethod
    def _extract_frames_with_indices_from_capture(
        cls,
        cap,
        timestamps: list[float],
    ) -> tuple[list[Image.Image | None], list[int | None]]:
        """Decode frames by media PTS, retaining decoded frame indices for caches.

        ``CAP_PROP_FPS`` is an average rate for variable-frame-rate inputs, so
        ``timestamp * fps`` can point many seconds away from the requested
        content.  Use ``CAP_PROP_POS_MSEC`` as the source of truth and only fall
        back to frame-number arithmetic for capture backends that do not expose
        timestamps.
        """
        cv2 = cls._require_cv2()
        frames: list[Image.Image | None] = [None] * len(timestamps)
        frame_indices: list[int | None] = [None] * len(timestamps)
        if not timestamps:
            return frames, frame_indices

        native_fps = cap.get(cv2.CAP_PROP_FPS)
        has_pts = hasattr(cv2, "CAP_PROP_POS_MSEC")
        if not has_pts:
            # Compatibility fallback for minimal/test capture backends. Real
            # OpenCV backends expose POS_MSEC even when FPS metadata is absent.
            native_fps = float(native_fps or 0.0)
            for index, timestamp in enumerate(timestamps):
                target_frame_index = max(
                    0,
                    int(round(max(0.0, float(timestamp)) * native_fps)),
                )
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame_index)
                ret, frame = cap.read()
                if not ret:
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames[index] = Image.fromarray(frame_rgb)
                frame_indices[index] = target_frame_index
            return frames, frame_indices

        ordered_targets = sorted(
            enumerate(timestamps),
            key=lambda item: max(0.0, float(item[1])),
        )
        fps_fallback = float(native_fps) if native_fps and native_fps > 0 else 30.0
        max_sequential_seconds = cls.MAX_SEQUENTIAL_GRAB_FRAMES / fps_fallback
        seek_preroll_seconds = min(2.0, max_sequential_seconds)

        # (presentation timestamp, frame index, BGR frame)
        previous: tuple[float, int | None, np.ndarray] | None = None
        current: tuple[float, int | None, np.ndarray] | None = None

        def read_decoded(
            last_pts: float | None = None,
        ) -> tuple[float, int | None, np.ndarray] | None:
            ret, frame = cap.read()
            if not ret:
                return None
            raw_index = cap.get(cv2.CAP_PROP_POS_FRAMES)
            frame_index = (
                max(0, int(round(raw_index)) - 1)
                if math.isfinite(raw_index) and raw_index > 0
                else None
            )
            raw_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            pts = float(raw_ms) / 1000.0 if math.isfinite(raw_ms) else -1.0
            if pts < 0.0 or (last_pts is not None and pts <= last_pts + 1e-9):
                if frame_index is not None:
                    pts = frame_index / fps_fallback
                elif last_pts is not None:
                    pts = last_pts + 1.0 / fps_fallback
                else:
                    pts = 0.0
            return pts, frame_index, frame

        for original_index, raw_timestamp in ordered_targets:
            timestamp = max(0.0, float(raw_timestamp))
            if (
                current is None
                or timestamp < current[0] - 1e-6
                or timestamp - current[0] > max_sequential_seconds
            ):
                # Timestamp seeks may land on either side of the requested PTS,
                # especially around VFR keyframes. Start slightly early so the
                # ordered decode can bracket the target and choose its nearest
                # presented frame.
                seek_time = max(0.0, timestamp - seek_preroll_seconds)
                cap.set(cv2.CAP_PROP_POS_MSEC, seek_time * 1000.0)
                previous = None
                current = read_decoded()

            while current is not None and current[0] < timestamp:
                decoded = read_decoded(current[0])
                if decoded is None:
                    break
                previous, current = current, decoded

            candidates = [item for item in (previous, current) if item is not None]
            if not candidates:
                continue
            chosen = min(candidates, key=lambda item: abs(item[0] - timestamp))
            frame_rgb = cv2.cvtColor(chosen[2], cv2.COLOR_BGR2RGB)
            frames[original_index] = Image.fromarray(frame_rgb)
            frame_indices[original_index] = chosen[1]

        return frames, frame_indices

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
        frames, _ = cls._extract_scene_probe_frames_with_indices(video_path, scene_items)
        return frames

    @classmethod
    def _extract_scene_probe_frames_with_indices(
        cls,
        video_path: Path,
        scene_items: list[tuple[int, Scene]],
    ) -> tuple[
        dict[int, tuple[Image.Image | None, Image.Image | None, Image.Image | None]],
        dict[int, tuple[int | None, int | None, int | None]],
    ]:
        """Same as :meth:`_extract_scene_probe_frames` but also reports the native
        source frame index decoded for each (scene, position).

        The frame indices are used as keys for the cross-pass probe-embedding
        cache so that re-matching a scene whose probes land on the same source
        frame skips both decode and embedding.
        """
        started_at = time.perf_counter()
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        frames_by_scene: dict[
            int,
            tuple[Image.Image | None, Image.Image | None, Image.Image | None],
        ] = {}
        indices_by_scene: dict[
            int,
            tuple[int | None, int | None, int | None],
        ] = {}
        try:
            targets: list[tuple[float, int, int]] = []
            for scene_index, scene in scene_items:
                frames_by_scene[scene_index] = (None, None, None)
                indices_by_scene[scene_index] = (None, None, None)
                for position, timestamp in enumerate(cls._scene_probe_times(scene)):
                    targets.append((max(0.0, timestamp), scene_index, position))
            targets.sort(key=lambda item: item[0])

            def assign(scene_index: int, position: int, image, frame_idx) -> None:
                scene_frames = list(frames_by_scene[scene_index])
                scene_frames[position] = image
                frames_by_scene[scene_index] = (
                    scene_frames[0],
                    scene_frames[1],
                    scene_frames[2],
                )
                scene_indices = list(indices_by_scene[scene_index])
                scene_indices[position] = frame_idx
                indices_by_scene[scene_index] = (
                    scene_indices[0],
                    scene_indices[1],
                    scene_indices[2],
                )

            decoded, frame_indices = cls._extract_frames_with_indices_from_capture(
                cap,
                [timestamp for timestamp, _, _ in targets],
            )
            for (_, scene_index, position), image, frame_index in zip(
                targets,
                decoded,
                frame_indices,
                strict=False,
            ):
                assign(scene_index, position, image, frame_index)
        finally:
            cap.release()
            cls._record_runtime_stat(
                "frame_decode_probe_seconds",
                time.perf_counter() - started_at,
            )
            cls._record_runtime_stat("frame_decode_probe_calls")
            cls._record_runtime_stat("frame_decode_probe_targets", len(scene_items) * 3)
        return frames_by_scene, indices_by_scene

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
        video_path: Path | None = None,
        probe_frame_indices: dict[
            int,
            tuple[int | None, int | None, int | None],
        ] | None = None,
    ) -> dict[
        int,
        tuple[list[MatchCandidate], list[MatchCandidate], list[MatchCandidate]],
    ]:
        """Search direct SSCD candidates for all scene probe frames in chunks.

        When ``video_path`` and ``probe_frame_indices`` are provided, embeddings
        are cached at (video signature, native frame index) granularity and
        reused across passes — useful when scene re-matching after a merge
        lands on a probe frame that was already embedded in pass 1.
        """
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
        use_cache_path = (
            video_path is not None
            and probe_frame_indices is not None
            and not flip
            and cls._query_processor is not None
        )

        if use_cache_path:
            video_signature = cls._video_signature(video_path)
            sig_path, sig_mtime, sig_size = video_signature
            cached_embeddings: dict[tuple[int, int], np.ndarray] = {}
            missing_keys: list[tuple[int, int]] = []
            missing_images: list[Image.Image] = []
            missing_cache_keys: list[
                tuple[str, int, int, int] | None
            ] = []
            for key, image in zip(flat_keys, flat_images, strict=False):
                scene_index, position = key
                indices = probe_frame_indices.get(scene_index)
                frame_idx = (
                    indices[position]
                    if indices is not None and position < len(indices)
                    else None
                )
                if frame_idx is None:
                    missing_keys.append(key)
                    missing_images.append(image)
                    missing_cache_keys.append(None)
                    continue
                cache_key = (sig_path, sig_mtime, sig_size, int(frame_idx))
                cached = cls._get_cached_video_frame_embedding(cache_key)
                if cached is not None:
                    cached_embeddings[key] = cached
                    cls._record_runtime_stat("probe_embedding_cache_hits")
                else:
                    missing_keys.append(key)
                    missing_images.append(image)
                    missing_cache_keys.append(cache_key)
                    cls._record_runtime_stat("probe_embedding_cache_misses")

            embedded_lookup: dict[tuple[int, int], np.ndarray] = dict(cached_embeddings)
            for start in range(0, len(missing_images), batch_size):
                batch_images = [
                    image.convert("RGB")
                    for image in missing_images[start : start + batch_size]
                ]
                batch_keys = missing_keys[start : start + batch_size]
                batch_cache_keys = missing_cache_keys[start : start + batch_size]
                batch_embeddings = cls._embed_pil_batch(batch_images)
                for key, cache_key, embedding in zip(
                    batch_keys,
                    batch_cache_keys,
                    batch_embeddings,
                    strict=False,
                ):
                    embedded_lookup[key] = embedding
                    if cache_key is not None:
                        cls._store_video_frame_embedding(cache_key, embedding)

            if embedded_lookup:
                stacked = np.stack(
                    [embedded_lookup[key] for key in flat_keys],
                    axis=0,
                ).astype(np.float32, copy=False)
                search_started_at = time.perf_counter()
                raw_results = cls._query_processor.index_manager.search_batch(
                    stacked,
                    top_n,
                    threshold,
                    series=series,
                )
                cls._record_runtime_stat(
                    "faiss_search_seconds",
                    time.perf_counter() - search_started_at,
                )
                cls._record_runtime_stat("faiss_search_queries", len(flat_keys))
                for key, results in zip(flat_keys, raw_results, strict=False):
                    candidate_lists[key] = [
                        MatchCandidate(
                            episode=meta.episode,
                            timestamp=meta.timestamp,
                            similarity=float(sim),
                            series=meta.series,
                        )
                        for sim, meta in results
                    ]
        else:
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
    def _embed_pil_batch(cls, images: list[Image.Image]) -> np.ndarray:
        """Embed a batch of PIL images, preferring the GPU-resident preprocessing
        path on the SSCD embedder when available.

        Falls back to ``embed_batch`` for test fakes and non-CUDA builds.
        """
        embedder = cls._embedder
        if embedder is None:
            return np.empty((0, 512), dtype=np.float32)
        if not images:
            return np.empty((0, 512), dtype=np.float32)
        # Large one-shot batches balloon the CUDA allocator cache (peak
        # activations scale with batch size and the reserve is never
        # returned), starving later phases on the 8 GB card. Chunking keeps
        # results bit-identical while bounding the peak.
        if len(images) > 64:
            return np.concatenate(
                [
                    cls._embed_pil_batch(images[k : k + 64])
                    for k in range(0, len(images), 64)
                ],
                axis=0,
            )
        gpu_embed = getattr(embedder, "embed_pil_batch_gpu", None)
        started_at = time.perf_counter()

        def is_cuda_oom(exc: BaseException) -> bool:
            message = str(exc).lower()
            return "cuda" in message and "out of memory" in message

        def clear_cuda_cache() -> None:
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

        def embed_chunk(batch: list[Image.Image]) -> np.ndarray:
            if callable(gpu_embed):
                return gpu_embed(batch)
            return embedder.embed_batch(batch)

        def embed_adaptive(batch: list[Image.Image]) -> np.ndarray:
            try:
                return embed_chunk(batch)
            except Exception as exc:
                if not callable(gpu_embed) or not is_cuda_oom(exc):
                    raise
                clear_cuda_cache()
                cls._record_runtime_stat("sscd_embedding_oom_retries")
                if len(batch) <= 1:
                    # The CPU preprocessing path feeds the model one image at a
                    # time and is the least-memory fallback for a single large
                    # frame. One cache-cleared retry: a single frame only
                    # fails when the allocator cache is still holding the
                    # previous burst.
                    try:
                        return embedder.embed_batch(batch)
                    except Exception as retry_exc:
                        if not is_cuda_oom(retry_exc):
                            raise
                        clear_cuda_cache()
                        return embedder.embed_batch(batch)
                midpoint = max(1, len(batch) // 2)
                left = embed_adaptive(batch[:midpoint])
                right = embed_adaptive(batch[midpoint:])
                return np.concatenate([left, right], axis=0)

        embeddings = embed_adaptive(images)
        cls._record_runtime_stat(
            "sscd_embedding_seconds",
            time.perf_counter() - started_at,
        )
        cls._record_runtime_stat("sscd_embedding_images", len(images))
        cls._record_runtime_stat("sscd_embedding_batches")
        return embeddings

    @classmethod
    def _video_signature(cls, video_path: Path) -> tuple[str, int, int]:
        """Return a (path, mtime_ns, size) signature used to invalidate cached frame embeddings."""
        try:
            stat = video_path.stat()
            return (str(video_path.resolve()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (str(video_path), -1, -1)

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
    def _open_source_capture(cls, path):
        """Open a capture for a SOURCE-episode window decode.

        FAST MODE (F1): returns a :class:`pynv_decode.PyNvCap` (GPU NVDEC) when
        fast decode is requested and live for this file; otherwise a plain
        ``cv2.VideoCapture``. Use ONLY for captures consumed exclusively through
        :meth:`_collect_frames_in_window_from_capture` — a PyNvCap supports no
        other cv2 operations. ``release()`` parity keeps every caller's
        ``finally: cap.release()`` valid.
        """
        from . import pynv_decode

        gpu_cap = pynv_decode.open_capture(str(path))
        if gpu_cap is not None:
            return gpu_cap
        cv2 = cls._require_cv2()
        return cv2.VideoCapture(str(path))

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
        cap = cls._open_source_capture(video_path)
        try:
            return cls._collect_frames_in_window_from_capture(
                cap,
                start_ts,
                end_ts,
                max_frames=max_frames,
                sample_frames=sample_frames,
            )
        finally:
            cap.release()

    @classmethod
    def _collect_frames_in_window_from_capture(
        cls,
        cap,
        start_ts: float,
        end_ts: float,
        max_frames: int = 48,
        sample_frames: int | None = None,
    ) -> list[tuple[float, Image.Image]]:
        """Window decode using an externally-managed VideoCapture.

        Lets ``_refine_boundaries`` share one capture across the start- and
        end-boundary windows instead of opening the source episode twice.
        """
        started_at = time.perf_counter()
        # FAST MODE (F1): a PyNvCap routes this window to the persistent NVDEC
        # decoder (GPU), reproducing cv2's POS_MSEC window selection. Any other
        # capture type is the exact mainline cv2 path below.
        from . import pynv_decode

        if isinstance(cap, pynv_decode.PyNvCap):
            frames: list[tuple[float, Image.Image]] = []
            try:
                try:
                    frames = pynv_decode.decode_window(
                        cap.path,
                        start_ts,
                        end_ts,
                        max_frames=max_frames,
                        sample_frames=sample_frames,
                    )
                except Exception as exc:  # CUDA OOM, cuvid errors, VRAM gate
                    if not pynv_decode.should_fallback_to_cv2(exc):
                        raise
                    # The shared 8 GB card is momentarily full (concurrent
                    # matching, NVENC preview encodes, indexation). Drop the
                    # pooled session — a decoder that went through a cuvid
                    # failure can be internally corrupt (2026-07-19 SIGSEGV) —
                    # clear our cache, and decode THIS window on cv2 instead:
                    # transparent, per-window, byte-identical.
                    cls._record_runtime_stat("fast_decode_oom_cv2_fallback")
                    pynv_decode.invalidate_session(cap.path)
                    try:
                        import torch

                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                    _cv2 = cls._require_cv2()
                    _fallback = _cv2.VideoCapture(cap.path)
                    try:
                        frames = cls._collect_frames_in_window_from_capture(
                            _fallback,
                            start_ts,
                            end_ts,
                            max_frames=max_frames,
                            sample_frames=sample_frames,
                        )
                    finally:
                        _fallback.release()
                return frames
            finally:
                cls._record_runtime_stat(
                    "frame_decode_window_seconds",
                    time.perf_counter() - started_at,
                )
                cls._record_runtime_stat("frame_decode_window_calls")
                cls._record_runtime_stat("frame_decode_window_frames", len(frames))
        cv2 = cls._require_cv2()
        start_ts = max(0.0, start_ts)
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
            if sample_frames is not None and len(frames) > sample_frames:
                indices = np.linspace(0, len(frames) - 1, sample_frames, dtype=np.int32)
                return [frames[int(index)] for index in indices]
            return frames
        finally:
            cls._record_runtime_stat(
                "frame_decode_window_seconds",
                time.perf_counter() - started_at,
            )
            cls._record_runtime_stat("frame_decode_window_calls")
            cls._record_runtime_stat("frame_decode_window_frames", len(frames))

    @classmethod
    def _refine_boundaries(
        cls,
        video_path: Path,
        scene: Scene,
        matched_episode: str,
        matched_start_ts: float,
        matched_end_ts: float,
        library_type: LibraryType | str,
        sample_frames_per_boundary: int | None = None,
    ) -> tuple[float, float] | None:
        """Refine (start_ts, end_ts) to native source FPS using argmax cosine.

        The 2-FPS index grid caps boundary precision at 0.5s. Post-match we
        decode the matched source episode at its own native FPS in a small
        window around each boundary, re-embed those frames, and pick the one
        whose SSCD embedding best matches the TikTok scene's actual first /
        last frame. Reduces boundary error from ~250ms to ~1 source frame.

        Returns None on failure; caller should keep the unrefined timestamps.
        """
        started_at = time.perf_counter()
        try:
            cls._record_runtime_stat("boundary_refine_calls")
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

            # Share one capture across both boundary windows. For cv2, walking
            # forward from the start window into the end window avoids a second
            # container open + codec reinit; for the FAST-MODE PyNvCap (F1) the
            # pooled NVDEC session is shared by path anyway, so both windows land
            # on the same decoder with identical frame selection.
            cap = cls._open_source_capture(episode_path)
            sample_frames = sample_frames_per_boundary or cls.REFINE_MAX_FRAMES_PER_BOUNDARY
            try:
                start_frames = cls._collect_frames_in_window_from_capture(
                    cap,
                    matched_start_ts - window,
                    matched_start_ts + window,
                    sample_frames=sample_frames,
                )
                end_frames = cls._collect_frames_in_window_from_capture(
                    cap,
                    matched_end_ts - window,
                    matched_end_ts + window,
                    sample_frames=sample_frames,
                )
            finally:
                cap.release()
            if not start_frames or not end_frames:
                return None

            query_embeddings = cls._embed_pil_batch([tiktok_start_frame, tiktok_end_frame])
            if query_embeddings.shape[0] < 2:
                return None
            q_start, q_end = query_embeddings[0], query_embeddings[1]

            target_aspect = (
                tiktok_start_frame.width / max(1, tiktok_start_frame.height)
            )
            refined_start = cls._best_boundary_timestamp(
                query_embedding=q_start,
                source_frames=start_frames,
                target_aspect=target_aspect,
            )
            refined_end = cls._best_boundary_timestamp(
                query_embedding=q_end,
                source_frames=end_frames,
                target_aspect=target_aspect,
            )

            # If refinement collapses or reverses the interval, keep the original
            # timestamps — a degenerate pick is worse than the coarse grid.
            if refined_end - refined_start <= 0.1:
                return None

            cls._record_runtime_stat("boundary_refine_successes")
            return refined_start, refined_end
        finally:
            cls._record_runtime_stat(
                "boundary_refine_seconds",
                time.perf_counter() - started_at,
            )

    @classmethod
    def _best_boundary_timestamp(
        cls,
        *,
        query_embedding: np.ndarray,
        source_frames: list[tuple[float, Image.Image]],
        target_aspect: float,
    ) -> float:
        source_images = [frame for _, frame in source_frames]
        source_embs = cls._embed_pil_batch(source_images)
        scores = source_embs @ query_embedding
        best_index = int(np.argmax(scores))
        best_score = float(scores[best_index])
        sorted_scores = np.sort(scores)
        margin = (
            best_score - float(sorted_scores[-2])
            if sorted_scores.size >= 2
            else best_score
        )

        source_aspect = (
            source_images[0].width / max(1, source_images[0].height)
            if source_images
            else 1.0
        )
        return float(source_frames[best_index][0])

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
        if not prepared:
            return []

        embeddings = cls._embed_pil_batch(prepared)
        search_started_at = time.perf_counter()
        per_image_results = processor.index_manager.search_batch(
            embeddings,
            top_n,
            threshold,
            series=series,
        )
        cls._record_runtime_stat(
            "faiss_search_seconds",
            time.perf_counter() - search_started_at,
        )
        cls._record_runtime_stat("faiss_search_queries", len(prepared))

        if flip:
            flipped = [ImageOps.mirror(img) for img in prepared]
            flip_embeddings = cls._embed_pil_batch(flipped)
            search_started_at = time.perf_counter()
            per_image_flip_results = processor.index_manager.search_batch(
                flip_embeddings,
                top_n,
                threshold,
                series=series,
            )
            cls._record_runtime_stat(
                "faiss_search_seconds",
                time.perf_counter() - search_started_at,
            )
            cls._record_runtime_stat("faiss_search_queries", len(prepared))
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
            timestamps = [
                float(timestamp)
                for timestamp in np.arange(0.0, duration, step, dtype=np.float32)
            ]
            decoded = cls._extract_frames_from_capture(cap, timestamps)
            for timestamp, image in zip(timestamps, decoded):
                if image is not None:
                    frames.append((timestamp, image))
        finally:
            cap.release()
        return frames

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
    def _source_duration_within_speed_bounds(
        scene_duration: float,
        start_time: float,
        end_time: float,
    ) -> bool:
        source_duration = end_time - start_time
        if scene_duration <= 0 or source_duration <= 0:
            return False
        speed_ratio = scene_duration / source_duration
        return settings.matcher_min_speed_factor <= speed_ratio <= 1.60

    @staticmethod
    def _proposal_source_priority(proposal: MatchProposal) -> int:
        if proposal.source == "refined":
            return 2
        if proposal.source in {"crop", "crop_projected", "cropped", "projected"}:
            return 1
        return 0

    @classmethod
    def _proposal_rank_key(cls, proposal: MatchProposal) -> tuple[float, int, float, int]:
        return (
            proposal.selection_score,
            proposal.vote_count,
            cls._proposal_source_priority(proposal),
            proposal.confidence,
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
        source: str = "projected",
        selection_bonus: float = 0.25,
    ) -> MatchProposal | None:
        """Project a short-scene interval from start/middle/end retrievals.

        The crop index is sparse by design for speed. On sub-2s zoomed cuts, the
        correct source interval is often present as a start, middle, or end
        projection while strict temporal triples are impossible.
        """
        if scene_duration <= 0:
            return None

        # sparse-retrieval support window (was 1/CROP_INDEX_FPS at 0.5 fps
        # before the crop-index deletion, GOAL v4.2 M5)
        support_tolerance = 2.0

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
            selection_score=projected_confidence + selection_bonus,
            source=source,
            vote_count=1,
            debug={"base_similarity": base_similarity},
        )

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
    def _monotonic_sequence_candidate_options(
        cls,
        scene: Scene,
        match: SceneMatch,
        dominant_episode: str,
    ) -> list[dict[str, float | str | int]]:
        duration = scene.duration
        if duration <= 0:
            return []

        options: dict[tuple[float, float], dict[str, float | str | int]] = {}

        def add(
            start_time: float,
            end_time: float,
            confidence: float,
            source: str,
            vote_count: int = 1,
        ) -> None:
            if confidence <= 0 or end_time <= start_time:
                return
            start_time = max(0.0, float(start_time))
            if not cls._source_duration_within_speed_bounds(
                duration,
                start_time,
                end_time,
            ):
                return

            speed_ratio = duration / (end_time - start_time)
            source_bonus = {
                "direct": 0.06,
                "crop": 0.04,
                "weighted_avg": 0.025,
                "start_mid_end": 0.045,
                "start_end": 0.025,
                "refined": -0.015,
            }.get(source, 0.0)
            speed_penalty = 0.0
            if speed_ratio > 1.25:
                speed_penalty = 0.025 * ((speed_ratio - 1.25) / 0.35)
            selection_score = float(confidence) + source_bonus - speed_penalty

            key = (round(start_time, 2), round(end_time, 2))
            previous = options.get(key)
            if previous is None or selection_score > float(previous["selection_score"]):
                options[key] = {
                    "episode": dominant_episode,
                    "start_time": start_time,
                    "end_time": float(end_time),
                    "confidence": float(confidence),
                    "selection_score": selection_score,
                    "source": source,
                    "vote_count": vote_count,
                }

        if match.episode == dominant_episode and match.end_time > match.start_time:
            add(
                match.start_time,
                match.end_time,
                match.confidence,
                "primary",
            )

        for alternative in match.alternatives:
            if alternative.episode != dominant_episode:
                continue
            add(
                alternative.start_time,
                alternative.end_time,
                alternative.confidence,
                alternative.algorithm or "alternative",
                alternative.vote_count,
            )

        for candidate in match.start_candidates[:20]:
            if candidate.episode == dominant_episode:
                add(
                    candidate.timestamp,
                    candidate.timestamp + duration,
                    candidate.similarity,
                    "start",
                )
        for candidate in match.middle_candidates[:20]:
            if candidate.episode == dominant_episode:
                add(
                    candidate.timestamp - duration / 2.0,
                    candidate.timestamp + duration / 2.0,
                    candidate.similarity * 0.98,
                    "middle",
                )
        for candidate in match.end_candidates[:20]:
            if candidate.episode == dominant_episode:
                add(
                    candidate.timestamp - duration,
                    candidate.timestamp,
                    candidate.similarity * 0.98,
                    "end",
                )

        for start_candidate in match.start_candidates[:14]:
            if start_candidate.episode != dominant_episode:
                continue
            for end_candidate in match.end_candidates[:14]:
                if end_candidate.episode != dominant_episode:
                    continue
                if end_candidate.timestamp <= start_candidate.timestamp:
                    continue
                if not cls._source_duration_within_speed_bounds(
                    duration,
                    start_candidate.timestamp,
                    end_candidate.timestamp,
                ):
                    continue

                source_duration = end_candidate.timestamp - start_candidate.timestamp
                expected_middle = start_candidate.timestamp + source_duration / 2.0
                middle_support = [
                    middle_candidate
                    for middle_candidate in match.middle_candidates[:14]
                    if (
                        middle_candidate.episode == dominant_episode
                        and abs(middle_candidate.timestamp - expected_middle)
                        <= max(0.7, source_duration * 0.4)
                    )
                ]
                confidence = (
                    start_candidate.similarity + end_candidate.similarity
                ) / 2.0
                source = "start_end"
                vote_count = 2
                if middle_support:
                    confidence = (
                        start_candidate.similarity
                        + end_candidate.similarity
                        + max(candidate.similarity for candidate in middle_support)
                    ) / 3.0
                    source = "start_mid_end"
                    vote_count = 3

                add(
                    start_candidate.timestamp,
                    end_candidate.timestamp,
                    confidence,
                    source,
                    vote_count,
                )

        return sorted(
            options.values(),
            key=lambda item: float(item["selection_score"]),
            reverse=True,
        )[:24]

    @classmethod
    def _recover_monotonic_boundary_alternatives(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> MatchList:
        """Prefer boundary-anchored probes when monotonic continuity drifts."""
        if len(scenes.scenes) != len(matches.matches):
            return matches

        adjusted = matches.model_copy(deep=True)
        changed = False
        continuity_sources = {"continuity", "best_frame", "refined"}
        for idx, match in enumerate(adjusted.matches):
            if not match.episode or match.was_no_match or not match.alternatives:
                continue
            primary_source = match.alternatives[0].algorithm or ""
            if primary_source not in continuity_sources:
                continue

            scene = scenes.scenes[idx]
            selected_alternative = None
            direct_alternatives = [
                alternative
                for alternative in match.alternatives
                if (
                    alternative.algorithm == "direct"
                    and alternative.episode == match.episode
                    and alternative.vote_count >= 3
                    and alternative.confidence >= 0.49
                    and alternative.confidence >= match.confidence - 0.40
                    and alternative.end_time > alternative.start_time
                    and abs(alternative.start_time - match.start_time) <= 1.0
                    and abs(alternative.end_time - match.end_time) <= 1.0
                    and cls._source_duration_within_speed_bounds(
                        scene.duration,
                        alternative.start_time,
                        alternative.end_time,
                    )
                )
            ]
            if direct_alternatives:
                selected_alternative = max(
                    direct_alternatives,
                    key=lambda alternative: alternative.confidence,
                )
            else:
                weighted_alternatives = [
                    alternative
                    for alternative in match.alternatives
                    if (
                        alternative.algorithm == "weighted_avg"
                        and alternative.episode == match.episode
                        and alternative.vote_count >= 12
                        and alternative.confidence >= 0.65
                        and alternative.confidence >= match.confidence - 0.12
                        and alternative.end_time > alternative.start_time
                        and abs(alternative.start_time - match.start_time) <= 0.60
                        and abs(alternative.end_time - match.end_time) <= 0.60
                        and cls._source_duration_within_speed_bounds(
                            scene.duration,
                            alternative.start_time,
                            alternative.end_time,
                        )
                    )
                ]
                if weighted_alternatives:
                    selected_alternative = max(
                        weighted_alternatives,
                        key=lambda alternative: (
                            alternative.vote_count,
                            alternative.confidence,
                        ),
                    )

            if selected_alternative is None:
                continue
            proposal = cls._proposal_from_alternative(
                selected_alternative,
                source=f"monotonic_{selected_alternative.algorithm}",
                selection_bonus=0.0,
            )
            if proposal is None:
                continue
            cls._apply_proposal_to_match(scene, match, proposal)
            changed = True

        return adjusted if changed else matches

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

        for alternative in match.alternatives:
            if (
                alternative.episode == dominant_episode
                and alternative.end_time > alternative.start_time
            ):
                candidates.append(
                    {
                        "episode": dominant_episode,
                        "start_time": alternative.start_time,
                        "end_time": alternative.end_time,
                        "confidence": max(alternative.confidence, 0.01),
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
            for candidate in position_candidates[:20]:
                if candidate.episode != dominant_episode:
                    continue
                if candidate.similarity < top_similarity - 0.10:
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
    def _is_dense_short_match_list(cls, scenes: SceneList, matches: MatchList) -> bool:
        if len(scenes.scenes) != len(matches.matches) or len(matches.matches) < 45:
            return False
        durations = sorted(scene.duration for scene in scenes.scenes if scene.duration > 0)
        if not durations:
            return False
        middle = len(durations) // 2
        median_duration = (
            durations[middle]
            if len(durations) % 2
            else (durations[middle - 1] + durations[middle]) / 2.0
        )
        return median_duration <= 1.5

    @staticmethod
    def _nearby_source_cuts(
        cuts: list[float],
        timestamp: float,
        *,
        window: float = 1.25,
    ) -> list[float]:
        if not cuts:
            return []
        insert_at = bisect_left(cuts, timestamp)
        start = max(0, insert_at - 12)
        end = min(len(cuts), insert_at + 13)
        return [
            cut
            for cut in cuts[start:end]
            if abs(float(cut) - timestamp) <= window
        ]

    @classmethod
    def _dense_candidate_support(
        cls,
        match: SceneMatch,
        episode: str,
        start_time: float,
        end_time: float,
    ) -> tuple[float, int, float]:
        if not episode or end_time <= start_time:
            return 0.0, 0, 0.0
        probes = (
            ("start", start_time, match.start_candidates),
            ("middle", (start_time + end_time) / 2.0, match.middle_candidates),
            ("end", end_time, match.end_candidates),
        )
        support = 0.0
        support_count = 0
        best_similarity = 0.0
        for _, timestamp, candidates in probes:
            best_probe: tuple[float, float] | None = None
            for candidate in candidates:
                if candidate.episode != episode:
                    continue
                delta = abs(candidate.timestamp - timestamp)
                if delta > 0.80:
                    continue
                weighted = max(0.0, 1.0 - delta / 0.80) * candidate.similarity
                if best_probe is None or weighted > best_probe[0]:
                    best_probe = (weighted, candidate.similarity)
            if best_probe is None:
                continue
            support += best_probe[0]
            support_count += 1
            best_similarity = max(best_similarity, best_probe[1])
        return support, support_count, best_similarity

    @classmethod
    def _make_dense_source_candidate(
        cls,
        scene: Scene,
        match: SceneMatch,
        *,
        episode: str,
        start_time: float,
        end_time: float,
        confidence: float,
        vote_count: int,
        source: str,
        base_start: float,
        base_end: float,
        cut_bonus: float = 0.0,
        is_cut_aligned: bool = False,
    ) -> DenseSourceCandidate | None:
        if not episode or end_time <= start_time or scene.duration <= 0:
            return None
        source_duration = end_time - start_time
        speed_ratio = scene.duration / source_duration
        # Candidate exposure can be slightly wider than primary promotion. Very
        # short montage cuts often use speed changes, but extreme durations are
        # usually repeated-frame false positives.
        if speed_ratio < 0.45 or speed_ratio > 1.85:
            return None

        support, support_count, best_similarity = cls._dense_candidate_support(
            match,
            episode,
            start_time,
            end_time,
        )
        proposal = MatchProposal(
            episode=episode,
            start_time=round(float(start_time), 3),
            end_time=round(float(end_time), 3),
            confidence=float(confidence),
            selection_score=float(confidence) + cut_bonus,
            source=source,
            vote_count=max(1, int(vote_count or 1)),
        )
        return DenseSourceCandidate(
            proposal=proposal,
            support=support,
            support_count=support_count,
            best_similarity=best_similarity,
            move_from_base=abs(float(start_time) - base_start)
            + abs(float(end_time) - base_end),
            duration_error=abs(source_duration - scene.duration) / max(
                scene.duration,
                1e-3,
            ),
            cut_bonus=cut_bonus,
            is_cut_aligned=is_cut_aligned,
        )

    @classmethod
    def _dense_source_candidate_score(cls, candidate: DenseSourceCandidate) -> float:
        duration_closeness = max(
            0.0,
            1.0 - min(1.5, candidate.duration_error) / 1.5,
        )
        return (
            candidate.proposal.confidence
            + 0.14 * candidate.support
            + 0.025 * candidate.support_count
            + 0.02 * min(candidate.proposal.vote_count, 8)
            + candidate.cut_bonus
            + 0.12 * duration_closeness
            - 0.06 * candidate.move_from_base
        )

    @classmethod
    def _scored_dense_source_proposal(
        cls,
        candidate: DenseSourceCandidate,
    ) -> MatchProposal:
        return MatchProposal(
            episode=candidate.proposal.episode,
            start_time=candidate.proposal.start_time,
            end_time=candidate.proposal.end_time,
            confidence=candidate.proposal.confidence,
            selection_score=cls._dense_source_candidate_score(candidate),
            source=candidate.proposal.source,
            vote_count=candidate.proposal.vote_count,
        )

    @classmethod
    def _dense_source_candidates(
        cls,
        scene: Scene,
        match: SceneMatch,
        source_cuts_by_episode: dict[str, list[float]],
    ) -> list[DenseSourceCandidate]:
        scene_duration = scene.duration
        if scene_duration <= 0:
            return []

        base_intervals: list[
            tuple[str, float, float, float, int, str, bool]
        ] = []

        if match.episode and match.end_time > match.start_time:
            primary_algorithm = (
                match.alternatives[0].algorithm
                if match.alternatives
                else "primary"
            )
            base_intervals.append(
                (
                    match.episode,
                    match.start_time,
                    match.end_time,
                    match.confidence,
                    1,
                    primary_algorithm or "primary",
                    False,
                )
            )

        for alternative in match.alternatives:
            if alternative.episode and alternative.end_time > alternative.start_time:
                base_intervals.append(
                    (
                        alternative.episode,
                        alternative.start_time,
                        alternative.end_time,
                        alternative.confidence,
                        alternative.vote_count,
                        alternative.algorithm or "alternative",
                        False,
                    )
                )

        for position, candidates in (
            ("start", match.start_candidates[:80]),
            ("middle", match.middle_candidates[:80]),
            ("end", match.end_candidates[:80]),
        ):
            for rank, candidate in enumerate(candidates):
                if position == "start":
                    start_time = candidate.timestamp
                    end_time = candidate.timestamp + scene_duration
                elif position == "middle":
                    start_time = candidate.timestamp - scene_duration / 2.0
                    end_time = candidate.timestamp + scene_duration / 2.0
                else:
                    start_time = candidate.timestamp - scene_duration
                    end_time = candidate.timestamp
                base_intervals.append(
                    (
                        candidate.episode,
                        start_time,
                        end_time,
                        candidate.similarity,
                        1,
                        f"{position}_probe",
                        rank < 30,
                    )
                )

        for start_rank, start_candidate in enumerate(match.start_candidates[:64]):
            for end_rank, end_candidate in enumerate(match.end_candidates[:64]):
                if start_candidate.episode != end_candidate.episode:
                    continue
                base_intervals.append(
                    (
                        start_candidate.episode,
                        start_candidate.timestamp,
                        end_candidate.timestamp,
                        (start_candidate.similarity + end_candidate.similarity) / 2.0,
                        2,
                        "probe_pair",
                        start_rank < 16 and end_rank < 16,
                    )
                )

        candidates: list[DenseSourceCandidate] = []
        for (
            episode,
            start_time,
            end_time,
            confidence,
            vote_count,
            source,
            allow_cut_variants,
        ) in base_intervals:
            base_candidate = cls._make_dense_source_candidate(
                scene,
                match,
                episode=episode,
                start_time=start_time,
                end_time=end_time,
                confidence=confidence,
                vote_count=vote_count,
                source=source,
                base_start=start_time,
                base_end=end_time,
            )
            if base_candidate is not None:
                candidates.append(base_candidate)

            cuts = source_cuts_by_episode.get(episode, [])
            if not cuts or not allow_cut_variants:
                continue

            nearby_starts = [
                start_time,
                *cls._nearby_source_cuts(cuts, start_time),
            ]
            nearby_ends = [
                end_time,
                *cls._nearby_source_cuts(cuts, end_time),
            ]
            for snapped_start in nearby_starts:
                for snapped_end in nearby_ends:
                    if (
                        abs(snapped_start - start_time)
                        + abs(snapped_end - end_time)
                        < 0.02
                    ):
                        continue
                    cut_bonus = (
                        (0.06 if abs(snapped_start - start_time) > 1e-6 else 0.0)
                        + (0.06 if abs(snapped_end - end_time) > 1e-6 else 0.0)
                    )
                    candidate = cls._make_dense_source_candidate(
                        scene,
                        match,
                        episode=episode,
                        start_time=snapped_start,
                        end_time=snapped_end,
                        confidence=confidence,
                        vote_count=vote_count,
                        source="source_cut_aligned",
                        base_start=start_time,
                        base_end=end_time,
                        cut_bonus=cut_bonus,
                        is_cut_aligned=True,
                    )
                    if candidate is not None:
                        candidates.append(candidate)

            for snapped_start in cls._nearby_source_cuts(cuts, start_time):
                candidate = cls._make_dense_source_candidate(
                    scene,
                    match,
                    episode=episode,
                    start_time=snapped_start,
                    end_time=snapped_start + scene_duration,
                    confidence=confidence,
                    vote_count=vote_count,
                    source="source_cut_duration",
                    base_start=start_time,
                    base_end=end_time,
                    cut_bonus=0.05,
                    is_cut_aligned=True,
                )
                if candidate is not None:
                    candidates.append(candidate)

            for snapped_end in cls._nearby_source_cuts(cuts, end_time):
                candidate = cls._make_dense_source_candidate(
                    scene,
                    match,
                    episode=episode,
                    start_time=snapped_end - scene_duration,
                    end_time=snapped_end,
                    confidence=confidence,
                    vote_count=vote_count,
                    source="source_cut_duration",
                    base_start=start_time,
                    base_end=end_time,
                    cut_bonus=0.05,
                    is_cut_aligned=True,
                )
                if candidate is not None:
                    candidates.append(candidate)

            for center in (start_time, (start_time + end_time) / 2.0, end_time):
                insert_at = bisect_left(cuts, center)
                start_index = max(0, insert_at - 8)
                end_index = min(len(cuts), insert_at + 8)
                for left_index in range(start_index, end_index):
                    for right_index in range(
                        left_index + 1,
                        min(len(cuts), left_index + 8),
                    ):
                        snapped_start = cuts[left_index]
                        snapped_end = cuts[right_index]
                        if (
                            abs(snapped_start - start_time) > 1.5
                            or abs(snapped_end - end_time) > 1.5
                        ):
                            continue
                        candidate = cls._make_dense_source_candidate(
                            scene,
                            match,
                            episode=episode,
                            start_time=snapped_start,
                            end_time=snapped_end,
                            confidence=confidence,
                            vote_count=vote_count,
                            source="source_cut_pair",
                            base_start=start_time,
                            base_end=end_time,
                            cut_bonus=0.10,
                            is_cut_aligned=True,
                        )
                        if candidate is not None:
                            candidates.append(candidate)

        best_by_key: dict[tuple[str, float, float], DenseSourceCandidate] = {}
        for candidate in candidates:
            key = cls._proposal_key(candidate.proposal)
            existing = best_by_key.get(key)
            if existing is None or cls._dense_source_candidate_score(
                candidate
            ) > cls._dense_source_candidate_score(existing):
                best_by_key[key] = candidate

        ranked = sorted(
            best_by_key.values(),
            key=cls._dense_source_candidate_score,
            reverse=True,
        )

        selected: list[DenseSourceCandidate] = []
        seen: set[tuple[str, float, float]] = set()

        def add_candidate(candidate: DenseSourceCandidate) -> None:
            key = cls._proposal_key(candidate.proposal)
            if key in seen:
                return
            selected.append(candidate)
            seen.add(key)

        for candidate in ranked[:220]:
            add_candidate(candidate)

        if match.episode and match.end_time > match.start_time:
            for candidate in ranked:
                proposal = candidate.proposal
                if proposal.episode != match.episode:
                    continue
                endpoint_distance = max(
                    abs(proposal.start_time - match.start_time),
                    abs(proposal.end_time - match.end_time),
                )
                if endpoint_distance <= 2.50:
                    add_candidate(candidate)
                if len(selected) >= 320:
                    break

        return selected[:320]

    @classmethod
    async def _load_dense_source_cuts(
        cls,
        scenes: SceneList,
        matches: MatchList,
        library_type: LibraryType | str,
    ) -> dict[str, list[float]]:
        if not cls._is_dense_short_match_list(scenes, matches):
            return {}

        episodes: dict[str, int] = defaultdict(int)
        for match in matches.matches:
            if match.episode:
                episodes[match.episode] += 4
            for alternative in match.alternatives[:6]:
                if alternative.episode:
                    episodes[alternative.episode] += 2
            for candidates in (
                match.start_candidates[:8],
                match.middle_candidates[:8],
                match.end_candidates[:8],
            ):
                for candidate in candidates:
                    if candidate.episode:
                        episodes[candidate.episode] += 1

        if not episodes:
            return {}

        from .anime_library import AnimeLibraryService
        from .gap_resolution import GapResolutionService

        selected_episodes = [
            episode
            for episode, _ in sorted(
                episodes.items(),
                key=lambda item: item[1],
                reverse=True,
            )[: cls.DENSE_SOURCE_CUT_MAX_EPISODES]
        ]
        cuts_by_episode: dict[str, list[float]] = {}
        for episode in selected_episodes:
            episode_path = AnimeLibraryService.resolve_episode_path(
                episode,
                library_type=library_type,
            )
            if episode_path is None:
                continue
            merged_cuts: list[float] = []
            for threshold in cls.DENSE_SOURCE_CUT_THRESHOLDS:
                started_at = time.perf_counter()
                cuts = GapResolutionService.load_cached_scene_cuts(
                    str(episode_path),
                    threshold=threshold,
                    min_scene_len=cls.DENSE_SOURCE_CUT_MIN_SCENE_LEN,
                    frame_skip=cls.DENSE_SOURCE_CUT_FRAME_SKIP,
                )
                cls._record_runtime_stat(
                    "dense_source_cut_cache_seconds",
                    time.perf_counter() - started_at,
                )
                cls._record_runtime_stat("dense_source_cut_cache_calls")
                if cuts is None:
                    cls._record_runtime_stat("dense_source_cut_cache_misses")
                    continue
                cls._record_runtime_stat("dense_source_cut_cache_hits")
                merged_cuts.extend(float(cut) for cut in cuts)
            if merged_cuts:
                cuts_by_episode[episode] = sorted(
                    {round(cut, 3) for cut in merged_cuts}
                )
        return cuts_by_episode

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
                if alternative.algorithm == "weighted_avg":
                    if alternative.vote_count < 2:
                        algorithm_bonus = -0.20
                    elif alternative.vote_count < 4:
                        algorithm_bonus = -0.05
                    else:
                        algorithm_bonus = 0.02
                else:
                    algorithm_bonus = {
                        "best_frame": -0.25,
                        "union_topk": -0.25,
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
    def _preserve_skipped_partial_rematch_matches(
        cls,
        matches: MatchList,
        existing_matches: MatchList | None,
        target_indices: set[int],
    ) -> MatchList:
        """Keep partial rematches from mutating scenes outside target_indices."""
        if existing_matches is None:
            return matches

        preserved_matches: list[SceneMatch] = []
        for index, match in enumerate(matches.matches):
            if index in target_indices or index >= len(existing_matches.matches):
                preserved_matches.append(match)
                continue

            existing_match = existing_matches.matches[index].model_copy(deep=True)
            existing_match.scene_index = match.scene_index
            preserved_matches.append(existing_match)

        return MatchList(matches=preserved_matches)

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
        probe_frames, probe_frame_indices = await loop.run_in_executor(
            None,
            cls._extract_scene_probe_frames_with_indices,
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
                video_path=video_path,
                probe_frame_indices=probe_frame_indices,
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

                if existing_matches and i < len(existing_matches.matches):
                    existing_match = existing_matches.matches[i]
                    if existing_match.merged_from is not None:
                        merged_seed = cls._proposal_from_match(
                            existing_match,
                            source="merged_seed",
                            selection_bonus=0.10,
                        )
                        if merged_seed is not None:
                            proposals.append(merged_seed)

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

                direct_projected_proposal = cls._find_projected_interval_proposal(
                    start_candidates,
                    scene.duration,
                    middle_candidates=middle_candidates,
                    end_candidates=end_candidates,
                    allowed_episodes=set(
                        cls._rank_candidate_episodes(
                            start_candidates,
                            middle_candidates,
                            end_candidates,
                            limit=2,
                        )
                    ),
                    source="projected",
                    selection_bonus=0.25,
                )
                if direct_projected_proposal is not None:
                    proposals.append(direct_projected_proposal)

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
                    if selected_before_refine.source != "merged_seed":
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
                            if cls._source_duration_within_speed_bounds(
                                scene.duration,
                                refined_start,
                                refined_end,
                            ):
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

        # GOAL v4.2 M5 (2026-07-13): the legacy correction-pass stack is
        # deleted — production matching runs the aligner; this pipeline
        # only serves the manual merge/rematch route, whose contract
        # (test_anime_matcher_partial_rematch) needs search evidence ->
        # proposals -> repair, nothing more.
        matches = cls._validate_and_repair_matches(scenes, matches)
        if scene_indices_to_match is not None:
            matches = cls._preserve_skipped_partial_rematch_matches(
                matches,
                existing_matches,
                target_indices,
            )

        yield MatchProgress(
            "complete",
            1.0,
            f"Matched {len(matches.matches)} scenes",
            total_scenes,
            total_scenes,
            matches,
        )
