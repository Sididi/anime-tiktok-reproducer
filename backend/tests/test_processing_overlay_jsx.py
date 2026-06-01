"""Focused tests for overlay decisions rendered into the Premiere JSX."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def _template() -> Template:
    return Template(
        label="Classic",
        foreground=ForegroundConfig(prfpset="fg.prfpset", zoom=0.76),
        background=BackgroundConfig(prfpset="bg.prfpset"),
        subtitles=SubtitlesConfig(mogrt="s.mogrt", raw_mogrt="r.mogrt"),
        white_border=WhiteBorderConfig(enabled=True, mogrt="border.mogrt"),
        overlay=OverlayConfig(
            enabled=True,
            title=OverlaySideConfig(style="classic", prfpset=None),
            category=OverlaySideConfig(style="classic", prfpset=None),
        ),
    )


def _render(*, title: bool, category: bool) -> str:
    return ProcessingService._render_jsx_from_template(
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
        overlay_title_enabled=title,
        overlay_category_enabled=category,
    )


def test_jsx_disables_overlay_when_title_and_category_are_empty():
    jsx = _render(title=False, category=False)
    assert "var OVERLAY_ENABLED = false;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = false;" in jsx
    assert "var TITLE_OVERLAY_ENABLED = false;" in jsx


def test_jsx_enables_only_title_overlay_for_title_only():
    jsx = _render(title=True, category=False)
    assert "var OVERLAY_ENABLED = true;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = false;" in jsx
    assert "var TITLE_OVERLAY_ENABLED = true;" in jsx
    assert "if (TITLE_OVERLAY_ENABLED)" in jsx
    assert "overlayFadeTrackIndexes.push(5);" in jsx


def test_jsx_enables_only_category_overlay_for_category_only():
    jsx = _render(title=False, category=True)
    assert "var OVERLAY_ENABLED = true;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = true;" in jsx
    assert "var TITLE_OVERLAY_ENABLED = false;" in jsx
    assert "if (CATEGORY_OVERLAY_ENABLED)" in jsx
    assert "overlayFadeTrackIndexes.push(4);" in jsx


def test_jsx_enables_both_overlays_with_forced_end_and_fades_each_overlay():
    jsx = _render(title=True, category=True)
    assert "var OVERLAY_ENABLED = true;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = true;" in jsx
    assert "var TITLE_OVERLAY_ENABLED = true;" in jsx
    assert "var OVERLAY_END_SEC = 2.5;" in jsx
    assert "var OVERLAY_FADE_DURATION_SEC = 0.5;" in jsx
    assert "var overlayFadeTrackIndexes = [];" in jsx
    assert "overlayFadeTrackIndexes.push(4);" in jsx
    assert "overlayFadeTrackIndexes.push(5);" in jsx
    assert "overlayFadeTrackIndexes[overlayFadeIdx]" in jsx
    assert "} else if (!overlayFadeItem) {" not in jsx


def test_jsx_tries_native_fondu_additif_then_opacity_fallback():
    jsx = _render(title=True, category=True)
    assert "applyOverlayFadeOut(" in jsx
    assert "resolveVideoTransitionByName(\"Fondu additif\")" in jsx
    assert "qe.project.getVideoTransitionList()" in jsx
    assert 'qeItem.addTransition(transition, false, durationString, "0", 1);' in jsx
    assert "qeTrack.addTransition(" in jsx
    assert "applyOverlayOpacityFadeOut(trackIndex, durationSec)" in jsx
    assert "opProp.setValueAtKey(fadeStartTime, 100);" in jsx
    assert "opProp.setValueAtKey(clipEndTime, 0);" in jsx
    assert "effect component did not appear" not in jsx
