from __future__ import annotations

import numpy as np
from PIL import Image

from app.models import MatchList, Scene, SceneList, SceneMatch
from app.services.fast_matching import bounded_matcher_enabled, matcher_v2_enabled
from app.services.hierarchical_matcher import (
    HierarchicalDiagnostics,
    HierarchicalMatcherService,
    HierarchicalResult,
    LineProposal,
    QueryFrame,
    RetrievalCandidate,
    TrackSegment,
)


def _sample(timestamp: float) -> QueryFrame:
    return QueryFrame(timestamp, np.ones(4, dtype=np.float32))


def _candidate(
    sample_index: int,
    query: float,
    source: float,
    *,
    episode: str = "episode-1",
    similarity: float = 0.65,
    variant: str = "plain",
) -> RetrievalCandidate:
    return RetrievalCandidate(
        sample_index,
        query,
        episode,
        source,
        similarity,
        "series",
        variant,
    )


def _decode(samples, values, detector=(), strong=()):
    return HierarchicalMatcherService._decode_beam(
        samples, values, list(detector), list(strong)
    )


def test_v2_flag_selects_old_matcher_only_when_true(monkeypatch):
    from app.config import settings

    # Keep this test independent of a developer's repository .env.  The
    # production selector is separately checked below against the settings
    # value loaded from that file.
    monkeypatch.setattr(settings, "matcher_v2", False)
    monkeypatch.delenv("ATR_MATCHER_V2", raising=False)
    assert matcher_v2_enabled() is False
    assert bounded_matcher_enabled() is True

    monkeypatch.setattr(settings, "matcher_v2", True)
    assert matcher_v2_enabled() is True
    assert bounded_matcher_enabled() is False

    monkeypatch.setattr(settings, "matcher_v2", False)
    monkeypatch.setenv("ATR_MATCHER_V2", "1")
    assert matcher_v2_enabled() is True
    assert bounded_matcher_enabled() is False


def test_scene_aligner_adapter_preserves_route_result_shape(monkeypatch):
    from app.services.scene_aligner import AlignmentDiagnostics, SceneAlignerService

    scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=1.0)])
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="episode-1",
                start_time=10.0,
                end_time=11.0,
                confidence=0.8,
                speed_ratio=1.0,
            )
        ]
    )
    diagnostics = HierarchicalDiagnostics(
        sample_count=4,
        counters={"native_source_seconds": 1.5},
    )
    # False selects the new bounded matcher; true is reserved for the old
    # matcher compatibility escape hatch.
    monkeypatch.setenv("ATR_MATCHER_V2", "0")
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "align_scenes_sync",
        classmethod(
            lambda cls, video_path, scenes_arg, library_type, anime_name=None, episode_whitelist=None: HierarchicalResult(
                scenes, matches, diagnostics
            )
        ),
    )
    result = SceneAlignerService.align_scenes_sync(
        "video.mp4", scenes, "anime", "series"
    )
    assert result.scenes is scenes
    assert result.matches is matches
    assert isinstance(result.diagnostics, AlignmentDiagnostics)
    assert result.diagnostics.counters["native_source_seconds"] == 1.5
    monkeypatch.setenv("ATR_MATCHER_V2", "1")
    assert matcher_v2_enabled() is True
    monkeypatch.setenv("ATR_MATCHER_V2", "false")
    assert matcher_v2_enabled() is False
    assert bounded_matcher_enabled() is True


def test_short_fragment_receives_at_least_three_query_probes():
    scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=0.5)])
    targets = HierarchicalMatcherService._target_times(scenes)
    assert len(targets) >= 3
    assert all(0.0 <= value < 0.5 for value in targets)


def test_portrait_geometry_variants_are_bounded():
    variants = HierarchicalMatcherService._query_variants(
        Image.new("RGB", (90, 160), "white")
    )
    assert variants[0][0] == "center_landscape"
    assert len(variants) <= 2


def test_beam_keeps_one_continuous_track_across_false_detector_cut():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75, 1.0)]
    values = [
        [_candidate(index, sample.t_query, 100.0 + sample.t_query)]
        for index, sample in enumerate(samples)
    ]
    state = _decode(samples, values, detector=(0.5,))
    assert all(point is not None for point in state.path)
    assert sum(state.breaks) == 1


def test_detector_boundary_wins_over_nearby_motion_peak():
    snapped = HierarchicalMatcherService._snap_boundary(
        6.08,
        [6.0],
        [5.95],
    )
    assert snapped == 6.0


def test_beam_tolerates_provisional_half_second_grid_slope():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75, 1.0)]
    # A real-time track quantized to the source index's 0.5s grid.  The first
    # two equal timestamps make the raw provisional slope 0x.
    source_times = (10.0, 10.0, 10.5, 10.5, 11.0)
    values = [
        [_candidate(index, sample.t_query, source_times[index])]
        for index, sample in enumerate(samples)
    ]
    state = _decode(samples, values, strong=(0.625,))
    assert sum(state.breaks) == 1


def test_beam_splits_source_jump_inside_one_detector_fragment():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75, 1.0)]
    values = []
    for index, sample in enumerate(samples):
        source = 100.0 + sample.t_query if index < 3 else 200.0 + sample.t_query
        values.append([_candidate(index, sample.t_query, source)])
    state = _decode(samples, values, strong=(0.625,))
    assert sum(state.breaks) >= 2


def test_beam_resets_when_episode_changes():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5)]
    values = [
        [_candidate(0, 0.0, 10.0, episode="episode-1")],
        [_candidate(1, 0.25, 10.25, episode="episode-1")],
        [_candidate(2, 0.5, 20.5, episode="episode-2")],
    ]
    state = _decode(samples, values, strong=(0.375,))
    assert state.path[-1].episode == "episode-2"
    assert sum(state.breaks) >= 2


def test_beam_tracks_non_unit_playback_rate():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75, 1.0)]
    values = [
        [_candidate(index, sample.t_query, 40.0 + 1.5 * sample.t_query)]
        for index, sample in enumerate(samples)
    ]
    state = _decode(samples, values)
    points = [point for point in state.path if point is not None]
    rate, _, residual = HierarchicalMatcherService._fit_points(points)
    assert rate == pytest.approx(1.5, abs=0.05)
    assert residual < 0.05


def test_continuity_beats_a_higher_scoring_repeated_instance_jump():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75)]
    values = []
    for index, sample in enumerate(samples):
        truth = _candidate(index, sample.t_query, 10.0 + sample.t_query, similarity=0.60)
        repeated = _candidate(
            index,
            sample.t_query,
            100.0 + index * 25.0,
            similarity=0.66,
        )
        values.append([repeated, truth])
    state = _decode(samples, values)
    chosen = [point for point in state.path if point is not None]
    assert len(chosen) == len(samples)
    assert max(point.t_source for point in chosen) < 20.0


