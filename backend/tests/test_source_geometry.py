"""Tests for resolution-adaptive SOURCE_GEOMETRY math and JSX rendering."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.library_types import LibraryType
from app.models.cleanup import CleanFeedRect
from app.models.template import (
    BackgroundConfig,
    ForegroundConfig,
    OverlayConfig,
    OverlaySideConfig,
    SubtitlesConfig,
    Template,
    WhiteBorderConfig,
)
from app.services.processing import ProcessingService


def _entry(
    width,
    height,
    sar=None,
    *,
    zoom=0.76,
    is_pure=False,
    rect=None,
    flip_fg=True,
    flip_bg=True,
    max_band=None,
):
    return ProcessingService._compute_source_geometry_entry(
        width,
        height,
        sar,
        zoom=zoom,
        is_pure=is_pure,
        clean_feed_rect=rect,
        flip_fg=flip_fg,
        flip_bg=flip_bg,
        pure_max_band_px=max_band,
    )


# --- Anime mode math -------------------------------------------------------


def test_1080p_reproduces_historical_scales_exactly():
    entry = _entry(1920, 1080)
    assert entry == {"mode": "anime", "fg_scale": 76.0, "bg_scale": 183.0}


def test_zoomed_template_1080p_is_exact():
    entry = _entry(1920, 1080, zoom=0.5)
    assert entry["fg_scale"] == 50.0
    assert entry["bg_scale"] == 183.0


def test_720p_scales_by_height_ratio():
    entry = _entry(1280, 720)
    assert entry["fg_scale"] == 114.0  # 76 * 1080/720
    assert entry["bg_scale"] == 274.5  # 1.029375 * 1920/720 * 100


def test_4_3_source_height_normalized_and_bg_fills():
    entry = _entry(1440, 1080)
    assert entry["fg_scale"] == 76.0  # same height as 1080p
    assert entry["bg_scale"] == 183.0  # height still dominates the fill


def test_anamorphic_sar_uses_display_width():
    # 1440x1080 with 4:3 SAR displays as 1920x1080.
    entry = _entry(1440, 1080, "4:3")
    assert entry["fg_scale"] == 76.0
    assert entry["bg_scale"] == 183.0


def test_invalid_sar_treated_as_square():
    assert _entry(1920, 1080, "0:1") == _entry(1920, 1080, None)
    assert _entry(1920, 1080, "N/A") == _entry(1920, 1080, None)


def test_degenerate_dimensions_return_none():
    assert _entry(None, 1080) is None
    assert _entry(1920, None) is None
    assert _entry(0, 1080) is None
    assert _entry(1920, -1) is None


# --- Pure mode math --------------------------------------------------------


def test_pure_requires_clean_feed_rect():
    assert _entry(720, 1280, is_pure=True, rect=None) is None


def test_pure_wide_band_rect_keeps_template_band():
    # Full-width band at y 0.2..0.5 of a 720x1280 source (aspect 1.875 >
    # 1080/820.8): height-driven, horizontal overflow like anime.
    rect = CleanFeedRect(x=0.0, y=0.2, w=1.0, h=0.3)
    entry = _entry(720, 1280, is_pure=True, rect=rect, max_band=1250)
    # rect_h = 384 px; band 820.8 -> factor 2.1375.
    assert entry["fg_scale"] == 213.75
    assert entry["band_height_px"] == 820.8
    # bg fill: 1.15 * max(1080/720, 1920/384) = 5.75.
    assert entry["bg_scale"] == 575.0
    assert entry["crop_top_pct"] == 20.0
    assert entry["crop_bottom_pct"] == 50.0
    assert entry["crop_left_pct"] == 0.0
    assert entry["crop_right_pct"] == 0.0
    # Rect center is 0.15 above frame center: offset_y = -192 px.
    # fg: dy = -192 * 2.1375 = -410.4 -> py = (960 + 410.4) / 1920.
    assert entry["fg_pos"] == [0.5, 0.71375]
    # bg: dy = -192 * 5.75 = -1104 -> py = (960 + 1104) / 1920.
    assert entry["bg_pos"] == [0.5, 1.075]


def test_pure_narrow_rect_fills_width_and_grows_band():
    # 3a87b0cd44ce-like: full width, rect 720x960 (aspect 0.75) in 720x1280.
    # Width-fill scale 1.5 -> native band 1440 > cap 1250 -> extra vertical
    # crop of (960 - 1250/1.5)/1280/2 = 4.9479% per side.
    rect = CleanFeedRect(x=0.0, y=0.125, w=1.0, h=0.75)
    entry = _entry(720, 1280, is_pure=True, rect=rect, max_band=1250)
    assert entry["fg_scale"] == 150.0
    assert entry["band_height_px"] == 1250.0
    assert entry["crop_top_pct"] == 17.4479
    assert entry["crop_bottom_pct"] == 17.4479
    assert entry["crop_left_pct"] == 0.0
    assert entry["crop_right_pct"] == 0.0
    # Rect center at frame center: no position compensation.
    assert entry["fg_pos"] == [0.5, 0.5]


def test_pure_full_frame_rect_uncapped_fills_frame():
    # Without an explicit cap the frame height is the limit: a 9:16 rect
    # renders edge to edge with no extra crop.
    rect = CleanFeedRect(x=0.0, y=0.0, w=1.0, h=1.0)
    entry = _entry(720, 1280, is_pure=True, rect=rect)
    assert entry["fg_scale"] == 150.0
    assert entry["band_height_px"] == 1920.0
    assert entry["crop_top_pct"] == 0.0
    assert entry["crop_bottom_pct"] == 0.0
    assert entry["fg_pos"] == [0.5, 0.5]
    assert entry["bg_pos"] == [0.5, 0.5]
    # 1.15 * max(1080/720, 1920/1280) * 100.
    assert entry["bg_scale"] == 172.5


def test_pure_full_frame_rect_capped_crops_symmetrically():
    rect = CleanFeedRect(x=0.0, y=0.0, w=1.0, h=1.0)
    entry = _entry(720, 1280, is_pure=True, rect=rect, max_band=1250)
    assert entry["fg_scale"] == 150.0
    assert entry["band_height_px"] == 1250.0
    # rh_eff = 1250/1.5 = 833.333; extra = (1280-833.333)/1280/2 = 17.4479%.
    assert entry["crop_top_pct"] == 17.4479
    assert entry["crop_bottom_pct"] == 17.4479
    # Symmetric crop keeps the center: no position compensation.
    assert entry["fg_pos"] == [0.5, 0.5]


def test_pure_horizontal_offset_negated_by_flip():
    # Rect hugging the left edge (0.8 width): center offset -72 px at 720
    # width. Width-fill factor = 1080/576 = 1.875; |dx| = 72*1.875 = 135 px.
    rect = CleanFeedRect(x=0.0, y=0.0, w=0.8, h=1.0)
    flipped = _entry(720, 1280, is_pure=True, rect=rect, flip_fg=True)
    unflipped = _entry(720, 1280, is_pure=True, rect=rect, flip_fg=False)
    assert flipped["fg_pos"][0] == 0.375
    assert unflipped["fg_pos"][0] == 0.625
    # Same y either way (band capped at frame height, symmetric crop).
    assert flipped["fg_pos"][1] == unflipped["fg_pos"][1] == 0.5


# --- Renderer wiring -------------------------------------------------------


def _template(*, zoom: float = 0.76, border_enabled: bool = True) -> Template:
    return Template(
        label="Classic",
        foreground=ForegroundConfig(prfpset="fg.prfpset", zoom=zoom),
        background=BackgroundConfig(prfpset="bg.prfpset"),
        subtitles=SubtitlesConfig(mogrt="s.mogrt", raw_mogrt="r.mogrt"),
        white_border=WhiteBorderConfig(enabled=border_enabled),
        overlay=OverlayConfig(
            enabled=True,
            title=OverlaySideConfig(enabled=True, style="classic", prfpset=None),
            category=OverlaySideConfig(enabled=True, style="classic", prfpset=None),
        ),
    )


def _render(**kwargs) -> str:
    defaults = dict(
        project_id="test_project",
        scenes=[],
        source_audio_policies={},
        source_fps_num=24000,
        source_fps_den=1001,
        subtitle_timing_relative_path="subtitles/subtitle_timings.srt",
        raw_scene_subtitle_timing_relative_path="raw_scene_subtitles/text_subtitles.srt",
        raw_scene_subtitle_mogrt_relative_dir="raw_scene_subtitles/text_mogrts",
        music_filename="",
        music_gain_db=-23.0,
        template=_template(),
        overlay_title_enabled=True,
        overlay_category_enabled=True,
    )
    defaults.update(kwargs)
    return ProcessingService._render_jsx_from_template(**defaults)


def test_renderer_injects_geometry_blob_and_defaults():
    jsx = _render(
        source_geometry={
            "ep01": {"mode": "anime", "fg_scale": 114.0, "bg_scale": 274.5}
        }
    )
    assert '"fg_scale": 114.0' in jsx
    assert '"bg_scale": 274.5' in jsx
    assert 'var BORDER_IMAGE_FILENAME = "white_border_frame.png";' in jsx
    # The historical hardcoded call sites are gone; only the lookup remains.
    assert "setScaleOnItem(v3Item, 76)" not in jsx
    assert "setScaleOnItem(v1Item, 183)" not in jsx
    assert jsx.count("applySceneGeometry(") >= 3  # definition + 2 call sites
    assert "var DEFAULT_FG_SCALE = 76;" in jsx
    assert "var DEFAULT_BG_SCALE = 183;" in jsx


def test_renderer_injects_pure_entry_with_crop_and_positions():
    jsx = _render(
        source_geometry={
            "tiktok_clean": {
                "mode": "pure",
                "fg_scale": 213.75,
                "bg_scale": 575.0,
                "crop_left_pct": 0.0,
                "crop_top_pct": 20.0,
                "crop_right_pct": 0.0,
                "crop_bottom_pct": 50.0,
                "fg_pos": [0.5, 0.71375],
                "bg_pos": [0.5, 1.075],
            }
        }
    )
    assert '"crop_top_pct": 20.0' in jsx
    assert '"crop_bottom_pct": 50.0' in jsx
    assert '"fg_pos"' in jsx and '"bg_pos"' in jsx
    # JSX-side pure machinery is present.
    assert "ensureCropOnSceneItem(" in jsx
    assert "setPositionOnItem(" in jsx


def test_renderer_zoomed_template_patches_default_fg_scale():
    jsx = _render(template=_template(zoom=0.5))
    assert "var DEFAULT_FG_SCALE = 50;" in jsx
    assert "var DEFAULT_FG_SCALE = 76;" not in jsx


def test_renderer_empty_geometry_renders_empty_map():
    jsx = _render()
    assert "var SOURCE_GEOMETRY =\n  {};" in jsx


# --- Border image generation ----------------------------------------------


class _StubProject:
    def __init__(self, library_type: LibraryType):
        self.library_type = library_type

    def resolved_template_key(self) -> str:
        return "classic"


def _patch_template(monkeypatch) -> None:
    """Pin the resolved template to the model defaults (8.4/6.0, zoom 0.76)
    so border tests don't depend on the live, owner-tuned config.yaml."""
    from app.services.template_service import TemplateService

    monkeypatch.setattr(TemplateService, "get", staticmethod(lambda key: _template()))


