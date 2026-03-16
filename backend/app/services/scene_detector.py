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

    EXTREME_SHORT_SECONDS_FLOOR = 0.08
    EXTREME_SHORT_MIN_FRAMES = 3
    HIGH_THRESHOLD_MULTIPLIER = 1.35
    HIGH_THRESHOLD_DELTA = 6.0
    HIGH_THRESHOLD_MAX = 60.0
    BOUNDARY_TOLERANCE_FLOOR = 0.02

    @classmethod
    async def detect_scenes(
        cls,
        video_path: Path,
        threshold: float = 18.0,
        min_scene_len: int = 10,
    ) -> AsyncIterator[SceneDetectionProgress]:
        """
        Detect scenes in a video and yield progress updates.

        Args:
            video_path: Path to the video file
            threshold: ContentDetector threshold (lower = more sensitive)
            min_scene_len: Minimum scene length in frames
        """
        yield SceneDetectionProgress("starting", 0, "Opening video...")

        try:
            # Run detection in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            scenes = await loop.run_in_executor(
                None,
                cls._detect_sync,
                video_path,
                threshold,
                min_scene_len,
            )

            yield SceneDetectionProgress(
                "complete",
                1.0,
                f"Detected {len(scenes)} scenes",
                scenes,
            )

        except Exception as e:
            yield SceneDetectionProgress("error", 0, "", error=str(e))

    @staticmethod
    def _detect_sync(
        video_path: Path,
        threshold: float,
        min_scene_len: int,
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
            threshold=threshold,
            min_scene_len=min_scene_len,
            fps=fps,
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

    @staticmethod
    def _extract_internal_boundaries(ranges: list[tuple[float, float]]) -> list[float]:
        if len(ranges) <= 1:
            return []
        return [ranges[i][1] for i in range(len(ranges) - 1)]

    @classmethod
    def _boundary_present(
        cls,
        boundary: float,
        boundaries: list[float],
        tolerance: float,
    ) -> bool:
        for candidate in boundaries:
            if abs(candidate - boundary) <= tolerance:
                return True
        return False

    @classmethod
    def _sanitize_extreme_short_ranges(
        cls,
        *,
        video_path: Path,
        ranges: list[tuple[float, float]],
        threshold: float,
        min_scene_len: int,
        fps: float | None,
    ) -> list[tuple[float, float]]:
        """Merge only extremely short outlier scenes with an adjacent scene."""
        if len(ranges) <= 1:
            return ranges

        short_threshold = cls._extreme_short_threshold_seconds(fps)
        has_short = any((end - start) < short_threshold for start, end in ranges)
        if not has_short:
            return ranges

        high_threshold = min(
            cls.HIGH_THRESHOLD_MAX,
            max(threshold + cls.HIGH_THRESHOLD_DELTA, threshold * cls.HIGH_THRESHOLD_MULTIPLIER),
        )
        high_ranges, _, _ = cls._detect_ranges(video_path, high_threshold, min_scene_len)
        high_boundaries = cls._extract_internal_boundaries(high_ranges)
        boundary_tolerance = max(cls.BOUNDARY_TOLERANCE_FLOOR, short_threshold / 2.0)

        merged = list(ranges)
        idx = 0
        while idx < len(merged):
            start, end = merged[idx]
            duration = end - start
            if duration >= short_threshold or len(merged) == 1:
                idx += 1
                continue

            if idx == 0:
                next_start, next_end = merged[1]
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

            left_boundary = start
            right_boundary = end

            left_present = cls._boundary_present(
                left_boundary,
                high_boundaries,
                boundary_tolerance,
            )
            right_present = cls._boundary_present(
                right_boundary,
                high_boundaries,
                boundary_tolerance,
            )

            merge_with_previous: bool
            if left_present and not right_present:
                merge_with_previous = False
            elif right_present and not left_present:
                merge_with_previous = True
            else:
                prev_duration = merged[idx - 1][1] - merged[idx - 1][0]
                next_duration = merged[idx + 1][1] - merged[idx + 1][0]
                merge_with_previous = prev_duration >= next_duration

            if merge_with_previous:
                prev_start, _ = merged[idx - 1]
                merged[idx - 1] = (prev_start, end)
                merged.pop(idx)
                idx = max(0, idx - 1)
            else:
                _, next_end = merged[idx + 1]
                merged[idx] = (start, next_end)
                merged.pop(idx + 1)

        return merged
