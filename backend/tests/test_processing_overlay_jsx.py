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


def _template(*, title_enabled: bool = True, category_enabled: bool = True) -> Template:
    return Template(
        label="Classic",
        foreground=ForegroundConfig(prfpset="fg.prfpset", zoom=0.76),
        background=BackgroundConfig(prfpset="bg.prfpset"),
        subtitles=SubtitlesConfig(mogrt="s.mogrt", raw_mogrt="r.mogrt"),
        white_border=WhiteBorderConfig(enabled=True, mogrt="border.mogrt"),
        overlay=OverlayConfig(
            enabled=True,
            title=OverlaySideConfig(
                enabled=title_enabled, style="classic", prfpset=None
            ),
            category=OverlaySideConfig(
                enabled=category_enabled, style="classic", prfpset=None
            ),
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


def test_jsx_template_side_disable_wins_over_non_empty_overlay():
    jsx = ProcessingService._render_jsx_from_template(
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
        template=_template(title_enabled=True, category_enabled=False),
        overlay_title_enabled=True,
        overlay_category_enabled=True,
    )
    assert "var TITLE_OVERLAY_ENABLED = true;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = false;" in jsx


def test_jsx_enables_only_category_overlay_for_category_only():
    jsx = _render(title=False, category=True)
    assert "var OVERLAY_ENABLED = true;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = true;" in jsx
    assert "var TITLE_OVERLAY_ENABLED = false;" in jsx
    assert "if (CATEGORY_OVERLAY_ENABLED)" in jsx
    assert "overlayFadeTrackIndexes.push(4);" in jsx


def test_jsx_enables_both_overlays_until_sequence_end_and_fades_each_overlay():
    jsx = _render(title=True, category=True)
    assert "var OVERLAY_ENABLED = true;" in jsx
    assert "var CATEGORY_OVERLAY_ENABLED = true;" in jsx
    assert "var TITLE_OVERLAY_ENABLED = true;" in jsx
    assert "var OVERLAY_END_SEC" not in jsx
    assert 'log("Adding overlays on V5/V6 until " + sequenceEndSec + "s...")' in jsx
    assert "CATEGORY_OVERLAY_FILENAME,\n          sequenceEndSec," in jsx
    assert "TITLE_OVERLAY_FILENAME,\n          sequenceEndSec," in jsx
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


def test_jsx_places_white_border_early_and_verifies_after_subtitles():
    jsx = _render(title=True, category=True)
    border_fn = jsx[
        jsx.index("function ensureWhiteBorderMogrt")
        : jsx.index("function getMotionComponent")
    ]

    border_call = "ensureWhiteBorderMogrt(sequence, v2, sequenceEndSec);"
    sequence_end = jsx.index("var sequenceEndSec = ttsEndSec;")
    early_border = jsx.index(border_call, sequence_end)
    subtitle_import = jsx.index("importUnifiedSubtitles(", early_border)
    final_border = jsx.index(border_call, subtitle_import)
    assert jsx.count(border_call) == 2
    assert sequence_end < early_border < subtitle_import < final_border

    assert border_fn.count("importedBorderCandidate = sequence.importMGT(") == 1
    assert 'BORDER_MOGRT_PATH,\n          "0",\n          1,\n          0,' in jsx
    assert "borderItem = waitForTrackItemAtStart(" in jsx
    assert "track.overwriteClip(importedBorderProjectItem, \"0\")" in jsx
    assert "pruneExtraBorderClips(track)" in border_fn
    assert "Border Mogrt verified on V2" in jsx

    assert "BORDER_MOGRT_MAX_ATTEMPTS" not in jsx
    assert "BORDER_MOGRT_RETRY_BASE_MS" not in jsx
    assert "BORDER_MOGRT_INSTALL_WAIT_MS" not in jsx
    assert "function installBorderMogrtFromFile(" not in jsx
    assert "findReusableWhiteBorderProjectItem" not in jsx
    assert "findInstalledBorderMogrtProjectItem" not in jsx
    assert "normalizeMogrtNameKey" not in jsx
    assert "app.project.importFiles" not in border_fn
    assert "[BORDER_MOGRT_PATH]" not in jsx


def test_jsx_border_failure_is_nonfatal_and_silent():
    jsx = _render(title=True, category=True)

    border_fn = jsx[
        jsx.index("function ensureWhiteBorderMogrt")
        : jsx.index("function getMotionComponent")
    ]
    assert "throw new Error" not in border_fn
    assert "recordImportWarning(" not in border_fn
    assert "function recordImportWarning(" not in jsx
    assert "__ATR_IMPORT_WARNINGS__" not in jsx
    assert "function pruneExtraBorderClips(" in jsx
    assert "Border Mogrt skipped: V2 track unavailable." in border_fn
    assert "Border Mogrt skipped: file missing at" in border_fn
    assert (
        "Border Mogrt was not placed on V2; continuing without it." in border_fn
    )
    assert "Border Mogrt end time could not be verified at" in border_fn
    assert "Required Border Mogrt could not be found on V2" not in jsx
    assert "V2 track unavailable; required Border Mogrt not inserted." not in jsx


def test_jsx_verifies_border_end_with_documented_time_object():
    jsx = _render(title=True, category=True)

    time_assignment = jsx.index("item.end = buildSequenceTimeFromSeconds(endSec);")
    numeric_fallback = jsx.index("item.end = endSec;", time_assignment)
    assert time_assignment < numeric_fallback
    assert "Math.abs(timeEndSec - endSec) <= 1 / SEQ_FPS" in jsx