def test_evidence_hole_and_fade_become_honest_no_evidence_segments():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5)]
    values = [
        [_candidate(0, 0.0, 10.0)],
        [],
        [],
    ]
    state = _decode(samples, values)
    segments = HierarchicalMatcherService._segments_from_state(
        state,
        samples,
        values,
        0.75,
        [],
        [],
    )
    assert segments[-1].episode is None
    assert "no_evidence" in segments[-1].doubt_reasons


def test_continuous_adjacent_segments_merge_but_discontinuous_do_not():
    first = TrackSegment(0.0, 1.0, "ep", 1.0, 10.0, confidence=0.6)
    second = TrackSegment(1.0, 2.0, "ep", 1.0, 10.1, confidence=0.6)
    third = TrackSegment(2.0, 3.0, "ep", 1.0, 30.0, confidence=0.6)
    merged = HierarchicalMatcherService._merge_continuous_segments(
        [first, second, third]
    )
    assert len(merged) == 2
    assert merged[0].q_start == 0.0
    assert merged[0].q_end == 2.0


def test_short_no_evidence_sliver_is_absorbed_forward():
    segments = [
        TrackSegment(0.0, 1.0, "left", 1.0, 10.0, confidence=0.6),
        TrackSegment(
            1.0,
            1.54,
            None,
            uncertain=True,
            doubt_reasons=["no_evidence"],
        ),
        TrackSegment(1.54, 3.0, "right", 1.0, 30.0, confidence=0.6),
    ]
    merged = HierarchicalMatcherService._absorb_tiny_segments(segments)
    assert len(merged) == 2
    assert merged[0].q_end == 1.0
    assert merged[1].q_start == 1.0


def test_weak_micro_fragment_merges_right_when_its_trailing_cut_is_weaker():
    samples = [_sample(value) for value in (1.0, 1.25, 1.5)]
    candidates = [
        [
            _candidate(
                index,
                sample.t_query,
                20.0 + sample.t_query,
                episode="correct",
                similarity=0.50,
            )
        ]
        for index, sample in enumerate(samples)
    ]
    segments = [
        TrackSegment(0.0, 1.0, "correct", 1.0, 20.0, confidence=0.70),
        TrackSegment(
            1.0,
            1.55,
            "wrong",
            1.0,
            90.0,
            confidence=0.34,
            uncertain=True,
            doubt_reasons=["weak_similarity"],
        ),
        TrackSegment(1.55, 3.0, "correct", 1.0, 20.0, confidence=0.64),
    ]

    merged = HierarchicalMatcherService._absorb_weak_micro_segments(
        segments,
        candidates,
        samples,
        [1.0, 1.55],
        [100.0, 30.0],
    )

    assert len(merged) == 2
    assert merged[0].q_end == 1.0
    assert merged[1].q_start == 1.0
    assert merged[1].episode == "correct"
    assert "weak_micro_absorbed" in merged[1].doubt_reasons


def test_weak_micro_fragment_bridges_continuous_flanks_without_inner_evidence():
    samples = [_sample(value) for value in (2.0, 2.2)]
    candidates = [
        [
            _candidate(
                index,
                sample.t_query,
                150.0 + sample.t_query,
                episode="unrelated",
                similarity=0.70,
            )
        ]
        for index, sample in enumerate(samples)
    ]
    segments = [
        TrackSegment(0.0, 2.0, "correct", 1.0, 50.0, confidence=0.70),
        TrackSegment(
            2.0,
            2.4,
            "wrong",
            1.0,
            90.0,
            confidence=0.34,
            uncertain=True,
            doubt_reasons=["weak_similarity"],
        ),
        TrackSegment(2.4, 4.0, "correct", 1.0, 50.4, confidence=0.51),
    ]

    merged = HierarchicalMatcherService._absorb_weak_micro_segments(
        segments,
        candidates,
        samples,
        [2.0, 2.4],
        [60.0, 58.0],
    )

    assert len(merged) == 2
    assert merged[0].q_end == 2.4
    assert merged[0].episode == "correct"
    assert merged[1].q_start == 2.4


def test_weak_micro_fragment_preserves_a_confident_half_second_edit():
    samples = [_sample(value) for value in (1.0, 1.25, 1.5)]
    candidates = [
        [
            _candidate(
                index,
                sample.t_query,
                20.0 + sample.t_query,
                episode="neighbor",
                similarity=0.70,
            )
        ]
        for index, sample in enumerate(samples)
    ]
    segments = [
        TrackSegment(0.0, 1.0, "neighbor", 1.0, 20.0, confidence=0.70),
        TrackSegment(1.0, 1.5, "real-edit", 1.0, 80.0, confidence=0.70),
        TrackSegment(1.5, 3.0, "neighbor", 1.0, 20.0, confidence=0.70),
    ]

    merged = HierarchicalMatcherService._absorb_weak_micro_segments(
        segments,
        candidates,
        samples,
        [1.0, 1.5],
        [100.0, 30.0],
    )

    assert merged == segments


def test_weak_micro_fragment_requires_neighbor_evidence_inside_the_fragment():
    samples = [_sample(value) for value in (1.0, 1.25, 1.5)]
    candidates = [
        [
            _candidate(
                index,
                sample.t_query,
                200.0 + sample.t_query,
                episode="unrelated",
                similarity=0.70,
            )
        ]
        for index, sample in enumerate(samples)
    ]
    segments = [
        TrackSegment(0.0, 1.0, "left-neighbor", 1.0, 20.0, confidence=0.70),
        TrackSegment(
            1.0,
            1.5,
            "wrong",
            1.0,
            80.0,
            confidence=0.34,
            uncertain=True,
            doubt_reasons=["weak_similarity"],
        ),
        TrackSegment(1.5, 3.0, "right-neighbor", 1.0, 20.0, confidence=0.70),
    ]

    merged = HierarchicalMatcherService._absorb_weak_micro_segments(
        segments,
        candidates,
        samples,
        [1.0, 1.5],
        [100.0, 30.0],
    )

    assert merged == segments


def test_weak_micro_fragment_does_not_bridge_discontinuous_flanks():
    samples = [_sample(value) for value in (1.0, 1.25)]
    candidates = [
        [
            _candidate(
                index,
                sample.t_query,
                200.0 + sample.t_query,
                episode="unrelated",
                similarity=0.70,
            )
        ]
        for index, sample in enumerate(samples)
    ]
    segments = [
        TrackSegment(0.0, 1.0, "neighbor", 1.0, 20.0, confidence=0.70),
        TrackSegment(
            1.0,
            1.4,
            "wrong",
            1.0,
            80.0,
            confidence=0.34,
            uncertain=True,
            doubt_reasons=["weak_similarity"],
        ),
        TrackSegment(1.4, 3.0, "neighbor", 1.0, 30.0, confidence=0.70),
    ]

    merged = HierarchicalMatcherService._absorb_weak_micro_segments(
        segments,
        candidates,
        samples,
        [1.0, 1.4],
        [60.0, 58.0],
    )

    assert merged == segments


