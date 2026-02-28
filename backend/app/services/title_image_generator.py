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
TITLE_FONT_SIZE = 54
TITLE_LINE_HEIGHT = 64
TITLE_PAD_V = 24
TITLE_TEXT_COLOR = (0, 0, 0)  # black
TITLE_PANEL_COLOR = (255, 255, 255, 245)  # white, nearly opaque
TITLE_PANEL_RADIUS = 8
TITLE_PAD_H = TITLE_PAD_V  # same as vertical for coherent box
TITLE_GAP_ABOVE_CENTER = 40  # px between panel bottom and center frame top
TITLE_MAX_LINES = 2

# --- Category style ---
CAT_FONT_SIZE = 38
CAT_TEXT_COLOR = (255, 255, 255)  # white
CAT_OUTLINE_COLOR = (0, 0, 0)  # black
CAT_OUTLINE_WIDTH = 5
CAT_GAP_BELOW_CENTER = 40  # px between center frame bottom and category text top

FONT_PATH = Path(__file__).resolve().parents[3] / "assets" / "fonts" / "Inter-Black.ttf"


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
    def _load_font(cls, size: int) -> ImageFont.FreeTypeFont:
        if FONT_PATH.exists():
            return ImageFont.truetype(str(FONT_PATH), size)
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
    def _wrap_text(
        cls, text: str, font: ImageFont.FreeTypeFont, max_width: int
    ) -> list[str]:
        """Word-wrap text to fit max_width, max 2 lines. Balanced split."""
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width:
            return [text]

        words = text.split()
        if len(words) < 2:
            # Single long word — force it (will overflow but can't split)
            return [text]

        best_split = len(words) // 2
        best_diff = float("inf")

        for i in range(1, len(words)):
            line1 = " ".join(words[:i])
            line2 = " ".join(words[i:])
            w1 = font.getbbox(line1)[2] - font.getbbox(line1)[0]
            w2 = font.getbbox(line2)[2] - font.getbbox(line2)[0]
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
        font = cls._load_font(TITLE_FONT_SIZE)
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        max_text_width = WIDTH - 160  # margin for text wrapping calculation
        lines = cls._wrap_text(title, font, max_text_width)

        # Calculate panel dimensions
        total_text_h = TITLE_LINE_HEIGHT * len(lines)
        panel_h = total_text_h + TITLE_PAD_V * 2

        text_w = max(
            font.getbbox(line)[2] - font.getbbox(line)[0] for line in lines
        )
        panel_w = text_w + TITLE_PAD_H * 2

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
            bbox = font.getbbox(line)
            tw = bbox[2] - bbox[0]
            tx = (WIDTH - tw) // 2
            ty = panel_y + TITLE_PAD_V + i * TITLE_LINE_HEIGHT
            draw.text((tx, ty), line, fill=TITLE_TEXT_COLOR, font=font)

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
        font = cls._load_font(CAT_FONT_SIZE)
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
            bullet_radius = 6
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
