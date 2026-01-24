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

    @classmethod
    async def detect_scenes(
        cls,
        video_path: Path,
        threshold: float = 27.0,
        min_scene_len: int = 15,
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
        video = open_video(str(video_path))
        scene_manager = SceneManager()
        scene_manager.add_detector(
            ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
        )

        # Detect scenes
        scene_manager.detect_scenes(video, show_progress=False)
        scene_list = scene_manager.get_scene_list()

        # Convert to our Scene model
        scenes: list[Scene] = []
        video_duration = video.duration.get_seconds()

        if not scene_list:
            # No cuts detected, treat entire video as one scene
            scenes.append(
                Scene(
                    index=0,
                    start_time=0.0,
                    end_time=video_duration,
                )
            )
        else:
            for i, (start, end) in enumerate(scene_list):
                scenes.append(
                    Scene(
                        index=i,
                        start_time=start.get_seconds(),
                        end_time=end.get_seconds(),
                    )
                )

        return scenes
