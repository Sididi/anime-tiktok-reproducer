"""
TikTok-style subtitle renderer with:
- One line at a time
- Only current spoken word in red, others white
- Scale pop IN animation
- Character count segmentation
- ALL CAPS, thick black outline + drop shadow
"""

import math
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from ..models.subtitle import SubtitleWord


@dataclass
class Phrase:
    """A group of words to display together."""
    words: list[SubtitleWord]
    start_time: float
    end_time: float


class TikTokSubtitleRenderer:
    """Renders TikTok-style subtitles with karaoke highlight."""

    WIDTH = 1080
    HEIGHT = 1920
    MAX_CHARS_PER_LINE = 20  # Character count limit

    # Style matching Premiere Pro settings
    FONT_SIZE = 75
    STROKE_WIDTH = 15  # Thick outline
    TEXT_COLOR = "#FFFFFF"
    HIGHLIGHT_COLOR = "#FF0000"  # Red for current word only
    STROKE_COLOR = "#000000"

    # Drop shadow settings
    SHADOW_COLOR = "#000000"
    SHADOW_OPACITY = 0.75  # 75%
    SHADOW_DISTANCE = 5
    SHADOW_SIZE = 10  # Spread
    SHADOW_BLUR = 20

    # Animation settings
    ANIMATION_DURATION = 0.15  # 150ms for scale pop
    SCALE_START = 0.85  # Start at 85% size
    SCALE_OVERSHOOT = 1.05  # Overshoot to 105%

    def __init__(self):
        self._font = self._load_font(self.FONT_SIZE)
        self._phrases: list[Phrase] = []

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont:
        """Load Impact font."""
        font_paths = [
            # Impact font paths
            "/usr/share/fonts/TTF/Impact.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
            "/usr/share/fonts/microsoft/Impact.ttf",
            # Fallbacks if Impact not available
            "/usr/share/fonts/noto/NotoSans-ExtraBold.ttf",
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/gnu-free/FreeSansBold.otf",
        ]

        for path in font_paths:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue

        return ImageFont.load_default()

    def set_words(self, words: list[SubtitleWord]):
        """
        Segment words into phrases based on character count.
        Must be called before rendering.
        """
        self._phrases = []
        current_phrase_words: list[SubtitleWord] = []
        current_char_count = 0

        for word in words:
            word_len = len(word.text)
            # +1 for space if not first word in phrase
            space_len = 1 if current_phrase_words else 0
            total_len = current_char_count + space_len + word_len

            if total_len > self.MAX_CHARS_PER_LINE and current_phrase_words:
                # Save current phrase and start new one
                self._phrases.append(Phrase(
                    words=current_phrase_words,
                    start_time=current_phrase_words[0].start,
                    end_time=current_phrase_words[-1].end,
                ))
                current_phrase_words = [word]
                current_char_count = word_len
            else:
                current_phrase_words.append(word)
                current_char_count = total_len

        # Don't forget last phrase
        if current_phrase_words:
            self._phrases.append(Phrase(
                words=current_phrase_words,
                start_time=current_phrase_words[0].start,
                end_time=current_phrase_words[-1].end,
            ))

    def _hex_to_rgba(self, hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
        """Convert hex color to RGBA tuple."""
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return (r, g, b, alpha)

    def _get_current_phrase(self, current_time: float) -> Phrase | None:
        """Find the phrase that should be displayed at current time."""
        for phrase in self._phrases:
            # Show phrase from its start until next phrase starts (or end + small buffer)
            if phrase.start_time <= current_time < phrase.end_time + 0.15:
                return phrase

        return None

    def _get_animation_scale(self, phrase: Phrase, current_time: float) -> float:
        """
        Calculate scale factor for pop-in animation.
        Returns 1.0 when animation is complete.
        """
        time_since_start = current_time - phrase.start_time

        if time_since_start < 0:
            return 0.0  # Not visible yet

        if time_since_start >= self.ANIMATION_DURATION:
            return 1.0  # Animation complete

        # Easing: ease-out with slight overshoot
        progress = time_since_start / self.ANIMATION_DURATION

        # Ease out cubic with overshoot
        if progress < 0.6:
            # Scale up to overshoot
            t = progress / 0.6
            scale = self.SCALE_START + (self.SCALE_OVERSHOOT - self.SCALE_START) * (1 - (1 - t) ** 3)
        else:
            # Settle back to 1.0
            t = (progress - 0.6) / 0.4
            scale = self.SCALE_OVERSHOOT + (1.0 - self.SCALE_OVERSHOOT) * t

        return scale

    def _apply_drop_shadow(self, image: Image.Image) -> Image.Image:
        """Apply drop shadow effect to the image."""
        # Create shadow layer from alpha channel
        alpha = image.split()[3]

        # Create shadow image
        shadow_alpha = int(255 * self.SHADOW_OPACITY)
        r, g, b, _ = self._hex_to_rgba(self.SHADOW_COLOR)

        # Expand the alpha for shadow spread
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))

        # Create the shadow with spread by dilating the alpha
        if self.SHADOW_SIZE > 0:
            # Simple dilation by max filter
            from PIL import ImageFilter
            spread_alpha = alpha.filter(ImageFilter.MaxFilter(self.SHADOW_SIZE * 2 + 1))
        else:
            spread_alpha = alpha

        # Apply blur
        if self.SHADOW_BLUR > 0:
            spread_alpha = spread_alpha.filter(ImageFilter.GaussianBlur(self.SHADOW_BLUR))

        # Adjust opacity
        spread_alpha = spread_alpha.point(lambda x: int(x * self.SHADOW_OPACITY))

        # Create colored shadow
        shadow_colored = Image.new("RGBA", image.size, (r, g, b, 0))
        shadow_colored.putalpha(spread_alpha)

        # Offset shadow
        offset_shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        offset_shadow.paste(shadow_colored, (self.SHADOW_DISTANCE, self.SHADOW_DISTANCE))

        # Composite: shadow behind original
        result = Image.new("RGBA", image.size, (0, 0, 0, 0))
        result = Image.alpha_composite(result, offset_shadow)
        result = Image.alpha_composite(result, image)

        return result

    def _draw_text_with_stroke(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        x: int,
        y: int,
        font: ImageFont.FreeTypeFont,
        text_color: tuple[int, int, int, int],
        stroke_width: int,
    ):
        """Draw text with thick black stroke outline."""
        stroke_color = self._hex_to_rgba(self.STROKE_COLOR)

        # Draw stroke by drawing text multiple times offset
        if stroke_width > 0:
            # For thick strokes, we need more samples
            for dx in range(-stroke_width, stroke_width + 1):
                for dy in range(-stroke_width, stroke_width + 1):
                    dist_sq = dx * dx + dy * dy
                    if dist_sq <= stroke_width * stroke_width:
                        draw.text((x + dx, y + dy), text, font=font, fill=stroke_color)

        # Draw main text
        draw.text((x, y), text, font=font, fill=text_color)

    def render_frame(self, current_time: float) -> Image.Image:
        """
        Render a single frame at the given time.

        Args:
            current_time: Current playback time in seconds

        Returns:
            RGBA image with transparent background
        """
        # Create transparent image
        image = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (0, 0, 0, 0))

        # Get current phrase
        phrase = self._get_current_phrase(current_time)
        if not phrase:
            return image

        # Get animation scale
        scale = self._get_animation_scale(phrase, current_time)
        if scale <= 0:
            return image

        # Build the line text (ALL CAPS) for measurement
        line_text = " ".join(w.text.upper() for w in phrase.words)

        # Calculate font size with scale
        scaled_size = int(self.FONT_SIZE * scale)
        if scaled_size < 10:
            return image

        scaled_font = self._load_font(scaled_size)
        scaled_stroke = max(1, int(self.STROKE_WIDTH * scale))

        # Get line dimensions
        bbox = scaled_font.getbbox(line_text)
        line_width = bbox[2] - bbox[0]
        line_height = bbox[3] - bbox[1]

        # Center position (50% vertical)
        center_x = self.WIDTH // 2
        center_y = self.HEIGHT // 2

        draw = ImageDraw.Draw(image)

        # Calculate starting X to center the line
        start_x = center_x - line_width // 2
        y = center_y - line_height // 2

        # Render each word
        x = start_x
        for word in phrase.words:
            word_text = word.text.upper()

            # Only highlight if word is CURRENTLY being spoken
            # (not before start, not after end)
            is_current = word.start <= current_time < word.end

            # Choose color: red only for current word, white otherwise
            if is_current:
                text_color = self._hex_to_rgba(self.HIGHLIGHT_COLOR)
            else:
                text_color = self._hex_to_rgba(self.TEXT_COLOR)

            # Draw the word
            self._draw_text_with_stroke(
                draw, word_text, x, y, scaled_font, text_color, scaled_stroke
            )

            # Move to next word position
            word_bbox = scaled_font.getbbox(word_text + " ")
            x += word_bbox[2] - word_bbox[0]

        # Apply drop shadow
        image = self._apply_drop_shadow(image)

        return image

    def render_frame_with_words(
        self, words: list[SubtitleWord], current_time: float
    ) -> Image.Image:
        """
        Convenience method that sets words and renders frame.
        Use set_words() + render_frame() for better performance when rendering many frames.
        """
        if not self._phrases:
            self.set_words(words)
        return self.render_frame(current_time)
