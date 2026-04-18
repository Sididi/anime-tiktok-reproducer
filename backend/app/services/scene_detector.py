from pathlib import Path
from typing import AsyncIterator
import asyncio

from scenedetect import open_video, SceneManager, ContentDetector

from ..models import Scene, SceneList


class SceneDetectionProgress:
    """Progress information for scene detection."""

    def __init__(
        self,
        status: str,
        progress: float = 0,
        message: str = "",
        scenes: list[Scene] | None = None,
        error: str | None = None,
    ):
        self.status = status
        self.progress = progress
        self.message = message
        self.scenes = scenes
        self.error = error

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
        }
        if self.scenes is not None:
            result["scenes"] = [
                {
                    "index": s.index,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "duration": s.duration,
                }
                for s in self.scenes
            ]
        return result


class SceneDetectorService:
    """Service for detecting scenes using PySceneDetect."""

    HEARTBEAT_INTERVAL_SECONDS = 3.0
    EXTREME_SHORT_SECONDS_FLOOR = 0.08
    EXTREME_SHORT_MIN_FRAMES = 3
    SSCD_TIE_EPSILON = 1e-3

    @classmethod
    async def detect_scenes(
        cls,
        video_path: Path,
        threshold: float = 16.0,
        min_scene_len: int = 10,
        library_path: Path | None = None,
        library_type: object = None,
        anime_name: str | None = None,
    ) -> AsyncIterator[SceneDetectionProgress]:
        """
        Detect scenes in a video and yield progress updates.

        Args:
            video_path: Path to the video file
            threshold: ContentDetector threshold (lower = more sensitive)
            min_scene_len: Minimum scene length in frames
            library_path / library_type / anime_name: when provided, the
                extreme-short-scene sanitizer uses the anime_searcher index
                (via AnimeMatcherService._init_searcher) to pick the merge
                direction by SSCD similarity. Without them it falls back to
                merging with the larger neighbour.
        """
        yield SceneDetectionProgress("starting", 0, "Opening video...")

        try:
            loop = asyncio.get_running_loop()
            detect_future = loop.run_in_executor(
                None,
                cls._detect_sync,
                video_path,
                threshold,
                min_scene_len,
                library_path,
                library_type,
                anime_name,
            )
            heartbeat_count = 0
            while True:
                try:
                    scenes = await asyncio.wait_for(
                        asyncio.shield(detect_future),
                        timeout=cls.HEARTBEAT_INTERVAL_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    heartbeat_count += 1
                    heartbeat_progress = min(0.9, 0.05 + (heartbeat_count * 0.03))
                    yield SceneDetectionProgress(
                        "processing",
                        heartbeat_progress,
                        "Analyzing scene boundaries...",
                    )

            yield SceneDetectionProgress(
                "complete",
                1.0,
                f"Detected {len(scenes)} scenes",
                scenes,
            )

        except Exception as e:
            yield SceneDetectionProgress("error", 0, "", error=str(e))

    @classmethod
    async def detect_project_scenes(
        cls,
        project_id: str,
        threshold: float = 16.0,
        min_scene_len: int = 10,
    ) -> AsyncIterator[SceneDetectionProgress]:
        from pathlib import Path

        from ..models import ProjectPhase, SceneList
        from .project_service import ProjectService

        project = ProjectService.load(project_id)
        if project is None:
            raise RuntimeError("Project not found")
        if not project.video_path:
            raise RuntimeError("No video available")

        video_path = Path(project.video_path)
        if not video_path.exists():
            raise RuntimeError("Video file not found")

        from .anime_library import AnimeLibraryService

        library_path = AnimeLibraryService.get_library_path(project.library_type)
        library_type = project.library_type
        anime_name = project.anime_name

        project.phase = ProjectPhase.SCENE_DETECTION
        ProjectService.save(project)

        async for progress in cls.detect_scenes(
            video_path,
            threshold,
            min_scene_len,
            library_path=library_path,
            library_type=library_type,
            anime_name=anime_name,
        ):
            if progress.status == "complete" and progress.scenes:
                scene_list = SceneList(scenes=progress.scenes)
                ProjectService.save_scenes(project_id, scene_list)

                project = ProjectService.load(project_id)
                if project is None:
                    raise RuntimeError("Project not found")
                project.phase = ProjectPhase.SCENE_VALIDATION
                ProjectService.save(project)
            elif progress.status == "error":
                project = ProjectService.load(project_id)
                if project is not None:
                    project.phase = ProjectPhase.SETUP
                    ProjectService.save(project)

            yield progress

    @staticmethod
    def _detect_sync(
        video_path: Path,
        threshold: float,
        min_scene_len: int,
        library_path: Path | None = None,
        library_type: object = None,
        anime_name: str | None = None,
    ) -> list[Scene]:
        """Synchronous scene detection."""
        ranges, _, fps = SceneDetectorService._detect_ranges(
            video_path,
            threshold,
            min_scene_len,
        )

        sanitized_ranges = SceneDetectorService._sanitize_extreme_short_ranges(
            video_path=video_path,
            ranges=ranges,
            fps=fps,
            library_path=library_path,
            library_type=library_type,
            anime_name=anime_name,
        )

        return [
            Scene(
                index=i,
                start_time=start,
                end_time=end,
            )
            for i, (start, end) in enumerate(sanitized_ranges)
        ]

    @staticmethod
    def _detect_ranges(
        video_path: Path,
        threshold: float,
        min_scene_len: int,
    ) -> tuple[list[tuple[float, float]], float, float | None]:
        """Detect scene ranges and return (ranges, video_duration, fps)."""
        video = open_video(str(video_path))
        scene_manager = SceneManager()
        scene_manager.add_detector(
            ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
        )

        scene_manager.detect_scenes(video, show_progress=False)
        scene_list = scene_manager.get_scene_list()
        video_duration = video.duration.get_seconds()
        fps_raw = getattr(video, "frame_rate", None)
        fps = float(fps_raw) if isinstance(fps_raw, (float, int)) and fps_raw > 0 else None

        if not scene_list:
            return [(0.0, video_duration)], video_duration, fps

        ranges: list[tuple[float, float]] = []
        for start, end in scene_list:
            ranges.append((start.get_seconds(), end.get_seconds()))
        return ranges, video_duration, fps

    @classmethod
    def _extreme_short_threshold_seconds(cls, fps: float | None) -> float:
        if fps and fps > 0:
            return max(cls.EXTREME_SHORT_SECONDS_FLOOR, cls.EXTREME_SHORT_MIN_FRAMES / fps)
        return cls.EXTREME_SHORT_SECONDS_FLOOR

    @classmethod
    def _sanitize_extreme_short_ranges(
        cls,
        *,
        video_path: Path,
        ranges: list[tuple[float, float]],
        fps: float | None,
        library_path: Path | None,
        library_type: object,
        anime_name: str | None,
    ) -> list[tuple[float, float]]:
        """Merge extremely short outlier scenes with the visually closest neighbour.

        When library context is provided, merge direction is chosen by SSCD
        cosine similarity (via the anime_searcher QueryProcessor already used
        by /matches). Otherwise — or on any loading/embedding failure — falls
        back to merging with the larger neighbour.
        """
        if len(ranges) <= 1:
            return ranges

        short_threshold = cls._extreme_short_threshold_seconds(fps)
        has_short = any((end - start) < short_threshold for start, end in ranges)
        if not has_short:
            return ranges

        searcher_ready = cls._ensure_anime_searcher(
            library_path=library_path,
            library_type=library_type,
            anime_name=anime_name,
        )

        merged = list(ranges)
        idx = 0
        while idx < len(merged):
            start, end = merged[idx]
            duration = end - start
            if duration >= short_threshold or len(merged) == 1:
                idx += 1
                continue

            if idx == 0:
                _, next_end = merged[1]
                merged[1] = (start, next_end)
                merged.pop(0)
                idx = 0
                continue

            if idx == len(merged) - 1:
                prev_start, _ = merged[idx - 1]
                merged[idx - 1] = (prev_start, end)
                merged.pop(idx)
                idx = max(0, idx - 1)
                continue

            prev_start, prev_end = merged[idx - 1]
            next_start, next_end = merged[idx + 1]

            merge_with_previous = cls._pick_merge_direction(
                video_path=video_path,
                short_range=(start, end),
                prev_range=(prev_start, prev_end),
                next_range=(next_start, next_end),
                searcher_ready=searcher_ready,
            )

            if merge_with_previous:
                merged[idx - 1] = (prev_start, end)
                merged.pop(idx)
                idx = max(0, idx - 1)
            else:
                merged[idx] = (start, next_end)
                merged.pop(idx + 1)

        return merged

    @staticmethod
    def _ensure_anime_searcher(
        *,
        library_path: Path | None,
        library_type: object,
        anime_name: str | None,
    ) -> bool:
        """Load the anime_searcher singletons using the same path /matches uses.

        Returns True if the QueryProcessor is ready for embedding.
        """
        if library_path is None or library_type is None:
            return False
        try:
            from .anime_matcher import AnimeMatcherService

            return AnimeMatcherService._init_searcher(
                library_path,
                library_type,
                anime_name,
            )
        except Exception:
            return False

    @classmethod
    def _pick_merge_direction(
        cls,
        *,
        video_path: Path,
        short_range: tuple[float, float],
        prev_range: tuple[float, float],
        next_range: tuple[float, float],
        searcher_ready: bool,
    ) -> bool:
        """Return True if the short scene should merge with its previous neighbour.

        When the anime_searcher singletons are loaded, picks direction by SSCD
        cosine similarity via the QueryProcessor's embedder (same path as
        /matches — see `_search_image_batch`). Falls back to the
        larger-neighbour rule on tie or any failure.
        """
        prev_duration = prev_range[1] - prev_range[0]
        next_duration = next_range[1] - next_range[0]
        duration_fallback = prev_duration >= next_duration

        if not searcher_ready:
            return duration_fallback

        def midpoint(r: tuple[float, float]) -> float:
            return (r[0] + r[1]) / 2.0

        try:
            from .anime_matcher import AnimeMatcherService

            processor = AnimeMatcherService._query_processor
            if processor is None:
                return duration_fallback

            frames = AnimeMatcherService.extract_frames(
                video_path,
                [midpoint(prev_range), midpoint(short_range), midpoint(next_range)],
            )
            if any(f is None for f in frames):
                return duration_fallback

            prepared = [img.convert("RGB") for img in frames]
            embeddings = processor.embedder.embed_batch(prepared)
            if embeddings.shape[0] < 3:
                return duration_fallback

            prev_emb, short_emb, next_emb = embeddings[0], embeddings[1], embeddings[2]
            sim_prev = float(short_emb @ prev_emb)
            sim_next = float(short_emb @ next_emb)

            if abs(sim_prev - sim_next) < cls.SSCD_TIE_EPSILON:
                return duration_fallback
            return sim_prev > sim_next
        except Exception:
            return duration_fallback
