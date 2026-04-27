from pydantic import BaseModel, Field


class Scene(BaseModel):
    """A scene in the TikTok video."""

    index: int
    start_time: float  # seconds
    end_time: float  # seconds

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time


class SceneList(BaseModel):
    """List of scenes for a project."""

    scenes: list[Scene] = Field(default_factory=list)

    def renumber(self) -> None:
        """Renumber scenes sequentially after modifications."""
        for i, scene in enumerate(self.scenes):
            scene.index = i

    def validate_continuity(self) -> bool:
        """Check that scenes are continuous with no gaps."""
        if not self.scenes:
            return True

        for i in range(1, len(self.scenes)):
            if abs(self.scenes[i].start_time - self.scenes[i - 1].end_time) > 0.001:
                return False
        return True

    def merge_tiny_scenes(
        self, threshold: float = 0.35
    ) -> tuple["SceneList", list[tuple[int, int]]]:
        """Merge scenes below a duration threshold into adjacent scenes.

        Tiny scenes (e.g. fade-out transitions from pyscenedetect) produce
        poor matcher results. This absorbs them into neighbors before matching.

        Merge direction: into previous scene, except leading tiny scenes which
        merge forward into the first non-tiny scene.

        Returns:
            (merged_scene_list, merge_log) where merge_log contains
            (absorbed_original_index, absorbed_into_original_index) tuples.
        """
        if len(self.scenes) <= 1:
            return SceneList(scenes=list(self.scenes)), []

        # If ALL scenes are tiny, return unchanged to avoid collapsing everything
        if all(s.duration < threshold for s in self.scenes):
            return SceneList(scenes=list(self.scenes)), []

        merge_log: list[tuple[int, int]] = []
        result: list[Scene] = []
        # Tiny scenes at the start, pending forward-merge
        pending_start: float | None = None
        pending_indices: list[int] = []

        for scene in self.scenes:
            if scene.duration < threshold:
                if not result:
                    # Leading tiny scene — accumulate for forward merge
                    if pending_start is None:
                        pending_start = scene.start_time
                    pending_indices.append(scene.index)
                else:
                    # Normal case — absorb into previous scene
                    result[-1] = Scene(
                        index=result[-1].index,
                        start_time=result[-1].start_time,
                        end_time=scene.end_time,
                    )
                    merge_log.append((scene.index, result[-1].index))
            else:
                # Non-tiny scene
                if pending_indices:
                    # Flush pending leading tiny scenes into this one
                    assert pending_start is not None
                    new_scene = Scene(
                        index=scene.index,
                        start_time=pending_start,
                        end_time=scene.end_time,
                    )
                    for pidx in pending_indices:
                        merge_log.append((pidx, scene.index))
                    result.append(new_scene)
                    pending_start = None
                    pending_indices = []
                else:
                    result.append(
                        Scene(
                            index=scene.index,
                            start_time=scene.start_time,
                            end_time=scene.end_time,
                        )
                    )

        merged = SceneList(scenes=result)
        merged.renumber()
        assert merged.validate_continuity(), "Tiny scene merge broke continuity"
        return merged, merge_log