def test_supported_flanks_replace_a_short_wrong_duplicate_island():
    left_points = [
        _candidate(index, 0.25 * index, 50.0 + 1.5 * 0.25 * index)
        for index in range(4)
    ]
    right_points = [
        _candidate(
            index + 4,
            1.5 + 0.25 * index,
            50.0 + 1.5 * (1.5 + 0.25 * index),
        )
        for index in range(4)
    ]
    segments = [
        TrackSegment(
            0.0,
            1.0,
            "episode-1",
            1.5,
            50.0,
            points=left_points,
            confidence=0.64,
        ),
        TrackSegment(
            1.0,
            1.5,
            "episode-1",
            1.0,
            700.0,
            points=[_candidate(8, 1.25, 701.25)],
            confidence=0.46,
        ),
        TrackSegment(
            1.5,
            2.5,
            "episode-1",
            1.5,
            50.0,
            points=right_points,
            confidence=0.62,
        ),
    ]

    repaired = HierarchicalMatcherService._repair_supported_flank_islands(
        segments
    )

    assert len(repaired) == 1
    assert repaired[0].q_start == 0.0
    assert repaired[0].q_end == 2.5
    assert repaired[0].a == pytest.approx(1.5, abs=0.05)
    assert repaired[0].source_at(1.25) == pytest.approx(51.875, abs=0.2)
    assert "supported_flank_bridge" in repaired[0].doubt_reasons


def test_supported_flank_bridge_requires_eight_inliers():
    left_points = [
        _candidate(index, 0.25 * index, 50.0 + 0.25 * index)
        for index in range(3)
    ]
    right_points = [
        _candidate(index + 3, 1.5 + 0.25 * index, 51.5 + 0.25 * index)
        for index in range(3)
    ]
    segments = [
        TrackSegment(0.0, 1.0, "episode-1", points=left_points, confidence=0.64),
        TrackSegment(1.0, 1.5, "wrong", 1.0, 700.0, confidence=0.40),
        TrackSegment(1.5, 2.5, "episode-1", points=right_points, confidence=0.62),
    ]

    repaired = HierarchicalMatcherService._repair_supported_flank_islands(
        segments
    )

    assert repaired == segments


def test_short_leading_duplicate_can_pool_into_longer_winning_track():
    micro_points = [
        _candidate(index, 1.0 + 0.2 * index, 901.0 + 0.2 * index)
        for index in range(2)
    ]
    following_points = [
        _candidate(index, 1.45 + 0.25 * index, 51.45 + 0.25 * index)
        for index in range(6)
    ]
    segments = [
        TrackSegment(
            1.0,
            1.45,
            "episode-1",
            1.0,
            900.0,
            points=micro_points,
            confidence=0.58,
            uncertain=True,
            doubt_reasons=["duplicate_margin"],
        ),
        TrackSegment(
            1.45,
            3.0,
            "episode-1",
            1.0,
            50.0,
            points=following_points,
            confidence=0.55,
            uncertain=True,
            doubt_reasons=["duplicate_margin"],
        ),
    ]
    collapsed = HierarchicalMatcherService._collapse_leading_duplicate_regions(
        segments
    )
    assert len(collapsed) == 1
    assert collapsed[0].q_start == 1.0
    assert collapsed[0].q_end == 3.0
    assert collapsed[0].source_at(1.45) == pytest.approx(51.45, abs=0.3)
    assert "duplicate_region_collapsed" in collapsed[0].doubt_reasons


def test_opening_duplicate_uses_strong_start_anchor_before_pooling():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5)]
    candidates = [
        [
            _candidate(0, 0.0, 737.0, similarity=0.76),
            _candidate(0, 0.0, 686.0, similarity=0.45),
        ],
        [_candidate(1, 0.25, 686.0, similarity=0.46)],
        [_candidate(2, 0.5, 686.0, similarity=0.44)],
        [_candidate(3, 0.75, 739.0, similarity=0.48)],
        [_candidate(4, 1.0, 739.0, similarity=0.52)],
        [_candidate(5, 1.5, 739.0, similarity=0.48)],
        [_candidate(6, 2.0, 740.0, similarity=0.64)],
        [_candidate(7, 2.5, 740.0, similarity=0.51)],
    ]
    micro = TrackSegment(
        0.0,
        0.625,
        "episode-1",
        1.0,
        685.63,
        points=[candidates[1][0], candidates[2][0]],
        confidence=0.452,
        uncertain=True,
        doubt_reasons=["duplicate_margin"],
    )
    following = TrackSegment(
        0.625,
        2.7333333333333334,
        "episode-1",
        1.0,
        737.9,
        points=[values[0] for values in candidates[3:]],
        confidence=0.479,
        uncertain=True,
        doubt_reasons=["duplicate_margin"],
    )

    repaired = HierarchicalMatcherService._repair_opening_duplicate_region(
        [micro, following], candidates, samples
    )

    assert len(repaired) == 1
    source_start, source_end = HierarchicalMatcherService._safe_primary_source_interval(
        repaired[0]
    )
    assert source_start == pytest.approx(737.0, abs=0.05)
    assert source_end == pytest.approx(740.633, abs=0.2)
    assert repaired[0].a == pytest.approx(1.33, abs=0.08)
    assert "opening_duplicate_anchored" in repaired[0].doubt_reasons


def test_opening_duplicate_relaxation_never_applies_to_an_interior_fragment():
    samples = [_sample(value) for value in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)]
    candidates = [
        [_candidate(0, 1.0, 737.0, similarity=0.76)],
        [_candidate(1, 1.25, 686.0, similarity=0.46)],
        [_candidate(2, 1.5, 686.0, similarity=0.44)],
        [_candidate(3, 1.75, 739.0, similarity=0.48)],
        [_candidate(4, 2.0, 739.0, similarity=0.52)],
        [_candidate(5, 2.5, 740.0, similarity=0.64)],
        [_candidate(6, 3.0, 740.0, similarity=0.51)],
    ]
    micro = TrackSegment(
        1.0,
        1.625,
        "episode-1",
        1.0,
        685.63,
        points=[candidates[1][0], candidates[2][0]],
        confidence=0.452,
        uncertain=True,
        doubt_reasons=["duplicate_margin"],
    )
    following = TrackSegment(
        1.625,
        3.2,
        "episode-1",
        1.0,
        737.0,
        points=[values[0] for values in candidates[3:]],
        confidence=0.479,
    )

    repaired = HierarchicalMatcherService._repair_opening_duplicate_region(
        [micro, following], candidates, samples
    )

    assert repaired == [micro, following]


