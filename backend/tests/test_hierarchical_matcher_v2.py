from __future__ import annotations

import numpy as np
from PIL import Image

from app.models import MatchList, Scene, SceneList, SceneMatch
from app.services.fast_matching import bounded_matcher_enabled, matcher_v2_enabled
from app.services.hierarchical_matcher import (
    HierarchicalDiagnostics,
    HierarchicalMatcherService,
    HierarchicalResult,
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
    rank: int = 0,
    variant: str = "plain",
) -> RetrievalCandidate:
    return RetrievalCandidate(
        sample_index,
        query,
        episode,
        source,
        similarity,
        "series",
        rank,
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
            lambda cls, video_path, scenes_arg, library_type, anime_name=None: HierarchicalResult(
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
        truth = _candidate(index, sample.t_query, 10.0 + sample.t_query, similarity=0.60, rank=1)
        repeated = _candidate(
            index,
            sample.t_query,
            100.0 + index * 25.0,
            similarity=0.66,
            rank=0,
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
            rank=20,
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


def test_native_recovery_budget_exhaustion_is_deterministic(monkeypatch):
    samples = [_sample(index + 0.5) for index in range(10)]
    candidates = []
    segments = []
    for index, sample in enumerate(samples):
        candidates.append(
            [
                _candidate(index, sample.t_query, 10.0 + sample.t_query),
                _candidate(index, sample.t_query, 40.0 + sample.t_query, rank=1),
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
    ) == 7


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


# Imported late so the helpers above remain usable in environments that only
# collect the flag/target tests without the pytest package injected globally.
import pytest