def _assert_classic_slab(target: Path) -> None:
    from PIL import Image

    with Image.open(target) as image:
        assert image.size == (1080, 1920)
        rgba = image.convert("RGBA")
        white = (255, 255, 255, 255)
        # Retired-mogrt geometry vs the 76% band (549.6..1370.4): white slab
        # 541.2..1376.4 (8.4px above / 6.0px below the band). V3 covers the
        # middle at runtime, leaving only the overhangs visible.
        assert rgba.getpixel((540, 545)) == white  # visible top overhang
        assert rgba.getpixel((540, 1374)) == white  # visible bottom overhang
        assert rgba.getpixel((540, 960)) == white  # slab middle (hidden by V3)
        assert rgba.getpixel((540, 540))[3] == 0  # above the slab
        assert rgba.getpixel((540, 1377))[3] == 0  # below the slab
        # Sub-pixel edges: rows 541/1376 carry fractional alpha (0.8 / 0.4).
        assert rgba.getpixel((540, 541))[3] == 204
        assert rgba.getpixel((540, 1376))[3] == 102
        # No frame ring: corners transparent.
        assert rgba.getpixel((5, 5))[3] == 0
        assert rgba.getpixel((1074, 1914))[3] == 0


def test_generate_border_slab_anime(tmp_path, monkeypatch):
    _patch_template(monkeypatch)
    ProcessingService._generate_border_images(
        tmp_path, project=_StubProject(LibraryType.ANIME)
    )
    _assert_classic_slab(tmp_path / "white_border_frame.png")