def test_supported_opening_continuation_extends_the_longer_affine_track():
    times = (
        0.14, 0.25, 0.35, 0.50, 0.56,
        0.75, 0.926667, 1.0, 1.25, 1.50, 1.75,
    )
    samples = [_sample(value) for value in times]
    candidates = [
        [
            _candidate(0, 0.14, 198.5, similarity=0.34),
            _candidate(0, 0.14, 200.5, similarity=0.33),
        ],
        [
            _candidate(1, 0.25, 198.5, similarity=0.49),
            _candidate(1, 0.25, 200.0, similarity=0.48),
        ],
        [
            _candidate(2, 0.35, 199.0, similarity=0.59),
            _candidate(2, 0.35, 200.5, similarity=0.58),
        ],
        [
            _candidate(3, 0.50, 199.0, similarity=0.66),
            _candidate(3, 0.50, 200.5, similarity=0.65),
        ],
        [
            _candidate(4, 0.56, 199.0, similarity=0.68),
            _candidate(4, 0.56, 201.0, similarity=0.67),
        ],
    ]
    following_sources = (201.0, 201.5, 201.5, 201.5, 202.0, 202.0)
    for index, (query, source) in enumerate(
        zip(times[5:], following_sources, strict=True),
        start=5,
    ):
        candidates.append(
            [_candidate(index, query, source, similarity=0.60)]
        )

    opening = TrackSegment(
        0.0,
        0.7,
        "episode-1",
        1.0,
        198.466,
        points=[values[0] for values in candidates[:5]],
        confidence=0.58,
        uncertain=True,
        doubt_reasons=["detector_discontinuity", "weak_similarity"],
    )
    following = TrackSegment(
        0.7,
        1.833333,
        "episode-1",
        1.0,
        200.375,
        points=[values[0] for values in candidates[5:]],
        confidence=0.60,
        uncertain=True,
        doubt_reasons=["detector_discontinuity"],
    )

    repaired = HierarchicalMatcherService._extend_supported_opening_continuation(
        [opening, following], candidates, samples
    )

    assert len(repaired) == 1
    assert repaired[0].a == pytest.approx(1.0)
    assert repaired[0].b == pytest.approx(200.375)
    source_start, source_end = HierarchicalMatcherService._safe_primary_source_interval(
        repaired[0]
    )
    assert source_start == pytest.approx(200.48)
    assert source_end == pytest.approx(202.208333, abs=1e-5)
    assert "supported_opening_continuation" in repaired[0].doubt_reasons


def test_supported_opening_continuation_requires_three_strong_probes():
    samples = [_sample(value) for value in (0.25, 0.5, 0.6, 0.75, 1.0, 1.25)]
    candidates = [
        [_candidate(0, 0.25, 200.5, similarity=0.60)],
        [_candidate(1, 0.5, 201.0, similarity=0.60)],
        # The third geometrically aligned probe is too weak.
        [_candidate(2, 0.6, 201.0, similarity=0.54)],
        [_candidate(3, 0.75, 201.0, similarity=0.60)],
        [_candidate(4, 1.0, 201.5, similarity=0.60)],
        [_candidate(5, 1.25, 201.5, similarity=0.60)],
    ]
    opening = TrackSegment(
        0.0, 0.7, "episode-1", 1.0, 198.5,
        confidence=0.58, uncertain=True,
        doubt_reasons=["detector_discontinuity", "weak_similarity"],
    )
    following = TrackSegment(
        0.7, 1.8, "episode-1", 1.0, 200.375,
        points=[values[0] for values in candidates[3:]] * 2,
        confidence=0.60, uncertain=True,
        doubt_reasons=["detector_discontinuity"],
    )

    repaired = HierarchicalMatcherService._extend_supported_opening_continuation(
        [opening, following], candidates, samples
    )

    assert repaired == [opening, following]


def test_sub_four_tenths_leading_shot_is_not_pooled():
    micro = TrackSegment(
        1.0,
        1.36,
        "episode-1",
        1.0,
        900.0,
        points=[_candidate(0, 1.1, 901.1)],
        confidence=0.58,
        uncertain=True,
        doubt_reasons=["duplicate_margin"],
    )
    following = TrackSegment(
        1.36,
        3.0,
        "episode-1",
        1.0,
        50.0,
        points=[
            _candidate(index, 1.5 + 0.25 * index, 51.5 + 0.25 * index)
            for index in range(5)
        ],
        confidence=0.55,
    )
    result = HierarchicalMatcherService._collapse_leading_duplicate_regions(
        [micro, following]
    )
    assert len(result) == 2


def test_detector_boundary_splits_only_with_supported_source_jump():
    samples = [_sample(value) for value in np.arange(0.0, 2.0, 0.25)]
    candidates = []
    for index, sample in enumerate(samples):
        source = (
            10.0 + sample.t_query
            if sample.t_query < 1.0
            else 30.0 + sample.t_query
        )
        candidates.append(
            [_candidate(index, sample.t_query, source, similarity=0.70)]
        )
    parent = TrackSegment(0.0, 2.0, "episode-1", 1.0, 10.0, confidence=0.65)
    split = HierarchicalMatcherService._split_supported_discontinuities(
        [parent], [1.0], samples, candidates
    )
    assert len(split) == 2
    assert split[0].source_at(1.0) < 15.0
    assert split[1].source_at(1.0) > 25.0


def test_detector_boundary_does_not_split_a_continuous_mapping():
    samples = [_sample(value) for value in np.arange(0.0, 2.0, 0.25)]
    candidates = [
        [_candidate(index, sample.t_query, 10.0 + sample.t_query)]
        for index, sample in enumerate(samples)
    ]
    parent = TrackSegment(0.0, 2.0, "episode-1", 1.0, 10.0, confidence=0.65)
    split = HierarchicalMatcherService._split_supported_discontinuities(
        [parent], [1.0], samples, candidates
    )
    assert split == [parent]


