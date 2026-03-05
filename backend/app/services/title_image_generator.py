"""Generate transparent title overlay images for TikTok videos."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- Layout constants (1080x1920 TikTok vertical) ---
WIDTH = 1080
HEIGHT = 1920
CENTER_FRAME_TOP = 580
CENTER_FRAME_BOT = 1340

# --- Title style ---
TITLE_FONT_SIZE = 59
TITLE_LINE_HEIGHT = 70
TITLE_PAD_V = 24
TITLE_TEXT_COLOR = (0, 0, 0)  # black
TITLE_PANEL_COLOR = (255, 255, 255, 245)  # white, nearly opaque
TITLE_PANEL_RADIUS = 16
TITLE_PAD_H = 38
TITLE_SCREEN_MARGIN = 60  # min px from screen edge to panel edge
TITLE_GAP_ABOVE_CENTER = 90  # px between panel bottom and center frame top
TITLE_LETTER_SPACING = -1.5  # px offset between characters (negative = tighter)
TITLE_MAX_LINES = 2

# --- Category style ---
CAT_FONT_SIZE = 50
CAT_TEXT_COLOR = (255, 255, 255)  # white
CAT_OUTLINE_COLOR = (0, 0, 0)  # black
CAT_OUTLINE_WIDTH = 4
CAT_GAP_BELOW_CENTER = 90  # px between center frame bottom and category text top (matches title gap)

FONT_DIR = Path(__file__).resolve().parents[3] / "assets" / "fonts"
TITLE_FONT_PATH = FONT_DIR / "Inter-BlackItalic.ttf"
CAT_FONT_PATH = FONT_DIR / "Inter-Black.ttf"


class TitleImageGeneratorService:
    """Generates transparent PNG overlays for title and category text."""

    @classmethod
    def generate(
        cls,
        title: str,
        category: str,
        output_dir: Path,
    ) -> dict[str, Path]:
        """Generate title and category overlay PNGs.

        Returns dict with keys 'title' and 'category' mapping to output paths.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        title_path = output_dir / "title_overlay.png"
        category_path = output_dir / "category_overlay.png"

        cls._render_title(title, title_path)
        cls._render_category(category, category_path)

        return {"title": title_path, "category": category_path}

    @classmethod
    def _load_font(cls, size: int, path: Path | None = None) -> ImageFont.FreeTypeFont:
        if path and path.exists():
            return ImageFont.truetype(str(path), size)
        # Fallback: try system fonts
        for fallback in [
            "/usr/share/fonts/noto/NotoSans-ExtraBold.ttf",
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/gnu-free/FreeSansBold.otf",
        ]:
            if Path(fallback).exists():
                return ImageFont.truetype(fallback, size)
        return ImageFont.load_default()

    @classmethod
    def _merge_french_punctuation(cls, words: list[str]) -> list[str]:
        """Merge standalone French punctuation (? ! : ;) with preceding word.

        In French typography, these punctuation marks have a space before them
        but must not be separated from the previous word during line wrapping.
        """
        merged: list[str] = []
        for word in words:
            if word in ("?", "!", ":", ";") and merged:
                merged[-1] = merged[-1] + " " + word
            else:
                merged.append(word)
        return merged

    @classmethod
    def _measure_text(
        cls, text: str, font: ImageFont.FreeTypeFont, letter_spacing: float = 0
    ) -> float:
        """Measure text width accounting for optional letter spacing."""
        if not text:
            return 0
        if letter_spacing == 0:
            return font.getlength(text)
        # Sum advance widths with custom spacing
        total = 0.0
        for i, ch in enumerate(text):
            total += font.getlength(ch)
            if i < len(text) - 1:
                total += letter_spacing
        return total

    @classmethod
    def _draw_spaced_text(
        cls,
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple,
        letter_spacing: float,
    ) -> None:
        """Draw text character by character with custom letter spacing."""
        x, y = xy
        for i, ch in enumerate(text):
            draw.text((x, y), ch, fill=fill, font=font)
            if i < len(text) - 1:
                x += font.getlength(ch) + letter_spacing

    @classmethod
    def _wrap_text(
        cls, text: str, font: ImageFont.FreeTypeFont, max_width: int,
        letter_spacing: float = 0,
    ) -> list[str]:
        """Word-wrap text to fit max_width, max 2 lines. Balanced split."""
        if cls._measure_text(text, font, letter_spacing) <= max_width:
            return [text]

        words = cls._merge_french_punctuation(text.split())
        if len(words) < 2:
            return [text]

        best_split = len(words) // 2
        best_diff = float("inf")

        for i in range(1, len(words)):
            line1 = " ".join(words[:i])
            line2 = " ".join(words[i:])
            w1 = cls._measure_text(line1, font, letter_spacing)
            w2 = cls._measure_text(line2, font, letter_spacing)
            if w1 <= max_width and w2 <= max_width:
                diff = abs(w1 - w2)
                if diff < best_diff:
                    best_diff = diff
                    best_split = i

        line1 = " ".join(words[:best_split])
        line2 = " ".join(words[best_split:])
        return [line1, line2]

    @classmethod
    def _render_title(cls, title: str, output_path: Path) -> None:
        """Render title text on white panel to transparent PNG."""
        font = cls._load_font(TITLE_FONT_SIZE, TITLE_FONT_PATH)
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        ls = TITLE_LETTER_SPACING
        max_text_width = WIDTH - 2 * (TITLE_SCREEN_MARGIN + TITLE_PAD_H)
        lines = cls._wrap_text(title.upper(), font, max_text_width, ls)

        # Calculate panel dimensions
        total_text_h = TITLE_LINE_HEIGHT * len(lines)
        panel_h = total_text_h + TITLE_PAD_V * 2

        text_w = max(cls._measure_text(line, font, ls) for line in lines)
        panel_w = int(text_w) + TITLE_PAD_H * 2

        panel_x = (WIDTH - panel_w) // 2
        panel_y = CENTER_FRAME_TOP - TITLE_GAP_ABOVE_CENTER - panel_h

        # Draw rounded rectangle panel
        draw.rounded_rectangle(
            [panel_x, panel_y, panel_x + panel_w, panel_y + panel_h],
            radius=TITLE_PANEL_RADIUS,
            fill=TITLE_PANEL_COLOR,
        )

        # Draw text lines centered in panel
        for i, line in enumerate(lines):
            tw = cls._measure_text(line, font, ls)
            tx = (WIDTH - tw) / 2
            ty = panel_y + TITLE_PAD_V + i * TITLE_LINE_HEIGHT
            cls._draw_spaced_text(draw, (tx, ty), line, font, TITLE_TEXT_COLOR, ls)

        img.save(output_path, "PNG")

    @classmethod
    def _draw_outlined_text(
        cls,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple,
        outline_fill: tuple,
        outline_width: int,
    ) -> None:
        """Draw text with circular outline stroke."""
        x, y = xy
        ow = outline_width
        for dx in range(-ow, ow + 1):
            for dy in range(-ow, ow + 1):
                if dx * dx + dy * dy <= ow * ow:
                    draw.text((x + dx, y + dy), text, font=font, fill=outline_fill)
        draw.text((x, y), text, fill=fill, font=font)

    @classmethod
    def _render_category(cls, category: str, output_path: Path) -> None:
        """Render uppercase category text with black outline to transparent PNG.

        The bullet separator is drawn as a filled circle vertically centered
        with the uppercase text, since the font's '•' glyph sits too low.
        """
        font = cls._load_font(CAT_FONT_SIZE, CAT_FONT_PATH)
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        text = category.upper()
        cat_y = CENTER_FRAME_BOT + CAT_GAP_BELOW_CENTER

        # Split at bullet separator to draw it as a centered circle
        separator = " \u2022 "  # " • "
        if separator in text:
            parts = text.split(separator)
            left, right = parts[0].strip(), parts[1].strip()
            space_w = font.getbbox(" ")[2] - font.getbbox(" ")[0]
            bullet_radius = 8
            bullet_gap = space_w  # space on each side of the circle

            left_w = font.getbbox(left)[2] - font.getbbox(left)[0]
            right_w = font.getbbox(right)[2] - font.getbbox(right)[0]
            total_w = left_w + bullet_gap + bullet_radius * 2 + bullet_gap + right_w

            start_x = (WIDTH - total_w) // 2

            # Draw left text
            cls._draw_outlined_text(
                draw, (start_x, cat_y), left, font,
                CAT_TEXT_COLOR, CAT_OUTLINE_COLOR, CAT_OUTLINE_WIDTH,
            )

            # Draw bullet circle at vertical center of uppercase text
            caps_bbox = font.getbbox("A")
            caps_center_y = cat_y + (caps_bbox[1] + caps_bbox[3]) // 2
            bullet_cx = start_x + left_w + bullet_gap + bullet_radius
            bullet_cy = caps_center_y

            # Outline for bullet
            ow = CAT_OUTLINE_WIDTH
            draw.ellipse(
                [
                    bullet_cx - bullet_radius - ow,
                    bullet_cy - bullet_radius - ow,
                    bullet_cx + bullet_radius + ow,
                    bullet_cy + bullet_radius + ow,
                ],
                fill=CAT_OUTLINE_COLOR,
            )
            # White bullet
            draw.ellipse(
                [
                    bullet_cx - bullet_radius,
                    bullet_cy - bullet_radius,
                    bullet_cx + bullet_radius,
                    bullet_cy + bullet_radius,
                ],
                fill=CAT_TEXT_COLOR,
            )

            # Draw right text
            right_x = start_x + left_w + bullet_gap + bullet_radius * 2 + bullet_gap
            cls._draw_outlined_text(
                draw, (right_x, cat_y), right, font,
                CAT_TEXT_COLOR, CAT_OUTLINE_COLOR, CAT_OUTLINE_WIDTH,
            )
        else:
            # No separator — render as single text
            cat_bbox = font.getbbox(text)
            cat_w = cat_bbox[2] - cat_bbox[0]
            cat_x = (WIDTH - cat_w) // 2
            cls._draw_outlined_text(
                draw, (cat_x, cat_y), text, font,
                CAT_TEXT_COLOR, CAT_OUTLINE_COLOR, CAT_OUTLINE_WIDTH,
            )

        img.save(output_path, "PNG")
