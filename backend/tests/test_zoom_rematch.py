"""Pure-logic contract for the extensive zoom search algorithm (no GPU)."""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.library_types import LibraryType
from app.models import AlternativeMatch, MatchList, Scene, SceneList, SceneMatch
from app.services import zoom_rematch
from app.services.anime_matcher import AnimeMatcherService
from app.services.hierarchical_matcher import (
    HierarchicalMatcherService,
    QueryFrame,
    RetrievalCandidate,
)
from app.services.scene_aligner import SceneAlignerService
from app.services.zoom_rematch import ZoomRematchService, _Hypothesis


def _match(episode: str, start: float, end: float, scene_index: int = 1) -> SceneMatch:
    return SceneMatch(
        scene_index=scene_index,
        episode=episode,
        start_time=start,
        end_time=end,
        confidence=0.8,
        speed_ratio=1.0,
    )


# ----------------------------------------------------------------------
# hypothesis building


def test_build_hypotheses_clusters_and_prepends_current() -> None:
    existing = _match("EP01", 100.0, 103.0)
    # Two clean lines: EP02 at intercept ~200 (3 distinct query times) and a
    # single-hit lookalike in EP05.
    hits = [
        (10.0, "EP02", 210.0, 0.5),
        (11.0, "EP02", 211.1, 0.55),
        (12.0, "EP02", 212.0, 0.6),
        (11.0, "EP05", 500.0, 0.9),
    ]
    hypotheses = ZoomRematchService._build_hypotheses(
        hits, existing, q_start=10.0, q_end=13.0, span=3.0
    )
    assert hypotheses[0].is_current
    assert hypotheses[0].episode == "EP01"
    # Multi-hit line ranks before the single-hit lookalike despite its
    # weaker raw similarity.
    assert hypotheses[1].episode == "EP02"
    assert hypotheses[1].support == 3
    assert hypotheses[2].episode == "EP05"


def test_build_hypotheses_skips_cluster_equal_to_current() -> None:
    existing = _match("EP01", 210.0, 213.0)
    hits = [
        (10.0, "EP01", 210.0, 0.9),
        (12.0, "EP01", 212.0, 0.9),
    ]
    hypotheses = ZoomRematchService._build_hypotheses(
        hits, existing, q_start=10.0, q_end=13.0, span=3.0
    )
    # The cluster rediscovers the current line: only hypothesis 0 remains.
    assert len(hypotheses) == 1
    assert hypotheses[0].is_current


def test_build_hypotheses_without_existing_match() -> None:
    existing = _match("", 0.0, 0.0)
    hits = [(10.0, "EP02", 210.0, 0.5), (12.0, "EP02", 212.0, 0.5)]
    hypotheses = ZoomRematchService._build_hypotheses(
        hits, existing, q_start=10.0, q_end=13.0, span=3.0
    )
    assert len(hypotheses) == 1
    assert not hypotheses[0].is_current


def test_build_hypotheses_prioritizes_supported_track_inside_neighbor_corridor(
) -> None:
    existing = _match("EP01", 500.0, 501.0)
    context = MatchList(
        matches=[
            _match("EP04", 220.0, 224.0, scene_index=0),
            existing,
            _match("EP04", 249.0, 251.0, scene_index=2),
        ]
    )
    hits: list[tuple[float, str, float, float]] = []
    for t_query in (10.0, 10.25, 10.5, 10.75, 11.0):
        # Stronger raw duplicate clusters would exhaust the old top-five
        # budget before native scoring ever saw the zoomed contextual track.
        hits.append((t_query, "EP01", 500.0 + t_query, 0.42))
        hits.append((t_query, "EP09", 800.0 + t_query, 0.41))
    for t_query, t_source in ((10.0, 229.0), (10.5, 229.5), (11.0, 230.0)):
        hits.append((t_query, "EP04", t_source, 0.28))

    hypotheses = ZoomRematchService._build_hypotheses(
        hits,
        existing,
        q_start=10.0,
        q_end=11.0,
        span=1.0,
        context_matches=context,
    )

    assert hypotheses[0].is_current
    assert hypotheses[1].episode == "EP04"
    assert hypotheses[1].source_at(10.5) == pytest.approx(229.5)