def test_top60_consensus_can_replace_a_weaker_primary_track():
    samples = [_sample(value) for value in (0.0, 0.25, 0.5, 0.75)]
    primary_points = []
    candidates = []
    for index, sample in enumerate(samples):
        primary = _candidate(
            index, sample.t_query, 10.0 + sample.t_query, similarity=0.60
        )
        alternate = _candidate(
            index,
            sample.t_query,
            30.0 + sample.t_query,
            similarity=0.70,
        )
        primary_points.append(primary)
        candidates.append([alternate, primary])
    parent = TrackSegment(
        0.0,
        1.0,
        "episode-1",
        1.0,
        10.0,
        points=primary_points,
        confidence=0.60,
    )
    promoted = HierarchicalMatcherService._promote_dominant_proposals(
        [parent], candidates, samples
    )
    assert promoted[0].source_at(0.5) == pytest.approx(30.5, abs=0.5)
    assert "dominant_retrieval" in promoted[0].doubt_reasons


def test_two_point_same_episode_consensus_can_fix_a_micro_duplicate():
    samples = [_sample(value) for value in (159.5, 160.0)]
    candidates = [
        [
            _candidate(0, 159.5, 1211.5, similarity=0.458),
            _candidate(0, 159.5, 1058.5, similarity=0.35),
        ],
        [
            _candidate(1, 160.0, 1212.0, similarity=0.45),
            _candidate(1, 160.0, 1059.0, similarity=0.352),
        ],
    ]
    primary = TrackSegment(
        159.46666666666667,
        160.14666666666665,
        "episode-1",
        1.0,
        899.0,
        points=[candidates[1][1]],
        confidence=0.352,
        uncertain=True,
        doubt_reasons=["duplicate_margin", "weak_similarity"],
    )

    promoted = HierarchicalMatcherService._promote_dominant_proposals(
        [primary], candidates, samples
    )

    assert promoted[0].source_at(160.0) == pytest.approx(1212.0, abs=0.3)
    assert "dominant_retrieval" in promoted[0].doubt_reasons


def test_two_point_micro_consensus_cannot_cross_episode_boundaries():
    samples = [_sample(value) for value in (10.0, 10.25)]
    candidates = [
        [
            _candidate(0, 10.0, 100.0, episode="episode-2", similarity=0.70),
            _candidate(0, 10.0, 20.0, similarity=0.50),
        ],
        [
            _candidate(1, 10.25, 100.25, episode="episode-2", similarity=0.70),
            _candidate(1, 10.25, 20.25, similarity=0.50),
        ],
    ]
    primary = TrackSegment(
        10.0,
        10.5,
        "episode-1",
        1.0,
        10.0,
        points=[values[1] for values in candidates],
        confidence=0.50,
    )

    promoted = HierarchicalMatcherService._promote_dominant_proposals(
        [primary], candidates, samples
    )

    assert promoted == [primary]


def test_strong_multi_probe_proposal_rescues_an_abstained_segment():
    samples = [_sample(value) for value in (94.25, 94.5, 94.75, 95.0)]
    candidates = [
        [
            _candidate(
                index,
                sample.t_query,
                1130.5 + sample.t_query,
                episode="episode-14",
                similarity=0.68,
                variant="wide_pad",
            )
        ]
        for index, sample in enumerate(samples)
    ]
    abstained = TrackSegment(
        94.2,
        95.2,
        None,
        confidence=0.0,
        uncertain=True,
        doubt_reasons=["no_evidence"],
    )

    rescued = HierarchicalMatcherService._rescue_strong_abstentions(
        [abstained], candidates, samples
    )

    assert rescued[0].episode == "episode-14"
    assert rescued[0].source_at(94.5) == pytest.approx(1225.0, abs=0.3)
    assert "strong_retrieval_rescue" in rescued[0].doubt_reasons


def test_strong_abstention_rescue_requires_three_distinct_probes():
    samples = [_sample(value) for value in (1.0, 1.25)]
    candidates = [
        [_candidate(index, sample.t_query, 30.0 + sample.t_query, similarity=0.75)]
        for index, sample in enumerate(samples)
    ]
    abstained = TrackSegment(
        1.0,
        1.5,
        None,
        uncertain=True,
        doubt_reasons=["no_evidence"],
    )

    rescued = HierarchicalMatcherService._rescue_strong_abstentions(
        [abstained], candidates, samples
    )

    assert rescued == [abstained]


def test_native_recovery_budget_exhaustion_is_deterministic(monkeypatch):
    samples = [_sample(index + 0.5) for index in range(10)]
    candidates = []
    segments = []
    for index, sample in enumerate(samples):
        candidates.append(
            [
                _candidate(index, sample.t_query, 10.0 + sample.t_query),
                _candidate(index, sample.t_query, 40.0 + sample.t_query),
            ]
        )
        segments.append(
            TrackSegment(
                float(index),
                float(index + 1),
                "episode-1",
                1.0,
                10.0,
                confidence=0.4,
                uncertain=True,
                doubt_reasons=["duplicate_margin"],
            )
        )
    from app.services.anime_library import AnimeLibraryService

    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        lambda *args, **kwargs: None,
    )
    HierarchicalMatcherService._verify_ambiguous_segments(
        segments,
        samples,
        candidates,
        "anime",
        60.0,
    )
    assert sum(
        "verification_budget" in segment.doubt_reasons for segment in segments
    ) == 5


def test_native_check_prefers_supported_neighbor_timeline_over_remote_duplicate():
    segments = [
        TrackSegment(0.0, 1.0, "ep", 1.0, 100.0, confidence=0.60),
        TrackSegment(1.0, 2.0, "wrong", 1.0, 900.0, confidence=0.58),
    ]
    proposals = [
        # Slightly stronger, but remote from the preceding source timeline.
        LineProposal("ep", 1.0, 199.0, 0.61, 4, "timeline_cluster"),
        # Competitive evidence and a plausible shot-to-shot continuation.
        LineProposal("ep", 1.0, 101.0, 0.57, 4, "timeline_cluster"),
    ]
    chosen, distance = HierarchicalMatcherService._verification_alternative(
        segments,
        1,
        proposals,
    )
    assert chosen is proposals[1]
    assert distance == pytest.approx(1.0)


def test_native_check_can_test_neighbor_for_short_duplicate_island():
    segments = [
        TrackSegment(0.0, 1.0, "ep", 1.0, 100.0, confidence=0.60),
        TrackSegment(
            1.0,
            1.5,
            "ep",
            1.0,
            900.0,
            confidence=0.58,
            uncertain=True,
            doubt_reasons=["duplicate_margin"],
        ),
        TrackSegment(1.5, 2.0, "other", 1.0, 300.0, confidence=0.62),
    ]
    chosen, distance = HierarchicalMatcherService._verification_alternative(
        segments,
        1,
        [],
    )
    assert chosen is not None
    assert chosen.algorithm == "neighbor_continuation"
    assert chosen.source_at(1.0) == pytest.approx(101.0)
    assert distance == pytest.approx(0.0)


