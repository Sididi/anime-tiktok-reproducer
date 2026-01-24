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
