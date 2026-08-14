"""Bounded hierarchical source-timeline matcher.

This is the default bounded matching path.  It deliberately reuses the frozen
anime_searcher index/model through :class:`AnimeMatcherService`, but replaces
the original matcher's unbounded source-window refinement tail with:

* one PTS-aware query decode;
* a bounded correspondence beam;
* adaptive query variants; and
* a deterministic native verification budget.

The service has no persistence responsibilities.  Its caller decides whether
the returned scenes and matches are saved.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from ..library_types import LibraryType
from ..models import (
    AlternativeMatch,
    MatchCandidate,
    MatchList,
    Scene,
    SceneMatch,
    SceneList,
)
from .anime_matcher import AnimeMatcherService
from .episode_names import canonical_episode_stem


logger = logging.getLogger("uvicorn.error")


BASE_SAMPLE_FPS = 4.0
RETRIEVAL_TOP_K = 60
PRIMARY_TOP_K = 20
BEAM_WIDTH = 32
MIN_SOURCE_RATE = 0.25
MAX_SOURCE_RATE = 5.0
TRACK_RESIDUAL_SECONDS = 0.75
VARIANT_EMBED_FRACTION = 0.25
VERIFY_FPS = 6.0
VERIFY_HALF_WINDOW_SECONDS = 0.40
MAX_ALTERNATIVES = 7
# A detector-sized micro-fragment may be an unreliable island between longer
# tracks.  Do not redefine ordinary short scenes: this wider absorption range
# is used only when the fragment is weak and either its retrieval evidence or
# two continuous flanking tracks disprove the selected island.
WEAK_MICRO_MIN_SECONDS = 0.35
WEAK_MICRO_MAX_SECONDS = 0.65
WEAK_MICRO_MAX_CONFIDENCE = 0.38
WEAK_MICRO_NEIGHBOR_MIN_CONFIDENCE = 0.50
WEAK_MICRO_PROPOSAL_MIN_CONFIDENCE = 0.40
WEAK_MICRO_PROPOSAL_MARGIN = 0.07
WEAK_MICRO_CLEAR_CUT_RATIO = 0.75
DUPLICATE_MICRO_MAX_SECONDS = 0.65
DUPLICATE_MICRO_DETECTOR_MAX_SECONDS = 0.50
DUPLICATE_MICRO_CONFIDENCE_MARGIN = 0.08
DUPLICATE_REGION_MIN_SECONDS = 0.40
# Source index timestamps are spaced at 0.5s.  Affine extrapolation from a
# short track can therefore land well before the first retrieved frame even
# when that frame is the first one from the correct source shot.  Keep only a
# tiny lead-in before concrete in-segment evidence; avoiding a previous-shot
# flash is more important than preserving a few uncertain opening frames.
SOURCE_START_MAX_PREROLL_SECONDS = 0.08
# How much a manual-merge prior tilts the episode vote. Big enough to break a
# near-tie toward the fragment the owner said this span continues, small enough
# that a clear body of fresh retrieval still overrules it.
PRIOR_EPISODE_WEIGHT = 1.5


@dataclass(frozen=True)
class QueryFrame:
    t_query: float
    embedding: np.ndarray
    preview: Image.Image | None = None
    variant_id: str = "plain"


@dataclass(frozen=True)
class RetrievalCandidate:
    sample_index: int
    t_query: float
    episode: str
    t_source: float
    similarity: float
    series: str
    variant_id: str = "plain"


@dataclass(frozen=True)
class RematchPrior:
    """The pre-merge match of the fragment a merged scene was absorbed into.

    Pressing merge-with-previous asserts that the merged span *continues* the
    previous fragment. That fragment's existing match is therefore real evidence
    — it names the episode and pins where on the source timeline the span
    starts. It is evidence, not proof: it breaks near-ties and anchors the fitted
    line, but a body of fresh retrieval that disagrees still wins.
    """

    episode: str
    q_start: float
    q_end: float
    source_start: float
    source_end: float
    confidence: float = 0.0

    def anchor_candidates(self) -> list[RetrievalCandidate]:
        """The prior expressed as two synthetic correspondences for the fit."""
        similarity = float(np.clip(self.confidence, 0.30, 0.95))
        return [
            RetrievalCandidate(
                sample_index=-1,
                t_query=t_query,
                episode=self.episode,
                t_source=t_source,
                similarity=similarity,
                series="",
                variant_id="prior",
            )
            for t_query, t_source in (
                (self.q_start, self.source_start),
                (self.q_end, self.source_end),
            )
        ]


@dataclass(frozen=True)
class LineProposal:
    episode: str
    a: float
    b: float
    confidence: float
    support: int
    algorithm: str

    def source_at(self, timestamp: float) -> float:
        return self.a * timestamp + self.b


@dataclass
class TrackSegment:
    q_start: float
    q_end: float
    episode: str | None
    a: float = 1.0
    b: float = 0.0
    points: list[RetrievalCandidate] = field(default_factory=list)
    confidence: float = 0.0
    residual: float = 0.0
    uncertain: bool = False
    doubt_reasons: list[str] = field(default_factory=list)

    def source_at(self, timestamp: float) -> float:
        return self.a * timestamp + self.b


@dataclass(frozen=True)
class _BeamState:
    score: float
    path: tuple[RetrievalCandidate | None, ...]
    breaks: tuple[bool, ...]
    episode: str | None
    count: int
    sum_w: float
    sum_t: float
    sum_y: float
    sum_tt: float
    sum_ty: float
    first_t: float
    first_y: float
    last_t: float
    last_y: float

    @property
    def line(self) -> tuple[float, float] | None:
        denom = self.sum_w * self.sum_tt - self.sum_t * self.sum_t
        if self.count < 2 or abs(denom) <= 1e-9:
            return None
        a = (self.sum_w * self.sum_ty - self.sum_t * self.sum_y) / denom
        b = (self.sum_y - a * self.sum_t) / max(self.sum_w, 1e-9)
        return float(a), float(b)


@dataclass
class HierarchicalDiagnostics:
    sample_count: int = 0
    correspondence_count: int = 0
    segment_count: int = 0
    weak_variant_sample_count: int = 0
    phase_timings: dict[str, float] = field(default_factory=dict)
    counters: dict[str, float] = field(default_factory=dict)


@dataclass
class HierarchicalResult:
    scenes: SceneList
    matches: MatchList
    diagnostics: HierarchicalDiagnostics


class HierarchicalMatcherService:
    """Fast, bounded matcher selected when ``ATR_MATCHER_V2`` is unset/false."""

    @classmethod
    def align_scenes_sync(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_type: LibraryType | str,
        anime_name: str | None = None,
        episode_whitelist: frozenset[str] | None = None,
    ) -> HierarchicalResult:
        if AnimeMatcherService._query_processor is None:
            raise RuntimeError("anime_searcher must be initialized before bounded matching")

        diagnostics = HierarchicalDiagnostics()
        run_started = time.perf_counter()

        started = time.perf_counter()
        samples, diff_times, diffs, duration = cls._sample_query_video(
            video_path, scenes
        )
        diagnostics.phase_timings["v2_query"] = time.perf_counter() - started
        diagnostics.sample_count = len(samples)

        started = time.perf_counter()
        candidates = cls._retrieve(
            samples, anime_name, episode_whitelist=episode_whitelist
        )
        diagnostics.phase_timings["v2_retrieve"] = time.perf_counter() - started
        diagnostics.correspondence_count = sum(len(values) for values in candidates)

        detector_boundaries = [scene.end_time for scene in scenes.scenes[:-1]]
        strong_boundaries = cls._strong_diff_boundaries(diff_times, diffs)

        started = time.perf_counter()
        state = cls._decode_beam(
            samples,
            candidates,
            detector_boundaries,
            strong_boundaries,
        )
        diagnostics.phase_timings["v2_track"] = time.perf_counter() - started

        # Adaptive query variants are bounded by work count and by the UX
        # watchdog.  No source decode occurs in this phase.
        #
        # The watchdog keys off the decoded video length, not the scene list's
        # end time: a scene list that stops short of the tail must not shrink
        # the wall target or the native verify budget.
        video_duration = max(duration, diff_times[-1] if diff_times else 0.0)
        target_seconds = 60.0 if video_duration <= 90.0 else 120.0
        deadline = run_started + target_seconds * 0.8
        variant_indices = cls._variant_sample_indices(samples, candidates, state)
        variant_budget = int(math.ceil(len(samples) * VARIANT_EMBED_FRACTION))
        variant_count = 0
        variant_sample_count = 0
        if variant_indices and variant_budget > 0 and time.perf_counter() < deadline:
            started = time.perf_counter()
            variant_frames: list[QueryFrame] = []
            variant_to_sample: list[int] = []
            for sample_index in variant_indices:
                sample = samples[sample_index]
                if sample.preview is None:
                    continue
                # The deadline bounds the phase itself, not only its entry:
                # a phase entered just under the gate must still stop at it.
                if time.perf_counter() >= deadline:
                    break
                for variant_id, image in cls._query_variants(sample.preview):
                    if variant_count >= variant_budget:
                        break
                    variant_frames.append(
                        QueryFrame(sample.t_query, np.empty(0, dtype=np.float32), image, variant_id)
                    )
                    variant_to_sample.append(sample_index)
                    variant_count += 1
                if variant_count >= variant_budget:
                    break
            if variant_frames:
                embeddings = AnimeMatcherService._embed_pil_batch(
                    [frame.preview.convert("RGB") for frame in variant_frames if frame.preview]
                )
                embedded_variants = [
                    replace(frame, embedding=embedding)
                    for frame, embedding in zip(variant_frames, embeddings, strict=False)
                ]
                cls._merge_variant_candidates(
                    candidates,
                    embedded_variants,
                    variant_to_sample,
                    anime_name,
                    episode_whitelist=episode_whitelist,
                )
                state = cls._decode_beam(
                    samples,
                    candidates,
                    detector_boundaries,
                    strong_boundaries,
                )
                variant_sample_count = len(set(variant_to_sample))
            diagnostics.phase_timings["v2_variants"] = time.perf_counter() - started
        # Samples that received a variant, not variant images: the image count
        # is reported separately as counters["variant_embeddings"].
        diagnostics.weak_variant_sample_count = variant_sample_count

        started = time.perf_counter()
        segments = cls._segments_from_state(
            state,
            samples,
            candidates,
            duration,
            detector_boundaries,
            strong_boundaries,
        )
        unsplit_count = len(segments)
        segments = cls._split_supported_discontinuities(
            segments,
            detector_boundaries,
            samples,
            candidates,
        )
        diagnostics.counters["evidence_splits"] = float(
            max(0, len(segments) - unsplit_count)
        )
        segments = cls._promote_dominant_proposals(
            segments,
            candidates,
            samples,
        )
        segments = cls._merge_continuous_segments(segments)
        segments = cls._absorb_tiny_segments(segments)
        before_weak_micro = len(segments)
        segments = cls._absorb_weak_micro_segments(
            segments,
            candidates,
            samples,
            diff_times,
            diffs,
        )
        diagnostics.counters["weak_micro_absorptions"] = float(
            max(0, before_weak_micro - len(segments))
        )
        before_duplicate_regions = len(segments)
        segments = cls._collapse_leading_duplicate_regions(segments)
        diagnostics.counters["duplicate_region_collapses"] = float(
            max(0, before_duplicate_regions - len(segments))
        )
        diagnostics.phase_timings["v2_assemble_tracks"] = time.perf_counter() - started

        started = time.perf_counter()
        native_seconds = 0.0
        if time.perf_counter() < deadline:
            native_seconds = cls._verify_ambiguous_segments(
                segments,
                samples,
                candidates,
                library_type,
                video_duration,
                deadline=deadline,
            )
        diagnostics.phase_timings["v2_native_verify"] = time.perf_counter() - started
        diagnostics.counters["native_source_seconds"] = native_seconds
        diagnostics.counters["variant_embeddings"] = float(variant_count)

        # Native arbitration can replace one side of a formerly discontinuous
        # pair with the adjacent affine track.  Re-run the same strict
        # continuity merge once; this joins only episode/rate-compatible
        # mappings and avoids leaving a harmless UI split behind.
        before_final_merge = len(segments)
        segments = cls._merge_continuous_segments(segments)
        diagnostics.counters["post_verify_continuous_merges"] = float(
            max(0, before_final_merge - len(segments))
        )

        started = time.perf_counter()
        final_scenes, matches = cls._build_output(segments, samples, candidates)
        diagnostics.phase_timings["v2_output"] = time.perf_counter() - started
        diagnostics.segment_count = len(final_scenes.scenes)
        diagnostics.counters["abstained_segments"] = float(
            sum(1 for segment in segments if segment.episode is None)
        )
        diagnostics.counters["elapsed_seconds"] = time.perf_counter() - run_started

        logger.info(
            "matching_v2_profile %s",
            {
                "phase_seconds": {
                    name: round(seconds, 2)
                    for name, seconds in diagnostics.phase_timings.items()
                },
                "counters": {
                    name: round(value, 2)
                    for name, value in diagnostics.counters.items()
                },
                "output_scenes": len(final_scenes.scenes),
            },
        )
        return HierarchicalResult(final_scenes, matches, diagnostics)

    @classmethod
    def rematch_scene_sync(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_type: LibraryType | str,
        anime_name: str | None = None,
        *,
        scene_index: int,
        existing_matches: MatchList,
        prior: RematchPrior | None = None,
    ) -> MatchList:
        """Re-match exactly one scene, keeping its boundaries and its siblings.

        This is the bounded matcher's partial path, used by the manual
        merge-with-previous route. Unlike :meth:`align_scenes_sync` it never
        reshapes the scene list: the owner just chose these boundaries by hand,
        so the result is exactly one match over exactly that span, spliced into
        ``existing_matches``.
        """
        if AnimeMatcherService._query_processor is None:
            raise RuntimeError("anime_searcher must be initialized before bounded matching")
        if not scenes.scenes:
            raise ValueError("scenes must not be empty")
        if not 0 <= scene_index < len(scenes.scenes):
            raise IndexError(f"scene_index {scene_index} out of range")

        # Native verification below opens PyNv source captures, so size the
        # NVDEC session budget as the full-match path does.
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
        target = scenes.scenes[scene_index]
        q_start = float(target.start_time)
        q_end = float(max(target.end_time, target.start_time + 1e-3))

        # Only this scene's span is embedded and retrieved. The decode still
        # walks the file (the diff curve is per-frame) but embedding and FAISS
        # search — the dominant cost — stay proportional to the merged scene.
        samples, _, _, _ = cls._sample_query_video(
            video_path, scenes, [(q_start, q_end)]
        )
        samples = [
            sample for sample in samples if q_start - 1e-9 <= sample.t_query <= q_end + 1e-9
        ]
        candidates = cls._retrieve(samples, anime_name)

        # No detector or difference boundaries: the owner explicitly merged this
        # span, so the beam must have no reset evidence that could re-split it.
        state = cls._decode_beam(samples, candidates, [], [])
        segments = cls._segments_from_state(
            state, samples, candidates, q_end, [], []
        )
        segment = cls._collapse_to_single_segment(segments, q_start, q_end, prior)

        native_seconds = 0.0
        # Verification arbitrates against a query frame, so it needs at least
        # one sample. A prior-only rescue can reach here with none.
        if samples and segment.episode is not None and segment.uncertain:
            native_seconds = cls._verify_ambiguous_segments(
                [segment],
                samples,
                candidates,
                library_type,
                q_end - q_start,
                deadline=run_started + 30.0,
            )

        _, built = cls._build_output([segment], samples, candidates)
        rematched = built.matches[0]

        result = MatchList()
        for index, scene in enumerate(scenes.scenes):
            if index == scene_index:
                match = rematched.model_copy()
            elif index < len(existing_matches.matches):
                match = existing_matches.matches[index].model_copy()
            else:
                match = SceneMatch(
                    scene_index=scene.index,
                    episode="",
                    start_time=0.0,
                    end_time=0.0,
                    confidence=0.0,
                    speed_ratio=1.0,
                    was_no_match=True,
                )
            match.scene_index = scene.index
            result.matches.append(match)

        logger.info(
            "matching_v2_partial %s",
            {
                "scene_index": scene_index,
                "span": [round(q_start, 3), round(q_end, 3)],
                "samples": len(samples),
                "prior_episode": prior.episode if prior else "",
                "episode": segment.episode or "",
                "confidence": round(segment.confidence, 3),
                "native_source_seconds": round(native_seconds, 2),
                "doubt_reasons": sorted(set(segment.doubt_reasons)),
                "elapsed_seconds": round(time.perf_counter() - run_started, 2),
            },
        )
        return result

    @classmethod
    def _collapse_to_single_segment(
        cls,
        segments: list[TrackSegment],
        q_start: float,
        q_end: float,
        prior: RematchPrior | None = None,
    ) -> TrackSegment:
        """Reduce tracked segments to exactly one over fixed query boundaries.

        The dominant episode wins on evidence count weighted by how much of the
        span it covers, and its line is refit over every point supporting it.
        A ``prior`` from a manual merge tilts the episode vote and anchors the
        fitted line without being able to override clear contrary evidence.
        """
        span = max(1e-6, q_end - q_start)
        prior_episode = prior.episode if prior and prior.episode else None

        def covered(segment: TrackSegment) -> float:
            return max(
                0.0, min(segment.q_end, q_end) - max(segment.q_start, q_start)
            )

        def evidence_weight(segment: TrackSegment) -> float:
            weight = len(segment.points) * max(
                covered(segment), 1.0 / BASE_SAMPLE_FPS
            )
            if prior_episode and segment.episode == prior_episode:
                weight *= PRIOR_EPISODE_WEIGHT
            return weight

        supported = [
            segment
            for segment in segments
            if segment.episode is not None and segment.points
        ]
        if not supported:
            reasons = {reason for segment in segments for reason in segment.doubt_reasons}
            if prior_episode:
                # No fresh evidence at all, but the owner told us this span
                # continues an already-matched fragment. Extending that line is
                # a better answer than abstaining outright.
                anchors = prior.anchor_candidates()
                a, b, residual = cls._fit_points(anchors)
                return TrackSegment(
                    q_start=q_start,
                    q_end=q_end,
                    episode=prior_episode,
                    a=a,
                    b=b,
                    points=anchors,
                    confidence=float(np.clip(prior.confidence, 0.0, 1.0)),
                    residual=residual,
                    uncertain=True,
                    doubt_reasons=sorted(reasons | {"prior_only"}),
                )
            return TrackSegment(
                q_start, q_end, None, uncertain=True,
                doubt_reasons=sorted(reasons | {"no_evidence"}),
            )

        winner = max(
            supported,
            key=lambda segment: (evidence_weight(segment), len(segment.points)),
        )
        same_episode = [
            segment for segment in supported if segment.episode == winner.episode
        ]
        points = [point for segment in same_episode for point in segment.points]

        # The prior only steers geometry when it agrees on the episode, and it
        # never counts toward confidence or support below — those stay measured
        # on real retrieval alone.
        anchors = (
            prior.anchor_candidates()
            if prior_episode and winner.episode == prior_episode
            else []
        )

        # Pooling same-episode groups recovers support the beam split at a
        # reset, but only while one line still explains them: a real source
        # discontinuity inside the span makes the pooled fit worse, and there
        # the winner's own coherent line is the honest answer.
        a, b, residual = cls._fit_points(points + anchors)
        own_a, own_b, own_residual = cls._fit_points(list(winner.points) + anchors)
        if len(same_episode) > 1 and residual > own_residual + 0.10:
            a, b, residual = own_a, own_b, own_residual
            points = list(winner.points)

        confidence = float(np.median([point.similarity for point in points]))
        support_expected = max(1, int(round(span * BASE_SAMPLE_FPS)))
        support_ratio = min(1.0, len(points) / support_expected)

        # Geometry-derived reasons are recomputed below against the FIXED span.
        # Inheriting them too would carry over a sub-segment's verdict measured
        # against a different interval — the sub-segments start at 0.0, not at
        # q_start, so their support ratio is not this segment's.
        recomputed = {"weak_similarity", "sparse_support", "timing_residual"}
        reasons = {
            reason
            for segment in same_episode
            for reason in segment.doubt_reasons
            if reason not in recomputed
        }
        if confidence < 0.36:
            reasons.add("weak_similarity")
        if support_ratio < 0.50:
            reasons.add("sparse_support")
        if residual > 0.45:
            reasons.add("timing_residual")
        if len({segment.episode for segment in supported}) > 1:
            reasons.add("partial_rematch_collapsed")
        if prior_episode and winner.episode != prior_episode:
            # The owner said this span continues the previous fragment, yet the
            # evidence points elsewhere. Surface the conflict rather than hide
            # it behind either answer.
            reasons.add("prior_episode_overruled")
        elif anchors and abs((a * prior.q_start + b) - prior.source_start) > 1.0:
            reasons.add("prior_offset_disagreement")

        return TrackSegment(
            q_start=q_start,
            q_end=q_end,
            episode=winner.episode,
            a=a,
            b=b,
            points=points,
            confidence=confidence,
            residual=residual,
            uncertain=bool(reasons),
            doubt_reasons=sorted(reasons),
        )

    @staticmethod
    def _target_times(
        scenes: SceneList,
        spans: list[tuple[float, float]] | None = None,
    ) -> list[float]:
        """Query timestamps to embed.

        ``spans`` restricts the result to the given query intervals, used by the
        partial rematch path so a single merged scene pays only for its own
        embeddings. ``spans=None`` returns the full-match target list unchanged.
        """
        if not scenes.scenes:
            return []
        duration = max(0.0, scenes.scenes[-1].end_time)
        targets = set(
            round(float(value), 6)
            for value in np.arange(0.0, duration + 1e-9, 1.0 / BASE_SAMPLE_FPS)
        )
        for scene in scenes.scenes:
            for fraction in (0.2, 0.5, 0.8):
                targets.add(
                    round(scene.start_time + fraction * max(0.0, scene.duration), 6)
                )
        ordered = sorted(value for value in targets if 0.0 <= value < duration)
        if spans is None:
            return ordered
        return [
            value
            for value in ordered
            if any(start - 1e-9 <= value <= end + 1e-9 for start, end in spans)
        ]

    @classmethod
    def _sample_query_video(
        cls,
        video_path: Path,
        scenes: SceneList,
        spans: list[tuple[float, float]] | None = None,
    ) -> tuple[list[QueryFrame], list[float], list[float], float]:
        """Decode once with PTS-preserving PyAV, with an OpenCV fallback."""
        try:
            import av  # type: ignore[import-not-found]
        except ImportError:
            return cls._sample_query_video_cv2(video_path, scenes, spans)
        try:
            return cls._sample_query_video_av(av, video_path, scenes, spans)
        except Exception:
            logger.exception("PyAV query decode failed; using OpenCV fallback")
            return cls._sample_query_video_cv2(video_path, scenes, spans)

    @classmethod
    def _sample_query_video_av(
        cls,
        av_module: Any,
        video_path: Path,
        scenes: SceneList,
        spans: list[tuple[float, float]] | None = None,
    ) -> tuple[list[QueryFrame], list[float], list[float], float]:
        """Collect native-rate luma diffs and query frames in one decode.

        PyAV leaves decoded frames in their native YUV representation. Only
        selected query frames are converted to RGB, avoiding the dominant
        full-resolution BGR conversion cost on 60fps portrait inputs.
        """
        targets = cls._target_times(scenes, spans)
        target_pos = 0
        samples: list[QueryFrame] = []
        diff_times: list[float] = []
        diffs: list[float] = []
        pending_images: list[Image.Image] = []
        pending_times: list[float] = []
        seen_native_indices: set[int] = set()

        def flush() -> None:
            if not pending_images:
                return
            embeddings = AnimeMatcherService._embed_pil_batch(pending_images)
            samples.extend(
                QueryFrame(
                    timestamp,
                    embedding,
                    ImageOps.contain(image, (384, 384)),
                )
                for timestamp, embedding, image in zip(
                    pending_times,
                    embeddings,
                    pending_images,
                    strict=False,
                )
            )
            pending_images.clear()
            pending_times.clear()

        def query_image(frame: Any) -> Image.Image:
            # Preserve the exact input resolution for the frozen SSCD
            # processor. Only ~4fps query frames take this RGB path; every
            # other decoded frame remains a cheap 64px luma diff frame.
            return frame.to_image().convert("RGB")

        def append_sample(
            frame: Any,
            target: float,
            native_index: int,
        ) -> None:
            if native_index in seen_native_indices:
                return
            seen_native_indices.add(native_index)
            pending_images.append(query_image(frame))
            pending_times.append(target)
            if len(pending_images) >= 64:
                flush()

        previous_frame: Any | None = None
        previous_pts: float | None = None
        previous_small: np.ndarray | None = None
        previous_index = 0
        first_pts: float | None = None
        last_pts = 0.0
        frame_index = 0
        container = av_module.open(str(video_path))
        try:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            fallback_fps = float(stream.average_rate or 30.0)
            for frame in container.decode(stream):
                raw_pts = (
                    float(frame.pts * frame.time_base)
                    if frame.pts is not None and frame.time_base is not None
                    else frame_index / max(fallback_fps, 1e-6)
                )
                if first_pts is None:
                    first_pts = raw_pts
                pts = max(0.0, raw_pts - first_pts)
                if previous_pts is not None and pts <= previous_pts + 1e-9:
                    pts = previous_pts + 1.0 / max(fallback_fps, 1e-6)
                last_pts = pts

                small = frame.reformat(width=64, height=64, format="gray").to_ndarray()
                if previous_small is not None:
                    diff_times.append(pts)
                    diffs.append(
                        float(
                            np.mean(
                                np.abs(
                                    small.astype(np.int16)
                                    - previous_small.astype(np.int16)
                                )
                            )
                        )
                    )

                if previous_frame is None or previous_pts is None:
                    while target_pos < len(targets) and targets[target_pos] <= pts + 1e-9:
                        append_sample(frame, targets[target_pos], frame_index)
                        target_pos += 1
                else:
                    midpoint = 0.5 * (previous_pts + pts)
                    while target_pos < len(targets) and targets[target_pos] <= midpoint + 1e-9:
                        append_sample(
                            previous_frame,
                            targets[target_pos],
                            previous_index,
                        )
                        target_pos += 1

                previous_frame = frame
                previous_pts = pts
                previous_small = small
                previous_index = frame_index
                frame_index += 1

            if previous_frame is not None:
                while target_pos < len(targets) and targets[target_pos] <= last_pts + 1e-9:
                    append_sample(
                        previous_frame,
                        targets[target_pos],
                        previous_index,
                    )
                    target_pos += 1
            flush()
        finally:
            container.close()
        samples.sort(key=lambda item: item.t_query)
        duration = scenes.scenes[-1].end_time if scenes.scenes else last_pts
        return samples, diff_times, diffs, float(duration)

    @classmethod
    def _sample_query_video_cv2(
        cls,
        video_path: Path,
        scenes: SceneList,
        spans: list[tuple[float, float]] | None = None,
    ) -> tuple[list[QueryFrame], list[float], list[float], float]:
        """Decode once, collecting target frames and a native diff curve."""
        cv2 = AnimeMatcherService._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        targets = cls._target_times(scenes, spans)
        target_pos = 0
        samples: list[QueryFrame] = []
        diff_times: list[float] = []
        diffs: list[float] = []
        pending_images: list[Image.Image] = []
        pending_previews: list[Image.Image] = []
        pending_times: list[float] = []
        seen_native_indices: set[int] = set()

        def flush() -> None:
            if not pending_images:
                return
            embeddings = AnimeMatcherService._embed_pil_batch(pending_images)
            samples.extend(
                QueryFrame(t, emb, preview)
                for t, emb, preview in zip(
                    pending_times,
                    embeddings,
                    pending_previews,
                    strict=False,
                )
            )
            pending_images.clear()
            pending_previews.clear()
            pending_times.clear()

        def append_sample(frame: np.ndarray, target: float, native_index: int) -> None:
            if native_index in seen_native_indices:
                return
            seen_native_indices.add(native_index)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            pending_images.append(image)
            pending_previews.append(ImageOps.contain(image, (384, 384)))
            pending_times.append(target)
            if len(pending_images) >= 64:
                flush()

        previous_frame: np.ndarray | None = None
        previous_pts: float | None = None
        previous_small: np.ndarray | None = None
        previous_index = 0
        frame_index = 0
        last_pts = 0.0
        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS)
            if not native_fps or native_fps <= 0:
                native_fps = 30.0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                raw_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
                pts = (
                    float(raw_ms) / 1000.0
                    if math.isfinite(raw_ms) and raw_ms >= 0.0
                    else frame_index / native_fps
                )
                if previous_pts is not None and pts <= previous_pts + 1e-9:
                    pts = max(frame_index / native_fps, previous_pts + 1.0 / native_fps)
                last_pts = pts

                small = cv2.cvtColor(
                    cv2.resize(frame, (64, 64), interpolation=cv2.INTER_AREA),
                    cv2.COLOR_BGR2GRAY,
                )
                if previous_small is not None:
                    diff_times.append(pts)
                    diffs.append(
                        float(
                            np.mean(
                                np.abs(
                                    small.astype(np.int16)
                                    - previous_small.astype(np.int16)
                                )
                            )
                        )
                    )

                if previous_frame is None or previous_pts is None:
                    while target_pos < len(targets) and targets[target_pos] <= pts + 1e-9:
                        append_sample(frame, targets[target_pos], frame_index)
                        target_pos += 1
                else:
                    midpoint = 0.5 * (previous_pts + pts)
                    while target_pos < len(targets) and targets[target_pos] <= midpoint + 1e-9:
                        append_sample(
                            previous_frame,
                            targets[target_pos],
                            previous_index,
                        )
                        target_pos += 1

                previous_frame = frame
                previous_pts = pts
                previous_small = small
                previous_index = frame_index
                frame_index += 1

            if previous_frame is not None:
                while target_pos < len(targets) and targets[target_pos] <= last_pts + 1e-9:
                    append_sample(previous_frame, targets[target_pos], previous_index)
                    target_pos += 1
            flush()
        finally:
            cap.release()
        samples.sort(key=lambda item: item.t_query)
        duration = scenes.scenes[-1].end_time if scenes.scenes else last_pts
        return samples, diff_times, diffs, float(duration)

    @classmethod
    def _retrieve(
        cls,
        samples: list[QueryFrame],
        anime_name: str | None,
        episode_whitelist: frozenset[str] | None = None,
    ) -> list[list[RetrievalCandidate]]:
        processor = AnimeMatcherService._query_processor
        if processor is None or not samples:
            return [[] for _ in samples]
        embeddings = np.stack([sample.embedding for sample in samples]).astype(
            np.float32, copy=False
        )
        started = time.perf_counter()
        raw = processor.index_manager.search_batch(
            embeddings, RETRIEVAL_TOP_K, None, series=anime_name
        )
        AnimeMatcherService._record_runtime_stat(
            "faiss_search_seconds", time.perf_counter() - started
        )
        AnimeMatcherService._record_runtime_stat("faiss_search_queries", len(samples))
        output: list[list[RetrievalCandidate]] = []
        for sample_index, (sample, values) in enumerate(zip(samples, raw, strict=False)):
            candidates = [
                RetrievalCandidate(
                    sample_index=sample_index,
                    t_query=sample.t_query,
                    episode=metadata.episode,
                    t_source=float(metadata.timestamp),
                    similarity=float(similarity),
                    series=metadata.series,
                    variant_id=sample.variant_id,
                )
                for similarity, metadata in values
                if float(similarity) >= 0.20
                and (
                    episode_whitelist is None
                    or canonical_episode_stem(metadata.episode) in episode_whitelist
                )
            ]
            output.append(cls._dedupe_candidates(candidates))
        return output

    @staticmethod
    def _dedupe_candidates(
        candidates: list[RetrievalCandidate],
    ) -> list[RetrievalCandidate]:
        deduped: dict[tuple[str, int], RetrievalCandidate] = {}
        for candidate in candidates:
            key = (candidate.episode, round(candidate.t_source * 2.0))
            previous = deduped.get(key)
            if previous is None or candidate.similarity > previous.similarity:
                deduped[key] = candidate
        return sorted(
            deduped.values(), key=lambda item: item.similarity, reverse=True
        )[:RETRIEVAL_TOP_K]

    @staticmethod
    def _strong_diff_boundaries(
        diff_times: list[float], diffs: list[float]
    ) -> list[float]:
        if not diff_times or len(diff_times) != len(diffs):
            return []
        values = np.asarray(diffs, dtype=np.float32)
        if len(diff_times) > 1:
            step = max(1e-3, float(np.median(np.diff(diff_times))))
        else:
            step = 1.0 / 30.0
        radius = max(3, int(round(0.5 / step)))
        result: list[float] = []
        for index in range(2, len(values) - 2):
            value = float(values[index])
            lo = max(0, index - radius)
            hi = min(len(values), index + radius + 1)
            local = values[lo:hi]
            local_floor = float(np.median(local))
            if (
                value < max(12.0, 3.0 * local_floor)
                or value < float(np.max(values[index - 2 : index + 3]))
            ):
                continue
            timestamp = float(diff_times[index])
            if not result or timestamp - result[-1] >= 0.12:
                result.append(timestamp)
            elif value > float(values[index - 1]):
                result[-1] = timestamp
        return result

    @staticmethod
    def _boundary_incentive(
        timestamp: float,
        detector_boundaries: list[float],
        strong_boundaries: list[float],
    ) -> float:
        if strong_boundaries and min(abs(timestamp - value) for value in strong_boundaries) <= 0.30:
            return 1.0
        if detector_boundaries and min(abs(timestamp - value) for value in detector_boundaries) <= 0.30:
            return 0.70
        return 0.0

    @staticmethod
    def _start_state(
        candidate: RetrievalCandidate,
        *,
        previous: _BeamState | None = None,
        reset_penalty: float = 0.0,
    ) -> _BeamState:
        weight = max(0.05, candidate.similarity) ** 2
        base_score = previous.score if previous is not None else 0.0
        emission = 1.5 * (candidate.similarity - 0.20)
        return _BeamState(
            score=base_score + emission - reset_penalty,
            path=(previous.path if previous else ()) + (candidate,),
            breaks=(previous.breaks if previous else ()) + (True,),
            episode=candidate.episode,
            count=1,
            sum_w=weight,
            sum_t=weight * candidate.t_query,
            sum_y=weight * candidate.t_source,
            sum_tt=weight * candidate.t_query * candidate.t_query,
            sum_ty=weight * candidate.t_query * candidate.t_source,
            first_t=candidate.t_query,
            first_y=candidate.t_source,
            last_t=candidate.t_query,
            last_y=candidate.t_source,
        )

    @classmethod
    def _continue_state(
        cls, state: _BeamState, candidate: RetrievalCandidate
    ) -> _BeamState | None:
        if state.episode != candidate.episode or candidate.t_query <= state.last_t:
            return None
        dt_total = candidate.t_query - state.first_t
        dy_total = candidate.t_source - state.first_y
        if dy_total < MIN_SOURCE_RATE * dt_total - 0.55:
            return None
        if dy_total > MAX_SOURCE_RATE * dt_total + 0.55:
            return None

        residual = 0.0
        rate_penalty = 0.0
        line = state.line
        if line is not None:
            a, b = line
            line_residual = abs(
                candidate.t_source - (a * candidate.t_query + b)
            )
            # Two or three samples on the 0.5s source-index grid commonly
            # produce a provisional slope of 0x or 2x.  Test the unit-rate
            # line as well until the track has enough span to establish a
            # genuine playback-rate change.  The small surcharge prevents a
            # unit-rate prior from beating an equally good measured line.
            unit_b = (state.sum_y - state.sum_t) / max(state.sum_w, 1e-9)
            unit_residual = abs(
                candidate.t_source - (candidate.t_query + unit_b)
            )
            use_unit_line = state.count < 4 or not (
                MIN_SOURCE_RATE <= a <= MAX_SOURCE_RATE
            )
            residual = (
                min(line_residual, unit_residual + 0.08)
                if use_unit_line
                else line_residual
            )
            if residual > TRACK_RESIDUAL_SECONDS:
                return None
            if a < MIN_SOURCE_RATE or a > MAX_SOURCE_RATE:
                rate_penalty = 0.20
            else:
                rate_penalty = 0.18 * abs(math.log(max(a, 1e-6)))
        elif dt_total > 1e-6:
            coarse_rate = max(MIN_SOURCE_RATE, dy_total / dt_total)
            rate_penalty = 0.12 * abs(math.log(coarse_rate))

        weight = max(0.05, candidate.similarity) ** 2
        emission = 1.5 * (candidate.similarity - 0.20)
        return _BeamState(
            score=state.score + emission + 0.30 - 0.45 * residual - rate_penalty,
            path=state.path + (candidate,),
            breaks=state.breaks + (False,),
            episode=state.episode,
            count=state.count + 1,
            sum_w=state.sum_w + weight,
            sum_t=state.sum_t + weight * candidate.t_query,
            sum_y=state.sum_y + weight * candidate.t_source,
            sum_tt=state.sum_tt + weight * candidate.t_query * candidate.t_query,
            sum_ty=state.sum_ty + weight * candidate.t_query * candidate.t_source,
            first_t=state.first_t,
            first_y=state.first_y,
            last_t=candidate.t_query,
            last_y=candidate.t_source,
        )

    @classmethod
    def _decode_beam(
        cls,
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
        detector_boundaries: list[float],
        strong_boundaries: list[float],
    ) -> _BeamState:
        if not samples:
            return _BeamState(0.0, (), (), None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

        first_values = candidates[0][:PRIMARY_TOP_K]
        beam = [cls._start_state(value) for value in first_values]
        beam.append(
            _BeamState(-0.15, (None,), (True,), None, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        )
        beam = sorted(beam, key=lambda item: item.score, reverse=True)[:BEAM_WIDTH]

        for sample_index in range(1, len(samples)):
            values = candidates[sample_index][:PRIMARY_TOP_K]
            previous_best = max(beam, key=lambda item: item.score)
            midpoint = 0.5 * (
                samples[sample_index - 1].t_query + samples[sample_index].t_query
            )
            incentive = cls._boundary_incentive(
                midpoint, detector_boundaries, strong_boundaries
            )
            # Both signals are priors.  A native diff peak may be motion,
            # subtitles, or a flash and must not reward abandoning an affine
            # track.  A real source jump still forces a reset because the
            # continuation residual/rate checks reject it.
            if incentive >= 1.0:
                reset_penalty = 0.30
            elif incentive > 0.0:
                reset_penalty = 0.20
            else:
                reset_penalty = 0.95
            expanded: list[_BeamState] = []

            for state in beam:
                for candidate in values:
                    continued = cls._continue_state(state, candidate)
                    if continued is not None:
                        expanded.append(continued)
            for candidate in values:
                expanded.append(
                    cls._start_state(
                        candidate,
                        previous=previous_best,
                        reset_penalty=reset_penalty,
                    )
                )
            expanded.append(
                _BeamState(
                    score=previous_best.score - 0.18,
                    path=previous_best.path + (None,),
                    breaks=previous_best.breaks + (True,),
                    episode=None,
                    count=0,
                    sum_w=0.0,
                    sum_t=0.0,
                    sum_y=0.0,
                    sum_tt=0.0,
                    sum_ty=0.0,
                    first_t=samples[sample_index].t_query,
                    first_y=0.0,
                    last_t=samples[sample_index].t_query,
                    last_y=0.0,
                )
            )

            deduped: dict[tuple[Any, ...], _BeamState] = {}
            for state in expanded:
                last = state.path[-1]
                line = state.line
                key = (
                    last.episode if last else None,
                    round(last.t_source * 2) if last else -1,
                    round(line[0] / 0.10) if line else -1,
                    min(state.count, 6),
                )
                previous = deduped.get(key)
                if previous is None or state.score > previous.score:
                    deduped[key] = state
            beam = sorted(
                deduped.values(), key=lambda item: item.score, reverse=True
            )[:BEAM_WIDTH]
        return max(beam, key=lambda item: item.score)

    @staticmethod
    def _variant_sample_indices(
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
        state: _BeamState,
    ) -> list[int]:
        uncertain: list[tuple[float, int]] = []
        for index, sample in enumerate(samples):
            values = candidates[index]
            selected = state.path[index] if index < len(state.path) else None
            if selected is None:
                uncertain.append((1.0, index))
                continue
            distinct = [
                value
                for value in values
                if value.episode != selected.episode
                or abs(value.t_source - selected.t_source) >= 2.0
            ]
            rival = distinct[0].similarity if distinct else 0.0
            margin = selected.similarity - rival
            if selected.similarity < 0.36 or margin < 0.04:
                priority = (0.36 - selected.similarity) + max(0.0, 0.04 - margin)
                uncertain.append((priority, index))
        uncertain.sort(reverse=True)
        # Keep the escalation spatially distributed rather than spending the
        # whole allowance on adjacent samples from one difficult frame.
        selected_indices: list[int] = []
        for _, index in uncertain:
            if any(abs(samples[index].t_query - samples[other].t_query) < 0.20 for other in selected_indices):
                continue
            selected_indices.append(index)
        return selected_indices

    @staticmethod
    def _query_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
        rgb = image.convert("RGB")
        width, height = rgb.size
        variants: list[tuple[str, Image.Image]] = []
        landscape_height = min(height, int(round(width * 9.0 / 16.0)))
        if landscape_height < height:
            top = (height - landscape_height) // 2
            variants.append(
                ("center_landscape", rgb.crop((0, top, width, top + landscape_height)))
            )
        gray = np.asarray(ImageOps.grayscale(rgb))
        row_energy = gray.mean(axis=1)
        non_dark = np.where(row_energy > 8.0)[0]
        if non_dark.size:
            top = int(non_dark[0])
            bottom = int(non_dark[-1]) + 1
            if bottom - top >= height * 0.65 and (top > 2 or bottom < height - 2):
                variants.append(("trim_bars", rgb.crop((0, top, width, bottom))))
        if len(variants) < 2 and height > width:
            wide_width = max(width, int(round(height * 16.0 / 9.0)))
            background = ImageOps.fit(rgb, (wide_width, height)).filter(
                ImageFilter.GaussianBlur(radius=max(2, width // 40))
            )
            background.paste(rgb, ((wide_width - width) // 2, 0))
            variants.append(("wide_pad", ImageOps.contain(background, (384, 384))))
        return variants[:2]

    @classmethod
    def _merge_variant_candidates(
        cls,
        base: list[list[RetrievalCandidate]],
        variants: list[QueryFrame],
        variant_to_sample: list[int],
        anime_name: str | None,
        episode_whitelist: frozenset[str] | None = None,
    ) -> None:
        processor = AnimeMatcherService._query_processor
        if processor is None or not variants:
            return
        embeddings = np.stack([frame.embedding for frame in variants]).astype(
            np.float32, copy=False
        )
        started = time.perf_counter()
        raw = processor.index_manager.search_batch(
            embeddings, RETRIEVAL_TOP_K, None, series=anime_name
        )
        AnimeMatcherService._record_runtime_stat(
            "faiss_search_seconds", time.perf_counter() - started
        )
        AnimeMatcherService._record_runtime_stat("faiss_search_queries", len(variants))
        for frame, sample_index, values in zip(
            variants, variant_to_sample, raw, strict=False
        ):
            extra = [
                RetrievalCandidate(
                    sample_index=sample_index,
                    t_query=frame.t_query,
                    episode=metadata.episode,
                    t_source=float(metadata.timestamp),
                    similarity=float(similarity),
                    series=metadata.series,
                    variant_id=frame.variant_id,
                )
                for similarity, metadata in values
                if float(similarity) >= 0.20
                and (
                    episode_whitelist is None
                    or canonical_episode_stem(metadata.episode) in episode_whitelist
                )
            ]
            base[sample_index] = cls._dedupe_candidates(base[sample_index] + extra)

    @staticmethod
    def _fit_points(
        points: list[RetrievalCandidate],
    ) -> tuple[float, float, float]:
        if not points:
            return 1.0, 0.0, 0.0
        if len(points) == 1:
            point = points[0]
            return 1.0, point.t_source - point.t_query, 0.0
        x = np.asarray([point.t_query for point in points], dtype=np.float64)
        y = np.asarray([point.t_source for point in points], dtype=np.float64)
        w = np.asarray(
            [max(0.05, point.similarity) ** 2 for point in points],
            dtype=np.float64,
        )
        a = 1.0
        b = float(np.average(y - x, weights=w))
        for _ in range(3):
            residual = y - (a * x + b)
            mask = np.abs(residual) <= TRACK_RESIDUAL_SECONDS
            if int(mask.sum()) < 2:
                break
            xx, yy, ww = x[mask], y[mask], w[mask]
            x_mean = float(np.average(xx, weights=ww))
            y_mean = float(np.average(yy, weights=ww))
            denom = float(np.sum(ww * (xx - x_mean) ** 2))
            if denom > 1e-9:
                a = float(np.sum(ww * (xx - x_mean) * (yy - y_mean)) / denom)
                b = y_mean - a * x_mean
        if not (MIN_SOURCE_RATE <= a <= MAX_SOURCE_RATE):
            a = 1.0
            b = float(np.average(y - x, weights=w))
        residual = float(np.median(np.abs(y - (a * x + b))))
        # Indexed timestamps live on a 0.5s grid. Free slopes can explain
        # which side of that grid adjacent samples landed on while producing
        # implausible 2-4x playback. Prefer real-time playback whenever it is
        # statistically indistinguishable at the matching tolerance.
        unit_b = float(np.average(y - x, weights=w))
        unit_residual = float(np.median(np.abs(y - (x + unit_b))))
        unit_equivalence_margin = 0.08 + 0.04 * abs(math.log(max(a, 1e-6)))
        if unit_residual <= residual + unit_equivalence_margin:
            a, b, residual = 1.0, unit_b, unit_residual
        return a, b, residual

    @staticmethod
    def _snap_boundary(
        timestamp: float,
        detector_boundaries: list[float],
        strong_boundaries: list[float],
    ) -> float:
        # PySceneDetect boundaries are actual cut estimates.  The native diff
        # curve is useful only when the detector missed a cut; preferring a
        # nearby motion/subtitle peak used to move correct cuts by several
        # frames and could expose the preceding source shot in the render.
        nearby_detector = [
            value for value in detector_boundaries if abs(value - timestamp) <= 0.30
        ]
        if nearby_detector:
            return min(nearby_detector, key=lambda value: abs(value - timestamp))
        nearby_strong = [
            value for value in strong_boundaries if abs(value - timestamp) <= 0.45
        ]
        if nearby_strong:
            return min(nearby_strong, key=lambda value: abs(value - timestamp))
        return timestamp

    @classmethod
    def _segments_from_state(
        cls,
        state: _BeamState,
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
        duration: float,
        detector_boundaries: list[float],
        strong_boundaries: list[float],
    ) -> list[TrackSegment]:
        if not samples:
            return [TrackSegment(0.0, duration, None, uncertain=True, doubt_reasons=["no_samples"])]
        groups: list[tuple[int, int, list[RetrievalCandidate]]] = []
        start = 0
        current: list[RetrievalCandidate] = []
        for index, point in enumerate(state.path):
            reset = index == 0 or state.breaks[index]
            point_episode = point.episode if point else None
            current_episode = current[-1].episode if current else None
            if index > start and (reset or point_episode != current_episode):
                groups.append((start, index - 1, current))
                start = index
                current = []
            if point is not None:
                current.append(point)
        groups.append((start, len(samples) - 1, current))

        raw_boundaries = [0.0]
        for left, right in zip(groups, groups[1:], strict=False):
            left_end = samples[left[1]].t_query
            right_start = samples[right[0]].t_query
            raw_boundaries.append(
                cls._snap_boundary(
                    0.5 * (left_end + right_start),
                    detector_boundaries,
                    strong_boundaries,
                )
            )
        raw_boundaries.append(duration)

        # Enforce monotone, non-empty intervals after snapping.
        for index in range(1, len(raw_boundaries) - 1):
            lower = raw_boundaries[index - 1] + 0.02
            upper = raw_boundaries[index + 1] - 0.02
            raw_boundaries[index] = min(max(raw_boundaries[index], lower), upper)

        result: list[TrackSegment] = []
        for group_index, (_, _, points) in enumerate(groups):
            q_start = raw_boundaries[group_index]
            q_end = raw_boundaries[group_index + 1]
            if not points:
                result.append(
                    TrackSegment(
                        q_start,
                        q_end,
                        None,
                        uncertain=True,
                        doubt_reasons=["no_evidence"],
                    )
                )
                continue
            a, b, residual = cls._fit_points(points)
            confidence = float(np.median([point.similarity for point in points]))
            support_expected = max(1, int(round((q_end - q_start) * BASE_SAMPLE_FPS)))
            support_ratio = min(1.0, len(points) / support_expected)
            reasons: list[str] = []
            if confidence < 0.36:
                reasons.append("weak_similarity")
            if support_ratio < 0.50:
                reasons.append("sparse_support")
            if residual > 0.45:
                reasons.append("timing_residual")
            result.append(
                TrackSegment(
                    q_start=q_start,
                    q_end=q_end,
                    episode=points[0].episode,
                    a=a,
                    b=b,
                    points=points,
                    confidence=confidence,
                    residual=residual,
                    uncertain=bool(reasons),
                    doubt_reasons=reasons,
                )
            )
        for segment in result:
            if segment.episode is None:
                continue
            midpoint = 0.5 * (segment.q_start + segment.q_end)
            required_support = max(2, int(math.ceil(len(segment.points) * 0.30)))
            for proposal in cls._line_proposals(segment, candidates, samples):
                distinct = (
                    proposal.episode != segment.episode
                    or abs(proposal.source_at(midpoint) - segment.source_at(midpoint))
                    >= 2.0
                )
                if (
                    distinct
                    and proposal.support >= required_support
                    and proposal.confidence >= segment.confidence - 0.04
                ):
                    segment.uncertain = True
                    segment.doubt_reasons.append("duplicate_margin")
                    break
        return result

    @classmethod
    def _proposal_for_interval(
        cls,
        parent: TrackSegment,
        q_start: float,
        q_end: float,
        candidates: list[list[RetrievalCandidate]],
        samples: list[QueryFrame],
    ) -> LineProposal | None:
        """Return a well-supported local line, excluding one-frame anchors."""
        indices = [
            index
            for index, sample in enumerate(samples)
            if q_start <= sample.t_query < q_end
        ]
        if len(indices) < 2:
            return None
        probe = TrackSegment(
            q_start,
            q_end,
            parent.episode,
            parent.a,
            parent.b,
            confidence=parent.confidence,
        )
        required = max(2, int(math.ceil(len(indices) * 0.35)))
        supported = [
            proposal
            for proposal in cls._line_proposals(probe, candidates, samples)
            if proposal.support >= required
            and proposal.confidence >= max(0.32, parent.confidence - 0.10)
        ]
        return supported[0] if supported else None

    @classmethod
    def _segment_from_proposal(
        cls,
        parent: TrackSegment,
        q_start: float,
        q_end: float,
        proposal: LineProposal,
        candidates: list[list[RetrievalCandidate]],
        samples: list[QueryFrame],
    ) -> TrackSegment:
        points: list[RetrievalCandidate] = []
        for index, sample in enumerate(samples):
            if not (q_start <= sample.t_query < q_end):
                continue
            nearby = [
                candidate
                for candidate in candidates[index]
                if candidate.episode == proposal.episode
                and abs(candidate.t_source - proposal.source_at(sample.t_query))
                <= TRACK_RESIDUAL_SECONDS
            ]
            if nearby:
                points.append(
                    max(
                        nearby,
                        key=lambda candidate: candidate.similarity
                        - 0.06
                        * abs(
                            candidate.t_source
                            - proposal.source_at(sample.t_query)
                        ),
                    )
                )
        if len(points) >= 2:
            a, b, residual = cls._fit_points(points)
            confidence = float(np.median([point.similarity for point in points]))
        else:
            a, b, residual = proposal.a, proposal.b, 0.0
            confidence = proposal.confidence
        return TrackSegment(
            q_start=q_start,
            q_end=q_end,
            episode=proposal.episode,
            a=a,
            b=b,
            points=points,
            confidence=confidence,
            residual=residual,
            uncertain=True,
            doubt_reasons=sorted(
                set(parent.doubt_reasons + ["detector_discontinuity"])
            ),
        )

    @classmethod
    def _line_evidence(
        cls,
        episode: str,
        a: float,
        b: float,
        q_start: float,
        q_end: float,
        candidates: list[list[RetrievalCandidate]],
        samples: list[QueryFrame],
    ) -> tuple[int, float]:
        similarities: list[float] = []
        for index, sample in enumerate(samples):
            if not (q_start <= sample.t_query < q_end):
                continue
            predicted = a * sample.t_query + b
            supported = [
                candidate.similarity
                for candidate in candidates[index]
                if candidate.episode == episode
                and abs(candidate.t_source - predicted) <= TRACK_RESIDUAL_SECONDS
            ]
            if supported:
                similarities.append(max(supported))
        return len(similarities), (
            float(np.median(similarities)) if similarities else 0.0
        )

    @classmethod
    def _split_supported_discontinuities(
        cls,
        segments: list[TrackSegment],
        detector_boundaries: list[float],
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
    ) -> list[TrackSegment]:
        """Split only where both sides support incompatible source lines.

        Detector cuts are deliberately over-complete. They do not force beam
        resets; they provide bounded places at which two correspondence
        clusters can prove a missed source-timeline jump.
        """

        def split_one(segment: TrackSegment, depth: int = 0) -> list[TrackSegment]:
            if segment.episode is None or depth >= 8:
                return [segment]
            choices: list[tuple[float, float, LineProposal, LineProposal]] = []
            for boundary in detector_boundaries:
                if (
                    boundary - segment.q_start < 0.55
                    or segment.q_end - boundary < 0.55
                ):
                    continue
                left = cls._proposal_for_interval(
                    segment,
                    max(segment.q_start, boundary - 2.5),
                    boundary,
                    candidates,
                    samples,
                )
                right = cls._proposal_for_interval(
                    segment,
                    boundary,
                    min(segment.q_end, boundary + 2.5),
                    candidates,
                    samples,
                )
                if left is None or right is None:
                    continue
                if left.episode != right.episode:
                    discontinuity = 100.0
                else:
                    discontinuity = abs(
                        right.source_at(boundary) - left.source_at(boundary)
                    )
                if discontinuity <= TRACK_RESIDUAL_SECONDS:
                    continue

                parent_source = segment.source_at(boundary)
                left_distance = (
                    abs(left.source_at(boundary) - parent_source)
                    if left.episode == segment.episode
                    else 100.0
                )
                right_distance = (
                    abs(right.source_at(boundary) - parent_source)
                    if right.episode == segment.episode
                    else 100.0
                )
                # One side must explain the current beam track. This rejects
                # unrelated high-scoring duplicate clusters on both sides.
                if min(left_distance, right_distance) > TRACK_RESIDUAL_SECONDS:
                    continue
                far = right if right_distance >= left_distance else left
                far_start, far_end = (
                    (boundary, min(segment.q_end, boundary + 2.5))
                    if far is right
                    else (max(segment.q_start, boundary - 2.5), boundary)
                )
                far_support, far_similarity = cls._line_evidence(
                    far.episode,
                    far.a,
                    far.b,
                    far_start,
                    far_end,
                    candidates,
                    samples,
                )
                parent_support, parent_similarity = cls._line_evidence(
                    segment.episode,
                    segment.a,
                    segment.b,
                    far_start,
                    far_end,
                    candidates,
                    samples,
                )
                interval_samples = sum(
                    far_start <= sample.t_query < far_end for sample in samples
                )
                required = max(2, int(math.ceil(interval_samples * 0.35)))
                if far_support < required:
                    continue
                # When the beam's line remains well supported, a competing
                # duplicate must win by a real similarity margin. This keeps
                # ordinary detector cuts continuous.
                if (
                    parent_support >= required
                    and far_similarity < parent_similarity + 0.035
                    and far.confidence < segment.confidence + 0.07
                ):
                    continue
                if far_similarity < segment.confidence - 0.04:
                    continue
                evidence = discontinuity + 0.25 * (
                    left.confidence + right.confidence + far_similarity
                )
                choices.append((evidence, boundary, left, right))

            if not choices:
                return [segment]
            _, boundary, left, right = max(choices, key=lambda value: value[0])
            left_segment = cls._segment_from_proposal(
                segment,
                segment.q_start,
                boundary,
                left,
                candidates,
                samples,
            )
            right_segment = cls._segment_from_proposal(
                segment,
                boundary,
                segment.q_end,
                right,
                candidates,
                samples,
            )
            return split_one(left_segment, depth + 1) + split_one(
                right_segment, depth + 1
            )

        output: list[TrackSegment] = []
        for segment in segments:
            output.extend(split_one(segment))
        return output

    @classmethod
    def _promote_dominant_proposals(
        cls,
        segments: list[TrackSegment],
        candidates: list[list[RetrievalCandidate]],
        samples: list[QueryFrame],
    ) -> list[TrackSegment]:
        """Let broad top-60 consensus correct a weaker top-20 beam line."""
        output: list[TrackSegment] = []
        for segment in segments:
            if segment.episode is None:
                output.append(segment)
                continue
            proposals = cls._line_proposals(segment, candidates, samples)
            if not proposals:
                output.append(segment)
                continue
            proposal = proposals[0]
            midpoint = 0.5 * (segment.q_start + segment.q_end)
            distinct = (
                proposal.episode != segment.episode
                or abs(proposal.source_at(midpoint) - segment.source_at(midpoint))
                >= 2.0
            )
            required = max(3, int(math.ceil(len(segment.points) * 0.80)))
            if (
                distinct
                and proposal.support >= required
                and proposal.confidence >= segment.confidence + 0.05
            ):
                promoted = cls._segment_from_proposal(
                    segment,
                    segment.q_start,
                    segment.q_end,
                    proposal,
                    candidates,
                    samples,
                )
                promoted.doubt_reasons = sorted(
                    set(promoted.doubt_reasons + ["dominant_retrieval"])
                )
                output.append(promoted)
            else:
                output.append(segment)
        return output

    @classmethod
    def _merge_continuous_segments(
        cls, segments: list[TrackSegment]
    ) -> list[TrackSegment]:
        merged: list[TrackSegment] = []
        for segment in segments:
            if not merged:
                merged.append(segment)
                continue
            previous = merged[-1]
            if previous.episode is None and segment.episode is None:
                previous.q_end = segment.q_end
                previous.doubt_reasons = sorted(
                    set(previous.doubt_reasons + segment.doubt_reasons)
                )
                continue
            if previous.episode is None or previous.episode != segment.episode:
                merged.append(segment)
                continue
            boundary = segment.q_start
            source_gap = segment.source_at(boundary) - previous.source_at(boundary)
            if abs(source_gap) > TRACK_RESIDUAL_SECONDS or abs(segment.a - previous.a) > 0.60:
                merged.append(segment)
                continue
            points = previous.points + segment.points
            if points:
                a, b, residual = cls._fit_points(points)
                confidence = float(
                    np.median([point.similarity for point in points])
                )
            else:
                a = 0.5 * (previous.a + segment.a)
                b = 0.5 * (previous.b + segment.b)
                residual = max(previous.residual, segment.residual)
                confidence = min(previous.confidence, segment.confidence)
            merged[-1] = TrackSegment(
                q_start=previous.q_start,
                q_end=segment.q_end,
                episode=previous.episode,
                a=a,
                b=b,
                points=points,
                confidence=confidence,
                residual=residual,
                uncertain=previous.uncertain or segment.uncertain,
                doubt_reasons=sorted(
                    set(previous.doubt_reasons + segment.doubt_reasons)
                ),
            )
        return merged

    @staticmethod
    def _absorb_tiny_segments(
        segments: list[TrackSegment], minimum_duration: float = 0.35
    ) -> list[TrackSegment]:
        """Remove snap-created slivers without erasing real short edits."""
        result = list(segments)
        index = 0
        while len(result) > 1 and index < len(result):
            segment = result[index]
            duration = segment.q_end - segment.q_start
            is_tiny = duration < minimum_duration
            is_short_evidence_hole = (
                segment.episode is None
                and duration <= DUPLICATE_MICRO_MAX_SECONDS
            )
            if not is_tiny and not is_short_evidence_hole:
                index += 1
                continue
            if index + 1 < len(result):
                result[index + 1].q_start = segment.q_start
                result[index + 1].doubt_reasons = sorted(
                    set(result[index + 1].doubt_reasons + segment.doubt_reasons)
                )
                result.pop(index)
                continue
            result[index - 1].q_end = segment.q_end
            result[index - 1].doubt_reasons = sorted(
                set(result[index - 1].doubt_reasons + segment.doubt_reasons)
            )
            result.pop(index)
            index = max(0, index - 1)
        return result

    @staticmethod
    def _boundary_diff_strength(
        timestamp: float,
        diff_times: list[float],
        diffs: list[float],
    ) -> float:
        """Return the strongest native-frame change immediately at a boundary."""
        if not diff_times or len(diff_times) != len(diffs):
            return 0.0
        nearby = [
            float(value)
            for time_value, value in zip(diff_times, diffs, strict=False)
            if abs(float(time_value) - timestamp) <= 0.08
        ]
        if nearby:
            return max(nearby)
        nearest = min(
            range(len(diff_times)),
            key=lambda value: abs(float(diff_times[value]) - timestamp),
        )
        if abs(float(diff_times[nearest]) - timestamp) <= 0.15:
            return float(diffs[nearest])
        return 0.0

    @classmethod
    def _absorb_weak_micro_segments(
        cls,
        segments: list[TrackSegment],
        candidates: list[list[RetrievalCandidate]],
        samples: list[QueryFrame],
        diff_times: list[float],
        diffs: list[float],
    ) -> list[TrackSegment]:
        """Absorb only retrieval-disproved micro-fragments into a neighbour.

        The ordinary sliver absorber intentionally stops at 0.35s.  Slightly
        longer false fragments need a stronger certificate: their selected
        track must be weak, while an adjacent track must have a supported
        proposal *inside the micro-fragment* that meets its affine source line
        at the shared boundary.  A sampling hole is accepted only between two
        strong, same-episode, affine-continuous flanks.  This preserves genuine
        confident 0.5s edits.

        If both sides qualify, a clearly weaker visual cut is removed.  With
        comparable cuts, the better-supported affine continuation wins.
        """
        result = list(segments)
        index = 0
        while len(result) > 1 and index < len(result):
            micro = result[index]
            duration = micro.q_end - micro.q_start
            if (
                not WEAK_MICRO_MIN_SECONDS <= duration <= WEAK_MICRO_MAX_SECONDS
                or micro.episode is None
                or micro.confidence > WEAK_MICRO_MAX_CONFIDENCE
                or "weak_similarity" not in micro.doubt_reasons
            ):
                index += 1
                continue

            proposals = cls._line_proposals(micro, candidates, samples)
            required_support = max(
                1,
                int(math.ceil(duration * BASE_SAMPLE_FPS * 0.50)),
            )
            choices: list[
                tuple[str, float, TrackSegment, LineProposal]
            ] = []
            for side, neighbor in (
                ("left", result[index - 1] if index > 0 else None),
                ("right", result[index + 1] if index + 1 < len(result) else None),
            ):
                if (
                    neighbor is None
                    or neighbor.episode is None
                    or neighbor.episode == micro.episode
                    or neighbor.confidence < WEAK_MICRO_NEIGHBOR_MIN_CONFIDENCE
                    or neighbor.q_end - neighbor.q_start < 1.0
                ):
                    continue
                boundary = micro.q_start if side == "left" else micro.q_end
                supported = [
                    proposal
                    for proposal in proposals
                    if proposal.episode == neighbor.episode
                    and proposal.support >= required_support
                    and proposal.confidence
                    >= max(
                        WEAK_MICRO_PROPOSAL_MIN_CONFIDENCE,
                        micro.confidence + WEAK_MICRO_PROPOSAL_MARGIN,
                    )
                    and abs(
                        proposal.source_at(boundary)
                        - neighbor.source_at(boundary)
                    )
                    <= TRACK_RESIDUAL_SECONDS
                ]
                if not supported:
                    continue
                proposal = max(
                    supported,
                    key=lambda value: (
                        value.confidence,
                        value.support,
                    ),
                )
                gap = abs(
                    proposal.source_at(boundary) - neighbor.source_at(boundary)
                )
                score = (
                    proposal.confidence
                    + 0.04 * min(proposal.support, 4)
                    + 0.15 * neighbor.confidence
                    - 0.12 * gap
                )
                choices.append((side, score, neighbor, proposal))

            # A short fragment can fall between useful retrieval probes.  In
            # that specific hole, two strong flanking tracks are an equivalent
            # certificate when they name the same episode and their affine
            # mappings agree over the whole fragment.  Do not use this for a
            # one-sided guess.
            left_neighbor = result[index - 1] if index > 0 else None
            right_neighbor = (
                result[index + 1] if index + 1 < len(result) else None
            )
            bridge_neighbors = (left_neighbor, right_neighbor)
            bridge_is_supported = (
                all(
                    neighbor is not None
                    and neighbor.episode is not None
                    and neighbor.episode != micro.episode
                    and neighbor.confidence >= WEAK_MICRO_NEIGHBOR_MIN_CONFIDENCE
                    and neighbor.q_end - neighbor.q_start >= 1.0
                    for neighbor in bridge_neighbors
                )
                and left_neighbor is not None
                and right_neighbor is not None
                and left_neighbor.episode == right_neighbor.episode
                and max(
                    abs(
                        left_neighbor.source_at(timestamp)
                        - right_neighbor.source_at(timestamp)
                    )
                    for timestamp in (micro.q_start, micro.q_end)
                )
                <= TRACK_RESIDUAL_SECONDS
            )
            if bridge_is_supported:
                existing_sides = {choice[0] for choice in choices}
                bridge_confidence = min(
                    left_neighbor.confidence,
                    right_neighbor.confidence,
                )
                for side, neighbor in (
                    ("left", left_neighbor),
                    ("right", right_neighbor),
                ):
                    if side in existing_sides:
                        continue
                    proposal = LineProposal(
                        episode=neighbor.episode,
                        a=neighbor.a,
                        b=neighbor.b,
                        confidence=bridge_confidence,
                        support=0,
                        algorithm="continuous_bridge",
                    )
                    score = bridge_confidence + 0.15 * neighbor.confidence
                    choices.append((side, score, neighbor, proposal))

            if not choices:
                index += 1
                continue

            by_side = {choice[0]: choice for choice in choices}
            chosen: tuple[str, float, TrackSegment, LineProposal]
            left_strength = cls._boundary_diff_strength(
                micro.q_start, diff_times, diffs
            )
            right_strength = cls._boundary_diff_strength(
                micro.q_end, diff_times, diffs
            )
            if (
                "right" in by_side
                and left_strength > 0.0
                and right_strength
                <= WEAK_MICRO_CLEAR_CUT_RATIO * left_strength
            ):
                chosen = by_side["right"]
            elif (
                "left" in by_side
                and right_strength > 0.0
                and left_strength
                <= WEAK_MICRO_CLEAR_CUT_RATIO * right_strength
            ):
                chosen = by_side["left"]
            else:
                chosen = max(choices, key=lambda value: value[1])

            side, _, neighbor, proposal = chosen
            evidence = cls._segment_from_proposal(
                micro,
                micro.q_start,
                micro.q_end,
                proposal,
                candidates,
                samples,
            )
            points = sorted(
                neighbor.points + evidence.points,
                key=lambda value: value.t_query,
            )
            if points:
                a, b, residual = cls._fit_points(points)
                confidence = float(
                    np.median([point.similarity for point in points])
                )
            else:  # Defensive: the proposal support certificate normally prevents this.
                a, b, residual = neighbor.a, neighbor.b, neighbor.residual
                confidence = neighbor.confidence
            reasons = sorted(
                set(neighbor.doubt_reasons + ["weak_micro_absorbed"])
            )
            uncertain = neighbor.uncertain
            if residual > 0.45:
                reasons = sorted(set(reasons + ["timing_residual"]))
                uncertain = True
            extended = TrackSegment(
                q_start=micro.q_start if side == "right" else neighbor.q_start,
                q_end=micro.q_end if side == "left" else neighbor.q_end,
                episode=neighbor.episode,
                a=a,
                b=b,
                points=points,
                confidence=confidence,
                residual=residual,
                uncertain=uncertain,
                doubt_reasons=reasons,
            )
            if side == "left":
                result[index - 1] = extended
                result.pop(index)
                index = max(0, index - 1)
            else:
                result[index + 1] = extended
                result.pop(index)
        return result

    @classmethod
    def _collapse_leading_duplicate_regions(
        cls,
        segments: list[TrackSegment],
    ) -> list[TrackSegment]:
        """Pool a tiny leading duplicate with a longer supported track.

        This is the automatic counterpart of the manual merge/rematch path.
        It does not merely stretch the following match: the combined points
        are collapsed and refit, the longer track must win that pooled vote,
        and its affine mapping must remain compatible at the join.  Native
        verification then evaluates the combined region as one unit.
        """
        result = list(segments)
        index = 0
        while index + 1 < len(result):
            micro = result[index]
            following = result[index + 1]
            if (
                micro.episode is None
                or following.episode is None
                or micro.episode != following.episode
                or micro.q_end - micro.q_start
                < DUPLICATE_REGION_MIN_SECONDS
                or micro.q_end - micro.q_start
                > DUPLICATE_MICRO_DETECTOR_MAX_SECONDS
                or following.q_end - following.q_start < 1.0
                or following.confidence < 0.48
                or "duplicate_margin" not in micro.doubt_reasons
                or abs(
                    following.source_at(micro.q_end)
                    - micro.source_at(micro.q_end)
                )
                < 2.0
            ):
                index += 1
                continue
            collapsed = cls._collapse_to_single_segment(
                [micro, following],
                micro.q_start,
                following.q_end,
            )
            if (
                collapsed.episode != following.episode
                or collapsed.confidence
                < micro.confidence - DUPLICATE_MICRO_CONFIDENCE_MARGIN
                or abs(
                    collapsed.source_at(micro.q_end)
                    - following.source_at(micro.q_end)
                )
                > TRACK_RESIDUAL_SECONDS
            ):
                index += 1
                continue
            collapsed.uncertain = True
            collapsed.doubt_reasons = sorted(
                set(
                    collapsed.doubt_reasons
                    + ["duplicate_region_collapsed"]
                )
            )
            result[index : index + 2] = [collapsed]
            index = max(0, index - 1)
        return result

    @classmethod
    def _line_proposals(
        cls,
        segment: TrackSegment,
        candidates: list[list[RetrievalCandidate]],
        samples: list[QueryFrame],
    ) -> list[LineProposal]:
        if segment.q_end <= segment.q_start:
            return []
        sample_indices = [
            index
            for index, sample in enumerate(samples)
            if segment.q_start <= sample.t_query < segment.q_end
        ]
        rate = segment.a if segment.episode else 1.0
        clusters: dict[tuple[str, int], list[RetrievalCandidate]] = {}
        # Internal retrieval clusters stay fine-grained enough for native
        # duplicate arbitration. The UI applies its broader scene-duration
        # separation later when choosing diverse alternatives.
        separation = 2.0
        for index in sample_indices:
            for candidate in candidates[index]:
                offset = candidate.t_source - rate * candidate.t_query
                key = (candidate.episode, round(offset / separation))
                clusters.setdefault(key, []).append(candidate)
        proposals: list[LineProposal] = []
        for values in clusters.values():
            distinct_times = {round(value.t_query, 3) for value in values}
            if not distinct_times:
                continue
            weights = np.asarray(
                [max(0.05, value.similarity) ** 2 for value in values]
            )
            offsets = np.asarray(
                [value.t_source - rate * value.t_query for value in values]
            )
            proposals.append(
                LineProposal(
                    episode=values[0].episode,
                    a=rate,
                    b=float(np.average(offsets, weights=weights)),
                    confidence=float(max(value.similarity for value in values)),
                    support=len(distinct_times),
                    algorithm=(
                        "crop_variant"
                        if any(value.variant_id != "plain" for value in values)
                        else "timeline_cluster"
                    ),
                )
            )

        for label, target in (
            ("start_anchor", segment.q_start),
            ("middle_anchor", 0.5 * (segment.q_start + segment.q_end)),
            ("end_anchor", segment.q_end),
        ):
            if not sample_indices:
                continue
            index = min(
                sample_indices,
                key=lambda value: abs(samples[value].t_query - target),
            )
            if candidates[index]:
                candidate = candidates[index][0]
                proposals.append(
                    LineProposal(
                        candidate.episode,
                        rate,
                        candidate.t_source - rate * candidate.t_query,
                        candidate.similarity,
                        1,
                        label,
                    )
                )

        proposals.sort(
            key=lambda value: (
                min(value.support, 8) * 0.05 + value.confidence,
                value.support,
            ),
            reverse=True,
        )
        deduped: list[LineProposal] = []
        for proposal in proposals:
            midpoint = proposal.source_at(0.5 * (segment.q_start + segment.q_end))
            if any(
                other.episode == proposal.episode
                and abs(
                    other.source_at(0.5 * (segment.q_start + segment.q_end))
                    - midpoint
                )
                < separation
                for other in deduped
            ):
                continue
            deduped.append(proposal)
        return deduped[: MAX_ALTERNATIVES + 2]

    @classmethod
    def _proposal_neighbor_distance(
        cls,
        segments: list[TrackSegment],
        segment_index: int,
        proposal: LineProposal,
    ) -> float:
        segment = segments[segment_index]
        distances: list[float] = []
        if segment_index > 0:
            previous = segments[segment_index - 1]
            if previous.episode == proposal.episode:
                distances.append(
                    abs(
                        proposal.source_at(segment.q_start)
                        - previous.source_at(segment.q_start)
                    )
                )
        if segment_index + 1 < len(segments):
            following = segments[segment_index + 1]
            if following.episode == proposal.episode:
                distances.append(
                    abs(
                        following.source_at(segment.q_end)
                        - proposal.source_at(segment.q_end)
                    )
                )
        return min(distances, default=math.inf)

    @classmethod
    def _verification_alternative(
        cls,
        segments: list[TrackSegment],
        segment_index: int,
        proposals: list[LineProposal],
    ) -> tuple[LineProposal | None, float]:
        """Choose the most useful distinct track for bounded native checking.

        Retrieval proposals are already paid for.  The first globally ranked
        duplicate is often remote, while a slightly lower-ranked proposal is
        the obvious continuation of the adjacent source timeline.  Prefer the
        latter when its retrieval confidence is still competitive; otherwise
        retain the strongest independent proposal.
        """
        segment = segments[segment_index]
        midpoint = 0.5 * (segment.q_start + segment.q_end)
        distinct = [
            proposal
            for proposal in proposals
            if proposal.episode != segment.episode
            or abs(
                proposal.source_at(midpoint) - segment.source_at(midpoint)
            )
            >= 2.0
        ]
        # A short duplicate island may have lost the adjacent cluster from its
        # own top proposals even though the neighbour is a strong affine
        # hypothesis.  Native verification is exactly the place to test that
        # hypothesis: add it without treating it as retrieval proof, and merge
        # only if native frames actually beat the island.
        if segment.q_end - segment.q_start <= DUPLICATE_MICRO_MAX_SECONDS:
            for neighbor in (
                segments[segment_index - 1] if segment_index > 0 else None,
                (
                    segments[segment_index + 1]
                    if segment_index + 1 < len(segments)
                    else None
                ),
            ):
                if (
                    neighbor is None
                    or neighbor.episode is None
                    or neighbor.q_end - neighbor.q_start < 0.75
                ):
                    continue
                proposal = LineProposal(
                    neighbor.episode,
                    neighbor.a,
                    neighbor.b,
                    neighbor.confidence,
                    len(neighbor.points),
                    "neighbor_continuation",
                )
                if (
                    proposal.episode == segment.episode
                    and abs(
                        proposal.source_at(midpoint)
                        - segment.source_at(midpoint)
                    )
                    < 2.0
                ):
                    continue
                if any(
                    existing.episode == proposal.episode
                    and abs(
                        existing.source_at(midpoint)
                        - proposal.source_at(midpoint)
                    )
                    < 2.0
                    for existing in distinct
                ):
                    continue
                distinct.append(proposal)
        if not distinct:
            return None, math.inf

        plausible_gap = max(3.0, 2.0 * (segment.q_end - segment.q_start))
        local = [
            (
                cls._proposal_neighbor_distance(
                    segments,
                    segment_index,
                    proposal,
                ),
                proposal,
            )
            for proposal in distinct
            if proposal.confidence >= segment.confidence - 0.08
            and cls._proposal_neighbor_distance(
                segments,
                segment_index,
                proposal,
            )
            <= plausible_gap
        ]
        if local:
            distance, proposal = min(
                local,
                key=lambda value: (
                    value[0],
                    -value[1].confidence,
                    -value[1].support,
                ),
            )
            return proposal, distance
        proposal = max(
            distinct,
            key=lambda value: (
                value.confidence + 0.025 * min(value.support, 6),
                value.support,
            ),
        )
        return proposal, cls._proposal_neighbor_distance(
            segments,
            segment_index,
            proposal,
        )

    @classmethod
    def _verification_priority(
        cls,
        segments: list[TrackSegment],
        segment_index: int,
        alternative: LineProposal | None,
        neighbor_distance: float,
    ) -> float:
        """Expected ambiguity reduction per fixed-size native window."""
        segment = segments[segment_index]
        priority = 0.10 * min(2.0, segment.q_end - segment.q_start)
        if "duplicate_margin" in segment.doubt_reasons:
            priority += 0.20
        if "dominant_retrieval" in segment.doubt_reasons:
            priority += 0.20
        if "duplicate_region_collapsed" in segment.doubt_reasons:
            priority += 3.0
        priority += max(0.0, 0.40 - segment.confidence)
        if alternative is None:
            return priority
        priority += 8.0 * max(
            0.0,
            alternative.confidence - segment.confidence + 0.01,
        )
        if alternative.episode != segment.episode:
            priority += 0.40
        if alternative.algorithm == "neighbor_continuation":
            priority += 3.0
        plausible_gap = max(3.0, 2.0 * (segment.q_end - segment.q_start))
        left_distance = math.inf
        right_distance = math.inf
        if segment_index > 0:
            previous = segments[segment_index - 1]
            if previous.episode == alternative.episode:
                left_distance = abs(
                    alternative.source_at(segment.q_start)
                    - previous.source_at(segment.q_start)
                )
        if segment_index + 1 < len(segments):
            following = segments[segment_index + 1]
            if following.episode == alternative.episode:
                right_distance = abs(
                    following.source_at(segment.q_end)
                    - alternative.source_at(segment.q_end)
                )
        # An interior proposal must join *both* sides before continuity can
        # spend scarce native budget.  Using only the nearer side propagated a
        # wrong duplicate into an otherwise-correct neighbour.  The final
        # segment has only a left side and is intentionally allowed: this is a
        # common place for the last detector sliver to choose a duplicate.
        continuity_certificate = (
            segment_index == len(segments) - 1
            and left_distance <= plausible_gap
        ) or (
            0 < segment_index < len(segments) - 1
            and left_distance <= plausible_gap
            and right_distance <= plausible_gap
        )
        if continuity_certificate:
            certified_distance = max(
                value
                for value in (left_distance, right_distance)
                if not math.isinf(value)
            )
            priority += 1.20 * (
                1.0 - min(certified_distance, plausible_gap) / plausible_gap
            )
        primary = LineProposal(
            segment.episode or "",
            segment.a,
            segment.b,
            segment.confidence,
            len(segment.points),
            "primary",
        )
        primary_distance = cls._proposal_neighbor_distance(
            segments,
            segment_index,
            primary,
        )
        if continuity_certificate and neighbor_distance < primary_distance:
            if math.isinf(primary_distance):
                priority += 2.0
            else:
                priority += min(
                    2.0,
                    max(0.0, (primary_distance - neighbor_distance) / 3.0),
                )
        return priority

    @classmethod
    def _verify_ambiguous_segments(
        cls,
        segments: list[TrackSegment],
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
        library_type: LibraryType | str,
        duration: float,
        deadline: float | None = None,
    ) -> float:
        """Use a capped amount of consolidated source decode to arbitrate a bounded track.

        The return value is decoded source-window duration, the deterministic
        work unit used by the evaluator.

        ``deadline`` is a ``time.perf_counter()`` instant past which no further
        source decode starts, so the caller's wall target bounds this phase and
        not merely its entry.
        """
        from .anime_library import AnimeLibraryService

        budget = min(24.0, max(8.0, 0.15 * duration))
        jobs: list[tuple[float, int, QueryFrame, list[LineProposal]]] = []
        for segment_index, segment in enumerate(segments):
            if segment.episode is None or not segment.uncertain:
                continue
            proposals = [
                LineProposal(
                    segment.episode,
                    segment.a,
                    segment.b,
                    segment.confidence,
                    len(segment.points),
                    "primary",
                )
            ]
            alternative, neighbor_distance = cls._verification_alternative(
                segments,
                segment_index,
                cls._line_proposals(segment, candidates, samples),
            )
            if alternative is not None:
                proposals.append(alternative)
            query = min(
                samples,
                key=lambda value: abs(
                    value.t_query - 0.5 * (segment.q_start + segment.q_end)
                ),
            )
            priority = cls._verification_priority(
                segments,
                segment_index,
                alternative,
                neighbor_distance,
            )
            jobs.append((priority, segment_index, query, proposals[:2]))
        jobs.sort(reverse=True, key=lambda value: value[0])

        accepted: list[tuple[int, QueryFrame, list[LineProposal]]] = []
        windows: dict[str, list[tuple[float, float]]] = {}
        used = 0.0
        for _, segment_index, query, proposals in jobs:
            requested = []
            for proposal in proposals:
                predicted = proposal.source_at(query.t_query)
                requested.append(
                    (
                        proposal.episode,
                        max(0.0, predicted - VERIFY_HALF_WINDOW_SECONDS),
                        predicted + VERIFY_HALF_WINDOW_SECONDS,
                    )
                )
            incremental = sum(end - start for _, start, end in requested)
            if used + incremental > budget:
                segments[segment_index].doubt_reasons.append("verification_budget")
                continue
            used += incremental
            accepted.append((segment_index, query, proposals))
            for episode, start, end in requested:
                windows.setdefault(episode, []).append((start, end))

        embedded_windows: dict[
            str, list[tuple[float, np.ndarray, Image.Image]]
        ] = {}
        actual_decoded_duration = 0.0
        timed_out = False
        for episode, episode_windows in windows.items():
            # Source decode is the expensive half of this phase; stop opening
            # new episodes once the caller's wall target is spent.
            if deadline is not None and time.perf_counter() >= deadline:
                timed_out = True
                break
            path = AnimeLibraryService.resolve_episode_path(
                episode, library_type=library_type
            )
            if path is None or not path.exists():
                continue
            merged_windows: list[list[float]] = []
            for start, end in sorted(episode_windows):
                if merged_windows and start <= merged_windows[-1][1] + 0.10:
                    merged_windows[-1][1] = max(merged_windows[-1][1], end)
                else:
                    merged_windows.append([start, end])
            cap = AnimeMatcherService._open_source_capture(path)
            frames: list[tuple[float, Image.Image]] = []
            try:
                for start, end in merged_windows:
                    if deadline is not None and time.perf_counter() >= deadline:
                        timed_out = True
                        break
                    actual_decoded_duration += end - start
                    frames.extend(
                        AnimeMatcherService._collect_frames_in_window_from_capture(
                            cap,
                            start,
                            end,
                            max_frames=max(4, int(math.ceil((end - start) * 65)) + 4),
                            sample_frames=max(3, int(math.ceil((end - start) * VERIFY_FPS)) + 1),
                        )
                    )
            finally:
                cap.release()
            if frames:
                embeddings = AnimeMatcherService._embed_pil_batch(
                    [image.convert("RGB") for _, image in frames]
                )
                embedded_windows[episode] = [
                    (timestamp, embedding, image)
                    for (timestamp, image), embedding in zip(
                        frames, embeddings, strict=False
                    )
                ]

        for segment_index, query, proposals in accepted:
            scores: list[float] = []
            decoded_any = False
            for proposal in proposals:
                predicted = proposal.source_at(query.t_query)
                frames = embedded_windows.get(proposal.episode, [])
                local = [
                    (timestamp, embedding, image)
                    for timestamp, embedding, image in frames
                    if abs(timestamp - predicted)
                    <= VERIFY_HALF_WINDOW_SECONDS + 0.05
                ]
                decoded_any = decoded_any or bool(local)
                score = (
                    max(float(embedding @ query.embedding) for _, embedding, _ in local)
                    if local
                    else -1.0
                )
                # Registration is paid only for the two arbitration tracks
                # already admitted by the deterministic source-duration
                # budget. It preserves the legacy matcher’s useful geometric
                # duplicate signal without restoring its unbounded tail.
                if local and query.preview is not None:
                    try:
                        from .scene_aligner import SceneAlignerService

                        query_gray = SceneAlignerService._small_gray(query.preview)
                        registered: list[Image.Image] = []
                        for _, _, image in sorted(
                            local, key=lambda value: abs(value[0] - predicted)
                        )[:2]:
                            rect = SceneAlignerService._footprint_rect(
                                query_gray,
                                SceneAlignerService._small_gray(image),
                            )
                            if rect is not None:
                                registered.append(
                                    SceneAlignerService._zoom_crop(image, rect).convert("RGB")
                                )
                        if registered:
                            registered_embeddings = AnimeMatcherService._embed_pil_batch(
                                registered
                            )
                            score = max(
                                score,
                                max(
                                    float(embedding @ query.embedding)
                                    for embedding in registered_embeddings
                                ),
                            )
                    except Exception:
                        pass
                scores.append(score)
            if not scores:
                continue
            segment = segments[segment_index]
            if not decoded_any:
                # No source window was decoded for any proposal (unresolved
                # episode path, or the wall deadline cut the decode short).
                # Absence of evidence must not read as evidence against: leave
                # the retrieval verdict standing instead of rejecting it.
                segment.doubt_reasons.append(
                    "native_timeout" if timed_out else "native_unavailable"
                )
                continue
            switch_margin = (
                0.15
                if "dominant_retrieval" in segment.doubt_reasons
                else 0.05
            )
            best_index = int(np.argmax(scores))
            if (
                best_index > 0
                and scores[best_index] >= scores[0] + switch_margin
            ):
                winner = proposals[best_index]
                replacement = cls._segment_from_proposal(
                    segment,
                    segment.q_start,
                    segment.q_end,
                    winner,
                    candidates,
                    samples,
                )
                segment.episode = replacement.episode
                segment.a = replacement.a
                segment.b = replacement.b
                segment.points = replacement.points
                segment.residual = replacement.residual
                segment.confidence = max(
                    replacement.confidence,
                    scores[best_index],
                )
                segment.uncertain = False
                segment.doubt_reasons = sorted(
                    set(segment.doubt_reasons + ["native_alternative"])
                )
            elif scores[best_index] < 0.28:
                segment.episode = None
                segment.doubt_reasons.append("native_rejected")
            else:
                segment.confidence = max(segment.confidence, scores[best_index])
                segment.uncertain = False
        return actual_decoded_duration

    @staticmethod
    def _nearest_match_candidates(
        timestamp: float,
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
    ) -> list[MatchCandidate]:
        if not samples:
            return []
        index = min(
            range(len(samples)),
            key=lambda value: abs(samples[value].t_query - timestamp),
        )
        return [
            MatchCandidate(
                episode=value.episode,
                timestamp=value.t_source,
                similarity=value.similarity,
                series=value.series,
            )
            for value in candidates[index][:RETRIEVAL_TOP_K]
        ]

    @staticmethod
    def _safe_primary_source_interval(
        segment: TrackSegment,
    ) -> tuple[float, float]:
        """Return an affine interval without unsupported previous-shot preroll.

        Only the start is guarded.  End evidence is allowed to extrapolate as
        before because an early source start creates a visible one-frame flash,
        while shortening both ends would unnecessarily alter otherwise-good
        timing.  The guard is applied only when a selected track point exists
        inside the final query interval.
        """
        source_start = max(0.0, segment.source_at(segment.q_start))
        source_end = max(
            source_start + 1e-3,
            segment.source_at(segment.q_end),
        )
        if segment.episode is None or segment.a <= 0.0:
            return source_start, source_end
        in_span = [
            point
            for point in segment.points
            if point.episode == segment.episode
            and segment.q_start - 1e-6 <= point.t_query < segment.q_end
        ]
        if not in_span:
            return source_start, source_end
        first_query = min(point.t_query for point in in_span)
        first_points = [
            point
            for point in in_span
            if abs(point.t_query - first_query) <= 1e-6
        ]
        first_source = max(point.t_source for point in first_points)
        guarded_start = max(
            source_start,
            first_source - SOURCE_START_MAX_PREROLL_SECONDS,
        )
        # Preserve a meaningful positive interval even for a pathological
        # short fit.  In normal tracks this branch is inert by a wide margin.
        if guarded_start < source_end - 1e-3:
            source_start = guarded_start
        return source_start, source_end

    @classmethod
    def _build_output(
        cls,
        segments: list[TrackSegment],
        samples: list[QueryFrame],
        candidates: list[list[RetrievalCandidate]],
    ) -> tuple[SceneList, MatchList]:
        scene_list = SceneList()
        match_list = MatchList()
        for index, segment in enumerate(segments):
            scene = Scene(index=index, start_time=segment.q_start, end_time=segment.q_end)
            scene_list.scenes.append(scene)
            start_candidates = cls._nearest_match_candidates(
                segment.q_start, samples, candidates
            )
            middle_candidates = cls._nearest_match_candidates(
                0.5 * (segment.q_start + segment.q_end), samples, candidates
            )
            end_candidates = cls._nearest_match_candidates(
                max(segment.q_start, segment.q_end - 1e-3), samples, candidates
            )
            proposals = cls._line_proposals(segment, candidates, samples)
            alternatives: list[AlternativeMatch] = []
            primary_mid = (
                segment.source_at(0.5 * (segment.q_start + segment.q_end))
                if segment.episode
                else None
            )
            separation = max(2.0, scene.duration)
            for proposal in proposals:
                proposal_mid = proposal.source_at(
                    0.5 * (segment.q_start + segment.q_end)
                )
                if (
                    segment.episode
                    and proposal.episode == segment.episode
                    and primary_mid is not None
                    and abs(proposal_mid - primary_mid) < separation
                    and len(proposals) > 1
                ):
                    continue
                source_start = max(0.0, proposal.source_at(segment.q_start))
                source_end = max(source_start + 1e-3, proposal.source_at(segment.q_end))
                alternatives.append(
                    AlternativeMatch(
                        episode=proposal.episode,
                        start_time=source_start,
                        end_time=source_end,
                        confidence=float(np.clip(proposal.confidence, 0.0, 1.0)),
                        speed_ratio=scene.duration / max(1e-6, source_end - source_start),
                        vote_count=proposal.support,
                        algorithm=proposal.algorithm,
                    )
                )
                if len(alternatives) >= MAX_ALTERNATIVES:
                    break

            if segment.episode is None:
                match_list.matches.append(
                    SceneMatch(
                        scene_index=index,
                        episode="",
                        start_time=0.0,
                        end_time=0.0,
                        confidence=0.0,
                        speed_ratio=1.0,
                        was_no_match=True,
                        doubt_reasons=sorted(set(segment.doubt_reasons)),
                        alternatives=alternatives,
                        start_candidates=start_candidates,
                        middle_candidates=middle_candidates,
                        end_candidates=end_candidates,
                    )
                )
                continue
            source_start, source_end = cls._safe_primary_source_interval(segment)
            match_list.matches.append(
                SceneMatch(
                    scene_index=index,
                    episode=segment.episode,
                    start_time=source_start,
                    end_time=source_end,
                    confidence=float(np.clip(segment.confidence, 0.0, 1.0)),
                    speed_ratio=scene.duration / max(1e-6, source_end - source_start),
                    was_no_match=False,
                    doubt_reasons=sorted(set(segment.doubt_reasons)),
                    alternatives=alternatives,
                    start_candidates=start_candidates,
                    middle_candidates=middle_candidates,
                    end_candidates=end_candidates,
                )
            )
        scene_list.renumber()
        return scene_list, match_list