def test_native_check_skips_a_near_primary_cluster_for_equal_remote_evidence():
    segment = TrackSegment(
        34.2,
        35.45,
        "ep",
        1.0,
        241.482853,
        confidence=0.5545,
        uncertain=True,
        doubt_reasons=["duplicate_margin"],
    )
    midpoint = 0.5 * (segment.q_start + segment.q_end)
    near = LineProposal(
        "ep", 1.0, 278.807 - midpoint, 0.5946, 9, "timeline_cluster"
    )
    exact = LineProposal(
        "ep", 1.0, 467.09 - midpoint, 0.5760, 9, "timeline_cluster"
    )

    chosen, _ = HierarchicalMatcherService._verification_alternative(
        [segment], 0, [near, exact]
    )

    assert chosen is not None
    assert chosen.source_at(midpoint) == pytest.approx(467.09)
    assert chosen.algorithm == "native_distinct_timeline_cluster"
    assert HierarchicalMatcherService._verification_priority(
        [segment], 0, chosen, float("inf")
    ) > 1.10


def test_native_check_keeps_near_cluster_when_remote_evidence_is_weaker():
    segment = TrackSegment(0.0, 1.25, "ep", 1.0, 10.0, confidence=0.55)
    near = LineProposal("ep", 1.0, 12.5, 0.60, 9, "timeline_cluster")
    remote = LineProposal("ep", 1.0, 100.0, 0.50, 9, "timeline_cluster")

    chosen, _ = HierarchicalMatcherService._verification_alternative(
        [segment], 0, [near, remote]
    )

    assert chosen is near


def test_primary_source_start_does_not_precede_first_in_scene_evidence():
    point = _candidate(0, 6.0, 161.0)
    segment = TrackSegment(
        6.0,
        6.9,
        "episode-1",
        2.0,
        148.5,  # predicts 160.5s at the query boundary
        points=[point],
        confidence=0.53,
    )
    source_start, source_end = (
        HierarchicalMatcherService._safe_primary_source_interval(segment)
    )
    assert source_start == pytest.approx(160.98)
    assert source_end > source_start


def test_pyav_decode_preserves_vfr_pts_for_native_diff(monkeypatch):
    class FakeFrame:
        width = 90
        height = 160
        time_base = 0.01

        def __init__(self, pts):
            self.pts = pts

        def reformat(self, **kwargs):
            return self

        def to_ndarray(self):
            return np.full((64, 64), self.pts, dtype=np.uint8)

        def to_image(self):
            return Image.new("RGB", (90, 160), (self.pts, 0, 0))

    class FakeStream:
        average_rate = 30.0
        thread_type = ""

    class FakeContainer:
        streams = type("Streams", (), {"video": [FakeStream()]})()

        def decode(self, stream):
            return iter([FakeFrame(0), FakeFrame(4), FakeFrame(11), FakeFrame(21)])

        def close(self):
            pass

    fake_av = type("FakeAv", (), {"open": staticmethod(lambda path: FakeContainer())})
    monkeypatch.setattr(
        "app.services.anime_matcher.AnimeMatcherService._embed_pil_batch",
        lambda images: [np.ones(4, dtype=np.float32) for _ in images],
    )
    scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=0.22)])
    _, diff_times, _, _ = HierarchicalMatcherService._sample_query_video_av(
        fake_av, "video.mp4", scenes
    )
    assert diff_times == pytest.approx([0.04, 0.11, 0.21])


def test_alternative_proposals_are_temporally_clustered_and_diverse():
    samples = [_sample(value) for value in (0.0, 0.5, 1.0)]
    candidates = []
    for index, sample in enumerate(samples):
        candidates.append(
            [
                _candidate(index, sample.t_query, 10.0 + sample.t_query, similarity=0.70),
                _candidate(index, sample.t_query, 30.0 + sample.t_query, similarity=0.60),
                _candidate(
                    index,
                    sample.t_query,
                    50.0 + sample.t_query,
                    episode="episode-2",
                    similarity=0.55,
                ),
            ]
        )
    segment = TrackSegment(0.0, 1.1, "episode-1", 1.0, 10.0, confidence=0.7)
    proposals = HierarchicalMatcherService._line_proposals(
        segment, candidates, samples
    )
    mids = {
        (proposal.episode, round(proposal.source_at(0.5)))
        for proposal in proposals
    }
    assert ("episode-1", 30) in mids
    assert any(episode == "episode-2" for episode, _ in mids)


def test_empty_matcher_flag_is_not_a_choice(monkeypatch):
    from app.config import settings

    # An exported-but-empty value must not silently select the old matcher.
    monkeypatch.setattr(settings, "matcher_v2", False)
    monkeypatch.setenv("ATR_MATCHER_V2", "")
    assert matcher_v2_enabled() is False
    assert bounded_matcher_enabled() is True

    monkeypatch.setenv("ATR_MATCHER_V2", "   ")
    assert matcher_v2_enabled() is False

    monkeypatch.setattr(settings, "matcher_v2", True)
    monkeypatch.setenv("ATR_MATCHER_V2", "")
    assert matcher_v2_enabled() is True


def test_target_times_span_restriction_leaves_full_match_untouched():
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=4.0),
            Scene(index=1, start_time=4.0, end_time=8.0),
        ]
    )
    full = HierarchicalMatcherService._target_times(scenes)
    assert HierarchicalMatcherService._target_times(scenes, None) == full

    restricted = HierarchicalMatcherService._target_times(scenes, [(4.0, 8.0)])
    assert restricted
    assert all(4.0 <= value <= 8.0 for value in restricted)
    assert set(restricted).issubset(set(full))
    assert len(restricted) < len(full)


def test_short_span_still_receives_its_detector_probes():
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=10.0),
            Scene(index=1, start_time=10.0, end_time=10.4),
            Scene(index=2, start_time=10.4, end_time=20.0),
        ]
    )
    restricted = HierarchicalMatcherService._target_times(scenes, [(10.0, 10.4)])
    assert len(restricted) >= 3


def test_collapse_picks_the_dominant_episode_and_pins_the_span():
    # A long, well-supported stretch of episode-1 plus a two-point burst of
    # episode-2: the merged scene must resolve to one episode-1 match spanning
    # exactly the fixed boundaries.
    dominant_points = [
        _candidate(index, 2.0 + 0.25 * index, 50.0 + 0.25 * index)
        for index in range(12)
    ]
    intruder_points = [
        _candidate(index, 5.1 + 0.25 * index, 900.0 + 0.25 * index, episode="episode-2")
        for index in range(2)
    ]
    segments = [
        TrackSegment(2.0, 5.0, "episode-1", 1.0, 48.0, points=dominant_points, confidence=0.7),
        TrackSegment(5.0, 5.5, "episode-2", 1.0, 894.9, points=intruder_points, confidence=0.9),
    ]
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(segments, 2.0, 5.5)

    assert collapsed.episode == "episode-1"
    assert collapsed.q_start == 2.0
    assert collapsed.q_end == 5.5
    assert "partial_rematch_collapsed" in collapsed.doubt_reasons
    assert collapsed.source_at(2.0) == pytest.approx(50.0, abs=0.2)


