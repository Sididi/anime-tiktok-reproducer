"""Tests for Pure mode: library type, prompts, identity matcher, detector."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.library_types import (
    STATIC_OVERLAY_TITLES,
    LibraryType,
    coerce_library_type,
    resolve_static_overlay_title,
)
from app.models.match import MatchList, SceneMatch
from app.models.scene import Scene, SceneList
from app.services.prompt_resolver import (
    METADATA,
    OVERLAY,
    SCRIPT,
    PromptResolver,
)
from app.services.pure_matcher import PureMatcherService
from app.services.scene_detector import SceneDetectorService


# ---------------------------------------------------------------------------
# Library type
# ---------------------------------------------------------------------------

def test_pure_library_type_coercion():
    assert coerce_library_type("pure") is LibraryType.PURE
    assert coerce_library_type(" PURE ") is LibraryType.PURE


def test_static_overlay_titles_cover_every_library_type():
    # Regression guard: a missing entry silently falls back to the ANIME title.
    for member in LibraryType:
        assert member in STATIC_OVERLAY_TITLES, member
    assert "ANIME" not in resolve_static_overlay_title("pure").upper()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("group", "variant"),
    [
        (SCRIPT, "fr"),
        (SCRIPT, "multi"),
        (SCRIPT, "same_lang"),
        (METADATA, "fr"),
        (METADATA, "multi"),
        (OVERLAY, "fr"),
        (OVERLAY, "multi"),
    ],
)
def test_pure_prompts_resolve_without_oeuvre(group, variant):
    PromptResolver.clear_cache()
    content = PromptResolver.resolve(
        prompt_group=group,
        language_variant=variant,
        library_type=LibraryType.PURE,
    )
    assert content.strip(), f"pure prompt {group}_{variant} is empty"
    assert "[OEUVRE]" not in content, f"[OEUVRE] leaked into pure {group}_{variant}"


def test_pure_prompts_differ_from_default():
    PromptResolver.clear_cache()
    pure = PromptResolver.resolve(
        prompt_group=METADATA, language_variant="fr", library_type=LibraryType.PURE
    )
    default = PromptResolver.resolve(
        prompt_group=METADATA, language_variant="fr", library_type=LibraryType.ANIME
    )
    assert pure != default
    assert "[OEUVRE]" in default  # sanity: the default set still uses it


# ---------------------------------------------------------------------------
# Identity matcher
# ---------------------------------------------------------------------------

def _scenes(*bounds: tuple[float, float]) -> SceneList:
    return SceneList(
        scenes=[
            Scene(index=i, start_time=start, end_time=end)
            for i, (start, end) in enumerate(bounds)
        ]
    )


def test_identity_matches_shape():
    video = Path("/tmp/some/project/tiktok_clean.mp4")
    scenes = _scenes((0.0, 2.5), (2.5, 4.0), (4.0, 9.25))
    matches = PureMatcherService.build_identity_matches(video, scenes)

    assert len(matches.matches) == 3
    for scene, match in zip(scenes.scenes, matches.matches):
        assert match.scene_index == scene.index
        assert match.episode == str(video)
        assert Path(match.episode).is_absolute()
        assert match.start_time == scene.start_time
        assert match.end_time == scene.end_time
        assert match.confidence == 1.0
        assert match.speed_ratio == 1.0
        assert match.confirmed is True


def test_identity_rematch_replaces_only_merged_scene():
    video = Path("/tmp/some/project/tiktok_clean.mp4")
    # Post-merge state: scene 1 spans the old scenes 1+2.
    merged_scenes = _scenes((0.0, 2.0), (2.0, 6.0), (6.0, 8.0))
    existing = MatchList(
        matches=[
            SceneMatch(
                scene_index=0, episode=str(video), start_time=0.0, end_time=2.0,
                confidence=1.0, speed_ratio=1.0, confirmed=True,
            ),
            SceneMatch(  # placeholder produced by the merge preparation
                scene_index=1, episode="", start_time=0.0, end_time=0.0,
                confidence=0.0, speed_ratio=1.0, merged_from=[1, 2],
            ),
            SceneMatch(
                scene_index=2, episode=str(video), start_time=6.0, end_time=8.0,
                confidence=1.0, speed_ratio=1.0, confirmed=True,
            ),
        ]
    )

    result = PureMatcherService.rematch_scene(
        video, merged_scenes, scene_index=1, existing_matches=existing
    )

    assert [m.scene_index for m in result.matches] == [0, 1, 2]
    merged = result.matches[1]
    assert merged.episode == str(video)
    assert merged.start_time == 2.0
    assert merged.end_time == 6.0
    assert merged.confirmed is True
    # Untouched neighbours survive as-is.
    assert result.matches[0].start_time == 0.0
    assert result.matches[2].start_time == 6.0


def test_identity_rematch_unknown_scene_raises():
    video = Path("/tmp/x.mp4")
    with pytest.raises(ValueError):
        PureMatcherService.rematch_scene(
            video, _scenes((0.0, 1.0)), scene_index=7,
            existing_matches=MatchList(matches=[]),
        )


# ---------------------------------------------------------------------------
# Scene detector defaults
# ---------------------------------------------------------------------------

def test_default_threshold_per_library_type():
    assert SceneDetectorService.default_threshold(LibraryType.PURE) == 27.0
    assert SceneDetectorService.default_threshold(LibraryType.ANIME) == 16.0
    assert SceneDetectorService.default_threshold(None) == 16.0
    # Above 16.0 both the sensitive-reinject and auto-dense passes are
    # disabled inside _detect_sync (they are gated on threshold <= 16.0).
    assert SceneDetectorService.PURE_THRESHOLD > 16.0


# ---------------------------------------------------------------------------
# Gap resolution by boundary borrowing (Pure, fully automatic)
# ---------------------------------------------------------------------------

class _GapStub:
    def __init__(self, scene_index: int, target_duration: float):
        self.scene_index = scene_index
        self.target_duration = target_duration


def _identity_match(index: int, start: float, end: float) -> SceneMatch:
    return SceneMatch(
        scene_index=index, episode="/tmp/v.mp4", start_time=start, end_time=end,
        confidence=1.0, speed_ratio=1.0, confirmed=True,
    )


def _timing(index: int, start: float, end: float, is_raw: bool = False) -> dict:
    return {
        "scene_index": index,
        "start_time": start,
        "end_time": end,
        "is_raw": is_raw,
        "words": [],
    }


def test_borrowing_moves_cut_and_keeps_contiguity():
    # Scene 1 needs 0.4*6=2.4s of source but only has 1.0s; scene 2 has
    # 4.0s of source for 2.0s narration (needs 0.85 incl. safety) => slack.
    matches = [
        _identity_match(0, 0.0, 3.0),
        _identity_match(1, 3.0, 4.0),
        _identity_match(2, 4.0, 8.0),
    ]
    gaps = [_GapStub(1, 6.0)]
    timings = [_timing(0, 0.0, 3.0), _timing(1, 3.0, 9.0), _timing(2, 9.0, 11.0)]

    report = PureMatcherService.resolve_gaps_by_borrowing(
        matches, gaps, timings, min_speed=0.4,
    )

    gapped = matches[1]
    assert gapped.end_time - gapped.start_time >= 2.4 - 1e-6
    # Contiguity preserved on both boundaries.
    assert abs(matches[0].end_time - gapped.start_time) < 1e-6
    assert abs(gapped.end_time - matches[2].start_time) < 1e-6
    assert any("borrowed" in line for line in report)
    assert not any("remains" in line for line in report)


def test_raw_neighbour_is_never_touched():
    matches = [
        _identity_match(0, 0.0, 4.0),
        _identity_match(1, 4.0, 5.0),
    ]
    gaps = [_GapStub(1, 6.0)]
    timings = [_timing(0, 0.0, 4.0, is_raw=True), _timing(1, 4.0, 10.0)]

    report = PureMatcherService.resolve_gaps_by_borrowing(
        matches, gaps, timings, min_speed=0.4,
    )

    assert matches[0].end_time == 4.0  # raw window untouched
    assert matches[1].start_time == 4.0
    assert any("remains" in line for line in report)


def test_residual_reported_when_no_slack_anywhere():
    matches = [
        _identity_match(0, 0.0, 1.0),
        _identity_match(1, 1.0, 2.0),
    ]
    gaps = [_GapStub(1, 10.0)]
    # Scene 0's own narration fills its window (no slack).
    timings = [_timing(0, 0.0, 2.5), _timing(1, 2.5, 12.5)]

    report = PureMatcherService.resolve_gaps_by_borrowing(
        matches, gaps, timings, min_speed=0.4,
    )
    assert matches[0].end_time == 1.0
    assert any("remains" in line for line in report)


def test_pure_default_min_playback_speed():
    from app.models.project import Project

    pure = Project(library_type=LibraryType.PURE)
    assert pure.resolved_min_playback_speed() == Project.PURE_DEFAULT_MIN_PLAYBACK_SPEED

    explicit = Project(library_type=LibraryType.PURE, min_playback_speed=0.7)
    assert explicit.resolved_min_playback_speed() == 0.7
