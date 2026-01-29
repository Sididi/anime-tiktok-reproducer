from enum import Enum
from pydantic import BaseModel


class SubtitleStyleType(str, Enum):
    """Type of subtitle style."""
    KARAOKE = "karaoke"
    REGULAR = "regular"


class SubtitleWord(BaseModel):
    """A word with timing for subtitle rendering."""
    text: str
    start: float
    end: float


class KaraokeEffect(str, Enum):
    """Karaoke animation effects."""
    COLOR_CHANGE = "color_change"
    SCALE = "scale"
    GLOW = "glow"
    BOX_HIGHLIGHT = "box_highlight"
    BOUNCE = "bounce"
    UNDERLINE = "underline"
    GRADIENT = "gradient"


class SubtitleStyle(BaseModel):
    """Definition of a subtitle style."""
    id: str
    name: str
    style_type: SubtitleStyleType

    # Text properties
    font_family: str = "Impact"
    font_size: int = 80
    text_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: int = 4

    # Glow effect
    glow_enabled: bool = False
    glow_color: str = "#00FFFF"
    glow_radius: int = 10

    # Background box
    box_enabled: bool = False
    box_color: str = "#000000"
    box_opacity: float = 0.8
    box_padding: int = 10

    # Karaoke-specific properties
    karaoke_effect: KaraokeEffect | None = None
    highlight_color: str = "#FFD700"
    highlight_scale: float = 1.2

    # Position (percentage from top, 0-100)
    vertical_position: float = 50.0


class SubtitleGenerationRequest(BaseModel):
    """Request to generate subtitle video."""
    style_id: str
    output_format: str = "webm"  # "webm" or "mov"
    use_new_tts: bool = True  # Use new TTS transcription or original


class SubtitlePreviewRequest(BaseModel):
    """Request to generate style previews."""
    duration: float = 7.0  # Preview duration in seconds


class SubtitleGenerationProgress(BaseModel):
    """Progress update for subtitle generation."""
    status: str  # "starting", "rendering", "encoding", "complete", "error"
    progress: float  # 0.0 to 1.0
    message: str
    output_file: str | None = None
    error: str | None = None