def test_context_corridor_requires_matching_resolved_neighbors() -> None:
    context = MatchList(
        matches=[
            _match("EP04", 220.0, 224.0, scene_index=0),
            _match("EP01", 500.0, 501.0, scene_index=1),
            _match("EP05", 249.0, 251.0, scene_index=2),
        ]
    )
    assert ZoomRematchService._context_corridor(context, 1) is None


def test_fit_cluster_clamps_absurd_slopes() -> None:
    # Nearly-vertical evidence would fit a slope far outside any plausible
    # speed ratio; the fit must fall back to a=1 with a median intercept.
    cluster = {
        "episode": "EP02",
        "points": [(10.0, 100.0), (10.2, 300.0), (10.4, 500.0)],
        "hits": {10.0, 10.2, 10.4},
        "max_sim": 0.5,
    }
    hypothesis = ZoomRematchService._fit_cluster(cluster)
    assert hypothesis is not None
    assert hypothesis.a == 1.0


def test_fit_cluster_does_not_infer_rate_from_sub_second_static_shot() -> None:
    cluster = {
        "episode": "EP04",
        "points": [(10.0, 229.0), (10.4, 229.0), (10.8, 229.5)],
        "hits": {10.0, 10.4, 10.8},
        "max_sim": 0.31,
    }
    hypothesis = ZoomRematchService._fit_cluster(cluster)
    assert hypothesis is not None
    assert hypothesis.a == 1.0


# ----------------------------------------------------------------------
# decision

def _best(episode: str, a: float, b: float, score: float, is_current=False) -> dict:
    return {
        "score": score,
        "delta": 0.0,
        "hypothesis": _Hypothesis(
            episode=episode, a=a, b=b, support=3, max_sim=0.6, is_current=is_current
        ),
        "doubt": [],
        "geometry": 1.6,
    }


def _decide(best, current_score, existing, *, scored_results=None):
    return ZoomRematchService._decide(
        best,
        current_score,
        existing,
        scene_index=1,
        q_start=10.0,
        q_end=13.0,
        span=3.0,
        scored=2,
        deadline_hit=False,
        run_started=0.0,
        scored_results=scored_results,
    )


def test_decide_accepts_clear_winner_and_keeps_old_as_alternative() -> None:
    existing = _match("EP01", 100.0, 103.0)
    outcome = _decide(_best("EP02", 1.0, 200.0, score=0.62), 0.35, existing)
    assert outcome.changed
    assert outcome.new_match is not None
    assert outcome.new_match.episode == "EP02"
    assert outcome.new_match.start_time == pytest.approx(210.0)
    assert outcome.new_match.end_time == pytest.approx(213.0)
    assert outcome.new_match.speed_ratio == pytest.approx(1.0)
    assert "zoom_search" in outcome.new_match.doubt_reasons
    alternatives = outcome.new_match.alternatives
    assert alternatives[0].algorithm == "pre_zoom_search"
    assert alternatives[0].episode == "EP01"


def test_decide_rejects_winner_below_margin() -> None:
    existing = _match("EP01", 100.0, 103.0)
    outcome = _decide(_best("EP02", 1.0, 200.0, score=0.52), 0.50, existing)
    assert not outcome.changed
    assert outcome.new_match is None


def test_decide_rejects_winner_below_floor_without_current_score() -> None:
    existing = _match("", 0.0, 0.0)
    outcome = _decide(_best("EP02", 1.0, 200.0, score=0.25), None, existing)
    assert not outcome.changed


def test_decide_sub_tolerance_shift_is_confirmation() -> None:
    existing = _match("EP01", 209.8, 212.9)
    outcome = _decide(_best("EP01", 1.0, 200.0, score=0.7), 0.4, existing)
    assert not outcome.changed
    assert outcome.new_match is None
    assert outcome.detail == "existing match confirmed"


