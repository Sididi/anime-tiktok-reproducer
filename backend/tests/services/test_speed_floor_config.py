from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from app.config import settings
from app.models.match import MatchCandidate, SceneMatch
from app.services.anime_matcher import AnimeMatcherService
from app.services.gap_resolution import GapInfo, GapResolutionService, _NeighborContext
from app.services.otio_timing import ClipTiming, FrameRateInfo, OTIOTimingCalculator


def _make_match(scene_index: int, *, start_time: float, end_time: float) -> SceneMatch:
    return SceneMatch(
        scene_index=scene_index,
        episode="episode-1",
        start_time=start_time,
        end_time=end_time,
        confidence=1.0,
        speed_ratio=1.0,
        confirmed=True,
    )


def _make_candidate(timestamp: float) -> MatchCandidate:
    return MatchCandidate(
        episode="episode-1",
        timestamp=timestamp,
        similarity=0.99,
        series="series-1",
    )


@pytest.mark.parametrize(
    ("min_speed_factor", "expected_effective_speed", "expected_leaves_gap"),
    [
        (0.75, Fraction(3, 4), False),
        (0.80, Fraction(4, 5), True),
    ],
)
def test_otio_calculate_speed_uses_configured_floor(
    monkeypatch: pytest.MonkeyPatch,
    min_speed_factor: float,
    expected_effective_speed: Fraction,
    expected_leaves_gap: bool,
):
    monkeypatch.setattr(settings, "min_playback_speed_factor", min_speed_factor)

    calculator = OTIOTimingCalculator()
    raw_speed, effective_speed, leaves_gap = calculator.calculate_speed(3.0, 4.0)

    assert raw_speed == Fraction(3, 4)
    assert effective_speed == expected_effective_speed
    assert leaves_gap is expected_leaves_gap


def test_gap_detection_uses_configured_floor(monkeypatch: pytest.MonkeyPatch):
    match = _make_match(0, start_time=0.0, end_time=3.0)
    scene_timings = [{"scene_index": 0, "start_time": 0.0, "end_time": 4.0, "words": []}]

    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.75)
    assert GapResolutionService.calculate_gaps([match], scene_timings) == []

    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.80)
    gaps = GapResolutionService.calculate_gaps([match], scene_timings)

    assert len(gaps) == 1
    assert gaps[0].required_speed == Fraction(3, 4)
    assert gaps[0].effective_speed == Fraction(4, 5)
    assert gaps[0].gap_duration == pytest.approx(0.25)


def test_raw_clip_continuity_uses_enforced_timeline_end():
    calculator = OTIOTimingCalculator(
        sequence_rate=FrameRateInfo(timebase=60, ntsc=False),
        source_rate=FrameRateInfo(timebase=24, ntsc=True),
    )
    raw_timeline_start = 10.9
    raw_timeline_end = 13.0
    raw_source_in = float(Fraction(100 * 1001, 24000))
    raw_source_out = float(Fraction(148 * 1001, 24000))

    raw_clip = ClipTiming(
        scene_index=0,
        source_path=Path("/tmp/raw.mp4"),
        bundle_filename="raw.mp4",
        source_in=calculator.seconds_to_source_time(raw_source_in),
        source_out=calculator.seconds_to_source_time(raw_source_out),
        source_rate=calculator.source_rate,
        timeline_start=calculator.seconds_to_timeline_time(raw_timeline_start),
        timeline_end=calculator.seconds_to_timeline_time(raw_timeline_end),
        timeline_rate=calculator.sequence_rate,
        speed_ratio=Fraction(1, 1),
        effective_speed=Fraction(1, 1),
        leaves_gap=False,
        enforced_timeline_end=calculator.seconds_to_timeline_time(raw_timeline_end),
    )
    next_clip = calculator.calculate_clip_timing(
        scene_index=1,
        source_path=Path("/tmp/next.mp4"),
        bundle_filename="next.mp4",
        source_in_seconds=0.0,
        source_out_seconds=1.0,
        timeline_start_seconds=raw_timeline_end,
        timeline_end_seconds=raw_timeline_end + 1.0,
    )

    issues_without_override = calculator.validate_clip_continuity(
        [replace(raw_clip, enforced_timeline_end=None), next_clip],
        tolerance_frames=0,
    )
    issues_with_override = calculator.validate_clip_continuity(
        [raw_clip, next_clip],
        tolerance_frames=0,
    )

    assert len(issues_without_override) == 1
    assert issues_without_override[0].issue_type == "gap"
    assert issues_without_override[0].duration_seconds == pytest.approx(
        raw_timeline_end - (raw_timeline_start + (raw_source_out - raw_source_in))
    )
    assert issues_with_override == []


def test_fallback_candidates_use_configured_floor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.80)

    gap = GapInfo(
        scene_index=0,
        episode="episode-1",
        current_start=10.0,
        current_end=11.0,
        current_duration=1.0,
        timeline_start=0.0,
        timeline_end=2.0,
        target_duration=2.0,
        required_speed=Fraction(1, 2),
        effective_speed=Fraction(4, 5),
        gap_duration=0.75,
    )

    candidates = GapResolutionService._generate_fallback_candidates(
        gap,
        _NeighborContext(),
    )

    extend_start = next(
        candidate
        for candidate in candidates
        if candidate.extend_type == "fallback_extend_start"
    )
    assert extend_start.duration == pytest.approx(1.6)
    assert extend_start.effective_speed == Fraction(4, 5)
    assert "80%" in extend_start.snap_description


def test_matcher_floor_tracks_playback_floor_minus_ten_points(
    monkeypatch: pytest.MonkeyPatch,
):
    start_candidates = [_make_candidate(0.0)]
    middle_candidates = [_make_candidate(5.0)]
    end_candidates = [_make_candidate(10.0)]

    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.75)
    assert settings.matcher_min_speed_factor == pytest.approx(0.65)
    assert (
        AnimeMatcherService._find_temporal_match(
            start_candidates,
            middle_candidates,
            end_candidates,
            scene_duration=6.8,
        )
        is not None
    )

    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.80)
    assert settings.matcher_min_speed_factor == pytest.approx(0.70)
    assert (
        AnimeMatcherService._find_temporal_match(
            start_candidates,
            middle_candidates,
            end_candidates,
            scene_duration=6.8,
        )
        is None
    )
