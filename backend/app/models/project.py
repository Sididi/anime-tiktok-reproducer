from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field
import uuid


class ProjectPhase(str, Enum):
    """Current phase of the project pipeline."""

    SETUP = "setup"
    DOWNLOADING = "downloading"
    SCENE_DETECTION = "scene_detection"
    SCENE_VALIDATION = "scene_validation"
    MATCHING = "matching"
    MATCH_VALIDATION = "match_validation"
    TRANSCRIPTION = "transcription"
    SCRIPT_RESTRUCTURE = "script_restructure"
    PROCESSING = "processing"
    COMPLETE = "complete"


class Project(BaseModel):
    """A TikTok reproducer project."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    tiktok_url: str | None = None
    anime_name: str | None = None  # Selected anime from indexed library
    source_paths: list[str] = Field(default_factory=list)  # Kept for backwards compatibility
    phase: ProjectPhase = ProjectPhase.SETUP
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    # Video metadata (populated after download)
    video_path: str | None = None
    video_duration: float | None = None
    video_fps: float | None = None
    video_width: int | None = None
    video_height: int | None = None
