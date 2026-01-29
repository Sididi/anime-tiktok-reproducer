"""
15 TikTok-optimized subtitle styles: 8 karaoke + 7 regular.
"""

from ..models.subtitle import SubtitleStyle, SubtitleStyleType, KaraokeEffect


# ============================================================================
# KARAOKE STYLES (8) - Word-by-word highlighting
# ============================================================================

YELLOW_POP = SubtitleStyle(
    id="yellow_pop",
    name="Yellow Pop",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=85,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=5,
    karaoke_effect=KaraokeEffect.COLOR_CHANGE,
    highlight_color="#FFD700",  # Gold
)

SCALE_CYAN = SubtitleStyle(
    id="scale_cyan",
    name="Scale Cyan",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=80,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=4,
    karaoke_effect=KaraokeEffect.SCALE,
    highlight_color="#00FFFF",  # Cyan
    highlight_scale=1.25,
)

GLOW_PINK = SubtitleStyle(
    id="glow_pink",
    name="Glow Pink",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=80,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=4,
    glow_enabled=True,
    glow_color="#FF69B4",  # Hot Pink
    glow_radius=15,
    karaoke_effect=KaraokeEffect.GLOW,
    highlight_color="#FF69B4",
)

BOX_HIGHLIGHT_RED = SubtitleStyle(
    id="box_highlight_red",
    name="Box Highlight Red",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=75,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=3,
    karaoke_effect=KaraokeEffect.BOX_HIGHLIGHT,
    highlight_color="#FF4444",  # Red
)

BOUNCE_GREEN = SubtitleStyle(
    id="bounce_green",
    name="Bounce Green",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=80,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=5,
    karaoke_effect=KaraokeEffect.BOUNCE,
    highlight_color="#00FF00",  # Green
)

UNDERLINE_ORANGE = SubtitleStyle(
    id="underline_orange",
    name="Underline Orange",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=80,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=4,
    karaoke_effect=KaraokeEffect.UNDERLINE,
    highlight_color="#FF8C00",  # Dark Orange
)

CLASSIC_WHITE_YELLOW = SubtitleStyle(
    id="classic_white_yellow",
    name="Classic White→Yellow",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=85,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=5,
    karaoke_effect=KaraokeEffect.COLOR_CHANGE,
    highlight_color="#FFFF00",  # Yellow
)

GRADIENT_PURPLE = SubtitleStyle(
    id="gradient_purple",
    name="Gradient Purple",
    style_type=SubtitleStyleType.KARAOKE,
    font_family="Impact",
    font_size=80,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=4,
    karaoke_effect=KaraokeEffect.GRADIENT,
    highlight_color="#9B59B6",  # Purple
)


# ============================================================================
# REGULAR STYLES (7) - Static styling
# ============================================================================

CLEAN_WHITE = SubtitleStyle(
    id="clean_white",
    name="Clean White",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=80,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=4,
)

BOLD_YELLOW = SubtitleStyle(
    id="bold_yellow",
    name="Bold Yellow",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=90,
    text_color="#FFD700",
    stroke_color="#000000",
    stroke_width=6,
)

BOX_BLACK = SubtitleStyle(
    id="box_black",
    name="Box Black",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=75,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=0,
    box_enabled=True,
    box_color="#000000",
    box_opacity=0.85,
    box_padding=12,
)

NEON_CYAN = SubtitleStyle(
    id="neon_cyan",
    name="Neon Cyan",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=80,
    text_color="#00FFFF",
    stroke_color="#000000",
    stroke_width=3,
    glow_enabled=True,
    glow_color="#00FFFF",
    glow_radius=12,
)

HEAVY_OUTLINE = SubtitleStyle(
    id="heavy_outline",
    name="Heavy Outline",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=85,
    text_color="#FFFFFF",
    stroke_color="#000000",
    stroke_width=8,
)

PINK_GLOW = SubtitleStyle(
    id="pink_glow",
    name="Pink Glow",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=80,
    text_color="#FF69B4",
    stroke_color="#000000",
    stroke_width=3,
    glow_enabled=True,
    glow_color="#FF69B4",
    glow_radius=10,
)

MINIMAL = SubtitleStyle(
    id="minimal",
    name="Minimal",
    style_type=SubtitleStyleType.REGULAR,
    font_family="Impact",
    font_size=70,
    text_color="#FFFFFF",
    stroke_color="#333333",
    stroke_width=2,
)


# ============================================================================
# Style Registry
# ============================================================================

ALL_STYLES: list[SubtitleStyle] = [
    # Karaoke styles
    YELLOW_POP,
    SCALE_CYAN,
    GLOW_PINK,
    BOX_HIGHLIGHT_RED,
    BOUNCE_GREEN,
    UNDERLINE_ORANGE,
    CLASSIC_WHITE_YELLOW,
    GRADIENT_PURPLE,
    # Regular styles
    CLEAN_WHITE,
    BOLD_YELLOW,
    BOX_BLACK,
    NEON_CYAN,
    HEAVY_OUTLINE,
    PINK_GLOW,
    MINIMAL,
]

STYLES_BY_ID: dict[str, SubtitleStyle] = {style.id: style for style in ALL_STYLES}

KARAOKE_STYLES = [s for s in ALL_STYLES if s.style_type == SubtitleStyleType.KARAOKE]
REGULAR_STYLES = [s for s in ALL_STYLES if s.style_type == SubtitleStyleType.REGULAR]


def get_style(style_id: str) -> SubtitleStyle | None:
    """Get a style by ID."""
    return STYLES_BY_ID.get(style_id)


def list_styles() -> list[SubtitleStyle]:
    """Get all available styles."""
    return ALL_STYLES
