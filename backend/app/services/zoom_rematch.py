"""Per-scene extensive zoom search.

The bounded matcher is tuned for speed and its only geometric handling is
query-side letterbox/16:9 crop variants.  Rarely an edit zooms a scene far
beyond that (or animates a zoom from point A to point B), and the true
instance never surfaces in the retrieval top-K — the query's crop geometry
biases the ranking.  This module runs a deliberately thorough single-scene
pass on demand: deep-tail FAISS retrieval, then registered-footprint /
zoom-swept SSCD arbitration reusing the legacy matcher's proven geometric
primitives (:class:`SceneAlignerService`).

The pass is manual-trigger only and budgeted (default 30s wall) — callers
run it through :class:`ZoomSearchService`, never inside the standard
matching pipeline.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..library_types import LibraryType
from ..models import AlternativeMatch, MatchList, SceneMatch, SceneList
from .anime_matcher import AnimeMatcherService
from .episode_names import canonical_episode_stem
from .hierarchical_matcher import HierarchicalMatcherService, QueryFrame
from .scene_aligner import (
    VERIFY_DECODE_FPS,
    SceneAlignerService,
    _WindowEmbedCache,
)

logger = logging.getLogger("uvicorn.error")


class _Interrupted(Exception):
    """Raised at a checkpoint when the budget or a cancellation cuts the run."""


@dataclass(frozen=True)
class ZoomSearchOutcome:
    changed: bool
    old_match: SceneMatch
    new_match: SceneMatch | None
    best_score: float
    current_score: float | None
    hypotheses_scored: int
    deadline_hit: bool
    detail: str
    # Every distinct hypothesis that completed native zoom scoring.  The job
    # layer persists these even when no hypothesis is strong enough to replace
    # the primary, so manual timing selection benefits from the expensive work.
    alternatives: tuple[AlternativeMatch, ...] = ()


@dataclass
class _Hypothesis:
    episode: str
    a: float
    b: float
    support: int
    max_sim: float
    is_current: bool = False

    def source_at(self, t_query: float) -> float:
        return self.a * t_query + self.b


@dataclass(frozen=True)
class _ContextCorridor:
    """A source interval certified by the resolved matches on both sides."""

    episode: str
    lower: float
    upper: float
    target: float


class ZoomRematchService:
    """One-scene zoom-aware re-match. Sync + GPU-bound: run in an executor
    while holding the matching lock and a heavy slot, like every other
    matcher entry point."""

    # Hard wall-clock budget. Owner decision 2026-08-14: ~30s per scene so a
    # handful of queued searches drain at a scrolling pace.
    BUDGET_S = float(os.environ.get("ATR_ZOOM_SEARCH_BUDGET_S", "30"))
    # Center-zoom fallback sweep when registration fails; extends the legacy
    # _CANDIDATE_ZOOMS ceiling (1.45) because the whole point of this pass is
    # "zoomed a lot".
    EXTENDED_ZOOMS: tuple[float, ...] = (1.0, 1.3, 1.6, 1.8, 2.0)
    # Deep-tail retrieval is deliberately broad.  A strong center zoom can put
    # the full source frame hundreds of places down the raw SSCD ranking; FAISS
    # is cheap and registered native scoring below, not this ranking, decides.
    DEEP_K = 500
    DEEP_COS_FLOOR = 0.25
    MAX_HYPOTHESES = 12
    MAX_CONTEXT_HYPOTHESES = 3
    EPISODE_DIVERSITY_SLOTS = 4
    CONTEXT_CORRIDOR_TOLERANCE_S = 2.0
    CONTEXT_MAX_SOURCE_GAP_S = 90.0
    MIN_RATE_FIT_QUERY_SPAN_S = 1.0
    ALTERNATIVE_SCORE_FLOOR = 0.25
    # Registered crop scoring is quantized to the native 12fps verification
    # grid.  Bias its emitted interval forward by one grid frame: a tiny loss
    # of uncertain opening content is preferable to a previous-shot/black
    # flash in the rendered clip.
    REGISTERED_ALIGNMENT_GUARD_S = 1.0 / VERIFY_DECODE_FPS
    # A result is a "change" when the episode differs or an endpoint moves
    # by more than this; anything smaller is a confirmation.
    CHANGE_EPS_S = 0.5
    # A non-current hypothesis must clear the absolute floor AND beat the
    # current match's own registered score by the margin.
    ACCEPT_FLOOR = 0.30
    ACCEPT_MARGIN = 0.04
    # Motion zoom (A→B): start/end footprint areas differing by more than
    # this ratio switch scoring to per-half rects.
    MOTION_AREA_RATIO = 0.15

    @classmethod
    def search_scene_sync(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_type: LibraryType | str,
        anime_name: str | None,
        *,
        scene_index: int,
        existing_match: SceneMatch,
        cancel_event: threading.Event,
        context_matches: MatchList | None = None,
        budget_s: float | None = None,
    ) -> ZoomSearchOutcome:
        if AnimeMatcherService._query_processor is None:
            raise RuntimeError(
                "anime_searcher must be initialized before zoom search"
            )
        if not 0 <= scene_index < len(scenes.scenes):
            raise IndexError(f"scene_index {scene_index} out of range")

        # Native window decode below opens PyNv source captures; size the
        # NVDEC session budget exactly like the other partial path.
        from .fast_matching import fast_r2_enabled

        if fast_r2_enabled():
            try:
                from . import pynv_decode
                from .indexation_queue import indexation_queue

                pynv_decode.set_session_budget(
                    3 if indexation_queue.gpu_slots_in_use() <= 1 else 2
                )
            except Exception:
                pass

        run_started = time.perf_counter()
        deadline = run_started + (budget_s if budget_s is not None else cls.BUDGET_S)
        deadline_hit = False

        def checkpoint() -> None:
            nonlocal deadline_hit
            if cancel_event.is_set():
                raise _Interrupted("cancelled")
            if time.perf_counter() > deadline:
                deadline_hit = True
                raise _Interrupted("deadline")

        scene = scenes.scenes[scene_index]
        q_start = float(scene.start_time)
        q_end = float(max(scene.end_time, scene.start_time + 1e-3))
        span = q_end - q_start

        samples, _, _, _ = HierarchicalMatcherService._sample_query_video(
            video_path, scenes, [(q_start, q_end)]
        )
        samples = [
            sample
            for sample in samples
            if q_start - 1e-9 <= sample.t_query <= q_end + 1e-9
        ]
        if not samples:
            return ZoomSearchOutcome(
                changed=False,
                old_match=existing_match,
                new_match=None,
                best_score=0.0,
                current_score=None,
                hypotheses_scored=0,
                deadline_hit=False,
                detail="no query frames decoded for this scene",
            )

        anchors = cls._pick_anchors(samples)
        q_mids = [(sample.t_query, sample.embedding) for sample in anchors]
        q_grays = {
            sample.t_query: SceneAlignerService._small_gray(sample.preview)
            for sample in anchors
            if sample.preview is not None
        }
        # Query-side scale velocity: evidence that the edit itself zooms
        # over time (motion zoom), measured on the query's own frames.
        sv_q = SceneAlignerService._zoom_rate(sorted(q_grays.items()))

        hits = cls._collect_hits(samples, anchors, anime_name)
        hypotheses = cls._build_hypotheses(
            hits,
            existing_match,
            q_start,
            q_end,
            span,
            context_matches=context_matches,
        )
        if not hypotheses:
            return ZoomSearchOutcome(
                changed=False,
                old_match=existing_match,
                new_match=None,
                best_score=0.0,
                current_score=None,
                hypotheses_scored=0,
                deadline_hit=False,
                detail="no candidate source positions retrieved",
            )

        cache = _WindowEmbedCache(
            library_type, SceneAlignerService._zoom_crop, VERIFY_DECODE_FPS
        )
        best: dict | None = None
        current_score: float | None = None
        scored = 0
        scored_results: list[dict] = []
        try:
            for hypothesis in hypotheses:
                try:
                    checkpoint()
                    result = cls._score_hypothesis(
                        hypothesis, q_mids, q_grays, cache, checkpoint
                    )
                except _Interrupted:
                    break
                if result is None:
                    continue
                scored += 1
                scored_results.append(result)
                if hypothesis.is_current:
                    current_score = result["score"]
                if best is None or result["score"] > best["score"]:
                    best = result

            # Motion-consistency tiebreak: when the winner displaces the
            # current match, a source that zooms like the query zooms is
            # extra evidence; a flat source under a zooming query is not
            # disqualifying (the edit's own motion zoom explains it) but
            # loses the tiny bonus.
            if (
                best is not None
                and not best["hypothesis"].is_current
                and sv_q is not None
                and time.perf_counter() < run_started + 0.8 * (deadline - run_started)
            ):
                best["score"] += cls._motion_consistency_bonus(
                    best, sv_q, cache, 0.5 * (q_start + q_end)
                )
        finally:
            cache.close()

        return cls._decide(
            best,
            current_score,
            existing_match,
            scene_index,
            q_start,
            q_end,
            span,
            scored,
            deadline_hit,
            run_started,
            scored_results=scored_results,
        )

    # ------------------------------------------------------------------
    # sampling & retrieval

    @staticmethod
    def _pick_anchors(samples: list[QueryFrame], count: int = 7) -> list[QueryFrame]:
        """Up to ``count`` samples spread evenly across the scene span."""
        if len(samples) <= count:
            return list(samples)
        indices = np.linspace(0, len(samples) - 1, count).round().astype(int)
        return [samples[index] for index in sorted(set(int(i) for i in indices))]

    @classmethod
    def _collect_hits(
        cls,
        samples: list[QueryFrame],
        anchors: list[QueryFrame],
        anime_name: str | None,
    ) -> list[tuple[float, str, float, float]]:
        """(t_query, episode, t_source, similarity) from the standard
        retrieval plus a deep-tail search over the anchor embeddings."""
        hits: list[tuple[float, str, float, float]] = []
        for per_sample in HierarchicalMatcherService._retrieve(samples, anime_name):
            for candidate in per_sample:
                hits.append(
                    (
                        candidate.t_query,
                        candidate.episode,
                        candidate.t_source,
                        candidate.similarity,
                    )
                )

        processor = AnimeMatcherService._query_processor
        if processor is not None and anchors:
            embeddings = np.stack(
                [anchor.embedding for anchor in anchors]
            ).astype(np.float32, copy=False)
            started = time.perf_counter()
            raw = processor.index_manager.search_batch(
                embeddings, cls.DEEP_K, None, series=anime_name
            )
            AnimeMatcherService._record_runtime_stat(
                "faiss_search_seconds", time.perf_counter() - started
            )
            AnimeMatcherService._record_runtime_stat(
                "faiss_search_queries", len(anchors)
            )
            for anchor, values in zip(anchors, raw, strict=False):
                for similarity, metadata in values:
                    if float(similarity) < cls.DEEP_COS_FLOOR:
                        continue
                    hits.append(
                        (
                            anchor.t_query,
                            metadata.episode,
                            float(metadata.timestamp),
                            float(similarity),
                        )
                    )
        return hits

    @classmethod
    def _build_hypotheses(
        cls,
        hits: list[tuple[float, str, float, float]],
        existing_match: SceneMatch,
        q_start: float,
        q_end: float,
        span: float,
        *,
        context_matches: MatchList | None = None,
    ) -> list[_Hypothesis]:
        """Cluster hits into (episode, source-line) hypotheses, the current
        match's own line first so a confirmation gets a fair score."""
        clusters: dict[tuple[str, int], dict] = {}
        for t_query, episode, t_source, similarity in hits:
            intercept = t_source - t_query
            key = (episode, int(round(intercept / 2.0)))
            cluster = clusters.get(key)
            if cluster is None:
                clusters[key] = {
                    "episode": episode,
                    "points": [(t_query, t_source)],
                    "hits": {round(t_query, 3)},
                    "max_sim": similarity,
                }
            else:
                cluster["points"].append((t_query, t_source))
                cluster["hits"].add(round(t_query, 3))
                cluster["max_sim"] = max(cluster["max_sim"], similarity)

        current: _Hypothesis | None = None
        if (
            existing_match.episode
            and existing_match.end_time > existing_match.start_time
        ):
            a0 = (existing_match.end_time - existing_match.start_time) / max(
                span, 1e-6
            )
            current = _Hypothesis(
                episode=existing_match.episode,
                a=a0,
                b=existing_match.start_time - a0 * q_start,
                support=0,
                max_sim=0.0,
                is_current=True,
            )

        candidates: list[_Hypothesis] = []
        for cluster in clusters.values():
            hypothesis = cls._fit_cluster(cluster)
            if hypothesis is None:
                continue
            if current is not None and cls._same_line(
                hypothesis, current, q_start, q_end
            ):
                # This cluster is the current match rediscovered — scoring
                # it separately would double-spend the budget.
                continue
            candidates.append(hypothesis)
        candidates.sort(key=lambda h: (h.support, h.max_sim), reverse=True)

        # Distinct-query-time support separates real lines from single-frame
        # lookalikes; singles are kept only while multi-hit lines are scarce.
        strong = [c for c in candidates if c.support >= 2]
        weak = [c for c in candidates if c.support < 2]
        ranked = strong + weak
        capacity = cls.MAX_HYPOTHESES - (1 if current else 0)
        selected: list[_Hypothesis] = []

        def append_distinct(hypothesis: _Hypothesis) -> bool:
            if len(selected) >= capacity:
                return False
            if any(
                cls._same_line(hypothesis, chosen, q_start, q_end)
                for chosen in selected
            ):
                return False
            selected.append(hypothesis)
            return True

        # A scene bracketed by two resolved matches from the same episode has
        # unusually reliable context.  Prioritise deep-tail tracks inside that
        # source corridor, but still require support from two query positions.
        # This is especially important for heavily zoomed static shots whose
        # raw full-frame embeddings rank poorly.
        corridor = cls._context_corridor(
            context_matches, existing_match.scene_index
        )
        if corridor is not None:
            contextual = [
                candidate
                for candidate in ranked
                if candidate.support >= 2
                and canonical_episode_stem(candidate.episode)
                == canonical_episode_stem(corridor.episode)
                and corridor.lower - cls.CONTEXT_CORRIDOR_TOLERANCE_S
                <= candidate.source_at(0.5 * (q_start + q_end))
                <= corridor.upper + cls.CONTEXT_CORRIDOR_TOLERANCE_S
            ]
            contextual.sort(
                key=lambda candidate: (
                    candidate.support,
                    candidate.max_sim,
                    -abs(
                        candidate.source_at(0.5 * (q_start + q_end))
                        - corridor.target
                    ),
                ),
                reverse=True,
            )
            for candidate in contextual[: cls.MAX_CONTEXT_HYPOTHESES]:
                append_distinct(candidate)

        # Reserve a few slots for different episodes before filling by raw
        # support.  This costs no extra retrieval and makes the manual results
        # materially more useful on repeated openings and generic close-ups.
        seen_episodes = {
            canonical_episode_stem(value.episode)
            for value in ([current] if current else []) + selected
        }
        for candidate in ranked:
            episode_key = canonical_episode_stem(candidate.episode)
            if episode_key in seen_episodes:
                continue
            if append_distinct(candidate):
                seen_episodes.add(episode_key)
            if len(seen_episodes) >= cls.EPISODE_DIVERSITY_SLOTS:
                break

        for candidate in ranked:
            if len(selected) >= capacity:
                break
            append_distinct(candidate)

        return ([current] if current else []) + selected

    @classmethod
    def _context_corridor(
        cls,
        context_matches: MatchList | None,
        scene_index: int,
    ) -> _ContextCorridor | None:
        if context_matches is None:
            return None
        resolved = [
            match
            for match in context_matches.matches
            if match.episode
            and match.confidence > 0
            and match.end_time > match.start_time
        ]
        previous = max(
            (match for match in resolved if match.scene_index < scene_index),
            key=lambda match: match.scene_index,
            default=None,
        )
        following = min(
            (match for match in resolved if match.scene_index > scene_index),
            key=lambda match: match.scene_index,
            default=None,
        )
        if previous is None or following is None:
            return None
        if canonical_episode_stem(previous.episode) != canonical_episode_stem(
            following.episode
        ):
            return None
        lower = float(previous.end_time)
        upper = float(following.start_time)
        gap = upper - lower
        if (
            gap < -cls.CONTEXT_CORRIDOR_TOLERANCE_S
            or gap > cls.CONTEXT_MAX_SOURCE_GAP_S
        ):
            return None
        if upper < lower:
            lower, upper = upper, lower
        return _ContextCorridor(
            episode=previous.episode,
            lower=lower,
            upper=upper,
            target=0.5 * (lower + upper),
        )

    @staticmethod
    def _fit_cluster(cluster: dict) -> _Hypothesis | None:
        points = cluster["points"]
        if not points:
            return None
        t_q = np.array([p[0] for p in points])
        t_s = np.array([p[1] for p in points])
        a = 1.0
        if (
            len(points) >= 3
            and float(t_q.max() - t_q.min())
            >= ZoomRematchService.MIN_RATE_FIT_QUERY_SPAN_S
        ):
            design = np.vstack([t_q, np.ones(len(points))]).T
            solution, *_ = np.linalg.lstsq(design, t_s, rcond=None)
            fitted = float(solution[0])
            # Slopes outside the plausible speed-ratio window are fit noise
            # from a thin cluster, not evidence of extreme retiming.
            if 0.3 <= fitted <= 3.0:
                a = fitted
        b = float(np.median(t_s - a * t_q))
        return _Hypothesis(
            episode=cluster["episode"],
            a=a,
            b=b,
            support=len(cluster["hits"]),
            max_sim=float(cluster["max_sim"]),
        )

    @staticmethod
    def _same_line(
        left: _Hypothesis, right: _Hypothesis, q_start: float, q_end: float
    ) -> bool:
        if canonical_episode_stem(left.episode) != canonical_episode_stem(
            right.episode
        ):
            return False
        mid = 0.5 * (q_start + q_end)
        return abs((left.a * mid + left.b) - (right.a * mid + right.b)) < 2.0

    # ------------------------------------------------------------------
    # scoring

    @classmethod
    def _score_hypothesis(
        cls,
        hypothesis: _Hypothesis,
        q_mids: list[tuple[float, np.ndarray]],
        q_grays: dict[float, np.ndarray],
        cache: _WindowEmbedCache,
        checkpoint,
    ) -> dict | None:
        line_fn = lambda t, _a=hypothesis.a, _b=hypothesis.b: _a * t + _b  # noqa: E731
        anchor_times = sorted(q_grays)
        rects: dict[str, tuple | None] = {"start": None, "mid": None, "end": None}
        if anchor_times:
            probes = {
                "start": anchor_times[0],
                "mid": anchor_times[len(anchor_times) // 2],
                "end": anchor_times[-1],
            }
            for name, t_k in probes.items():
                checkpoint()
                rects[name] = cls._register_at(
                    hypothesis.episode, line_fn(t_k), q_grays[t_k], cache
                )

        doubt: list[str] = []
        result: tuple[float, float, np.ndarray] | None = None
        used_geometry: object = None

        if cls._is_motion_zoom(rects["start"], rects["end"]):
            checkpoint()
            result = cls._score_motion(
                q_mids, line_fn, cache, hypothesis.episode, rects, doubt
            )
            used_geometry = "motion"
        if result is None:
            rect = rects["mid"] or rects["start"] or rects["end"]
            if rect is not None:
                checkpoint()
                result = SceneAlignerService._zoom_sscd_score_line(
                    q_mids, line_fn, cache, hypothesis.episode, rect, sweep=0.8
                )
                used_geometry = rect
        if result is None:
            # Registration failed everywhere: sweep center zooms instead.
            for zoom in cls.EXTENDED_ZOOMS:
                checkpoint()
                candidate = SceneAlignerService._zoom_sscd_score_line(
                    q_mids, line_fn, cache, hypothesis.episode, zoom, sweep=0.8
                )
                if candidate is not None and (
                    result is None or candidate[0] > result[0]
                ):
                    result = candidate
                    used_geometry = zoom
            # Rect retry at the scalar sweep's own best alignment: the mid
            # probe may have landed in another shot (legacy pattern).
            if result is not None and anchor_times:
                checkpoint()
                t_mid = anchor_times[len(anchor_times) // 2]
                rect = cls._register_at(
                    hypothesis.episode,
                    line_fn(t_mid) + result[1],
                    q_grays[t_mid],
                    cache,
                )
                if rect is not None:
                    retried = SceneAlignerService._zoom_sscd_score_line(
                        q_mids, line_fn, cache, hypothesis.episode, rect, sweep=0.8
                    )
                    if retried is not None and retried[0] > result[0]:
                        result = retried
                        used_geometry = rect
        if result is None:
            return None
        score, delta, _ = result
        return {
            "score": float(score),
            "delta": float(delta),
            "hypothesis": hypothesis,
            "doubt": doubt,
            "geometry": used_geometry,
        }

    @classmethod
    def _register_at(
        cls,
        episode: str,
        pred: float,
        q_gray: np.ndarray,
        cache: _WindowEmbedCache,
    ) -> tuple | None:
        frames = cache.probe_frames(episode, pred)
        for _, image in sorted(frames, key=lambda frame: abs(frame[0] - pred))[:2]:
            rect = SceneAlignerService._footprint_rect(
                q_gray, SceneAlignerService._small_gray(image)
            )
            if rect is not None:
                return rect
        return None

    @classmethod
    def _is_motion_zoom(cls, rect_start, rect_end) -> bool:
        if rect_start is None or rect_end is None:
            return False

        def area(rect: tuple) -> float:
            return max(1e-6, (rect[2] - rect[0]) * (rect[3] - rect[1]))

        ratio = area(rect_start) / area(rect_end)
        return abs(1.0 - min(ratio, 1.0 / ratio)) > cls.MOTION_AREA_RATIO

    @classmethod
    def _score_motion(
        cls,
        q_mids: list[tuple[float, np.ndarray]],
        line_fn,
        cache: _WindowEmbedCache,
        episode: str,
        rects: dict,
        doubt: list[str],
    ) -> tuple[float, float, np.ndarray] | None:
        """Score a motion-zoomed edit: the footprint at the scene's start
        differs from the end's, so each half is scored under its own
        registered geometry and the halves share one alignment line."""
        half = len(q_mids) // 2 or 1
        first = SceneAlignerService._zoom_sscd_score_line(
            q_mids[:half], line_fn, cache, episode, rects["start"], sweep=0.8
        )
        second = SceneAlignerService._zoom_sscd_score_line(
            q_mids[half:], line_fn, cache, episode, rects["end"], sweep=0.8
        )
        if first is None and second is None:
            return None
        if first is None or second is None:
            return first or second
        w_first, w_second = half, max(1, len(q_mids) - half)
        score = (first[0] * w_first + second[0] * w_second) / (w_first + w_second)
        delta = (first[1] * w_first + second[1] * w_second) / (w_first + w_second)
        if abs(first[1] - second[1]) > 0.4:
            doubt.append("zoom_search_motion_delta")
        return (score, delta, first[2])

    @classmethod
    def _motion_consistency_bonus(
        cls, best: dict, sv_q: float, cache: _WindowEmbedCache, t_mid: float
    ) -> float:
        """±0.02 tiebreak: does the source zoom the way the query zooms?"""
        try:
            hypothesis: _Hypothesis = best["hypothesis"]
            # Probe around the aligned midpoint of the scored line.
            mid_pred = hypothesis.a * t_mid + hypothesis.b + best["delta"]
            frames = cache.probe_frames(hypothesis.episode, mid_pred)
            grays = [
                (t, SceneAlignerService._small_gray(image)) for t, image in frames
            ]
            sv_src = SceneAlignerService._zoom_rate(grays)
            if sv_src is None:
                return 0.0
            return 0.02 if abs(sv_q - sv_src) < 0.02 else -0.02
        except Exception:
            return 0.0

    @staticmethod
    def _aligned_interval(
        result: dict,
        q_start: float,
        q_end: float,
        span: float,
    ) -> tuple[float, float]:
        hypothesis: _Hypothesis = result["hypothesis"]
        b_aligned = hypothesis.b + result["delta"]
        start_time = hypothesis.a * q_start + b_aligned
        end_time = hypothesis.a * q_end + b_aligned
        if end_time <= start_time:
            end_time = start_time + max(0.2, span * 0.5)
        if result.get("geometry") == "motion" or isinstance(
            result.get("geometry"), tuple
        ):
            start_time += ZoomRematchService.REGISTERED_ALIGNMENT_GUARD_S
            end_time += ZoomRematchService.REGISTERED_ALIGNMENT_GUARD_S
        return float(start_time), float(end_time)

    @staticmethod
    def _alternative_algorithm(result: dict) -> str:
        geometry = result.get("geometry")
        if geometry == "motion":
            return "zoom_search_motion"
        if isinstance(geometry, tuple):
            return "zoom_search_registered"
        if isinstance(geometry, (float, int)):
            return f"zoom_search_center_{float(geometry):g}x"
        return "zoom_search"

    @classmethod
    def _alternatives_from_results(
        cls,
        results: list[dict],
        q_start: float,
        q_end: float,
        span: float,
    ) -> list[AlternativeMatch]:
        alternatives: list[AlternativeMatch] = []
        for result in sorted(
            results,
            key=lambda value: float(value["score"]),
            reverse=True,
        ):
            hypothesis: _Hypothesis = result["hypothesis"]
            if hypothesis.is_current or result["score"] < cls.ALTERNATIVE_SCORE_FLOOR:
                continue
            start_time, end_time = cls._aligned_interval(
                result, q_start, q_end, span
            )
            alternatives.append(
                AlternativeMatch(
                    episode=hypothesis.episode,
                    start_time=round(start_time, 3),
                    end_time=round(end_time, 3),
                    confidence=max(0.0, min(0.95, float(result["score"]))),
                    speed_ratio=span / max(1e-6, end_time - start_time),
                    vote_count=hypothesis.support,
                    algorithm=cls._alternative_algorithm(result),
                )
            )
        return alternatives

    @classmethod
    def merge_alternatives(
        cls,
        primary: SceneMatch,
        *groups: list[AlternativeMatch] | tuple[AlternativeMatch, ...],
    ) -> list[AlternativeMatch]:
        """Keep every distinct candidate cluster, without an arbitrary cap."""
        source_duration = max(0.0, primary.end_time - primary.start_time)
        query_duration = source_duration * max(0.0, primary.speed_ratio)
        separation = max(2.0, query_duration)

        def same_cluster(left: AlternativeMatch, right: AlternativeMatch) -> bool:
            if canonical_episode_stem(left.episode) != canonical_episode_stem(
                right.episode
            ):
                return False
            left_mid = 0.5 * (left.start_time + left.end_time)
            right_mid = 0.5 * (right.start_time + right.end_time)
            return abs(left_mid - right_mid) < separation

        primary_as_alternative = AlternativeMatch(
            episode=primary.episode,
            start_time=primary.start_time,
            end_time=primary.end_time,
            confidence=primary.confidence,
            speed_ratio=primary.speed_ratio,
            algorithm="primary",
        )
        merged: list[AlternativeMatch] = []
        for group in groups:
            for candidate in group:
                if (
                    not candidate.episode
                    or candidate.end_time <= candidate.start_time
                    or same_cluster(candidate, primary_as_alternative)
                    or any(same_cluster(candidate, kept) for kept in merged)
                ):
                    continue
                merged.append(candidate)
        return merged

    # ------------------------------------------------------------------
    # decision

    @classmethod
    def _decide(
        cls,
        best: dict | None,
        current_score: float | None,
        existing_match: SceneMatch,
        scene_index: int,
        q_start: float,
        q_end: float,
        span: float,
        scored: int,
        deadline_hit: bool,
        run_started: float,
        *,
        scored_results: list[dict] | None = None,
    ) -> ZoomSearchOutcome:
        zoom_alternatives = cls.merge_alternatives(
            existing_match,
            cls._alternatives_from_results(
                scored_results or [], q_start, q_end, span
            ),
        )

        def outcome(
            changed: bool, new_match: SceneMatch | None, detail: str
        ) -> ZoomSearchOutcome:
            logger.info(
                "zoom_search %s",
                {
                    "scene_index": scene_index,
                    "changed": changed,
                    "best_score": round(best["score"], 3) if best else None,
                    "current_score": (
                        round(current_score, 3) if current_score is not None else None
                    ),
                    "hypotheses_scored": scored,
                    "deadline_hit": deadline_hit,
                    "elapsed_seconds": round(time.perf_counter() - run_started, 2),
                    "detail": detail,
                },
            )
            return ZoomSearchOutcome(
                changed=changed,
                old_match=existing_match,
                new_match=new_match,
                best_score=float(best["score"]) if best else 0.0,
                current_score=current_score,
                hypotheses_scored=scored,
                deadline_hit=deadline_hit,
                detail=detail,
                alternatives=tuple(zoom_alternatives),
            )

        if best is None:
            return outcome(False, None, "no hypothesis could be scored")

        hypothesis: _Hypothesis = best["hypothesis"]
        if not hypothesis.is_current:
            floor = cls.ACCEPT_FLOOR
            if current_score is not None:
                floor = max(floor, current_score + cls.ACCEPT_MARGIN)
            if best["score"] < floor:
                return outcome(
                    False,
                    None,
                    "no alternative beat the current match under zoom scoring",
                )
        # Endpoints follow the fitted line shifted by the sweep's alignment.
        start_time, end_time = cls._aligned_interval(best, q_start, q_end, span)

        changed = (
            canonical_episode_stem(hypothesis.episode)
            != canonical_episode_stem(existing_match.episode or "")
            or abs(start_time - existing_match.start_time) > cls.CHANGE_EPS_S
            or abs(end_time - existing_match.end_time) > cls.CHANGE_EPS_S
        )
        if hypothesis.is_current or not changed:
            return outcome(False, None, "existing match confirmed")

        previous_primary: list[AlternativeMatch] = []
        if (
            existing_match.episode
            and existing_match.end_time > existing_match.start_time
        ):
            previous_primary.append(
                AlternativeMatch(
                    episode=existing_match.episode,
                    start_time=existing_match.start_time,
                    end_time=existing_match.end_time,
                    confidence=existing_match.confidence,
                    speed_ratio=existing_match.speed_ratio,
                    algorithm="pre_zoom_search",
                )
            )

        new_match = SceneMatch(
            scene_index=existing_match.scene_index,
            episode=hypothesis.episode,
            start_time=round(start_time, 3),
            end_time=round(end_time, 3),
            confidence=min(0.95, 0.45 + best["score"]),
            speed_ratio=span / max(1e-6, end_time - start_time),
            confirmed=False,
            merged_from=existing_match.merged_from,
            doubt_reasons=sorted({"zoom_search", *best["doubt"]}),
        )
        new_match.alternatives = cls.merge_alternatives(
            new_match,
            zoom_alternatives,
            previous_primary,
            existing_match.alternatives,
        )
        return outcome(True, new_match, "found a better zoom-consistent match")


def splice_match(matches: MatchList, scene_index: int, new_match: SceneMatch) -> bool:
    """Replace the match keyed by ``scene_index`` in place; False if absent."""
    for position, match in enumerate(matches.matches):
        if match.scene_index == scene_index:
            matches.matches[position] = new_match
            return True
    return False
