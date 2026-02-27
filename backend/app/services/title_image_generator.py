"""Generate transparent title overlay images for TikTok videos."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..config import settings

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
TITLE_GAP_ABOVE_CENTER = 40  # px between panel bottom and center frame top
TITLE_MARGIN_SHRINK = 0.8  # panel-to-edge margin is 80% of natural (20% less gap)
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
        # Expand panel: reduce margin to screen edge by 20%
        tight_pad_h = 29
        old_panel_w = text_w + tight_pad_h * 2
        old_margin = (WIDTH - old_panel_w) / 2
        new_margin = old_margin * TITLE_MARGIN_SHRINK
        panel_w = int(WIDTH - 2 * new_margin)

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
    def _render_category(cls, category: str, output_path: Path) -> None:
        """Render uppercase category text with black outline to transparent PNG."""
        font = cls._load_font(CAT_FONT_SIZE)
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        text = category.upper()
        cat_bbox = font.getbbox(text)
        cat_w = cat_bbox[2] - cat_bbox[0]
        cat_x = (WIDTH - cat_w) // 2
        cat_y = CENTER_FRAME_BOT + CAT_GAP_BELOW_CENTER

        # Draw black outline (circular stroke)
        ow = CAT_OUTLINE_WIDTH
        for dx in range(-ow, ow + 1):
            for dy in range(-ow, ow + 1):
                if dx * dx + dy * dy <= ow * ow:
                    draw.text(
                        (cat_x + dx, cat_y + dy),
                        text,
                        font=font,
                        fill=CAT_OUTLINE_COLOR,
                    )

        # Draw white text on top
        draw.text((cat_x, cat_y), text, fill=CAT_TEXT_COLOR, font=font)

        img.save(output_path, "PNG")