def test_collapse_recomputes_geometry_doubts_against_the_fixed_span():
    # _segments_from_state measures support against its own interval, which
    # starts at 0.0 on the partial path. Dense evidence over the real span must
    # not inherit that stale "sparse_support" verdict.
    points = [
        _candidate(index, 2.0 + 0.25 * index, 50.0 + 0.25 * index)
        for index in range(12)
    ]
    segment = TrackSegment(
        0.0, 5.0, "episode-1", 1.0, 48.0,
        points=points, confidence=0.7,
        uncertain=True,
        doubt_reasons=["sparse_support", "duplicate_margin"],
    )
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(
        [segment], 2.0, 5.0
    )
    assert "sparse_support" not in collapsed.doubt_reasons
    # Non-geometric doubts still carry over.
    assert "duplicate_margin" in collapsed.doubt_reasons


def _prior(episode="episode-1", q=(0.0, 2.0), source=(48.0, 50.0), confidence=0.85):
    from app.services.hierarchical_matcher import RematchPrior

    return RematchPrior(
        episode=episode,
        q_start=q[0],
        q_end=q[1],
        source_start=source[0],
        source_end=source[1],
        confidence=confidence,
    )


def test_prior_breaks_a_near_tie_toward_the_merged_fragment():
    # Two episodes with comparable support. Without the prior the later,
    # slightly denser group wins; the prior tips it back to the fragment the
    # owner said this span continues.
    a_points = [_candidate(i, 2.0 + 0.25 * i, 50.0 + 0.25 * i) for i in range(5)]
    b_points = [
        _candidate(i, 3.5 + 0.25 * i, 900.0 + 0.25 * i, episode="episode-2")
        for i in range(6)
    ]
    segments = [
        TrackSegment(2.0, 3.5, "episode-1", 1.0, 48.0, points=a_points, confidence=0.6),
        TrackSegment(3.5, 5.0, "episode-2", 1.0, 896.5, points=b_points, confidence=0.6),
    ]

    without = HierarchicalMatcherService._collapse_to_single_segment(
        [s for s in segments], 2.0, 5.0
    )
    assert without.episode == "episode-2"

    with_prior = HierarchicalMatcherService._collapse_to_single_segment(
        segments, 2.0, 5.0, _prior(q=(0.0, 2.0), source=(48.0, 50.0))
    )
    assert with_prior.episode == "episode-1"


def test_clear_evidence_still_overrules_the_prior():
    # One weak point for the prior's episode against a dense contrary body.
    weak = [_candidate(0, 2.0, 50.0)]
    strong = [
        _candidate(i, 2.25 + 0.25 * i, 900.0 + 0.25 * i, episode="episode-2")
        for i in range(11)
    ]
    segments = [
        TrackSegment(2.0, 2.25, "episode-1", 1.0, 48.0, points=weak, confidence=0.5),
        TrackSegment(2.25, 5.0, "episode-2", 1.0, 897.75, points=strong, confidence=0.7),
    ]
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(
        segments, 2.0, 5.0, _prior()
    )
    assert collapsed.episode == "episode-2"
    assert "prior_episode_overruled" in collapsed.doubt_reasons


def test_prior_anchors_the_line_without_counting_as_confidence():
    points = [_candidate(i, 2.0 + 0.25 * i, 50.0 + 0.25 * i) for i in range(8)]
    segments = [
        TrackSegment(2.0, 4.0, "episode-1", 1.0, 48.0, points=points, confidence=0.65)
    ]
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(
        segments, 2.0, 4.0, _prior(q=(0.0, 2.0), source=(48.0, 50.0))
    )
    # The prior agrees with the evidence, so the line still lands on it.
    assert collapsed.source_at(2.0) == pytest.approx(50.0, abs=0.2)
    # Confidence is measured on real retrieval only, not the synthetic anchors.
    assert collapsed.confidence == pytest.approx(0.65, abs=0.01)


def test_prior_rescues_a_span_with_no_fresh_evidence():
    segments = [
        TrackSegment(0.0, 3.0, None, uncertain=True, doubt_reasons=["no_evidence"])
    ]
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(
        segments, 2.0, 5.0, _prior(q=(0.0, 2.0), source=(48.0, 50.0))
    )
    # Extending the previous fragment's line beats abstaining outright.
    assert collapsed.episode == "episode-1"
    assert collapsed.uncertain is True
    assert "prior_only" in collapsed.doubt_reasons
    assert collapsed.source_at(2.0) == pytest.approx(50.0, abs=0.2)


def test_no_prior_still_abstains_without_evidence():
    segments = [
        TrackSegment(0.0, 3.0, None, uncertain=True, doubt_reasons=["no_evidence"])
    ]
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(
        segments, 2.0, 5.0, None
    )
    assert collapsed.episode is None
    assert "no_evidence" in collapsed.doubt_reasons


def test_collapse_without_evidence_abstains():
    segments = [
        TrackSegment(0.0, 1.0, None, uncertain=True, doubt_reasons=["no_evidence"]),
    ]
    collapsed = HierarchicalMatcherService._collapse_to_single_segment(segments, 0.0, 1.0)
    assert collapsed.episode is None
    assert collapsed.uncertain is True
    assert collapsed.q_start == 0.0 and collapsed.q_end == 1.0