def test_generate_border_slab_pure_standard_band(tmp_path, monkeypatch):
    # Pure with a standard band uses the same slab as anime.
    _patch_template(monkeypatch)
    ProcessingService._generate_border_images(
        tmp_path, project=_StubProject(LibraryType.PURE)
    )
    _assert_classic_slab(tmp_path / "white_border_frame.png")
    assert not (tmp_path / "white_margin_solid.png").exists()


def test_generate_border_slab_follows_grown_band(tmp_path, monkeypatch):
    _patch_template(monkeypatch)
    from PIL import Image

    ProcessingService._generate_border_images(
        tmp_path, project=_StubProject(LibraryType.PURE), band_height_px=1250.0
    )
    with Image.open(tmp_path / "white_border_frame.png") as image:
        rgba = image.convert("RGBA")
        white = (255, 255, 255, 255)
        # Band 335..1585 -> slab 326.6..1591.0: core rows 327..1590,
        # fractional top row 326 (0.4 coverage), exact bottom edge.
        assert rgba.getpixel((540, 330)) == white
        assert rgba.getpixel((540, 1590)) == white
        assert rgba.getpixel((540, 960)) == white
        assert rgba.getpixel((540, 326))[3] == 102
        assert rgba.getpixel((540, 325))[3] == 0
        assert rgba.getpixel((540, 1591))[3] == 0