def test_decide_current_hypothesis_never_reports_change() -> None:
    existing = _match("EP01", 100.0, 103.0)
    outcome = _decide(
        _best("EP01", 1.0, 90.0, score=0.7, is_current=True), 0.7, existing
    )
    assert not outcome.changed


def test_decide_exposes_scored_candidate_even_when_primary_is_confirmed() -> None:
    existing = _match("EP01", 100.0, 103.0)
    alternative = _best("EP08", 1.0, 300.0, score=0.40)
    outcome = _decide(
        _best("EP01", 1.0, 90.0, score=0.70, is_current=True),
        0.70,
        existing,
        scored_results=[alternative],
    )
    assert not outcome.changed
    assert len(outcome.alternatives) == 1
    assert outcome.alternatives[0].episode == "EP08"
    assert outcome.alternatives[0].algorithm == "zoom_search_center_1.6x"


def test_registered_result_biases_one_native_frame_past_uncertain_leadin() -> None:
    result = _best("EP04", 1.0, 200.0, score=0.80)
    result["geometry"] = (0.2, 0.2, 0.8, 0.8)
    start, end = ZoomRematchService._aligned_interval(
        result, q_start=10.0, q_end=13.0, span=3.0
    )
    guard = ZoomRematchService.REGISTERED_ALIGNMENT_GUARD_S
    assert start == pytest.approx(210.0 + guard)
    assert end == pytest.approx(213.0 + guard)


def test_merge_alternatives_has_no_count_cap_but_dedupes_temporal_clusters() -> None:
    primary = _match("EP01", 100.0, 101.0)
    candidates = [
        AlternativeMatch(
            episode=f"EP{i:02d}",
            start_time=float(i * 10),
            end_time=float(i * 10 + 1),
            confidence=0.5,
            speed_ratio=1.0,
            algorithm="zoom_search",
        )
        for i in range(2, 12)
    ]
    candidates.append(
        AlternativeMatch(
            episode="EP02",
            start_time=20.5,
            end_time=21.5,
            confidence=0.9,
            speed_ratio=1.0,
            algorithm="zoom_search_registered",
        )
    )
    merged = ZoomRematchService.merge_alternatives(primary, candidates)
    assert len(merged) == 10
    assert sum(candidate.episode == "EP02" for candidate in merged) == 1


# ----------------------------------------------------------------------
# end-to-end (all heavy primitives faked)


class _FakeCache:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    def probe_frames(self, episode: str, pred: float) -> list:
        return []

    def close(self) -> None:
        self.closed = True


def _fake_samples(times: list[float]) -> list[QueryFrame]:
    rng = np.random.default_rng(7)
    return [
        QueryFrame(t, rng.normal(size=8).astype(np.float32), None) for t in times
    ]


def _install_fakes(monkeypatch, *, scores: dict[str, float]) -> dict:
    """Fake sampling/retrieval/scoring around the real orchestration."""
    calls = {"scored_episodes": [], "deep_searched": False}
    samples = _fake_samples([10.0, 10.5, 11.0, 11.5, 12.0, 12.5])

    monkeypatch.setattr(
        HierarchicalMatcherService,
        "_sample_query_video",
        classmethod(lambda cls, path, scenes, spans=None: (samples, [], [], 13.0)),
    )

    def retrieve(cls, sample_list, anime_name, episode_whitelist=None):
        return [
            [
                RetrievalCandidate(
                    sample_index=i,
                    t_query=sample.t_query,
                    episode="EP02",
                    t_source=200.0 + sample.t_query,
                    similarity=0.5,
                    series="S",
                )
            ]
            for i, sample in enumerate(sample_list)
        ]

    monkeypatch.setattr(
        HierarchicalMatcherService, "_retrieve", classmethod(retrieve)
    )

    def search_batch(embeddings, k, _none, series=None):
        calls["deep_searched"] = True
        meta = types.SimpleNamespace(episode="EP09", timestamp=900.0, series="S")
        return [[(0.42, meta)] for _ in range(len(embeddings))]

    fake_processor = types.SimpleNamespace(
        index_manager=types.SimpleNamespace(search_batch=search_batch)
    )
    monkeypatch.setattr(AnimeMatcherService, "_query_processor", fake_processor)
    monkeypatch.setattr(zoom_rematch, "_WindowEmbedCache", _FakeCache)

    def score_line(cls, q_mids, source_at, cache, episode, zoom, sweep=0.6):
        calls["scored_episodes"].append(episode)
        score = scores.get(episode)
        if score is None:
            return None
        return (score, 0.0, np.zeros((1, 8)))

    monkeypatch.setattr(
        SceneAlignerService, "_zoom_sscd_score_line", classmethod(score_line)
    )
    monkeypatch.setattr(
        SceneAlignerService,
        "_footprint_rect",
        classmethod(lambda cls, q, s: None),
    )
    from app.services import fast_matching

    monkeypatch.setattr(fast_matching, "fast_r2_enabled", lambda: False)
    return calls


