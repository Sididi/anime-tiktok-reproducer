from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import Any
import uuid

from ..config import settings
from ..library_types import DEFAULT_LIBRARY_TYPE, LibraryType


class ProjectPhase(str, Enum):
    """Current phase of the project pipeline."""

    SETUP = "setup"
    DOWNLOADING = "downloading"
    SCENE_DETECTION = "scene_detection"
    SCENE_VALIDATION = "scene_validation"
    MATCHING = "matching"
    MATCH_VALIDATION = "match_validation"
    TRANSCRIPTION = "transcription"
    RAW_SCENE_VALIDATION = "raw_scene_validation"
    SCRIPT_RESTRUCTURE = "script_restructure"
    PROCESSING = "processing"
    COMPLETE = "complete"


class PlatformSchedule(BaseModel):
    """Per-platform slot reservation on a Project."""

    slot: datetime
    scheduled_at: datetime
    manual: bool = False


class Project(BaseModel):
    """A TikTok reproducer project."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    tiktok_url: str | None = None
    anime_name: str | None = None  # Selected anime from indexed library
    series_id: str | None = None
    library_type: LibraryType = DEFAULT_LIBRARY_TYPE
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

    # Output / integration state
    output_language: str | None = None
    drive_folder_id: str | None = None
    drive_folder_url: str | None = None
    drive_export_uploaded_once: bool = False
    generation_discord_message_id: str | None = None
    final_upload_discord_message_id: str | None = None
    upload_completed_at: datetime | None = None
    upload_last_result: dict[str, Any] | None = None
    # Script phase settings
    music_key: str | None = None
    tts_speed: float | None = None
    video_overlay: dict[str, Any] | None = None
    voice_key: str | None = None
    llm_preset: str | None = None
    template: str | None = None
    min_playback_speed: float | None = None

    # Scheduling
    scheduled_at: datetime | None = None  # derived aggregate: max of platform_schedules[*].scheduled_at
    scheduled_account_id: str | None = None
    scheduled_slot: str | None = None  # legacy; no longer written by new code
    platform_schedules: dict[str, PlatformSchedule] = Field(default_factory=dict)

    # Per-platform pending platform-side notifications. Set when a reschedule
    # could not be propagated to YT/FB/IG and is awaiting retry.
    # key = platform; value = {target_scheduled_at, retries, last_error, last_attempt_at}.
    reschedule_pending: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("min_playback_speed")
    @classmethod
    def _validate_min_playback_speed(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if value <= 0.10 or value > 1.0:
            raise ValueError(
                "min_playback_speed must be greater than 0.10 and at most 1.0"
            )
        return value

    def resolved_min_playback_speed(self) -> float:
        if self.min_playback_speed is not None:
            return self.min_playback_speed
        template_value = self._resolved_template().min_playback_speed
        if template_value is not None:
            return template_value
        return settings.min_playback_speed_factor

    def resolved_llm_preset_key(self) -> str:
        from ..services.llm_config_service import LLMConfigService
        return (
            self.llm_preset
            or self._resolved_template().llm_preset
            or LLMConfigService.default_preset_key()
        )

    def resolved_voice_key(self) -> str | None:
        from ..services.voice_config_service import VoiceConfigService

        if self.voice_key is not None:
            return self.voice_key
        template_value = self._resolved_template().voice_key
        if template_value is not None:
            return template_value
        return VoiceConfigService.get_config().default_voice_key

    def resolved_music_key(self) -> str | None:
        from ..services.music_config_service import MusicConfigService

        if self.music_key is not None:
            return self.music_key
        template_value = self._resolved_template().music_key
        if template_value is not None:
            return template_value
        return MusicConfigService.get_config().default_music_key

    def _resolved_template(self):
        from ..services.template_service import TemplateService

        return TemplateService.get(self.resolved_template_key())

    def resolved_template_key(self) -> str:
        from ..services.template_service import TemplateService
        return self.template or TemplateService.default_key()