# --- Overlay band anchoring -------------------------------------------------


def test_overlay_shifts_follow_grown_band(tmp_path):
    from PIL import Image

    from app.services.title_image_generator import TitleImageGeneratorService

    default_dir = tmp_path / "default"
    grown_dir = tmp_path / "grown"
    for target, kwargs in (
        (default_dir, {}),
        # Band 1250 vs standard 820.8: each band edge moved by 214.6px ->
        # title up 215px, category down 215px (gap to the band preserved;
        # owner-calibrated on ded367648062, Y 960 -> 990).
        (grown_dir, {"band_shift_px": 215}),
    ):
        TitleImageGeneratorService.generate(
            title="TEST TITLE",
            category="TEST CATEGORY",
            output_dir=target,
            **kwargs,
        )

    def _bbox(path):
        with Image.open(path) as image:
            return image.convert("RGBA").getbbox()

    default_title = _bbox(default_dir / "title_overlay.png")
    grown_title = _bbox(grown_dir / "title_overlay.png")
    default_cat = _bbox(default_dir / "category_overlay.png")
    grown_cat = _bbox(grown_dir / "category_overlay.png")
    assert default_title[1] - grown_title[1] == 215  # title moved up
    assert grown_cat[1] - default_cat[1] == 215  # category moved down
    # Horizontal placement untouched.
    assert grown_title[0] == default_title[0]
    assert grown_cat[0] == default_cat[0]