def _run(existing: SceneMatch, budget_s: float | None = None) -> tuple:
    scenes = SceneList(
        scenes=[Scene(index=0, start_time=0.0, end_time=10.0), Scene(index=1, start_time=10.0, end_time=13.0)]
    )
    event = threading.Event()
    outcome = ZoomRematchService.search_scene_sync(
        Path("/nonexistent/video.mp4"),
        scenes,
        LibraryType.ANIME,
        "Anime",
        scene_index=1,
        existing_match=existing,
        cancel_event=event,
        budget_s=budget_s,
    )
    return outcome, event


def test_search_replaces_wrong_match_via_deep_scoring(monkeypatch) -> None:
    calls = _install_fakes(
        monkeypatch, scores={"EP01": 0.30, "EP02": 0.68, "EP09": 0.20}
    )
    outcome, _ = _run(_match("EP01", 100.0, 103.0))
    assert calls["deep_searched"]
    assert outcome.changed
    assert outcome.new_match is not None
    assert outcome.new_match.episode == "EP02"
    assert outcome.current_score == pytest.approx(0.30)
    # Registration always failed, so scoring fell back to the extended
    # center-zoom sweep — each scored episode appears once per zoom level.
    assert set(calls["scored_episodes"]) == {"EP01", "EP02", "EP09"}


def test_search_confirms_when_current_wins(monkeypatch) -> None:
    _install_fakes(monkeypatch, scores={"EP01": 0.80, "EP02": 0.40, "EP09": 0.20})
    outcome, _ = _run(_match("EP01", 100.0, 103.0))
    assert not outcome.changed
    assert outcome.new_match is None
    assert outcome.detail == "existing match confirmed"


def test_search_zero_budget_hits_deadline(monkeypatch) -> None:
    _install_fakes(monkeypatch, scores={"EP02": 0.9})
    outcome, _ = _run(_match("EP01", 100.0, 103.0), budget_s=0.0)
    assert outcome.deadline_hit
    assert not outcome.changed
    assert outcome.hypotheses_scored == 0


def test_search_cancel_event_short_circuits(monkeypatch) -> None:
    _install_fakes(monkeypatch, scores={"EP02": 0.9})
    scenes = SceneList(
        scenes=[Scene(index=0, start_time=0.0, end_time=10.0), Scene(index=1, start_time=10.0, end_time=13.0)]
    )
    event = threading.Event()
    event.set()
    outcome = ZoomRematchService.search_scene_sync(
        Path("/nonexistent/video.mp4"),
        scenes,
        LibraryType.ANIME,
        "Anime",
        scene_index=1,
        existing_match=_match("EP01", 100.0, 103.0),
        cancel_event=event,
    )
    assert not outcome.changed
    assert outcome.hypotheses_scored == 0
    assert not outcome.deadline_hit


def test_search_without_query_frames(monkeypatch) -> None:
    _install_fakes(monkeypatch, scores={})
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "_sample_query_video",
        classmethod(lambda cls, path, scenes, spans=None: ([], [], [], 13.0)),
    )
    outcome, _ = _run(_match("EP01", 100.0, 103.0))
    assert not outcome.changed
    assert "no query frames" in outcome.detail
