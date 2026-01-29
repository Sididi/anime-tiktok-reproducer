"""
Pillow-based frame renderer for transparent subtitle videos with karaoke effects.
"""

import math
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from ..models.subtitle import SubtitleStyle, SubtitleWord, KaraokeEffect, SubtitleStyleType


@dataclass
class WordPosition:
    """Position and properties for rendering a word."""
    text: str
    x: int
    y: int
    is_highlighted: bool
    highlight_progress: float  # 0.0 to 1.0 for animation


class SubtitleFrameRenderer:
    """Renders subtitle frames with transparency and karaoke effects."""

    WIDTH = 1080
    HEIGHT = 1920
    MAX_CHARS_PER_LINE = 15

    def __init__(self, style: SubtitleStyle):
        self.style = style
        self._font = self._load_font(style.font_size)
        self._highlight_font = self._load_font(
            int(style.font_size * style.highlight_scale)
        )

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont:
        """Load font with fallback."""
        font_paths = [
            # Arch Linux / common paths
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/TTF/Impact.ttf",
            "/usr/share/fonts/gnu-free/FreeSansBold.otf",
            # Ubuntu / Debian paths
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]

        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue

        # Last resort: default font
        return ImageFont.load_default()

    def _hex_to_rgba(self, hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
        """Convert hex color to RGBA tuple."""
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return (r, g, b, alpha)

    def _wrap_text(self, words: list[SubtitleWord]) -> list[list[SubtitleWord]]:
        """Wrap words into lines with max characters per line."""
        lines: list[list[SubtitleWord]] = []
        current_line: list[SubtitleWord] = []
        current_length = 0

        for word in words:
            word_len = len(word.text)
            # +1 for space if not first word
            space_len = 1 if current_line else 0

            if current_length + space_len + word_len > self.MAX_CHARS_PER_LINE:
                if current_line:
                    lines.append(current_line)
                current_line = [word]
                current_length = word_len
            else:
                current_line.append(word)
                current_length += space_len + word_len

        if current_line:
            lines.append(current_line)

        return lines

    def _get_word_highlight_state(
        self, word: SubtitleWord, current_time: float
    ) -> tuple[bool, float]:
        """
        Determine if word is highlighted and animation progress.

        Returns (is_highlighted, progress) where progress is 0-1.
        """
        if current_time < word.start:
            return False, 0.0
        elif current_time >= word.end:
            return True, 1.0
        else:
            # Currently being spoken - animate
            duration = word.end - word.start
            if duration > 0:
                progress = (current_time - word.start) / duration
            else:
                progress = 1.0
            return True, min(1.0, progress)

    def _draw_text_with_stroke(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: int,
        y: int,
        font: ImageFont.FreeTypeFont,
        text_color: tuple[int, int, int, int],
        stroke_color: tuple[int, int, int, int],
        stroke_width: int,
    ):
        """Draw text with stroke outline."""
        # Draw stroke by drawing text multiple times offset
        if stroke_width > 0:
            for dx in range(-stroke_width, stroke_width + 1):
                for dy in range(-stroke_width, stroke_width + 1):
                    if dx * dx + dy * dy <= stroke_width * stroke_width:
                        draw.text((x + dx, y + dy), text, font=font, fill=stroke_color)

        # Draw main text
        draw.text((x, y), text, font=font, fill=text_color)

    def _apply_glow(
        self, image: Image.Image, glow_color: str, radius: int
    ) -> Image.Image:
        """Apply glow effect to image."""
        # Create glow layer from alpha channel
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)

        # Get alpha from original
        alpha = image.split()[3]

        # Colorize the alpha as glow
        r, g, b, _ = self._hex_to_rgba(glow_color)
        glow_colored = Image.new("RGBA", image.size, (r, g, b, 0))
        glow_colored.putalpha(alpha)

        # Blur for glow effect
        glow_blurred = glow_colored.filter(ImageFilter.GaussianBlur(radius))

        # Composite: glow behind original
        result = Image.new("RGBA", image.size, (0, 0, 0, 0))
        result = Image.alpha_composite(result, glow_blurred)
        result = Image.alpha_composite(result, image)

        return result

    def render_frame(
        self, words: list[SubtitleWord], current_time: float
    ) -> Image.Image:
        """
        Render a single frame with subtitles.

        Args:
            words: List of words with timing
            current_time: Current playback time in seconds

        Returns:
            RGBA image with transparent background
        """
        # Create transparent image
        image = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        if not words:
            return image

        # Filter to visible words (currently being spoken or recently spoken)
        visible_words = self._get_visible_words(words, current_time)
        if not visible_words:
            return image

        # Wrap text into lines
        lines = self._wrap_text(visible_words)

        # Calculate total height and vertical position
        line_height = int(self.style.font_size * 1.4)
        total_height = len(lines) * line_height

        # Center vertically at specified position
        center_y = int(self.HEIGHT * (self.style.vertical_position / 100))
        start_y = center_y - total_height // 2

        # Render each line
        for line_idx, line_words in enumerate(lines):
            y = start_y + line_idx * line_height
            self._render_line(image, draw, line_words, y, current_time)

        # Apply glow if enabled
        if self.style.glow_enabled:
            image = self._apply_glow(
                image, self.style.glow_color, self.style.glow_radius
            )

        return image

    def _get_visible_words(
        self, words: list[SubtitleWord], current_time: float
    ) -> list[SubtitleWord]:
        """Get words that should be visible at current time."""
        if not words:
            return []

        # Find the current "window" of words to display
        # Show words that are being spoken or were just spoken
        visible = []
        window_start = current_time - 2.0  # Show words from last 2 seconds
        window_end = current_time + 0.5  # And upcoming words

        for word in words:
            if word.start <= window_end and word.end >= window_start:
                visible.append(word)

        # Group into coherent phrases (words close together in time)
        if not visible:
            return []

        # Find the phrase containing the current word
        current_phrase: list[SubtitleWord] = []
        phrase_gap_threshold = 0.5  # Max gap between words in same phrase

        for i, word in enumerate(visible):
            if not current_phrase:
                current_phrase.append(word)
            else:
                gap = word.start - current_phrase[-1].end
                if gap <= phrase_gap_threshold:
                    current_phrase.append(word)
                else:
                    # Check if current time is in this phrase or next
                    if current_time >= word.start:
                        current_phrase = [word]
                    # else keep current phrase

        return current_phrase

    def _render_line(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        words: list[SubtitleWord],
        y: int,
        current_time: float,
    ):
        """Render a single line of words."""
        # Calculate line width for centering
        line_text = " ".join(w.text for w in words)
        bbox = self._font.getbbox(line_text)
        line_width = bbox[2] - bbox[0]
        start_x = (self.WIDTH - line_width) // 2

        # Track x position as we render each word
        x = start_x

        for word in words:
            is_highlighted, progress = self._get_word_highlight_state(word, current_time)

            # Get word dimensions
            word_bbox = self._font.getbbox(word.text)
            word_width = word_bbox[2] - word_bbox[0]

            if self.style.style_type == SubtitleStyleType.KARAOKE and is_highlighted:
                self._render_karaoke_word(image, draw, word, x, y, progress)
            else:
                self._render_regular_word(draw, word.text, x, y)

            # Move to next word position (word width + space)
            space_bbox = self._font.getbbox(" ")
            space_width = space_bbox[2] - space_bbox[0]
            x += word_width + space_width

    def _render_regular_word(
        self, draw: ImageDraw.ImageDraw, text: str, x: int, y: int
    ):
        """Render a word with regular styling."""
        text_color = self._hex_to_rgba(self.style.text_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        # Draw background box if enabled
        if self.style.box_enabled:
            bbox = self._font.getbbox(text)
            padding = self.style.box_padding
            box_alpha = int(255 * self.style.box_opacity)
            box_color = self._hex_to_rgba(self.style.box_color, box_alpha)

            draw.rectangle(
                [
                    x - padding,
                    y - padding,
                    x + bbox[2] - bbox[0] + padding,
                    y + bbox[3] - bbox[1] + padding,
                ],
                fill=box_color,
            )

        self._draw_text_with_stroke(
            draw, text, x, y, self._font, text_color, stroke_color, self.style.stroke_width
        )

    def _render_karaoke_word(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        word: SubtitleWord,
        x: int,
        y: int,
        progress: float,
    ):
        """Render a highlighted karaoke word with effect."""
        effect = self.style.karaoke_effect

        if effect == KaraokeEffect.COLOR_CHANGE:
            self._render_color_change(draw, word.text, x, y)

        elif effect == KaraokeEffect.SCALE:
            self._render_scale(image, word.text, x, y, progress)

        elif effect == KaraokeEffect.GLOW:
            self._render_glow_word(image, word.text, x, y)

        elif effect == KaraokeEffect.BOX_HIGHLIGHT:
            self._render_box_highlight(draw, word.text, x, y)

        elif effect == KaraokeEffect.BOUNCE:
            self._render_bounce(draw, word.text, x, y, progress)

        elif effect == KaraokeEffect.UNDERLINE:
            self._render_underline(draw, word.text, x, y, progress)

        elif effect == KaraokeEffect.GRADIENT:
            self._render_gradient(draw, word.text, x, y, progress)

        else:
            # Fallback to color change
            self._render_color_change(draw, word.text, x, y)

    def _render_color_change(
        self, draw: ImageDraw.ImageDraw, text: str, x: int, y: int
    ):
        """Render word with highlight color."""
        text_color = self._hex_to_rgba(self.style.highlight_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        self._draw_text_with_stroke(
            draw, text, x, y, self._font, text_color, stroke_color, self.style.stroke_width
        )

    def _render_scale(
        self, image: Image.Image, text: str, x: int, y: int, progress: float
    ):
        """Render word with scale-up effect."""
        # Create temporary image for scaled text
        temp = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (0, 0, 0, 0))
        temp_draw = ImageDraw.Draw(temp)

        # Use larger font
        text_color = self._hex_to_rgba(self.style.highlight_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        # Calculate offset to keep word centered during scale
        normal_bbox = self._font.getbbox(text)
        scaled_bbox = self._highlight_font.getbbox(text)

        normal_width = normal_bbox[2] - normal_bbox[0]
        scaled_width = scaled_bbox[2] - scaled_bbox[0]
        normal_height = normal_bbox[3] - normal_bbox[1]
        scaled_height = scaled_bbox[3] - scaled_bbox[1]

        # Offset to center the scaled text at the same position
        x_offset = (normal_width - scaled_width) // 2
        y_offset = (normal_height - scaled_height) // 2

        self._draw_text_with_stroke(
            temp_draw,
            text,
            x + x_offset,
            y + y_offset,
            self._highlight_font,
            text_color,
            stroke_color,
            self.style.stroke_width + 1,
        )

        # Composite onto main image
        image.alpha_composite(temp)

    def _render_glow_word(self, image: Image.Image, text: str, x: int, y: int):
        """Render single word with glow effect."""
        temp = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (0, 0, 0, 0))
        temp_draw = ImageDraw.Draw(temp)

        text_color = self._hex_to_rgba(self.style.highlight_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        self._draw_text_with_stroke(
            temp_draw, text, x, y, self._font, text_color, stroke_color, self.style.stroke_width
        )

        # Apply glow to this word
        temp = self._apply_glow(temp, self.style.highlight_color, self.style.glow_radius + 5)

        image.alpha_composite(temp)

    def _render_box_highlight(
        self, draw: ImageDraw.ImageDraw, text: str, x: int, y: int
    ):
        """Render word with colored box behind it."""
        bbox = self._font.getbbox(text)
        padding = 8

        # Draw highlight box
        box_color = self._hex_to_rgba(self.style.highlight_color, 200)
        draw.rectangle(
            [
                x - padding,
                y - padding // 2,
                x + bbox[2] - bbox[0] + padding,
                y + bbox[3] - bbox[1] + padding // 2,
            ],
            fill=box_color,
        )

        # Draw text on top
        text_color = self._hex_to_rgba("#FFFFFF")
        stroke_color = self._hex_to_rgba("#000000")

        self._draw_text_with_stroke(
            draw, text, x, y, self._font, text_color, stroke_color, 2
        )

    def _render_bounce(
        self, draw: ImageDraw.ImageDraw, text: str, x: int, y: int, progress: float
    ):
        """Render word with bounce animation."""
        # Bounce curve: quick up, then settle
        bounce_height = 15
        if progress < 0.3:
            # Going up
            offset = -bounce_height * (progress / 0.3)
        elif progress < 0.5:
            # Coming down
            offset = -bounce_height * (1 - (progress - 0.3) / 0.2)
        else:
            # Small bounce
            offset = -5 * math.sin((progress - 0.5) * math.pi * 2)

        text_color = self._hex_to_rgba(self.style.highlight_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        self._draw_text_with_stroke(
            draw,
            text,
            x,
            int(y + offset),
            self._font,
            text_color,
            stroke_color,
            self.style.stroke_width,
        )

    def _render_underline(
        self, draw: ImageDraw.ImageDraw, text: str, x: int, y: int, progress: float
    ):
        """Render word with animated underline."""
        text_color = self._hex_to_rgba(self.style.highlight_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        self._draw_text_with_stroke(
            draw, text, x, y, self._font, text_color, stroke_color, self.style.stroke_width
        )

        # Draw animated underline
        bbox = self._font.getbbox(text)
        word_width = bbox[2] - bbox[0]
        underline_y = y + bbox[3] - bbox[1] + 5
        underline_width = int(word_width * progress)

        underline_color = self._hex_to_rgba(self.style.highlight_color)
        draw.rectangle(
            [x, underline_y, x + underline_width, underline_y + 4],
            fill=underline_color,
        )

    def _render_gradient(
        self, draw: ImageDraw.ImageDraw, text: str, x: int, y: int, progress: float
    ):
        """Render word with gradient sweep effect."""
        # For simplicity, we'll use the highlight color with varying intensity
        # based on progress
        text_color = self._hex_to_rgba(self.style.highlight_color)
        stroke_color = self._hex_to_rgba(self.style.stroke_color)

        self._draw_text_with_stroke(
            draw, text, x, y, self._font, text_color, stroke_color, self.style.stroke_width
        )