def test_rematch_scene_sync_replaces_one_match_and_preserves_the_rest(monkeypatch):
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=2.0),
            Scene(index=1, start_time=2.0, end_time=5.0),
            Scene(index=2, start_time=5.0, end_time=7.0),
        ]
    )
    existing = MatchList(
        matches=[
            SceneMatch(
                scene_index=0, episode="episode-9", start_time=1.0, end_time=3.0,
                confidence=0.9, speed_ratio=1.0, confirmed=True,
            ),
            SceneMatch(
                scene_index=1, episode="stale", start_time=0.0, end_time=1.0,
                confidence=0.1, speed_ratio=1.0, merged_from=[1, 2],
            ),
            SceneMatch(
                scene_index=2, episode="episode-9", start_time=8.0, end_time=10.0,
                confidence=0.8, speed_ratio=1.0, confirmed=True,
            ),
        ]
    )

    samples = [_sample(2.0 + 0.25 * index) for index in range(12)]
    candidates = [
        [_candidate(index, sample.t_query, 100.0 + sample.t_query)]
        for index, sample in enumerate(samples)
    ]

    monkeypatch.setattr(
        "app.services.anime_matcher.AnimeMatcherService._query_processor",
        object(),
        raising=False,
    )
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "_sample_query_video",
        classmethod(lambda cls, path, scenes_arg, spans=None: (samples, [], [], 7.0)),
    )
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "_retrieve",
        classmethod(lambda cls, samples_arg, anime: candidates),
    )

    result = HierarchicalMatcherService.rematch_scene_sync(
        "video.mp4", scenes, "anime", "series",
        scene_index=1, existing_matches=existing,
    )

    assert len(result.matches) == 3
    # Untouched siblings, including their confirmations.
    assert result.matches[0].episode == "episode-9"
    assert result.matches[0].confirmed is True
    assert result.matches[2].start_time == 8.0
    assert result.matches[2].confirmed is True
    # The target scene was actually re-matched from the faked evidence.
    assert result.matches[1].episode == "episode-1"
    assert result.matches[1].start_time == pytest.approx(102.0, abs=0.3)
    assert [match.scene_index for match in result.matches] == [0, 1, 2]


def test_rematch_scene_sync_survives_a_span_that_decoded_no_samples(monkeypatch):
    """Prior-only rescue must not reach verification, which needs a query frame."""
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=2.0),
            Scene(index=1, start_time=2.0, end_time=5.0),
        ]
    )
    existing = MatchList(
        matches=[
            SceneMatch(
                scene_index=0, episode="episode-1", start_time=48.0, end_time=50.0,
                confidence=0.85, speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=1, episode="", start_time=0.0, end_time=0.0,
                confidence=0.0, speed_ratio=1.0, was_no_match=True,
            ),
        ]
    )
    monkeypatch.setattr(
        "app.services.anime_matcher.AnimeMatcherService._query_processor",
        object(),
        raising=False,
    )
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "_sample_query_video",
        classmethod(lambda cls, path, scenes_arg, spans=None: ([], [], [], 5.0)),
    )
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "_retrieve",
        classmethod(lambda cls, samples_arg, anime: []),
    )

    result = HierarchicalMatcherService.rematch_scene_sync(
        "video.mp4", scenes, "anime", "series",
        scene_index=1, existing_matches=existing,
        prior=_prior(q=(0.0, 2.0), source=(48.0, 50.0)),
    )
    assert len(result.matches) == 2
    assert result.matches[1].episode == "episode-1"
    assert "prior_only" in result.matches[1].doubt_reasons


def test_rematch_scene_sync_rejects_an_out_of_range_index(monkeypatch):
    monkeypatch.setattr(
        "app.services.anime_matcher.AnimeMatcherService._query_processor",
        object(),
        raising=False,
    )
    scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=1.0)])
    with pytest.raises(IndexError):
        HierarchicalMatcherService.rematch_scene_sync(
            "video.mp4", scenes, "anime", None,
            scene_index=5, existing_matches=MatchList(),
        )


def test_undecoded_verification_window_does_not_reject_the_segment(monkeypatch):
    """Absence of source evidence must not read as evidence against."""
    samples = [_sample(0.5 + index) for index in range(4)]
    candidates = [
        [_candidate(index, sample.t_query, 10.0 + sample.t_query)]
        for index, sample in enumerate(samples)
    ]
    segment = TrackSegment(
        0.0, 4.0, "episode-1", 1.0, 9.5,
        points=[values[0] for values in candidates],
        confidence=0.4,
        uncertain=True,
    )
    # No episode path resolves, so nothing is decoded.
    monkeypatch.setattr(
        "app.services.anime_library.AnimeLibraryService.resolve_episode_path",
        classmethod(lambda cls, episode, library_type=None: None),
    )
    decoded = HierarchicalMatcherService._verify_ambiguous_segments(
        [segment], samples, candidates, "anime", 10.0
    )
    assert decoded == 0.0
    assert segment.episode == "episode-1"
    assert "native_rejected" not in segment.doubt_reasons
    assert "native_unavailable" in segment.doubt_reasons


# Imported late so the helpers above remain usable in environments that only
# collect the flag/target tests without the pytest package injected globally.
import pytest


class _FakeIndexManager:
    """search_batch stub returning fixed (similarity, metadata) hits per query."""

    def __init__(self, hits):
        self._hits = hits

    def search_batch(self, embeddings, top_k, threshold, series=None):
        return [list(self._hits) for _ in range(len(embeddings))]


class _FakeMetadata:
    def __init__(self, episode: str):
        self.episode = episode
        self.timestamp = 12.0
        self.series = "series"


def _patch_fake_processor(monkeypatch, hits):
    from types import SimpleNamespace

    from app.services.anime_matcher import AnimeMatcherService

    monkeypatch.setattr(
        AnimeMatcherService,
        "_query_processor",
        SimpleNamespace(index_manager=_FakeIndexManager(hits)),
        raising=False,
    )


def test_retrieve_episode_whitelist_filters_by_canonical_stem(monkeypatch):
    """Whitelist keeps path/extension variants of allowed episodes, drops the rest."""
    hits = [
        (0.9, _FakeMetadata("dir/E01.mkv")),
        (0.8, _FakeMetadata("E02")),
        (0.7, _FakeMetadata("E01")),
    ]
    _patch_fake_processor(monkeypatch, hits)
    samples = [_sample(0.0), _sample(0.25)]

    unfiltered = HierarchicalMatcherService._retrieve(samples, "series")
    assert {c.episode for c in unfiltered[0]} == {"dir/E01.mkv", "E02", "E01"}

    filtered = HierarchicalMatcherService._retrieve(
        samples, "series", episode_whitelist=frozenset({"E01"})
    )
    for per_sample in filtered:
        assert per_sample, "whitelisted hits must survive"
        assert {c.episode for c in per_sample} == {"dir/E01.mkv", "E01"}

    empty = HierarchicalMatcherService._retrieve(
        samples, "series", episode_whitelist=frozenset({"E99"})
    )
    assert all(not per_sample for per_sample in empty)


def test_merge_variant_candidates_respects_episode_whitelist(monkeypatch):
    hits = [
        (0.9, _FakeMetadata("dir/E01.mkv")),
        (0.8, _FakeMetadata("E02")),
    ]
    _patch_fake_processor(monkeypatch, hits)
    variant = QueryFrame(1.0, np.ones(4, dtype=np.float32), None, "crop")
    base = [[]]

    HierarchicalMatcherService._merge_variant_candidates(
        base, [variant], [0], "series", episode_whitelist=frozenset({"E01"})
    )
    assert base[0], "whitelisted variant hits must be merged"
    assert {c.episode for c in base[0]} == {"dir/E01.mkv"}
