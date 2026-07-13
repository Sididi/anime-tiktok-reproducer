"""Global scene alignment for TikTok edits against indexed anime sources."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field, replace as dc_replace
from pathlib import Path
from typing import AsyncIterator

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from ..library_types import LibraryType
from ..models import AlternativeMatch, MatchCandidate, MatchList, Scene, SceneMatch, SceneList
from .anime_matcher import AnimeMatcherService, MatchProgress


# 8 fps gives several samples even in 0.5-1.0s montage scenes while staying cheap.
DENSE_SAMPLE_FPS = 8.0
# 60 neighbors improves recall for zoomed/cropped edits while FAISS remains cheap.
RETRIEVAL_TOP_K = 60
# Primary decode uses the originally prescribed conservative neighborhood.
DECODE_RETRIEVAL_TOP_K = 20
# Primary decode keeps the old narrow state count; broad states are for alternatives.
DECODE_SEGMENTS_PER_SCENE = 30
# Evidence collection must include slow edits and the known fast 4.07x scene.
MIN_EVIDENCE_SPEED = 0.25
MAX_EVIDENCE_SPEED = 5.0
# Common playback range from the domain prior, expressed as source seconds/query second.
COMMON_SOURCE_RATE_MIN = 1.0 / 1.7
COMMON_SOURCE_RATE_MAX = 1.0 / 0.5
# SSCD false positives are common; this keeps extremely weak retrievals out of voting.
SIMILARITY_FLOOR = 0.20
# A 2 fps index has a 0.5s grid; inliers tolerate about one grid step plus decode jitter.
SEGMENT_RESIDUAL_SECONDS = 0.75
# Dense sampling should provide at least two distinct query times for a real segment.
MIN_SEGMENT_INLIER_TIMES = 2
# Pairwise line seeding uses a time subset; 12 points spans long scenes at 8 fps cheaply.
SEED_PAIR_TIME_LIMIT = 12
# Seed fits are deduped at coarse speed resolution before expensive inlier scans.
SEED_SPEED_QUANTIZATION = 0.05
# Keep broad ambiguity for global decode; 80 states keeps DP cheap for 40-60 scenes.
MAX_SEGMENTS_PER_SCENE = 80
# Episode switches are rare intruders, so they pay a mild but survivable penalty.
EPISODE_SWITCH_PENALTY = 0.25
# Playback is centered near 1x, but this prior is deliberately weak for fast/slow edits.
UNIT_SOURCE_RATE_PRIOR_WEIGHT = 0.04
# Backward source jumps exist, so penalize instead of forbidding them.
BACKWARD_JUMP_PENALTY = 0.20
# Dense inlier support matters, but repeated anime stills make it weaker than similarity.
SUPPORT_PRIOR_WEIGHT = 0.15
# UI contract exposes a compact ranked list, matching the old frontend expectation.
ALTERNATIVES_PER_SCENE = 5
# Query-side crop variants are searched when dense direct support stays weak.
QUERY_VARIANT_MIN_SCENE_SUPPORT = 4

# Redescending inlier window ~3.5 sigma keeps legitimate grid scatter while a
# 0.5s edit skip (a real cut) loses meaningful weight.
INLIER_TOLERANCE_SECONDS = 0.55
# Longest observed GT scene is 14.5s; spans above this are never one scene.
MAX_GROUP_SPAN_SECONDS = 20.0
MAX_GROUP_FRAGMENTS = 16
# TikTok content resuming across a cut at the 8 fps sample scale marks a
# flash/in-shot artifact. Set high: under equivalence folding an over-cut
# true flash folds back for free, while lookalike jump cuts at 0.5-0.7
# wrongly merged are unrecoverable.
TIKTOK_CUT_CONTINUES_COS = 0.75
# Below this cross-boundary cosine the pixels certify a hard cut: no
# extrapolation evidence may lean the boundary prior toward merging
# (blur/lookalike extrapolation across the 5e85@32.5 swoosh cut measured
# 0.67 while its tcos was 0.067).
HARD_CUT_TIKTOK_COS = 0.30
# Weight of the per-boundary keep/merge prior in DP score units (a sample
# contributes <= ~0.7 similarity mass; one boundary decision is worth ~2).
CUT_PRIOR_WEIGHT = 1.0
# Emission floor per expected sample for the explicit no-match state; real
# evidence (median true sim ~0.55) must always beat it.
NO_MATCH_SAMPLE_SCORE = 0.15
# Beam width for the segmentation DP; states are (span, fit) pairs.
DP_BEAM_WIDTH = 8
# Ridge mass pulling fitted slopes toward 1.0 (real-time playback): strong
# enough to pin statics (whose slope evidence mass is ~0.1) but weak enough
# that a 1.3s scene with real retiming keeps its measured slope.
SLOPE_UNIT_RIDGE = 0.15
# Parsimony margin for slope model selection: a free slope is kept only when
# real-time playback (rate 1.0) explains less than this fraction of the free
# fit's evidence mass. Grid quantization plus lookalike phantoms let a free
# slope always fit noise slightly better; genuine retimes beat unit rate by
# far more than 5%.
UNIT_SLOPE_PARSIMONY = 0.95
# Weak reward for chronological near-continuity between consecutive scenes:
# resolves exact-duplicate (OP/ED) ambiguity without forbidding real jumps.
CONTINUITY_REWARD = 0.35
# Short scale: the reward's job is picking the source-continuous instance
# among exact-duplicate candidates (its decisive range must be ~1 grid step,
# not the scale of legitimate edit jumps).
CONTINUITY_SCALE_SECONDS = 1.5
# Native-decode verification of merge-leaning static boundaries: the source
# is decoded at this fps around the predicted continuation and an offset
# sweep checks whether the after-cut content truly lives at the prediction.
# 12 fps is load-bearing for R2 per-end precision: 10 fps cost 8 source
# exacts and staled 9 waivers across the four GT projects (v111).
VERIFY_DECODE_FPS = 12.0
# Index self-similarity floor for duplicate-instance recall: true repeats
# measure >=0.85 on healthy indices; lookalike non-repeats sit ~0.75-0.8
# (the squish-index tax measures ~0.77, so 0.80 keeps recall usable there
# while excluding montage lookalikes).
DUPLICATE_RECALL_MIN_COS = 0.80
# Query-side deep-recall floor: cross-geometry (cropped query vs full
# source frame) cosines run far lower than self-similarity (true instances
# measured at 0.51-0.54 on owner-labeled fails); candidates are only
# proposals — the registered-footprint SSCD arbitration decides.
DEEP_RECALL_MIN_COS = 0.45
# A chain whose current line scores this high under its own registered
# footprint needs no duplicate arbitration absent other suspicion, and
# below it the chain is doubtful enough to pay for recall + chronology
# proposals: every owner-labeled wrong instance measured <=0.724
# registered while owner-passed chains measure >=0.78 (2026-07-11).
# Perf gate, not a correctness gate — margins still decide every switch.
DUPLICATE_TRUSTED_SSCD = 0.75
# Certification bar for recovering a no-match scene at fallback (grid)
# geometry: owner-labeled recoverable truths measure 0.37-0.58 while junk
# windows measure <=0.16 (bench 2026-07-11). A recovered line below the
# bar stays no-match.
RECOVERY_CERT_SSCD = 0.32


@dataclass(frozen=True)
class QuerySample:
    t_tiktok: float
    embedding: np.ndarray
    variant_id: str = "plain"


@dataclass(frozen=True)
class Correspondence:
    sample_index: int
    t_tiktok: float
    t_source: float
    episode: str
    similarity: float
    series: str
    variant_id: str = "plain"
    rank: int = 0


@dataclass(frozen=True)
class SegmentHypothesis:
    id: int
    episode: str
    tiktok_start: float
    tiktok_end: float
    a: float
    b: float
    inlier_count: int
    mean_similarity: float
    score: float
    scene_index: int | None = None

    def source_at(self, t_tiktok: float) -> float:
        return self.a * t_tiktok + self.b

    def source_interval(self, scene: Scene) -> tuple[float, float]:
        return self.source_at(scene.start_time), self.source_at(scene.end_time)


@dataclass
class AlignmentDiagnostics:
    sample_count: int = 0
    correspondence_count: int = 0
    segment_count: int = 0
    weak_variant_sample_count: int = 0
    phase_timings: dict[str, float] = field(default_factory=dict)
    segments: list[SegmentHypothesis] = field(default_factory=list)
    stage4_groups: list[dict[str, object]] = field(default_factory=list)
    decoded_fragments: list[dict[str, object]] = field(default_factory=list)
    decoded_candidates: list[dict[str, object]] = field(default_factory=list)
    stage4_attempts: list[dict[str, object]] = field(default_factory=list)

    def stats(self) -> dict[str, float]:
        result = {
            "aligner_sample_count": float(self.sample_count),
            "aligner_correspondence_count": float(self.correspondence_count),
            "aligner_segment_count": float(self.segment_count),
            "aligner_weak_variant_sample_count": float(self.weak_variant_sample_count),
            "aligner_stage4_group_count": float(len(self.stage4_groups)),
        }
        if self.stage4_groups:
            fragment_counts = [
                float(group.get("fragment_count", 0.0))
                for group in self.stage4_groups
            ]
            result["aligner_stage4_mean_fragments_per_group"] = float(
                np.mean(fragment_counts)
            )
            result["aligner_stage4_max_fragments_per_group"] = float(
                max(fragment_counts)
            )
        for name, seconds in self.phase_timings.items():
            result[f"aligner_{name}_seconds"] = seconds
        return result


@dataclass
class AlignmentResult:
    scenes: SceneList
    matches: MatchList
    diagnostics: AlignmentDiagnostics


@dataclass(frozen=True)
class _DecodedState:
    segment: SegmentHypothesis | None
    emission: float


@dataclass(frozen=True)
class _GroupFit:
    segment: SegmentHypothesis
    inlier_count: int
    residual_sse: float
    mean_abs_residual: float
    covered_fragments: frozenset[int]


@dataclass(frozen=True)
class _SpanFit:
    """One pooled affine hypothesis for a run of consecutive detector fragments."""

    episode: str | None  # None encodes the explicit no-match state
    a: float
    b: float
    quality: float  # emission mass: sum over sample bins of best inlier weight
    inlier_count: int
    mean_similarity: float

    def source_at(self, t_tiktok: float) -> float:
        return self.a * t_tiktok + self.b


@dataclass
class _FragmentEvidence:
    """Per-fragment correspondence arrays for vectorized pooled fits."""

    x: np.ndarray  # t_tiktok
    y: np.ndarray  # t_source
    w: np.ndarray  # similarity
    episode_ids: np.ndarray  # int codes into episodes list
    time_bins: np.ndarray  # int sample bins (round(t * DENSE_SAMPLE_FPS))
    n_sample_bins: int  # distinct dense sample bins inside the fragment


def _presize_images(images: list[Image.Image]) -> list[Image.Image]:
    """Parallel pre-resize with the embedder's own transform. torchvision
    Resize is bit-identical on re-application (verified), so the embedder's
    internal single-threaded resize becomes a no-op while the CPU cost of
    native-frame embedding drops ~6x."""
    embedder = AnimeMatcherService._embedder
    resize = getattr(embedder, "_resize", None)
    if resize is None or len(images) < 8:
        return images
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(resize, images))


class _WindowEmbedCache:
    """Per-run cache of natively decoded + embedded episode frames on the
    VERIFY_DECODE_FPS grid, keyed by (episode, zoom). Chains, duplicate
    candidates and boundary jobs hit the same source neighbourhoods
    repeatedly (R5); each slot is decoded and embedded at most once."""

    def __init__(self, library_type, zoom_crop, fps: float) -> None:
        self.library_type = library_type
        self.zoom_crop = zoom_crop
        self.fps = fps
        self.caps: dict[str, object] = {}
        self.slots: dict[tuple, dict[int, tuple[float, np.ndarray] | None]] = {}
        self.t_decode = 0.0
        self.t_embed = 0.0
        # prefetch: one worker decodes upcoming windows on its OWN captures
        # while the main thread embeds. Staged frames are keyed by the exact
        # slot run and produced by the same decode call, so window() emits
        # byte-identical embeddings whether or not the prefetch won the race.
        import threading
        from concurrent.futures import ThreadPoolExecutor

        self._staged: dict[tuple[str, int, int], list] = {}
        self._staged_lock = threading.Lock()
        # decoded-frames LRU: the SAME slot run requested under a second
        # geometry (gray-zone rescores, zoom<->rect retries, grid sweeps)
        # reuses the identical frame objects instead of re-decoding —
        # byte-identical by construction, bounded RAM (~6 windows)
        from collections import OrderedDict

        self._frames_lru: "OrderedDict[tuple[str, int, int], list]" = (
            OrderedDict()
        )
        self._inflight: dict[tuple[str, int, int], object] = {}
        self._prefetch_caps: dict[tuple[int, str], object] = {}
        self._prefetch_pool = ThreadPoolExecutor(max_workers=2)

    def get_cap(self, episode: str):
        from .anime_library import AnimeLibraryService

        path = AnimeLibraryService.resolve_episode_path(
            episode, library_type=self.library_type
        )
        if path is None or not path.exists():
            return None
        cap = self.caps.get(str(path))
        if cap is None:
            cv2 = AnimeMatcherService._require_cv2()
            cap = cv2.VideoCapture(str(path))
            self.caps[str(path)] = cap
        return cap

    def _decode_run(self, cap, r0: int, r1: int) -> list:
        """The single decode call both window() and the prefetch worker
        use — identical parameters guarantee identical frames."""
        w_lo = r0 / self.fps
        w_hi = (r1 + 1) / self.fps
        return AnimeMatcherService._collect_frames_in_window_from_capture(
            cap,
            w_lo,
            w_hi,
            max_frames=int((w_hi - w_lo) * 65) + 8,
            sample_frames=max(2, int(round((w_hi - w_lo) * self.fps)) + 1),
        )

    def probe_frames(self, episode: str, pred: float) -> list:
        """The registration probe's frames (pred +-0.3, max 8, sample 3) —
        staged by prefetch_probe when the worker got there first, decoded
        on the caller's capture otherwise. One decode call shape, so the
        frames are identical either way."""
        key = ("probe", episode, round(pred, 3))
        with self._staged_lock:
            fut = self._inflight.get(key)
        if fut is not None:
            try:
                fut.result()
            except Exception:
                pass
        with self._staged_lock:
            staged = self._staged.pop(key, None)
        if staged is not None:
            return staged
        cap = self.get_cap(episode)
        if cap is None:
            return []
        return AnimeMatcherService._collect_frames_in_window_from_capture(
            cap, pred - 0.3, pred + 0.3, max_frames=8, sample_frames=3
        )

    def prefetch_probe(self, episode: str, pred: float) -> None:
        key = ("probe", episode, round(pred, 3))
        with self._staged_lock:
            if (
                key in self._staged
                or key in self._inflight
                or len(self._inflight) + len(self._staged) > 12
            ):
                return

            def work() -> None:
                from .anime_library import AnimeLibraryService

                path = AnimeLibraryService.resolve_episode_path(
                    episode, library_type=self.library_type
                )
                frames = []
                if path is not None and path.exists():
                    import threading

                    cap_key = (threading.get_ident(), str(path))
                    cap = self._prefetch_caps.get(cap_key)
                    if cap is None:
                        cv2 = AnimeMatcherService._require_cv2()
                        cap = cv2.VideoCapture(str(path))
                        self._prefetch_caps[cap_key] = cap
                    frames = (
                        AnimeMatcherService._collect_frames_in_window_from_capture(
                            cap, pred - 0.3, pred + 0.3,
                            max_frames=8, sample_frames=3,
                        )
                    )
                with self._staged_lock:
                    self._staged[key] = frames
                    self._inflight.pop(key, None)

            try:
                self._inflight[key] = self._prefetch_pool.submit(work)
            except RuntimeError:
                self._inflight.pop(key, None)

    def prefetch(self, episode: str, lo: float, hi: float) -> None:
        """Ask the worker to decode [lo, hi] ahead of time. Only the
        cold-cache full-range run can be staged (partial cache hits change
        the runs); anything else quietly falls through to normal decode."""
        i0 = int(math.floor(max(0.0, lo) * self.fps))
        i1 = int(math.ceil(hi * self.fps))
        key = (episode, i0, i1)
        with self._staged_lock:
            if (
                key in self._staged
                or key in self._inflight
                or len(self._inflight) + len(self._staged) > 8
            ):
                return

            def work() -> None:
                from .anime_library import AnimeLibraryService

                path = AnimeLibraryService.resolve_episode_path(
                    episode, library_type=self.library_type
                )
                if path is None or not path.exists():
                    frames = []
                else:
                    import threading

                    cap_key = (threading.get_ident(), str(path))
                    cap = self._prefetch_caps.get(cap_key)
                    if cap is None:
                        cv2 = AnimeMatcherService._require_cv2()
                        cap = cv2.VideoCapture(str(path))
                        self._prefetch_caps[cap_key] = cap
                    frames = self._decode_run(cap, i0, i1)
                with self._staged_lock:
                    self._staged[key] = frames
                    self._inflight.pop(key, None)

            try:
                self._inflight[key] = self._prefetch_pool.submit(work)
            except RuntimeError:
                self._inflight.pop(key, None)

    def window(
        self,
        episode: str,
        zoom: "float | tuple[float, float, float, float]",
        lo: float,
        hi: float,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """(times, embeddings) covering [lo, hi] at the decode grid."""
        geom_key = (
            # 0.05-fraction quantization: crops within a few percent are
            # visually the same footprint and share decoded windows
            tuple(round(v * 20) / 20 for v in zoom)
            if isinstance(zoom, tuple)
            else round(zoom, 2)
        )
        slots = self.slots.setdefault((episode, geom_key), {})
        i0 = int(math.floor(max(0.0, lo) * self.fps))
        i1 = int(math.ceil(hi * self.fps))
        missing = [k for k in range(i0, i1 + 1) if k not in slots]
        if missing:
            cap = self.get_cap(episode)
            if cap is None:
                return None
            runs: list[tuple[int, int]] = []
            for k in missing:
                if runs and k == runs[-1][1] + 1:
                    runs[-1] = (runs[-1][0], k)
                else:
                    runs.append((k, k))
            for r0, r1 in runs:
                _t0 = time.perf_counter()
                key = (episode, r0, r1)
                frames = self._frames_lru.get(key)
                if frames is not None:
                    self._frames_lru.move_to_end(key)
                else:
                    with self._staged_lock:
                        fut = self._inflight.get(key)
                    if fut is not None:
                        try:
                            fut.result()
                        except Exception:
                            pass
                    with self._staged_lock:
                        frames = self._staged.pop(key, None)
                    if frames is None:
                        frames = self._decode_run(cap, r0, r1)
                    self._frames_lru[key] = frames
                    while len(self._frames_lru) > 6:
                        self._frames_lru.popitem(last=False)
                self.t_decode += time.perf_counter() - _t0
                if frames:
                    _t1 = time.perf_counter()
                    embs = AnimeMatcherService._embed_pil_batch(
                        _presize_images(
                            [
                                self.zoom_crop(im, zoom).convert("RGB")
                                for _, im in frames
                            ]
                        )
                    )
                    self.t_embed += time.perf_counter() - _t1
                    for (t, _), emb in zip(frames, embs, strict=False):
                        slot = int(round(t * self.fps))
                        if r0 <= slot <= r1 and slots.get(slot) is None:
                            slots[slot] = (t, emb)
                for k in range(r0, r1 + 1):
                    slots.setdefault(k, None)
        entries = [
            slots[k]
            for k in range(i0, i1 + 1)
            if slots.get(k) is not None
        ]
        if not entries:
            return None
        times = np.array([t for t, _ in entries])
        embs = np.stack([e for _, e in entries])
        return times, embs

    def close(self) -> None:
        import os as _os

        if _os.environ.get("ATR_RERANK_DEBUG"):
            print(
                f"[winprof] decode={self.t_decode:.1f}s embed={self.t_embed:.1f}s"
            )
        self._prefetch_pool.shutdown(wait=False, cancel_futures=True)
        with self._staged_lock:
            self._staged.clear()
            self._inflight.clear()
        self._frames_lru.clear()
        for cap in list(self.caps.values()) + list(
            self._prefetch_caps.values()
        ):
            try:
                cap.release()
            except Exception:
                pass
        self.caps.clear()
        self._prefetch_caps.clear()


class SceneAlignerService:
    """Dense correspondence, robust segment extraction, and global timeline decode."""

    _last_diagnostics: AlignmentDiagnostics = AlignmentDiagnostics()
    _last_result: AlignmentResult | None = None

    @classmethod
    def get_last_diagnostics(cls) -> AlignmentDiagnostics:
        return cls._last_diagnostics

    @classmethod
    def get_last_result(cls) -> AlignmentResult | None:
        return cls._last_result

    @classmethod
    async def align_scenes_progress(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_path: Path,
        library_type: LibraryType | str,
        anime_name: str | None = None,
    ) -> AsyncIterator[MatchProgress]:
        total = len(scenes.scenes)
        yield MatchProgress("starting", 0.0, "Initializing global aligner...", 0, total)
        loop = asyncio.get_running_loop()
        init_success = await loop.run_in_executor(
            None,
            AnimeMatcherService._init_searcher,
            library_path,
            library_type,
            anime_name,
        )
        if not init_success:
            yield MatchProgress(
                "error",
                0.0,
                "",
                error="Failed to initialize anime_searcher. Check library path and model.",
            )
            return

        yield MatchProgress("matching", 0.05, "Building dense correspondences...", 0, total)
        result = await loop.run_in_executor(
            None,
            cls.align_scenes_sync,
            video_path,
            scenes,
            library_type,
            anime_name,
        )
        yield MatchProgress(
            "complete",
            1.0,
            f"Aligned {len(result.matches.matches)} scenes",
            total,
            total,
            result.matches,
        )

    @classmethod
    def align_scenes_sync(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_type: LibraryType | str,
        anime_name: str | None = None,
    ) -> AlignmentResult:
        diagnostics = AlignmentDiagnostics()
        started = time.perf_counter()
        samples, diff_times, diffs = cls.sample_query_video_with_diffs(video_path)
        diagnostics.phase_timings["sample"] = time.perf_counter() - started
        diagnostics.sample_count = len(samples)

        started = time.perf_counter()
        scenes = cls._presnap_boundaries(scenes, diff_times, diffs)
        cls._last_diff_curve = (diff_times, diffs)
        diagnostics.phase_timings["presnap"] = time.perf_counter() - started

        started = time.perf_counter()
        correspondences = cls.retrieve_correspondences(samples, anime_name)
        diagnostics.phase_timings["retrieve"] = time.perf_counter() - started

        started = time.perf_counter()
        decode_correspondences = cls._decode_correspondences(correspondences)
        scene_segments = cls.extract_scene_segments(
            scenes,
            correspondences,
            include_low_rank_common_seeds=True,
        )
        decode_segments = cls.extract_scene_segments(
            scenes,
            decode_correspondences,
            max_segments=DECODE_SEGMENTS_PER_SCENE,
            line_limit=DECODE_RETRIEVAL_TOP_K,
        )
        cls._trim_scene_segments(decode_segments, DECODE_SEGMENTS_PER_SCENE)
        diagnostics.phase_timings["segment"] = time.perf_counter() - started

        weak_indices = cls._weak_scene_sample_indices(scenes, samples, decode_segments)
        if weak_indices:
            started = time.perf_counter()
            variant_samples = cls.sample_query_variants(video_path, samples, weak_indices)
            variant_correspondences = cls.retrieve_correspondences(variant_samples, anime_name)
            correspondences = cls._merge_correspondences(correspondences, variant_correspondences)
            decode_correspondences = cls._decode_correspondences(correspondences)
            scene_segments = cls.extract_scene_segments(
                scenes,
                correspondences,
                include_low_rank_common_seeds=True,
            )
            decode_segments = cls.extract_scene_segments(
                scenes,
                decode_correspondences,
                max_segments=DECODE_SEGMENTS_PER_SCENE,
                line_limit=DECODE_RETRIEVAL_TOP_K,
            )
            cls._trim_scene_segments(decode_segments, DECODE_SEGMENTS_PER_SCENE)
            diagnostics.phase_timings["variant_retrieve"] = time.perf_counter() - started
            diagnostics.weak_variant_sample_count = len(variant_samples)

        diagnostics.correspondence_count = len(correspondences)
        diagnostics.segments = [segment for values in scene_segments.values() for segment in values]
        diagnostics.segment_count = len(diagnostics.segments)
        diagnostics.decoded_candidates = cls._decode_candidate_records(
            scenes,
            decode_segments,
        )

        started = time.perf_counter()
        (
            final_scenes,
            remapped,
            stage4_groups,
            stage4_attempts,
        ) = cls._segment_timeline_dp(
            video_path,
            scenes,
            decode_segments,
            correspondences,
            samples,
            anime_name,
            library_type,
        )
        diagnostics.stage4_groups = stage4_groups
        diagnostics.stage4_attempts = stage4_attempts
        diagnostics.phase_timings["merge"] = time.perf_counter() - started

        window_cache = _WindowEmbedCache(
            library_type, cls._zoom_crop, VERIFY_DECODE_FPS
        )
        try:
            started = time.perf_counter()
            final_scenes, remapped = cls._interior_splits(
                final_scenes, remapped, correspondences, diff_times, diffs
            )
            final_scenes = cls._tug_boundaries(
                final_scenes, remapped, correspondences, diff_times, diffs
            )
            final_scenes = cls._native_tug_boundaries(
                video_path,
                final_scenes,
                remapped,
                library_type,
                window_cache,
                correspondences=correspondences,
            )
            diagnostics.phase_timings["interior_split"] = time.perf_counter() - started

            started = time.perf_counter()
            matches = cls._build_matches(
                video_path,
                final_scenes,
                remapped,
                scene_segments,
                correspondences,
                library_type,
                samples,
                window_cache=window_cache,
            )
            diagnostics.phase_timings["refine_build"] = time.perf_counter() - started
        finally:
            window_cache.close()
        cls._last_diagnostics = diagnostics
        result = AlignmentResult(final_scenes, matches, diagnostics)
        cls._last_result = result
        return result

    @staticmethod
    def _decode_correspondences(correspondences: list[Correspondence]) -> list[Correspondence]:
        return [corr for corr in correspondences if corr.rank < DECODE_RETRIEVAL_TOP_K]

    @staticmethod
    def _trim_scene_segments(
        scene_segments: dict[int, list[SegmentHypothesis]],
        limit: int,
    ) -> None:
        for segments in scene_segments.values():
            del segments[limit:]





    @classmethod
    def sample_query_video(cls, video_path: Path) -> list[QuerySample]:
        samples, _, _ = cls.sample_query_video_with_diffs(video_path)
        return samples

    @classmethod
    def sample_query_video_with_diffs(
        cls,
        video_path: Path,
    ) -> tuple[list[QuerySample], list[float], list[float]]:
        """One sequential decode: dense 8 fps SSCD samples plus the per-frame
        64x64 gray diff curve used for boundary snapping and interior splits."""
        cv2 = AnimeMatcherService._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        diff_times: list[float] = []
        diffs: list[float] = []
        previous_small: np.ndarray | None = None
        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS)
            if not native_fps or native_fps <= 0:
                native_fps = 30.0
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = frame_count / native_fps if frame_count and frame_count > 0 else None
            if duration is None:
                return [], [], []

            sample_times = np.arange(0.0, max(0.0, duration), 1.0 / DENSE_SAMPLE_FPS)
            targets = {
                max(0, int(round(float(t) * native_fps))): float(t)
                for t in sample_times
            }
            if not targets:
                return [], [], []

            samples: list[QuerySample] = []

            # producer/consumer: the worker owns the sequential decode +
            # diff curve while the main thread embeds each 96-frame batch
            # — batch composition is unchanged, so embeddings (and every
            # downstream decision) stay byte-identical to the serial loop
            import queue as _queue
            import threading as _threading

            batch_q: _queue.Queue = _queue.Queue(maxsize=2)
            producer_error: list[BaseException] = []

            def _produce() -> None:
                images: list[Image.Image] = []
                times: list[float] = []
                previous = None
                try:
                    next_target_iter = iter(sorted(targets.items()))
                    try:
                        target_frame, target_time = next(next_target_iter)
                    except StopIteration:
                        return
                    exhausted = False
                    frame_index = 0
                    while True:
                        ret, frame = cap.read()
                        if not ret:
                            break
                        small = cv2.resize(
                            frame, (64, 64), interpolation=cv2.INTER_AREA
                        )
                        small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                        if previous is not None:
                            diff_times.append(frame_index / native_fps)
                            diffs.append(
                                float(
                                    np.mean(
                                        np.abs(
                                            small.astype(np.int16)
                                            - previous.astype(np.int16)
                                        )
                                    )
                                )
                            )
                        previous = small
                        while not exhausted and frame_index >= target_frame:
                            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                            images.append(Image.fromarray(frame_rgb))
                            times.append(target_time)
                            if len(images) >= 96:
                                batch_q.put((images, times))
                                images, times = [], []
                            try:
                                target_frame, target_time = next(
                                    next_target_iter
                                )
                            except StopIteration:
                                exhausted = True
                        frame_index += 1
                    if images:
                        batch_q.put((images, times))
                except BaseException as exc:  # surfaced on the main thread
                    producer_error.append(exc)
                finally:
                    batch_q.put(None)

            worker = _threading.Thread(target=_produce, daemon=True)
            worker.start()
            while True:
                item = batch_q.get()
                if item is None:
                    break
                b_images, b_times = item
                embeddings = AnimeMatcherService._embed_pil_batch(
                    _presize_images(
                        [image.convert("RGB") for image in b_images]
                    )
                )
                for sample_time, embedding in zip(
                    b_times, embeddings, strict=False
                ):
                    samples.append(
                        QuerySample(sample_time, embedding, "plain")
                    )
            worker.join()
            if producer_error:
                raise producer_error[0]
            return samples, diff_times, diffs
        finally:
            cap.release()

    @classmethod
    def sample_query_variants(
        cls,
        video_path: Path,
        base_samples: list[QuerySample],
        sample_indices: set[int],
    ) -> list[QuerySample]:
        targets = [base_samples[index].t_tiktok for index in sorted(sample_indices)]
        frames = AnimeMatcherService.extract_frames(video_path, targets)
        images: list[Image.Image] = []
        times: list[float] = []
        variant_ids: list[str] = []
        for sample_time, frame in zip(targets, frames, strict=False):
            if frame is None:
                continue
            for variant_id, image in cls._query_variants(frame):
                images.append(image)
                times.append(sample_time)
                variant_ids.append(variant_id)
        if not images:
            return []
        embeddings = cls._embed_variant_images(images)
        return [
            QuerySample(sample_time, embedding, variant_id)
            for sample_time, embedding, variant_id in zip(times, embeddings, variant_ids, strict=False)
        ]

    @staticmethod
    def _embed_variant_images(images: list[Image.Image]) -> np.ndarray:
        embeddings: list[np.ndarray | None] = [None] * len(images)
        by_size: dict[tuple[int, int], list[tuple[int, Image.Image]]] = {}
        presized = _presize_images([image.convert("RGB") for image in images])
        for index, rgb in enumerate(presized):
            by_size.setdefault(rgb.size, []).append((index, rgb))
        for values in by_size.values():
            batch = AnimeMatcherService._embed_pil_batch([image for _, image in values])
            for (index, _), embedding in zip(values, batch, strict=False):
                embeddings[index] = embedding
        return np.stack([embedding for embedding in embeddings if embedding is not None], axis=0)

    @classmethod
    def _query_variants(cls, image: Image.Image) -> list[tuple[str, Image.Image]]:
        rgb = image.convert("RGB")
        width, height = rgb.size
        variants: list[tuple[str, Image.Image]] = []

        landscape_height = min(height, int(round(width * 9.0 / 16.0)))
        if landscape_height < height:
            top = (height - landscape_height) // 2
            variants.append(
                (
                    "center_landscape",
                    cls._limit_variant_pixels(
                        rgb.crop((0, top, width, top + landscape_height)),
                        width * height,
                    ),
                )
            )

        wide_width = max(width, int(round(height * 16.0 / 9.0)))
        if wide_width > width:
            background = ImageOps.fit(rgb, (wide_width, height)).filter(
                ImageFilter.GaussianBlur(radius=max(2, width // 80))
            )
            canvas = background.copy()
            canvas.paste(rgb, ((wide_width - width) // 2, 0))
            variants.append(("wide_pad", cls._limit_variant_pixels(canvas, width * height)))

        gray = ImageOps.grayscale(rgb)
        arr = np.asarray(gray)
        row_energy = arr.mean(axis=1)
        non_dark = np.where(row_energy > 8.0)[0]
        if non_dark.size > 0:
            top = int(non_dark[0])
            bottom = int(non_dark[-1]) + 1
            if bottom - top >= height * 0.65 and (top > 2 or bottom < height - 2):
                variants.append(
                    (
                        "trim_bars",
                        cls._limit_variant_pixels(rgb.crop((0, top, width, bottom)), width * height),
                    )
                )

        if not variants:
            crop_width = min(width, int(round(height * 9.0 / 16.0)))
            if crop_width < width:
                left = (width - crop_width) // 2
                variants.append(
                    (
                        "center_portrait",
                        cls._limit_variant_pixels(
                            rgb.crop((left, 0, left + crop_width, height)),
                            width * height,
                        ),
                    )
                )
        return variants

    @staticmethod
    def _limit_variant_pixels(image: Image.Image, max_pixels: int) -> Image.Image:
        pixels = image.width * image.height
        if pixels <= max_pixels or pixels <= 0:
            return image
        scale = math.sqrt(max_pixels / float(pixels))
        size = (
            max(1, int(round(image.width * scale))),
            max(1, int(round(image.height * scale))),
        )
        return image.resize(size, Image.Resampling.LANCZOS)

    @classmethod
    def retrieve_correspondences(
        cls,
        samples: list[QuerySample],
        anime_name: str | None,
    ) -> list[Correspondence]:
        processor = AnimeMatcherService._query_processor
        if processor is None or not samples:
            return []
        embeddings = np.stack([sample.embedding for sample in samples], axis=0).astype(
            np.float32,
            copy=False,
        )
        started = time.perf_counter()
        raw_results = processor.index_manager.search_batch(
            embeddings,
            RETRIEVAL_TOP_K,
            None,
            series=anime_name,
        )
        AnimeMatcherService._record_runtime_stat(
            "faiss_search_seconds",
            time.perf_counter() - started,
        )
        AnimeMatcherService._record_runtime_stat("faiss_search_queries", len(samples))
        correspondences: list[Correspondence] = []
        for sample_index, (sample, results) in enumerate(zip(samples, raw_results, strict=False)):
            for rank, (similarity, metadata) in enumerate(results):
                sim = float(similarity)
                if sim < SIMILARITY_FLOOR:
                    continue
                correspondences.append(
                    Correspondence(
                        sample_index=sample_index,
                        t_tiktok=sample.t_tiktok,
                        t_source=float(metadata.timestamp),
                        episode=metadata.episode,
                        similarity=sim,
                        series=metadata.series,
                        variant_id=sample.variant_id,
                        rank=rank,
                    )
                )
        return correspondences

    @staticmethod
    def _merge_correspondences(
        first: list[Correspondence],
        second: list[Correspondence],
    ) -> list[Correspondence]:
        merged: dict[tuple[str, float, float, str], Correspondence] = {}
        for corr in first + second:
            key = (
                corr.episode,
                round(corr.t_tiktok, 3),
                round(corr.t_source, 3),
                corr.variant_id,
            )
            prev = merged.get(key)
            if prev is None or corr.similarity > prev.similarity:
                merged[key] = corr
        return list(merged.values())

    @classmethod
    def extract_scene_segments(
        cls,
        scenes: SceneList,
        correspondences: list[Correspondence],
        *,
        max_segments: int = MAX_SEGMENTS_PER_SCENE,
        line_limit: int | None = None,
        include_low_rank_common_seeds: bool = False,
    ) -> dict[int, list[SegmentHypothesis]]:
        by_scene: dict[int, list[Correspondence]] = {i: [] for i in range(len(scenes.scenes))}
        scene_index = 0
        ordered = sorted(correspondences, key=lambda corr: corr.t_tiktok)
        for corr in ordered:
            while (
                scene_index < len(scenes.scenes)
                and corr.t_tiktok >= scenes.scenes[scene_index].end_time
            ):
                scene_index += 1
            if scene_index >= len(scenes.scenes):
                break
            scene = scenes.scenes[scene_index]
            if scene.start_time <= corr.t_tiktok < scene.end_time:
                by_scene[scene_index].append(corr)

        next_id = 0
        result: dict[int, list[SegmentHypothesis]] = {}
        for index, scene in enumerate(scenes.scenes):
            hypotheses = cls._fit_scene_hypotheses(
                scene,
                by_scene[index],
                next_id,
                max_segments=max_segments,
                line_limit=line_limit,
                include_low_rank_common_seeds=include_low_rank_common_seeds,
            )
            next_id += len(hypotheses)
            result[index] = hypotheses
        return result

    @classmethod
    def _fit_scene_hypotheses(
        cls,
        scene: Scene,
        correspondences: list[Correspondence],
        first_id: int,
        *,
        max_segments: int,
        line_limit: int | None,
        include_low_rank_common_seeds: bool,
    ) -> list[SegmentHypothesis]:
        by_episode: dict[str, list[Correspondence]] = {}
        for corr in correspondences:
            by_episode.setdefault(corr.episode, []).append(corr)

        line_raw: list[SegmentHypothesis] = []
        for episode, episode_corrs in by_episode.items():
            candidates = cls._episode_line_candidates(
                scene,
                episode,
                episode_corrs,
                include_low_rank_common_seeds=include_low_rank_common_seeds,
            )
            line_raw.extend(candidates)
        edge_raw = cls._edge_pair_hypotheses(scene, correspondences)

        line_cap = min(line_limit or RETRIEVAL_TOP_K, max_segments)
        deduped = cls._dedupe_segment_candidates(line_raw, line_cap)
        edge_limit = max_segments - len(deduped)
        if edge_limit > 0:
            existing_keys = {
                (
                    segment.episode,
                    round(segment.source_interval(scene)[0] * 2),
                    round(segment.source_interval(scene)[1] * 2),
                )
                for segment in deduped
            }
            for segment in cls._dedupe_segment_candidates(
                edge_raw,
                edge_limit,
                key=cls._emission_score,
            ):
                start, end = segment.source_interval(scene)
                key = (segment.episode, round(start * 2), round(end * 2))
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                deduped.append(segment)

        return [
            SegmentHypothesis(
                id=first_id + offset,
                episode=segment.episode,
                tiktok_start=segment.tiktok_start,
                tiktok_end=segment.tiktok_end,
                a=segment.a,
                b=segment.b,
                inlier_count=segment.inlier_count,
                mean_similarity=segment.mean_similarity,
                score=segment.score,
                scene_index=scene.index,
            )
            for offset, segment in enumerate(deduped[:max_segments])
        ]

    @staticmethod
    def _dedupe_segment_candidates(
        candidates: list[SegmentHypothesis],
        limit: int,
        key=None,
    ) -> list[SegmentHypothesis]:
        if limit <= 0:
            return []
        sort_key = key or (lambda item: item.score)
        deduped: list[SegmentHypothesis] = []
        seen: set[tuple[str, int, int]] = set()
        for segment in sorted(candidates, key=sort_key, reverse=True):
            start, end = segment.source_interval(
                Scene(
                    index=segment.scene_index or 0,
                    start_time=segment.tiktok_start,
                    end_time=segment.tiktok_end,
                )
            )
            key = (segment.episode, round(start * 2), round(end * 2))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(segment)
            if len(deduped) >= limit:
                break
        return deduped

    @classmethod
    def _edge_pair_hypotheses(
        cls,
        scene: Scene,
        correspondences: list[Correspondence],
    ) -> list[SegmentHypothesis]:
        if scene.duration <= 0:
            return []
        edge_window = 1.0 / DENSE_SAMPLE_FPS
        start_target, end_target = cls._scene_edge_times(scene)
        start_corrs = [
            corr
            for corr in correspondences
            if abs(corr.t_tiktok - start_target) <= edge_window
        ]
        end_corrs = [
            corr
            for corr in correspondences
            if abs(corr.t_tiktok - end_target) <= edge_window
        ]
        if not start_corrs or not end_corrs:
            return []

        by_episode_start: dict[str, list[Correspondence]] = {}
        by_episode_end: dict[str, list[Correspondence]] = {}
        for corr in start_corrs:
            by_episode_start.setdefault(corr.episode, []).append(corr)
        for corr in end_corrs:
            by_episode_end.setdefault(corr.episode, []).append(corr)
        for values in by_episode_start.values():
            values.sort(key=lambda corr: corr.similarity, reverse=True)
        for values in by_episode_end.values():
            values.sort(key=lambda corr: corr.similarity, reverse=True)

        hypotheses: list[SegmentHypothesis] = []
        per_edge_limit = max(5, DECODE_RETRIEVAL_TOP_K // 2)
        for episode, starts in by_episode_start.items():
            ends = by_episode_end.get(episode)
            if not ends:
                continue
            for start_corr in starts[:per_edge_limit]:
                for end_corr in ends[:per_edge_limit]:
                    query_delta = end_corr.t_tiktok - start_corr.t_tiktok
                    if query_delta <= 0:
                        continue
                    source_delta = end_corr.t_source - start_corr.t_source
                    if source_delta <= 0:
                        continue
                    speed = source_delta / query_delta
                    if not (MIN_EVIDENCE_SPEED <= speed <= MAX_EVIDENCE_SPEED):
                        continue
                    offset = start_corr.t_source - speed * start_corr.t_tiktok
                    mean_similarity = (start_corr.similarity + end_corr.similarity) / 2.0
                    hypotheses.append(
                        SegmentHypothesis(
                            id=-1,
                            episode=episode,
                            tiktok_start=scene.start_time,
                            tiktok_end=scene.end_time,
                            a=speed,
                            b=offset,
                            inlier_count=2,
                            mean_similarity=float(mean_similarity),
                            score=float(MIN_SEGMENT_INLIER_TIMES + mean_similarity),
                            scene_index=scene.index,
                        )
                    )
        return hypotheses

    @classmethod
    def _episode_line_candidates(
        cls,
        scene: Scene,
        episode: str,
        correspondences: list[Correspondence],
        *,
        include_low_rank_common_seeds: bool,
    ) -> list[SegmentHypothesis]:
        if not correspondences:
            return []
        by_time: dict[float, list[Correspondence]] = {}
        for corr in correspondences:
            by_time.setdefault(round(corr.t_tiktok, 3), []).append(corr)
        for values in by_time.values():
            values.sort(key=lambda corr: corr.similarity, reverse=True)

        times = sorted(by_time)
        seed_times = cls._seed_times(times)
        seeds: list[tuple[float, float]] = []
        per_time_limit = max(5, DECODE_RETRIEVAL_TOP_K // 2)
        seed_time_set = set(seed_times)
        for sample_time, values in by_time.items():
            if include_low_rank_common_seeds and sample_time in seed_time_set:
                for corr in values:
                    for source_rate in (
                        COMMON_SOURCE_RATE_MIN,
                        1.0,
                        COMMON_SOURCE_RATE_MAX,
                    ):
                        seeds.append((source_rate, corr.t_source - source_rate * corr.t_tiktok))
            for corr in values[:per_time_limit]:
                seeds.append((1.0, corr.t_source - corr.t_tiktok))
        if len(times) >= 2:
            for left_index, left_time in enumerate(seed_times):
                for right_time in seed_times[left_index + 1 :]:
                    dt = right_time - left_time
                    if dt < 0.15:
                        continue
                    for left in by_time[left_time][:per_time_limit]:
                        for right in by_time[right_time][:per_time_limit]:
                            speed = (right.t_source - left.t_source) / dt
                            if MIN_EVIDENCE_SPEED <= speed <= MAX_EVIDENCE_SPEED:
                                seeds.append((speed, left.t_source - speed * left.t_tiktok))
        else:
            only_time = times[0]
            scene_offset = only_time - scene.start_time
            for corr in by_time[only_time][:5]:
                seeds.append((1.0, corr.t_source - scene_offset - scene.start_time))

        candidates: list[SegmentHypothesis] = []
        for speed, offset in cls._dedupe_line_seeds(seeds):
            fit = cls._refit_line_from_inliers(scene, episode, correspondences, speed, offset)
            if fit is not None:
                candidates.append(fit)
        return candidates

    @staticmethod
    def _dedupe_line_seeds(seeds: list[tuple[float, float]]) -> list[tuple[float, float]]:
        deduped: list[tuple[float, float]] = []
        seen: set[tuple[int, int]] = set()
        for speed, offset in seeds:
            key = (
                round(speed / SEED_SPEED_QUANTIZATION),
                round(offset * AnimeMatcherService.get_index_fps()),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append((speed, offset))
        return deduped

    @staticmethod
    def _seed_times(times: list[float]) -> list[float]:
        if len(times) <= SEED_PAIR_TIME_LIMIT:
            return times
        indices = np.linspace(0, len(times) - 1, SEED_PAIR_TIME_LIMIT, dtype=np.int32)
        return [times[int(index)] for index in indices]

    @classmethod
    def _refit_line_from_inliers(
        cls,
        scene: Scene,
        episode: str,
        correspondences: list[Correspondence],
        speed: float,
        offset: float,
    ) -> SegmentHypothesis | None:
        inliers = [
            corr
            for corr in correspondences
            if abs(corr.t_source - (speed * corr.t_tiktok + offset))
            <= SEGMENT_RESIDUAL_SECONDS
        ]
        best_by_time: dict[float, Correspondence] = {}
        for corr in inliers:
            key = round(corr.t_tiktok, 3)
            prev = best_by_time.get(key)
            if prev is None or corr.similarity > prev.similarity:
                best_by_time[key] = corr
        if len(best_by_time) < MIN_SEGMENT_INLIER_TIMES:
            return None

        xs = np.array([corr.t_tiktok for corr in best_by_time.values()], dtype=np.float64)
        ys = np.array([corr.t_source for corr in best_by_time.values()], dtype=np.float64)
        weights = np.array([max(0.01, corr.similarity) for corr in best_by_time.values()])
        if len(xs) >= 2 and float(np.ptp(xs)) > 1e-6:
            x_mean = float(np.average(xs, weights=weights))
            y_mean = float(np.average(ys, weights=weights))
            denom = float(np.sum(weights * (xs - x_mean) ** 2))
            if denom > 1e-9:
                refined_speed = float(np.sum(weights * (xs - x_mean) * (ys - y_mean)) / denom)
                refined_offset = y_mean - refined_speed * x_mean
            else:
                refined_speed = speed
                refined_offset = offset
        else:
            refined_speed = speed
            refined_offset = offset
        if not (MIN_EVIDENCE_SPEED <= refined_speed <= MAX_EVIDENCE_SPEED):
            return None

        residuals = np.abs(ys - (refined_speed * xs + refined_offset))
        support = float(len(best_by_time))
        mean_similarity = float(np.average(weights))
        residual_penalty = float(np.mean(residuals)) / max(SEGMENT_RESIDUAL_SECONDS, 1e-6)
        score = support + mean_similarity - residual_penalty
        return SegmentHypothesis(
            id=-1,
            episode=episode,
            tiktok_start=scene.start_time,
            tiktok_end=scene.end_time,
            a=refined_speed,
            b=refined_offset,
            inlier_count=len(best_by_time),
            mean_similarity=mean_similarity,
            score=score,
            scene_index=scene.index,
        )

    @classmethod
    def _weak_scene_sample_indices(
        cls,
        scenes: SceneList,
        samples: list[QuerySample],
        scene_segments: dict[int, list[SegmentHypothesis]],
    ) -> set[int]:
        weak: set[int] = set()
        sample_times = [sample.t_tiktok for sample in samples]
        for scene_index, scene in enumerate(scenes.scenes):
            segments = scene_segments.get(scene_index, [])
            best = segments[0] if segments else None
            if best is not None and best.inlier_count >= QUERY_VARIANT_MIN_SCENE_SUPPORT:
                continue
            for sample_index, sample_time in enumerate(sample_times):
                if scene.start_time <= sample_time < scene.end_time:
                    weak.add(sample_index)
        return weak





    # ------------------------------------------------------------------
    # Stage 4 (2026-07-06 rework): global segmentation DP
    # ------------------------------------------------------------------

    @staticmethod
    def _emission_score(segment: SegmentHypothesis) -> float:
        scene_duration = max(0.0, segment.tiktok_end - segment.tiktok_start)
        expected_sample_times = max(
            float(QUERY_VARIANT_MIN_SCENE_SUPPORT),
            scene_duration * DENSE_SAMPLE_FPS,
        )
        support = min(
            float(segment.inlier_count),
            expected_sample_times,
        ) / expected_sample_times
        speed_prior = SceneAlignerService._speed_prior_penalty(segment.a)
        unit_rate_prior = UNIT_SOURCE_RATE_PRIOR_WEIGHT * abs(math.log(max(segment.a, 1e-3)))
        return (
            (support * SUPPORT_PRIOR_WEIGHT)
            + segment.mean_similarity
            - speed_prior
            - unit_rate_prior
        )

    @staticmethod
    def _speed_prior_penalty(source_rate: float) -> float:
        source_rate = max(source_rate, 1e-3)
        if COMMON_SOURCE_RATE_MIN <= source_rate <= COMMON_SOURCE_RATE_MAX:
            return 0.0
        if source_rate < COMMON_SOURCE_RATE_MIN:
            return abs(math.log(source_rate / COMMON_SOURCE_RATE_MIN))
        return abs(math.log(source_rate / COMMON_SOURCE_RATE_MAX))

    @classmethod
    def _interior_splits(
        cls,
        final_scenes: SceneList,
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        correspondences: list[Correspondence],
        diff_times: list[float],
        diffs: list[float],
    ) -> tuple[SceneList, list[tuple[list[int], SegmentHypothesis | None]]]:
        """Split a final scene whose own line leaves a dead sample-run at one
        edge: the rare case where the detector missed a real cut entirely, so
        no boundary existed for the DP to keep. The dead edge must be long
        enough to be a scene (>=0.5s), a strong alternative line must fit it,
        and the split snaps to the strongest local frame-diff peak."""
        if not final_scenes.scenes:
            return final_scenes, remapped
        diff_times_arr = np.asarray(diff_times) if diff_times else np.empty(0)
        diffs_arr = np.asarray(diffs) if diffs else np.empty(0)
        tol = INLIER_TOLERANCE_SECONDS
        new_scenes: list[Scene] = []
        new_remapped: list[tuple[list[int], SegmentHypothesis | None]] = []
        for index, (indices, segment) in enumerate(remapped):
            scene = final_scenes.scenes[index]
            if segment is None or scene.duration < 1.2:
                new_scenes.append(scene)
                new_remapped.append((indices, segment))
                continue
            corrs = [
                c
                for c in correspondences
                if c.rank < DECODE_RETRIEVAL_TOP_K
                and c.episode == segment.episode
                and scene.start_time <= c.t_tiktok < scene.end_time
            ]
            bins: dict[int, float] = {}
            for c in corrs:
                key = int(round(c.t_tiktok * DENSE_SAMPLE_FPS))
                r = abs(c.t_source - segment.source_at(c.t_tiktok))
                w = c.similarity ** 2 * max(0.0, 1.0 - (r / tol) ** 2)
                bins[key] = max(bins.get(key, 0.0), w)
            first_bin = int(math.ceil(scene.start_time * DENSE_SAMPLE_FPS))
            last_bin = int(math.floor((scene.end_time - 1e-6) * DENSE_SAMPLE_FPS))
            ordered = [bins.get(k, 0.0) for k in range(first_bin, last_bin + 1)]
            if len(ordered) < 8:
                new_scenes.append(scene)
                new_remapped.append((indices, segment))
                continue
            # dead run at an edge: >=4 bins (0.5s) below 20% of scene median
            level = max(0.05, 0.2 * float(np.median([v for v in ordered if v > 0] or [0.3])))
            dead_prefix = 0
            for v in ordered:
                if v >= level:
                    break
                dead_prefix += 1
            dead_suffix = 0
            for v in reversed(ordered):
                if v >= level:
                    break
                dead_suffix += 1
            split_bin: int | None = None
            if dead_prefix >= 4 and dead_prefix <= len(ordered) - 4:
                split_bin = first_bin + dead_prefix
                dead_lo, dead_hi = scene.start_time, split_bin / DENSE_SAMPLE_FPS
            elif dead_suffix >= 4 and dead_suffix <= len(ordered) - 4:
                split_bin = last_bin - dead_suffix + 1
                dead_lo, dead_hi = split_bin / DENSE_SAMPLE_FPS, scene.end_time
            if split_bin is None:
                new_scenes.append(scene)
                new_remapped.append((indices, segment))
                continue
            # a strong alternative line must fit the dead range
            dead_corrs = [
                c
                for c in correspondences
                if c.rank < DECODE_RETRIEVAL_TOP_K
                and dead_lo <= c.t_tiktok < dead_hi
            ]
            xs = np.array([c.t_tiktok for c in dead_corrs])
            alt = None
            if xs.size >= MIN_SEGMENT_INLIER_TIMES:
                by_ep: dict[str, list[Correspondence]] = {}
                for c in dead_corrs:
                    by_ep.setdefault(c.episode, []).append(c)
                for ep, cs in by_ep.items():
                    x = np.array([c.t_tiktok for c in cs])
                    y = np.array([c.t_source for c in cs])
                    w = np.array([c.similarity for c in cs])
                    seeds = [(1.0, float(yy - xx)) for xx, yy in zip(x[:8], y[:8])]
                    fit = cls._pooled_refit(
                        x, y, w, np.round(x * DENSE_SAMPLE_FPS).astype(np.int64), seeds
                    )
                    if fit is None:
                        continue
                    a, b, quality, inliers, mean_sim = fit
                    per_bin = quality / max(1.0, (dead_hi - dead_lo) * DENSE_SAMPLE_FPS)
                    if per_bin >= 0.12 and (alt is None or quality > alt[2]):
                        alt = (a, b, quality, ep, mean_sim, inliers)
            if alt is None:
                new_scenes.append(scene)
                new_remapped.append((indices, segment))
                continue
            split_t = split_bin / DENSE_SAMPLE_FPS
            if diff_times_arr.size:
                near = np.abs(diff_times_arr - split_t) <= 0.4
                if near.any():
                    local = np.where(near, diffs_arr, -np.inf)
                    peak = int(np.argmax(local))
                    if diffs_arr[peak] >= 3.0 * float(np.median(diffs_arr[near])):
                        split_t = float(diff_times_arr[peak])
            if (
                split_t - scene.start_time < 0.4
                or scene.end_time - split_t < 0.4
            ):
                new_scenes.append(scene)
                new_remapped.append((indices, segment))
                continue
            a, b, _, ep, mean_sim, inliers = alt
            alt_segment = SegmentHypothesis(
                id=-1,
                episode=ep,
                tiktok_start=dead_lo,
                tiktok_end=dead_hi,
                a=a,
                b=b,
                inlier_count=inliers,
                mean_similarity=mean_sim,
                score=0.0,
                scene_index=scene.index,
            )
            left_seg = alt_segment if dead_lo == scene.start_time else segment
            right_seg = segment if dead_lo == scene.start_time else alt_segment
            new_scenes.append(
                Scene(index=0, start_time=scene.start_time, end_time=split_t)
            )
            new_remapped.append((indices, left_seg))
            new_scenes.append(Scene(index=0, start_time=split_t, end_time=scene.end_time))
            new_remapped.append((indices, right_seg))
        result = SceneList(scenes=new_scenes)
        result.renumber()
        return result, new_remapped

    @classmethod
    def _tug_boundaries(
        cls,
        scenes: SceneList,
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        correspondences: list[Correspondence],
        diff_times: list[float],
        diffs: list[float],
    ) -> SceneList:
        """Re-place each boundary between scenes on different source lines at
        the TikTok frame-diff peak that best splits the straddling evidence.

        The DP can only cut at detector fragment boundaries; when the
        detector misses a hard cut (montage motion) the kept boundary sits on
        a nearby false position and both neighbours' source intervals inherit
        the error. The two fitted lines themselves say where the content
        changes; the diff peak says where a cut is physically possible.
        """
        if len(scenes.scenes) < 2 or not diff_times:
            return scenes
        tol = INLIER_TOLERANCE_SECONDS
        dt = np.asarray(diff_times)
        dv = np.asarray(diffs)
        corrs = [c for c in correspondences if c.rank < DECODE_RETRIEVAL_TOP_K]
        bounds = [s.end_time for s in scenes.scenes[:-1]]
        for i in range(len(scenes.scenes) - 1):
            fl = remapped[i][1]
            fr = remapped[i + 1][1]
            if fl is None or fr is None:
                continue
            t = bounds[i]
            if (
                fl.episode == fr.episode
                and abs(fr.source_at(t) - fl.source_at(t)) <= tol
            ):
                # same line: the boundary position does not change content
                continue
            near = np.abs(dt - t) <= 0.65
            if not near.any():
                continue
            local_med = float(np.median(dv[near]))
            strong = near & (dv >= max(2.5 * local_med, 1e-6))
            if not strong.any():
                continue
            order = np.argsort(dv[strong])[::-1][:6]
            cand_ts = [float(v) for v in dt[strong][order]]
            left_start = bounds[i - 1] if i > 0 else scenes.scenes[0].start_time
            right_end = (
                bounds[i + 1]
                if i + 1 < len(bounds)
                else scenes.scenes[-1].end_time
            )
            cand_ts = [
                c
                for c in cand_ts
                if c - left_start >= 0.35 and right_end - c >= 0.35
            ]
            if not cand_ts:
                continue
            window = [c for c in corrs if abs(c.t_tiktok - t) <= 0.9]
            if not window:
                continue
            # per sample bin, the best redescending weight under each line
            bins: dict[int, tuple[float, float]] = {}
            for c in window:
                key = int(round(c.t_tiktok * DENSE_SAMPLE_FPS))
                wl = wr = 0.0
                if c.episode == fl.episode:
                    r = abs(c.t_source - fl.source_at(c.t_tiktok))
                    if r <= tol:
                        wl = c.similarity ** 2 * (1.0 - (r / tol) ** 2)
                if c.episode == fr.episode:
                    r = abs(c.t_source - fr.source_at(c.t_tiktok))
                    if r <= tol:
                        wr = c.similarity ** 2 * (1.0 - (r / tol) ** 2)
                prev = bins.get(key, (0.0, 0.0))
                bins[key] = (max(prev[0], wl), max(prev[1], wr))
            if not bins:
                continue

            def split_score(tt: float) -> float:
                s = 0.0
                for key, (wl, wr) in bins.items():
                    s += wl if key / DENSE_SAMPLE_FPS < tt else wr
                return s

            base = split_score(t)
            best_t, best_s = t, base
            for c_t in cand_ts:
                s = split_score(c_t)
                if s > best_s:
                    best_t, best_s = c_t, s
            import os as _os
            if _os.environ.get("ATR_TUG_DEBUG"):
                print(
                    f"[tug] i={i} t={t:.2f} cands={[round(c,2) for c in cand_ts]} "
                    f"base={base:.3f} best=({best_t:.2f},{best_s:.3f}) "
                    f"L=({fl.episode[-20:]},{fl.source_at(t):.1f}) "
                    f"R=({fr.episode[-20:]},{fr.source_at(t):.1f})"
                )
            if best_t != t and best_s >= base + 0.05:
                bounds[i] = best_t
        starts = [scenes.scenes[0].start_time] + bounds
        ends = bounds + [scenes.scenes[-1].end_time]
        new_scenes = [
            Scene(index=k, start_time=s0, end_time=s1)
            for k, (s0, s1) in enumerate(zip(starts, ends))
        ]
        return SceneList(scenes=new_scenes)

    @classmethod
    def _native_tug_boundaries(
        cls,
        video_path: Path,
        scenes: SceneList,
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        library_type: LibraryType | str,
        window_cache: "_WindowEmbedCache | None" = None,
        correspondences: list[Correspondence] | None = None,
    ) -> SceneList:
        """Native placement of boundaries between scenes on DIFFERENT source
        lines: decode the TikTok at 24 fps around the boundary and place the
        cut where left-line similarity ends and right-line similarity begins.

        The TikTok diff curve cannot place these cuts (measured: several GT
        cuts are invisible in the diff curve while a stronger flash peak
        sits 0.3-0.5s away); the two fitted lines themselves say where the
        content changes. A boundary only moves when the split evidence
        clearly improves, so given-correct boundaries stay (oracle guard)."""
        sl = scenes.scenes
        if len(sl) < 2:
            return scenes
        cache = window_cache or _WindowEmbedCache(
            library_type, cls._zoom_crop, VERIFY_DECODE_FPS
        )
        tol = INLIER_TOLERANCE_SECONDS
        decode_corrs = [
            c
            for c in (correspondences or [])
            if c.rank < DECODE_RETRIEVAL_TOP_K
        ]

        def duplicate_suspect(scene_idx: int, fit: SegmentHypothesis) -> bool:
            """A wrong duplicate-instance line makes the split evidence
            meaningless; the tug must not move boundaries it cannot judge
            (this is how given-true boundaries between WP scenes broke)."""
            t0 = sl[scene_idx].start_time
            t1 = sl[scene_idx].end_time
            support = 0.0
            clusters: dict[tuple[str, int], float] = {}
            for c in decode_corrs:
                if not (t0 <= c.t_tiktok < t1):
                    continue
                if (
                    c.episode == fit.episode
                    and abs(c.t_source - fit.source_at(c.t_tiktok))
                    <= SEGMENT_RESIDUAL_SECONDS
                ):
                    support = max(support, c.similarity)
                key = (c.episode, int(round((c.t_source - c.t_tiktok) / 2.0)))
                clusters[key] = max(clusters.get(key, 0.0), c.similarity)
            t_mid = 0.5 * (t0 + t1)
            for (episode, _), sim in clusters.items():
                if sim < support - 0.05:
                    continue
                # distant cluster with near-tie support: duplicate risk
                for c in decode_corrs:
                    if (
                        c.episode == episode
                        and t0 <= c.t_tiktok < t1
                        and abs(
                            (c.t_source - c.t_tiktok + t_mid)
                            - fit.source_at(t_mid)
                        )
                        > 3.0
                        and c.similarity >= support - 0.05
                    ):
                        return True
            return False

        jobs: list[tuple[int, float, float, bool]] = []
        for k in range(len(sl) - 1):
            fl = remapped[k][1]
            fr = remapped[k + 1][1]
            if fl is None or fr is None:
                continue
            t = sl[k].end_time
            if fl.episode != fr.episode:
                # an episode switch is a hard content change the diff curve
                # always sees; the invisible-cut pathology this pass fixes
                # only arises WITHIN an episode (lookalike adjacent shots)
                continue
            if abs(fr.source_at(t) - fl.source_at(t)) <= tol:
                continue
            if duplicate_suspect(k, fl) or duplicate_suspect(k + 1, fr):
                # certified-tug experiment (2026-07-11): letting a
                # registered-SSCD certification override the suspect gate
                # moved owner-passed dcd#11 (stale waiver) without fixing
                # anything — the dcd#6 target is a MISSED SPLIT, not a
                # misplaced boundary. Binary gate stays (v88).
                continue
            lo = max(t - 0.65, sl[k].start_time + 0.3)
            hi = min(t + 0.65, sl[k + 1].end_time - 0.3)
            if hi - lo < 0.15:
                continue
            jobs.append((k, lo, hi, False))
        if not jobs:
            return scenes

        frame_times: list[float] = []
        spans: list[tuple[int, int]] = []
        for _, lo, hi, _suspect in jobs:
            ts = list(np.arange(lo, hi + 1e-6, 1.0 / 16.0))
            spans.append((len(frame_times), len(ts)))
            frame_times.extend(ts)
        decoded = AnimeMatcherService.extract_frames(video_path, frame_times)
        keep_rows = [k for k, fr in enumerate(decoded) if fr is not None]
        if not keep_rows:
            return scenes
        all_embs = AnimeMatcherService._embed_pil_batch(
            _presize_images([decoded[k].convert("RGB") for k in keep_rows])
        )
        emb_of: dict[int, np.ndarray] = {
            row: all_embs[n] for n, row in enumerate(keep_rows)
        }

        moves: dict[int, float] = {}
        try:
            for (k, lo, hi, suspect), (off, cnt) in zip(jobs, spans):
                q = [
                    (frame_times[off + d], emb_of[off + d])
                    for d in range(cnt)
                    if off + d in emb_of
                ]
                if len(q) < 4:
                    continue
                fl = remapped[k][1]
                fr = remapped[k + 1][1]

                def line_sims(fit: SegmentHypothesis) -> np.ndarray | None:
                    s_lo = min(fit.source_at(lo), fit.source_at(hi)) - 0.4
                    s_hi = max(fit.source_at(lo), fit.source_at(hi)) + 0.4
                    win = cache.window(fit.episode, 1.0, s_lo, s_hi)
                    if win is None:
                        return None
                    times, embs = win
                    sims = np.stack([e for _, e in q]) @ embs.T
                    preds = np.array([fit.source_at(t) for t, _ in q])
                    cols = np.clip(np.searchsorted(times, preds), 0, len(times) - 1)
                    prev_cols = np.clip(cols - 1, 0, len(times) - 1)
                    use_prev = np.abs(times[prev_cols] - preds) < np.abs(
                        times[cols] - preds
                    )
                    cols = np.where(use_prev, prev_cols, cols)
                    valid = np.abs(times[cols] - preds) <= 0.25
                    out = sims[np.arange(len(q)), cols]
                    return np.where(valid, out, 0.0)

                sim_left = line_sims(fl)
                sim_right = line_sims(fr)
                if sim_left is None or sim_right is None:
                    continue
                q_times = np.array([t for t, _ in q])
                current = sl[k].end_time

                def split_score(cut: float) -> float:
                    left_mask = q_times < cut
                    return float(
                        sim_left[left_mask].sum() + sim_right[~left_mask].sum()
                    )

                cuts = list((q_times[1:] + q_times[:-1]) / 2.0)
                base = split_score(current)
                best_t, best_s = current, base
                for cut in cuts:
                    s = split_score(cut)
                    if s > best_s:
                        best_t, best_s = cut, s
                # oracle guard: a boundary that is already a local optimum
                # of the split evidence has strong bilateral support and
                # must not move (a distant higher score under a wrong line
                # is exactly how given-true boundaries get broken)
                locally_optimal = all(
                    split_score(c) <= base + 1e-9
                    for c in cuts
                    if abs(c - current) <= 0.15
                )
                # only real misplacements move: sub-0.12s nudges sit inside
                # the exact tolerance on both axes and only add jitter to
                # the source interval mapping (measured on 85de). The gain
                # is a sum over moved frames, so the floor scales with the
                # query sampling rate (16 fps here).
                if (
                    not locally_optimal
                    and abs(best_t - current) >= 0.12
                    and best_s >= base + 0.0667
                ):
                    moves[k] = float(best_t)
        finally:
            if window_cache is None:
                cache.close()
        if not moves:
            return scenes
        bounds = [s.end_time for s in sl[:-1]]
        for k, t in moves.items():
            bounds[k] = t
        starts = [sl[0].start_time] + bounds
        ends = bounds + [sl[-1].end_time]
        return SceneList(
            scenes=[
                Scene(index=n, start_time=s0, end_time=s1)
                for n, (s0, s1) in enumerate(zip(starts, ends))
            ]
        )

    @classmethod
    def _fragment_evidence(
        cls,
        scenes: SceneList,
        correspondences: list[Correspondence],
    ) -> tuple[list[_FragmentEvidence], list[str]]:
        """Convert per-fragment correspondences (decode set) to numpy arrays."""
        episodes: list[str] = []
        episode_codes: dict[str, int] = {}
        by_fragment = cls._correspondences_by_fragment(scenes, correspondences)
        result: list[_FragmentEvidence] = []
        for index, scene in enumerate(scenes.scenes):
            corrs = [c for c in by_fragment.get(index, []) if c.rank < DECODE_RETRIEVAL_TOP_K]
            xs = np.array([c.t_tiktok for c in corrs], dtype=np.float64)
            ys = np.array([c.t_source for c in corrs], dtype=np.float64)
            ws = np.array([c.similarity for c in corrs], dtype=np.float64)
            codes = np.empty(len(corrs), dtype=np.int32)
            for k, c in enumerate(corrs):
                code = episode_codes.get(c.episode)
                if code is None:
                    code = len(episodes)
                    episode_codes[c.episode] = code
                    episodes.append(c.episode)
                codes[k] = code
            bins = np.round(xs * DENSE_SAMPLE_FPS).astype(np.int64)
            first_bin = int(math.ceil(scene.start_time * DENSE_SAMPLE_FPS - 1e-6))
            last_bin = int(math.floor((scene.end_time - 1e-6) * DENSE_SAMPLE_FPS))
            n_bins = max(1, last_bin - first_bin + 1)
            result.append(_FragmentEvidence(xs, ys, ws, codes, bins, n_bins))
        return result, episodes

    @staticmethod
    def _pooled_refit(
        x: np.ndarray,
        y: np.ndarray,
        w: np.ndarray,
        bins: np.ndarray,
        seeds: list[tuple[float, float]],
        unit_prior: bool = False,
    ) -> tuple[float, float, float, int, float] | None:
        """IRLS line fit over pooled correspondences; returns the best
        (a, b, quality, inlier_count, mean_similarity) across seeds.

        quality = sum over distinct sample bins of the best redescending
        inlier weight w * (1 - (r/tol)^2) in that bin.
        """
        if x.size < 2:
            return None
        tol = INLIER_TOLERANCE_SECONDS
        # map bins to compact indices once
        uniq_bins, bin_index = np.unique(bins, return_inverse=True)
        best: tuple[float, float, float, int, float] | None = None
        seen: set[tuple[int, int]] = set()
        for a0, b0 in seeds:
            key = (round(a0 / SEED_SPEED_QUANTIZATION), round(b0 * 2.0))
            if key in seen:
                continue
            seen.add(key)
            a, b = a0, b0
            for _ in range(2):
                r = y - (a * x + b)
                mask = np.abs(r) <= tol
                if int(mask.sum()) < MIN_SEGMENT_INLIER_TIMES:
                    break
                # squared similarity sharpens the contrast between true
                # matches (~0.6) and phantom repeats (~0.45)
                ww = w[mask] ** 2 * (1.0 - (r[mask] / tol) ** 2)
                xs, ys_ = x[mask], y[mask]
                x_mean = float(np.average(xs, weights=ww))
                y_mean = float(np.average(ys_, weights=ww))
                denom = float(np.sum(ww * (xs - x_mean) ** 2))
                if denom > 1e-9:
                    # ridge toward unit playback: in static content the slope
                    # is unidentifiable and real-time playback is the prior
                    num = float(np.sum(ww * (xs - x_mean) * (ys_ - y_mean)))
                    a = (num + SLOPE_UNIT_RIDGE) / (denom + SLOPE_UNIT_RIDGE)
                    b = y_mean - a * x_mean
                if not (MIN_EVIDENCE_SPEED <= a <= MAX_EVIDENCE_SPEED):
                    break
            if not (MIN_EVIDENCE_SPEED <= a <= MAX_EVIDENCE_SPEED):
                continue
            def line_quality(aa: float, bb: float) -> tuple[float, np.ndarray]:
                rr = y - (aa * x + bb)
                weight = np.where(
                    np.abs(rr) <= tol, w ** 2 * (1.0 - (rr / tol) ** 2), 0.0
                )
                per_bin = np.zeros(uniq_bins.size, dtype=np.float64)
                np.maximum.at(per_bin, bin_index, weight)
                return float(per_bin.sum()), per_bin

            quality, per_bin = line_quality(a, b)
            if unit_prior and abs(a - 1.0) > 1e-3:
                # slope model selection: keep the free slope only when it
                # clearly out-explains real-time playback on the same
                # evidence; otherwise it is fitting grid/lookalike noise.
                # Requested only for final interval fits — inside the DP the
                # snap perturbs span scores and thus segmentation.
                r = y - (a * x + b)
                m = np.abs(r) <= tol
                if m.any():
                    ww = np.maximum(w[m] ** 2 * (1.0 - (r[m] / tol) ** 2), 1e-9)
                    b1 = float(np.average(y[m] - x[m], weights=ww))
                    r1 = y - (x + b1)
                    m1 = np.abs(r1) <= tol
                    if m1.any():
                        ww1 = np.maximum(
                            w[m1] ** 2 * (1.0 - (r1[m1] / tol) ** 2), 1e-9
                        )
                        b1 = float(np.average(y[m1] - x[m1], weights=ww1))
                    q1, pb1 = line_quality(1.0, b1)
                    if q1 >= UNIT_SLOPE_PARSIMONY * quality:
                        a, b = 1.0, b1
                        quality, per_bin = q1, pb1
            covered = per_bin > 0.0
            inliers = int(covered.sum())
            if inliers < MIN_SEGMENT_INLIER_TIMES:
                continue
            mean_sim = float(np.mean(per_bin[covered])) if inliers else 0.0
            if best is None or quality > best[2]:
                best = (a, b, quality, inliers, mean_sim)
        return best

    @classmethod
    def _fit_span(
        cls,
        scenes: SceneList,
        evidence: list[_FragmentEvidence],
        episodes: list[str],
        decode_segments: dict[int, list[SegmentHypothesis]],
        i: int,
        j: int,
    ) -> list[_SpanFit]:
        """Best pooled fits for fragments i..j, one per candidate episode,
        plus the explicit no-match state."""
        n_bins = sum(evidence[k].n_sample_bins for k in range(i, j + 1))
        fits: list[_SpanFit] = [
            _SpanFit(None, 1.0, 0.0, NO_MATCH_SAMPLE_SCORE * n_bins, 0, 0.0)
        ]
        # candidate episodes: ranked by the best member-hypothesis score
        episode_rank: dict[str, float] = {}
        for k in range(i, j + 1):
            for seg in decode_segments.get(k, [])[:8]:
                prev = episode_rank.get(seg.episode)
                if prev is None or seg.score > prev:
                    episode_rank[seg.episode] = seg.score
        candidates = sorted(episode_rank, key=episode_rank.get, reverse=True)[:3]
        if not candidates:
            return fits

        xs = np.concatenate([evidence[k].x for k in range(i, j + 1)])
        ys = np.concatenate([evidence[k].y for k in range(i, j + 1)])
        ws = np.concatenate([evidence[k].w for k in range(i, j + 1)])
        codes = np.concatenate([evidence[k].episode_ids for k in range(i, j + 1)])
        bins = np.concatenate([evidence[k].time_bins for k in range(i, j + 1)])
        code_of = {name: code for code, name in enumerate(episodes)}

        for episode in candidates:
            code = code_of.get(episode)
            if code is None:
                continue
            mask = codes == code
            if int(mask.sum()) < MIN_SEGMENT_INLIER_TIMES:
                continue
            seeds: list[tuple[float, float]] = []
            for k in range(i, j + 1):
                for seg in decode_segments.get(k, [])[:8]:
                    if seg.episode == episode:
                        seeds.append((seg.a, seg.b))
            if not seeds:
                continue
            fit = cls._pooled_refit(xs[mask], ys[mask], ws[mask], bins[mask], seeds[:12])
            if fit is None:
                continue
            a, b, quality, inliers, mean_sim = fit
            fits.append(_SpanFit(episode, a, b, quality, inliers, mean_sim))
        fits.sort(key=lambda f: f.quality, reverse=True)
        return fits[:4]

    @classmethod
    def _index_cos_across(
        cls,
        series: str | None,
        episode: str,
        t_source: float,
        half: float = 0.5,
    ) -> float | None:
        """Cosine between the indexed source frames at t_source -/+ half.

        Low values mean the source itself has a shot change there, i.e. a
        TikTok cut at the mapped position is source-explained.
        """
        manager = AnimeMatcherService._index_manager
        if manager is None:
            return None
        try:
            series_names = [series] if series else list(
                getattr(manager, "series_metadata", {})
            )
            for name in series_names:
                metadata = manager.series_metadata.get(name)
                index = manager.series_indices.get(name)
                if metadata is None or index is None:
                    try:
                        manager._ensure_series_loaded(name)
                    except Exception:
                        continue
                    metadata = manager.series_metadata.get(name)
                    index = manager.series_indices.get(name)
                    if metadata is None or index is None:
                        continue
                cache_key = (name, episode)
                cached = cls._episode_grid_cache.get(cache_key)
                if cached is None:
                    entries = sorted(
                        (meta.timestamp, frame_id)
                        for frame_id, meta in metadata.items()
                        if meta.episode == episode
                    )
                    if not entries:
                        continue
                    cached = (
                        np.array([t for t, _ in entries], dtype=np.float64),
                        [fid for _, fid in entries],
                        index,
                    )
                    cls._episode_grid_cache[cache_key] = cached
                times, ids, idx = cached
                vectors = []
                for target in (t_source - half, t_source + half):
                    pos = int(np.argmin(np.abs(times - target)))
                    if abs(times[pos] - target) > half + 0.11:
                        vectors = []
                        break
                    v = idx.reconstruct(int(ids[pos]))
                    v = v / (np.linalg.norm(v) + 1e-9)
                    vectors.append(v)
                if len(vectors) == 2:
                    return float(np.dot(vectors[0], vectors[1]))
            return None
        except Exception:
            return None

    _episode_grid_cache: dict[tuple[str, str], tuple[np.ndarray, list[int], object]] = {}

    @classmethod
    def _boundary_priors(
        cls,
        scenes: SceneList,
        evidence: list[_FragmentEvidence],
        episodes: list[str],
        span_fits: dict[tuple[int, int], list[_SpanFit]],
        decode_segments: dict[int, list[SegmentHypothesis]],
        samples: list[QuerySample],
        series: str | None,
        library_type: LibraryType | str,
        video_path: Path,
    ) -> list[float]:
        """Keep(+)/merge(-) prior for each internal detector boundary.

        A flash artifact is recognized by TikTok content resuming across the
        cut. Otherwise the regime decides the instrument: in static content,
        merging is only allowed along a line both sides independently
        discovered AND whose multi-depth continuation probe validates; in
        dynamic content, line extrapolation across the boundary is reliable.
        """
        sample_times = np.array([s.t_tiktok for s in samples], dtype=np.float64)
        code_of = {name: code for code, name in enumerate(episodes)}
        tol = INLIER_TOLERANCE_SECONDS

        def side_quality(frag_index: int, fit: _SpanFit) -> float:
            """Quality of a fragment's samples under a FIXED line (no refit)."""
            ev = evidence[frag_index]
            code = code_of.get(fit.episode)
            if code is None:
                return 0.0
            mask = ev.episode_ids == code
            if not mask.any():
                return 0.0
            r = ev.y[mask] - (fit.a * ev.x[mask] + fit.b)
            weight = np.where(
                np.abs(r) <= tol, ev.w[mask] ** 2 * (1.0 - (r / tol) ** 2), 0.0
            )
            bins = ev.time_bins[mask]
            uniq, inv = np.unique(bins, return_inverse=True)
            per_bin = np.zeros(uniq.size)
            np.maximum.at(per_bin, inv, weight)
            return float(per_bin.sum())

        def prediction_sim(sample_index: int, fit: _SpanFit) -> float | None:
            """Cos between a query sample and the indexed source frame the
            fit predicts for it."""
            if sample_index < 0 or sample_index >= len(samples):
                return None
            sample = samples[sample_index]
            vector = cls._index_embedding_at(
                series, fit.episode, fit.source_at(sample.t_tiktok)
            )
            if vector is None:
                return None
            return float(np.dot(sample.embedding, vector))

        def fragment_ref_sim(frag_index: int, fit: _SpanFit) -> float | None:
            """Typical prediction sim inside the fragment (the yardstick the
            boundary continuation value is compared against)."""
            scene = scenes.scenes[frag_index]
            sims = []
            for t in np.linspace(
                scene.start_time + 0.08, max(scene.start_time + 0.08, scene.end_time - 0.08), 3
            ):
                k = int(np.argmin(np.abs(sample_times - t))) if sample_times.size else -1
                s = prediction_sim(k, fit)
                if s is not None:
                    sims.append(s)
            return float(np.median(sims)) if sims else None

        def compatible_line_exists(k: int, boundary: float) -> bool:
            """True when both fragments independently discovered lines that
            agree at the boundary (value and slope) with near-top quality on
            each side: low-ranked phantom lines pairing across a jump between
            reused lookalike shots must not qualify."""
            left = decode_segments.get(k, [])[:4]
            right = decode_segments.get(k + 1, [])[:4]
            if not left or not right:
                return False
            best_l = max(f.score for f in left)
            best_r = max(f.score for f in right)
            for fl in left:
                if fl.score < 0.7 * best_l:
                    continue
                for fr_ in right:
                    if fr_.score < 0.7 * best_r:
                        continue
                    if fl.episode != fr_.episode:
                        continue
                    if abs(fl.source_at(boundary) - fr_.source_at(boundary)) > 0.75:
                        continue
                    if abs(fl.a - fr_.a) > 0.6:
                        continue
                    return True
            return False

        priors: list[float] = []
        diagnostics: list[dict[str, object]] = []
        for k in range(len(scenes.scenes) - 1):
            boundary = scenes.scenes[k].end_time
            single_l = next(
                (f for f in span_fits.get((k, k), []) if f.episode is not None), None
            )
            single_r = next(
                (f for f in span_fits.get((k + 1, k + 1), []) if f.episode is not None),
                None,
            )
            record: dict[str, object] = {"boundary": boundary}
            diagnostics.append(record)
            li = int(np.searchsorted(sample_times, boundary - 0.05) - 1)
            ri = int(np.searchsorted(sample_times, boundary + 0.05))
            tcos: float | None = None
            if 0 <= li < len(samples) and 0 <= ri < len(samples) and li != ri:
                tcos = float(np.dot(samples[li].embedding, samples[ri].embedding))
            record["tiktok_cos"] = round(tcos, 3) if tcos is not None else None
            if tcos is not None and tcos >= TIKTOK_CUT_CONTINUES_COS:
                # TikTok content resumes across the cut (flash/overlay pop);
                # only a time-compatible line makes this a safe merge - jump
                # cuts between reused lookalike shots also score high tcos
                if compatible_line_exists(k, boundary):
                    priors.append(-0.9)
                    record["rule"] = "tiktok_continues"
                else:
                    priors.append(0.4)
                    record["rule"] = "tiktok_continues_incompatible"
                continue
            if single_l is None or single_r is None:
                # detector said cut; no evidence available to overrule it
                priors.append(0.2)
                record["rule"] = "no_fit"
                continue
            # regime: static content defeats line extrapolation (phantom
            # repeats); fast motion makes it trustworthy
            intra_vals = []
            if li - 1 >= 0:
                intra_vals.append(
                    float(np.dot(samples[li - 1].embedding, samples[li].embedding))
                )
            if ri + 1 < len(samples):
                intra_vals.append(
                    float(np.dot(samples[ri].embedding, samples[ri + 1].embedding))
                )
            intra = min(intra_vals) if intra_vals else 0.0
            record["intra_cos"] = round(intra, 3)
            if intra >= 0.80:
                # static regime, hard TikTok cut: "one clip spanning the
                # source's own transition" and "editor trim at the cut"
                # render identically and are content-undecidable (measured at
                # native pixel level, 2026-07-06); the owner-decided editing
                # prior is that hard cuts in static content are edit cuts
                priors.append(0.6)
                record["rule"] = "static_hard_cut"
            else:
                # dynamic regime: frames are unique, extrapolating each
                # side's own line over the other side is trustworthy -
                # provided the extrapolating line plays at a sane rate: a
                # degenerate-slope fit (lookalike phantoms sweeping through a
                # montage) explains anything, so its extrapolation success
                # must not be allowed to merge across a real cut
                sane_l = abs(single_l.a - 1.0) <= 0.5
                sane_r = abs(single_r.a - 1.0) <= 0.5
                record["rate_l"] = round(single_l.a, 2)
                record["rate_r"] = round(single_r.a, 2)
                if not sane_l and not sane_r:
                    priors.append(0.4)
                    record["rule"] = "dynamic_unratable"
                    continue
                ratio_lr = (
                    side_quality(k + 1, single_l) / max(single_r.quality, 1e-6)
                    if sane_l
                    else 0.0
                )
                ratio_rl = (
                    side_quality(k, single_r) / max(single_l.quality, 1e-6)
                    if sane_r
                    else 0.0
                )
                ratio = max(ratio_lr, ratio_rl)
                record["extrapolation_ratio"] = round(ratio, 3)
                prior = float(np.clip((0.60 - ratio) * 2.5, -0.8, 1.0))
                record["rule"] = "dynamic_extrapolation"
                if (
                    tcos is not None
                    and tcos <= HARD_CUT_TIKTOK_COS
                    and intra - tcos >= 0.35
                    and prior < 0.2
                ):
                    # the pixels say this boundary is special: content
                    # coheres on each side (intra) yet craters across the
                    # cut. Extrapolation success across such a boundary is
                    # lookalike/blur evidence (5e85@32.5: tcos 0.067,
                    # intra 0.702, extrapolated 0.67 and wrongly merged).
                    # Fast action needs the CONTRAST term: within-shot
                    # motion makes tcos low everywhere, and over-splitting
                    # an evidence hole does NOT fold back for free (411f
                    # 73->79 scenes, two new fold-no-chain fails without
                    # the contrast gate).
                    prior = 0.2
                    record["rule"] = "dynamic_hard_cut_floor"
                priors.append(prior)

        cls._last_boundary_diagnostics = diagnostics
        import os as _os

        if _os.environ.get("ATR_BOUNDARY_DEBUG"):
            for rec, pr in zip(diagnostics, priors, strict=False):
                print(f"[prior] {pr:+.2f} {rec}")
        return priors

    _last_boundary_diagnostics: list[dict[str, object]] = []
    _last_verify_debug: list[dict[str, object]] = []


    @classmethod
    def _index_embedding_at(
        cls,
        series: str | None,
        episode: str,
        t_source: float,
    ) -> np.ndarray | None:
        """Reconstruct the indexed embedding nearest to t_source."""
        manager = AnimeMatcherService._index_manager
        if manager is None:
            return None
        try:
            series_names = [series] if series else list(
                getattr(manager, "series_metadata", {})
            )
            for name in series_names:
                metadata = manager.series_metadata.get(name)
                index = manager.series_indices.get(name)
                if metadata is None or index is None:
                    try:
                        manager._ensure_series_loaded(name)
                    except Exception:
                        continue
                    metadata = manager.series_metadata.get(name)
                    index = manager.series_indices.get(name)
                    if metadata is None or index is None:
                        continue
                cache_key = (name, episode)
                cached = cls._episode_grid_cache.get(cache_key)
                if cached is None:
                    entries = sorted(
                        (meta.timestamp, frame_id)
                        for frame_id, meta in metadata.items()
                        if meta.episode == episode
                    )
                    if not entries:
                        continue
                    cached = (
                        np.array([t for t, _ in entries], dtype=np.float64),
                        [fid for _, fid in entries],
                        index,
                    )
                    cls._episode_grid_cache[cache_key] = cached
                times, ids, idx = cached
                pos = int(np.argmin(np.abs(times - t_source)))
                if abs(times[pos] - t_source) > 0.6:
                    return None
                v = idx.reconstruct(int(ids[pos]))
                return v / (np.linalg.norm(v) + 1e-9)
            return None
        except Exception:
            return None

    @classmethod
    def _index_duplicate_recall(
        cls,
        episode: str,
        line_fn,
        mid_ts: list[float],
        series: str | None = None,
    ) -> list[dict[str, float | str]]:
        """Duplicate instances of the CURRENT line's content elsewhere in
        the source, recalled by querying the index with the line's own
        indexed frames. Query-side retrieval misses whole instances when
        the edit's crop makes one instance dominate top-K (v101: truth
        absent from Stage-3 candidates on 5 of 10 owner-labeled 85de
        duplicate fails); source self-similarity is independent of the
        query's geometry. Returned as unit-rate candidate lines."""
        manager = AnimeMatcherService._index_manager
        if manager is None or not mid_ts:
            return []
        try:
            series_names = (
                [series]
                if series
                else list(getattr(manager, "series_metadata", {}))
            )
            clusters: dict[tuple[str, int], dict] = {}
            for name in series_names:
                metadata = manager.series_metadata.get(name)
                index = manager.series_indices.get(name)
                if metadata is None or index is None:
                    continue
                for t in mid_ts[:3]:
                    pos = float(line_fn(t))
                    v = cls._index_embedding_at(name, episode, pos)
                    if v is None:
                        continue
                    _, ids = index.search(
                        v[None].astype(np.float32), 24
                    )
                    for fid in ids[0]:
                        if int(fid) < 0:
                            continue
                        meta = metadata.get(int(fid))
                        if meta is None:
                            continue
                        if (
                            meta.episode == episode
                            and abs(meta.timestamp - pos) <= 3.0
                        ):
                            continue  # the instance the line already sits on
                        w = index.reconstruct(int(fid))
                        cos = float(v @ (w / (np.linalg.norm(w) + 1e-9)))
                        if cos < DUPLICATE_RECALL_MIN_COS:
                            continue
                        b = float(meta.timestamp) - t
                        key = (meta.episode, int(round(b / 2.0)))
                        cur = clusters.get(key)
                        if cur is None:
                            clusters[key] = {
                                "episode": meta.episode,
                                "bs": [b],
                                "rank_sim": cos,
                                "hits": {t},
                            }
                        else:
                            cur["bs"].append(b)
                            cur["hits"].add(t)
                            cur["rank_sim"] = max(cur["rank_sim"], cos)
            return cls._recall_clusters_to_candidates(clusters)
        except Exception:
            return []

    @staticmethod
    def _recall_clusters_to_candidates(
        clusters: dict[tuple[str, int], dict],
    ) -> list[dict[str, float | str]]:
        """>=2 distinct query times must agree (a single-frame hit on a
        2 fps grid is a lookalike still, not an instance); the offset is
        the cluster median. Neighbouring keys merge first — the quantized
        key boundary otherwise splits one instance's hits into two 1-hit
        clusters that both die at the agreement gate."""
        merged: list[dict] = []
        for key in sorted(clusters, key=lambda k: (k[0], k[1])):
            c = clusters[key]
            prev = merged[-1] if merged else None
            if (
                prev is not None
                and prev["episode"] == c["episode"]
                and abs(
                    float(np.median(prev["bs"])) - float(np.median(c["bs"]))
                )
                <= 2.0
            ):
                prev["bs"].extend(c["bs"])
                prev["hits"] |= c["hits"]
                prev["rank_sim"] = max(prev["rank_sim"], c["rank_sim"])
            else:
                merged.append(
                    {
                        "episode": c["episode"],
                        "bs": list(c["bs"]),
                        "hits": set(c["hits"]),
                        "rank_sim": c["rank_sim"],
                    }
                )
        out = [
            {
                "episode": c["episode"],
                "a": 1.0,
                "b": float(np.median(c["bs"])),
                "rank_sim": c["rank_sim"],
            }
            for c in merged
            if len(c["hits"]) >= 2
        ]
        out.sort(key=lambda c: float(c["rank_sim"]), reverse=True)
        return out[:3]

    @classmethod
    def _query_deep_recall(
        cls,
        q_mids: list[tuple[float, np.ndarray]],
        line_fn,
        episode: str,
        series: str | None = None,
    ) -> list[dict[str, float | str]]:
        """Distinct source positions recalled by searching the index
        DEEPER with the chain's own query embeddings. Montage lookalikes
        can outrank the true instance inside the retrieval top-K (the
        query's crop geometry biases the ranking); the true instance still
        sits in the deep tail, and the registered-footprint SSCD
        arbitration — not this ranking — decides."""
        manager = AnimeMatcherService._index_manager
        if manager is None or not q_mids:
            return []
        try:
            series_names = (
                [series]
                if series
                else list(getattr(manager, "series_metadata", {}))
            )
            clusters: dict[tuple[str, int], dict] = {}
            for name in series_names:
                metadata = manager.series_metadata.get(name)
                index = manager.series_indices.get(name)
                if metadata is None or index is None:
                    continue
                for t, emb in q_mids[:8]:
                    pos = float(line_fn(t))
                    q = emb.astype(np.float32)
                    q = q / (np.linalg.norm(q) + 1e-9)
                    _, ids = index.search(q[None], 40)
                    for fid in ids[0]:
                        if int(fid) < 0:
                            continue
                        meta = metadata.get(int(fid))
                        if meta is None:
                            continue
                        if (
                            meta.episode == episode
                            and abs(meta.timestamp - pos) <= 3.0
                        ):
                            continue
                        w = index.reconstruct(int(fid))
                        cos = float(q @ (w / (np.linalg.norm(w) + 1e-9)))
                        if cos < DEEP_RECALL_MIN_COS:
                            continue
                        b = float(meta.timestamp) - t
                        key = (meta.episode, int(round(b / 2.0)))
                        cur = clusters.get(key)
                        if cur is None:
                            clusters[key] = {
                                "episode": meta.episode,
                                "bs": [b],
                                "rank_sim": cos,
                                "hits": {t},
                            }
                        else:
                            cur["bs"].append(b)
                            cur["hits"].add(t)
                            cur["rank_sim"] = max(cur["rank_sim"], cos)
            return cls._recall_clusters_to_candidates(clusters)
        except Exception:
            return []

    @classmethod
    def _segment_timeline_dp(
        cls,
        video_path: Path,
        scenes: SceneList,
        decode_segments: dict[int, list[SegmentHypothesis]],
        correspondences: list[Correspondence],
        samples: list[QuerySample],
        series: str | None,
        library_type: LibraryType | str,
    ) -> tuple[
        SceneList,
        list[tuple[list[int], SegmentHypothesis | None]],
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        n = len(scenes.scenes)
        if n == 0:
            return scenes, [], [], []
        evidence, episodes = cls._fragment_evidence(scenes, correspondences)

        # span fits, bounded by span duration and fragment count
        span_fits: dict[tuple[int, int], list[_SpanFit]] = {}
        for i in range(n):
            for j in range(i, min(n, i + MAX_GROUP_FRAGMENTS)):
                if (
                    j > i
                    and scenes.scenes[j].end_time - scenes.scenes[i].start_time
                    > MAX_GROUP_SPAN_SECONDS
                ):
                    break
                span_fits[(i, j)] = cls._fit_span(
                    scenes, evidence, episodes, decode_segments, i, j
                )

        priors = cls._boundary_priors(
            scenes,
            evidence,
            episodes,
            span_fits,
            decode_segments,
            samples,
            series,
            library_type,
            video_path,
        )

        # interior boundary prior mass per span (merged boundaries pay -B)
        def span_emission(i: int, j: int, fit: _SpanFit) -> float:
            interior = sum(priors[k] for k in range(i, j))
            return fit.quality - CUT_PRIOR_WEIGHT * interior

        def transition(prev: _SpanFit, nxt: _SpanFit, boundary: float) -> float:
            if prev.episode is None or nxt.episode is None:
                return 0.0
            if prev.episode != nxt.episode:
                return -EPISODE_SWITCH_PENALTY
            gap = nxt.source_at(boundary) - prev.source_at(boundary)
            score = 0.0
            if gap < -INLIER_TOLERANCE_SECONDS:
                score -= BACKWARD_JUMP_PENALTY
            # chronological near-continuity is the norm; this weak reward
            # picks the nearby instance among exact-duplicate candidates
            score += CONTINUITY_REWARD * math.exp(
                -abs(gap) / CONTINUITY_SCALE_SECONDS
            )
            return score

        # beam DP over prefix endings
        # entries[j] = list of (score, start_i, fit, prev_entry_index_in[i-1])
        entries: list[list[tuple[float, int, _SpanFit, int]]] = [[] for _ in range(n)]
        for j in range(n):
            best_for_j: list[tuple[float, int, _SpanFit, int]] = []
            for i in range(max(0, j - MAX_GROUP_FRAGMENTS + 1), j + 1):
                fits = span_fits.get((i, j))
                if not fits:
                    continue
                for fit in fits:
                    emission = span_emission(i, j, fit)
                    if i == 0:
                        best_for_j.append((emission, i, fit, -1))
                        continue
                    boundary = scenes.scenes[i - 1].end_time
                    cut_bonus = CUT_PRIOR_WEIGHT * priors[i - 1]
                    best_prev = None
                    best_prev_idx = -1
                    for prev_idx, (p_score, _, p_fit, _) in enumerate(entries[i - 1]):
                        cand = p_score + transition(p_fit, fit, boundary)
                        if best_prev is None or cand > best_prev:
                            best_prev = cand
                            best_prev_idx = prev_idx
                    if best_prev is None:
                        continue
                    best_for_j.append(
                        (best_prev + cut_bonus + emission, i, fit, best_prev_idx)
                    )
            best_for_j.sort(key=lambda item: item[0], reverse=True)
            entries[j] = best_for_j[:DP_BEAM_WIDTH]

        if not entries[-1]:
            # degenerate: no fits anywhere; one no-match group per fragment
            groups: list[tuple[list[int], _SpanFit]] = [
                ([k], _SpanFit(None, 1.0, 0.0, 0.0, 0, 0.0)) for k in range(n)
            ]
        else:
            groups = []
            j = n - 1
            entry_idx = 0
            while j >= 0:
                score, i, fit, prev_idx = entries[j][entry_idx]
                groups.append((list(range(i, j + 1)), fit))
                j = i - 1
                entry_idx = prev_idx if prev_idx >= 0 else 0
            groups.reverse()

        final_scenes = SceneList(
            scenes=[
                Scene(
                    index=group_index,
                    start_time=scenes.scenes[indices[0]].start_time,
                    end_time=scenes.scenes[indices[-1]].end_time,
                )
                for group_index, (indices, _) in enumerate(groups)
            ]
        )
        snapped = final_scenes

        remapped: list[tuple[list[int], SegmentHypothesis | None]] = []
        group_records: list[dict[str, object]] = []
        for group_index, (indices, fit) in enumerate(groups):
            scene = snapped.scenes[group_index]
            record: dict[str, object] = {
                "scene_index": group_index,
                "fragment_indices": list(indices),
                "fragment_count": len(indices),
                "tiktok_start": scene.start_time,
                "tiktok_end": scene.end_time,
                "episode": fit.episode,
            }
            if fit.episode is None:
                remapped.append((indices, None))
                group_records.append(record)
                continue
            record.update(
                {
                    "source_start": fit.source_at(scene.start_time),
                    "source_end": fit.source_at(scene.end_time),
                    "source_rate": fit.a,
                    "inlier_count": fit.inlier_count,
                    "quality": fit.quality,
                }
            )
            group_records.append(record)
            remapped.append(
                (
                    indices,
                    SegmentHypothesis(
                        id=-1,
                        episode=fit.episode,
                        tiktok_start=scene.start_time,
                        tiktok_end=scene.end_time,
                        a=fit.a,
                        b=fit.b,
                        inlier_count=fit.inlier_count,
                        mean_similarity=fit.mean_similarity,
                        score=fit.quality,
                        scene_index=scene.index,
                    ),
                )
            )
        boundary_records = []
        for k in range(len(priors)):
            record: dict[str, object] = {
                "boundary_index": k,
                "boundary": scenes.scenes[k].end_time,
                "prior": priors[k],
            }
            if k < len(cls._last_boundary_diagnostics):
                record.update(cls._last_boundary_diagnostics[k])
            boundary_records.append(record)
        return snapped, remapped, group_records, boundary_records


    @staticmethod
    def _decoded_fragment_records(
        scenes: SceneList,
        decoded: list[SegmentHypothesis | None],
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for index, scene in enumerate(scenes.scenes):
            segment = decoded[index] if index < len(decoded) else None
            record: dict[str, object] = {
                "fragment_index": index,
                "tiktok_start": scene.start_time,
                "tiktok_end": scene.end_time,
            }
            if segment is not None:
                record.update(
                    {
                        "episode": segment.episode,
                        "source_start": segment.source_at(scene.start_time),
                        "source_end": segment.source_at(scene.end_time),
                        "source_rate": segment.a,
                        "inlier_count": segment.inlier_count,
                        "mean_similarity": segment.mean_similarity,
                        "score": segment.score,
                    }
                )
            records.append(record)
        return records

    @staticmethod
    def _decode_candidate_records(
        scenes: SceneList,
        decode_segments: dict[int, list[SegmentHypothesis]],
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for index, scene in enumerate(scenes.scenes):
            candidates: list[dict[str, object]] = []
            for segment in decode_segments.get(index, [])[:ALTERNATIVES_PER_SCENE]:
                candidates.append(
                    {
                        "episode": segment.episode,
                        "source_start": segment.source_at(scene.start_time),
                        "source_end": segment.source_at(scene.end_time),
                        "source_rate": segment.a,
                        "inlier_count": segment.inlier_count,
                        "mean_similarity": segment.mean_similarity,
                        "score": segment.score,
                    }
                )
            records.append(
                {
                    "fragment_index": index,
                    "tiktok_start": scene.start_time,
                    "tiktok_end": scene.end_time,
                    "candidates": candidates,
                }
            )
        return records

    @classmethod
    def _correspondences_by_fragment(
        cls,
        scenes: SceneList,
        correspondences: list[Correspondence],
    ) -> dict[int, list[Correspondence]]:
        by_fragment: dict[int, list[Correspondence]] = {
            index: [] for index in range(len(scenes.scenes))
        }
        scene_index = 0
        for corr in sorted(correspondences, key=lambda item: item.t_tiktok):
            while (
                scene_index < len(scenes.scenes)
                and corr.t_tiktok >= scenes.scenes[scene_index].end_time
            ):
                scene_index += 1
            if scene_index >= len(scenes.scenes):
                break
            scene = scenes.scenes[scene_index]
            if scene.start_time <= corr.t_tiktok < scene.end_time:
                by_fragment[scene_index].append(corr)
        return by_fragment











    _last_diff_curve: tuple[list[float], list[float]] = ([], [])

    @classmethod
    def _presnap_boundaries(
        cls,
        scenes: SceneList,
        diff_times: list[float],
        diffs: list[float],
    ) -> SceneList:
        """Snap detector boundaries to the strongest local frame-diff peak
        BEFORE alignment: placement offsets of 0.2-0.4s are a systematic
        detector error mode and every later stage (priors, verification,
        source mapping) assumes boundaries sit on the true cut."""
        if len(scenes.scenes) < 2 or not diff_times:
            return scenes
        moves = cls._visual_boundary_snap_candidates(scenes, diff_times, diffs)
        if not moves:
            return scenes
        snapped = scenes.model_copy(deep=True)
        from .scene_merger import SceneMergerService

        duration_floor = SceneMergerService.DENSE_VISUAL_SNAP_MIN_SCENE_DURATION
        for index, boundary in sorted(moves.items()):
            if index < 0 or index + 1 >= len(snapped.scenes):
                continue
            left_scene = snapped.scenes[index]
            right_scene = snapped.scenes[index + 1]
            if (
                boundary - left_scene.start_time < duration_floor
                or right_scene.end_time - boundary < duration_floor
            ):
                continue
            left_scene.end_time = boundary
            right_scene.start_time = boundary
        snapped.renumber()
        if not snapped.validate_continuity():
            return scenes
        return snapped


    @staticmethod
    def _visual_boundary_snap_candidates(
        scenes: SceneList,
        diff_times: list[float],
        diffs: list[float],
    ) -> dict[int, float]:
        """Snap targets: strongest frame-diff peak within the window around
        each boundary. Thresholds are relative to the local diff level, not
        absolute: zoomed/padded edits attenuate global diff amplitude but
        cut peaks still tower over their neighbourhood."""
        from bisect import bisect_right

        if len(diff_times) != len(diffs) or len(scenes.scenes) < 2:
            return {}
        diffs_arr = np.asarray(diffs)
        moves: dict[int, float] = {}
        window = 0.45
        move_floor = 0.08
        duration_floor = 0.25

        for index in range(len(scenes.scenes) - 1):
            left_scene = scenes.scenes[index]
            right_scene = scenes.scenes[index + 1]
            current_boundary = left_scene.end_time
            lower_bound = max(current_boundary - window, left_scene.start_time + duration_floor)
            upper_bound = min(current_boundary + window, right_scene.end_time - duration_floor)
            if lower_bound >= upper_bound:
                continue

            start_idx = bisect_right(diff_times, lower_bound)
            end_idx = bisect_right(diff_times, upper_bound)
            if start_idx >= end_idx:
                continue

            local = diffs_arr[start_idx:end_idx]
            best_off = int(np.argmax(local))
            best_diff = float(local[best_off])
            # the peak must dominate its neighbourhood to count as a cut
            local_floor = float(np.median(local))
            if best_diff < max(12.0, 3.0 * local_floor):
                continue

            current_diff = SceneAlignerService._closest_diff_at_time(
                diff_times,
                diffs,
                current_boundary,
            )
            if current_diff > 0 and best_diff < current_diff * 1.2:
                continue

            best_time = diff_times[start_idx + best_off]
            if abs(best_time - current_boundary) < move_floor:
                continue
            moves[index] = round(best_time, 3)

        return moves

    @staticmethod
    def _closest_diff_at_time(
        diff_times: list[float],
        diffs: list[float],
        timestamp: float,
    ) -> float:
        from bisect import bisect_right

        if not diff_times:
            return 0.0
        insert_at = bisect_right(diff_times, timestamp)
        candidates = []
        if insert_at < len(diff_times):
            candidates.append(insert_at)
        if insert_at > 0:
            candidates.append(insert_at - 1)
        if not candidates:
            return 0.0
        closest = min(candidates, key=lambda idx: abs(diff_times[idx] - timestamp))
        return diffs[closest]



    @classmethod
    def _build_matches(
        cls,
        video_path: Path,
        final_scenes: SceneList,
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        scene_segments: dict[int, list[SegmentHypothesis]],
        correspondences: list[Correspondence],
        library_type: LibraryType | str,
        samples: list[QuerySample] | None = None,
        window_cache: _WindowEmbedCache | None = None,
    ) -> MatchList:
        matches = MatchList()
        n = len(remapped)

        # detect source-continuous chains: runs of consecutive scenes whose
        # lines agree at the shared boundaries (same clip cut for pacing);
        # refining interior boundaries independently would break the very
        # continuity that makes them one clip, so only chain ends are refined
        # and interior boundaries stay exactly continuous
        chain_of = list(range(n))
        for i in range(n - 1):
            left = remapped[i][1]
            right = remapped[i + 1][1]
            if left is None or right is None or left.episode != right.episode:
                continue
            boundary = final_scenes.scenes[i].end_time
            gap = right.source_at(boundary) - left.source_at(boundary)
            if abs(gap) <= INLIER_TOLERANCE_SECONDS:
                chain_of[i + 1] = chain_of[i]

        # sandwich reconciliation: a short piece whose neighbours sit on one
        # line while its own primary jumped elsewhere (overlay/lookalike
        # degradation) is pulled back onto the neighbours' line - its true
        # candidates stay exposed in alternatives. Real intruder scenes are
        # safe: their content is not explained by the neighbours' line.
        for i in range(1, n - 1):
            left = remapped[i - 1][1]
            mid = remapped[i][1]
            right = remapped[i + 1][1]
            if left is None or right is None:
                continue
            if left.episode != right.episode:
                continue
            scene_i = final_scenes.scenes[i]
            if scene_i.duration > 2.5:
                continue
            boundary_l = final_scenes.scenes[i - 1].end_time
            boundary_r = scene_i.end_time
            gap_lr = right.source_at(boundary_r) - left.source_at(boundary_r)
            if abs(gap_lr) > INLIER_TOLERANCE_SECONDS + 0.5 * scene_i.duration:
                continue
            already_on_line = (
                mid is not None
                and mid.episode == left.episode
                and abs(mid.source_at(boundary_l) - left.source_at(boundary_l))
                <= INLIER_TOLERANCE_SECONDS
            )
            if already_on_line:
                continue
            reconciled = SegmentHypothesis(
                id=-1,
                episode=left.episode,
                tiktok_start=scene_i.start_time,
                tiktok_end=scene_i.end_time,
                a=left.a,
                b=left.b,
                inlier_count=max(1, left.inlier_count // 2),
                mean_similarity=min(
                    left.mean_similarity, 0.35
                ),
                score=0.0,
                scene_index=scene_i.index,
            )
            remapped[i] = (remapped[i][0], reconciled)
            chain_of[i] = chain_of[i - 1]
            if (
                remapped[i + 1][1] is not None
                and abs(gap_lr) <= INLIER_TOLERANCE_SECONDS
            ):
                chain_of[i + 1] = chain_of[i]

        # primaries may have moved: recompute source-continuity chains
        chain_of = list(range(n))
        for i in range(n - 1):
            left = remapped[i][1]
            right = remapped[i + 1][1]
            if left is None or right is None or left.episode != right.episode:
                continue
            boundary = final_scenes.scenes[i].end_time
            if (
                abs(right.source_at(boundary) - left.source_at(boundary))
                <= INLIER_TOLERANCE_SECONDS
            ):
                chain_of[i + 1] = chain_of[i]

        # raw line-based intervals with interior continuity enforced
        raw: list[tuple[float, float] | None] = []
        for final_index, (_, segment) in enumerate(remapped):
            if segment is None:
                raw.append(None)
                continue
            scene = final_scenes.scenes[final_index]
            start_val = segment.source_at(scene.start_time)
            end_val = segment.source_at(scene.end_time)
            raw.append((start_val, end_val))
        # one pooled line per chain: pieces inherit its values, which fixes
        # both interior continuity and the per-piece slope noise of statics
        chain_start = 0
        while chain_start < n:
            chain_end = chain_start
            while chain_end + 1 < n and chain_of[chain_end + 1] == chain_of[chain_start]:
                chain_end += 1
            if remapped[chain_start][1] is not None:
                episode = remapped[chain_start][1].episode
                t0 = final_scenes.scenes[chain_start].start_time
                t1 = final_scenes.scenes[chain_end].end_time
                xs, ys, ws = [], [], []
                for corr in correspondences:
                    if (
                        corr.rank < DECODE_RETRIEVAL_TOP_K
                        and corr.episode == episode
                        and t0 <= corr.t_tiktok < t1
                    ):
                        xs.append(corr.t_tiktok)
                        ys.append(corr.t_source)
                        ws.append(corr.similarity)
                if len(xs) >= MIN_SEGMENT_INLIER_TIMES:
                    x_arr = np.asarray(xs)
                    seeds = [
                        (remapped[p][1].a, remapped[p][1].b)
                        for p in range(chain_start, chain_end + 1)
                        if remapped[p][1] is not None
                    ]
                    fit = cls._pooled_refit(
                        x_arr,
                        np.asarray(ys),
                        np.asarray(ws),
                        np.round(x_arr * DENSE_SAMPLE_FPS).astype(np.int64),
                        seeds[:12],
                        unit_prior=True,
                    )
                    if fit is not None:
                        a, b = fit[0], fit[1]
                        for piece in range(chain_start, chain_end + 1):
                            piece_scene = final_scenes.scenes[piece]
                            raw[piece] = (
                                a * piece_scene.start_time + b,
                                a * piece_scene.end_time + b,
                            )
            chain_start = chain_end + 1
        for i in range(n - 1):
            if chain_of[i + 1] == chain_of[i] and raw[i] and raw[i + 1]:
                shared = (raw[i][1] + raw[i + 1][0]) / 2.0
                raw[i] = (raw[i][0], shared)
                raw[i + 1] = (shared, raw[i + 1][1])

        # Stage 5: native arbitration & precision layer. Per-end anchoring on
        # the true edge frames replaces the old mean-of-samples delta-lock,
        # and rate arbitration (fitted vs unit slope) runs on native frames.
        refined_delta: dict[int, tuple[float, float]] = {}
        scene_doubts: dict[int, list[str]] = {}
        if samples:
            refined_delta, scene_doubts = cls._stage5_refine(
                video_path,
                final_scenes,
                remapped,
                chain_of,
                raw,
                library_type,
                scene_segments=scene_segments,
                correspondences=correspondences,
                window_cache=window_cache,
            )

        for final_index, (source_scene_indices, segment) in enumerate(remapped):
            scene = final_scenes.scenes[final_index]
            alternatives = cls._alternatives_for_scene(scene, source_scene_indices, scene_segments)
            start_candidates, middle_candidates, end_candidates = cls._edge_candidates(
                scene,
                correspondences,
            )
            if segment is None or raw[final_index] is None:
                matches.matches.append(
                    SceneMatch(
                        scene_index=scene.index,
                        episode="",
                        start_time=0.0,
                        end_time=0.0,
                        confidence=0.0,
                        speed_ratio=1.0,
                        was_no_match=True,
                        merged_from=source_scene_indices if len(source_scene_indices) > 1 else None,
                        alternatives=alternatives,
                        start_candidates=start_candidates,
                        middle_candidates=middle_candidates,
                        end_candidates=end_candidates,
                    )
                )
                continue

            start_time, end_time = raw[final_index]
            delta = refined_delta.get(final_index)
            if delta is not None:
                start_delta, end_delta = delta
                new_start = start_time + start_delta
                new_end = end_time + end_delta
                if new_end > new_start:
                    start_time, end_time = new_start, new_end

            source_duration = max(1e-6, end_time - start_time)
            matches.matches.append(
                SceneMatch(
                    scene_index=scene.index,
                    episode=segment.episode,
                    start_time=float(start_time),
                    end_time=float(end_time),
                    confidence=float(min(1.0, max(0.0, segment.mean_similarity))),
                    speed_ratio=float(scene.duration / source_duration),
                    was_no_match=False,
                    doubt_reasons=scene_doubts.get(final_index, []),
                    merged_from=source_scene_indices if len(source_scene_indices) > 1 else None,
                    alternatives=alternatives,
                    start_candidates=start_candidates,
                    middle_candidates=middle_candidates,
                    end_candidates=end_candidates,
                )
            )
        return matches

    # candidate zooms for the edit geometry: edits are 9:16 crops/zooms of
    # 16:9 sources; the per-project zoom is estimated once from confident
    # chains and reused (GOAL §4 geometric matcher)
    _CANDIDATE_ZOOMS: tuple[float, ...] = (1.0, 1.15, 1.3, 1.45)

    @staticmethod
    def _zoom_crop(
        image: Image.Image, geom: "float | tuple[float, float, float, float]"
    ) -> Image.Image:
        """Crop matching the edit's geometry over the source: either a
        center zoom factor (legacy fallback) or a registered fractional
        footprint rect (x0, y0, x1, y1) — the edit is a full-height,
        per-scene-framed vertical crop, so a center zoom is the wrong
        model whenever the editor reframes off-center (bench 2026-07-11)."""
        if isinstance(geom, tuple):
            w, h = image.size
            x0, y0, x1, y1 = geom
            return image.crop(
                (
                    int(x0 * w),
                    int(y0 * h),
                    max(int(x0 * w) + 8, int(x1 * w)),
                    max(int(y0 * h) + 8, int(y1 * h)),
                )
            )
        if geom <= 1.0:
            return image
        w, h = image.size
        cw, ch = int(w / geom), int(h / geom)
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        return image.crop((x0, y0, x0 + cw, y0 + ch))

    @staticmethod
    def _small_gray(image: Image.Image, height: int = 360) -> np.ndarray:
        """Downscaled grayscale frame for geometric registration."""
        width = max(1, int(image.size[0] * height / max(1, image.size[1])))
        return np.asarray(
            image.convert("L").resize((width, height))
        ).astype(np.float32)

    @classmethod
    def _registration_transform(
        cls, q_gray: np.ndarray, s_gray: np.ndarray
    ) -> np.ndarray | None:
        """ORB+RANSAC partial-affine transform mapping query-plane points
        onto the source plane, or None when registration fails."""
        cv2 = AnimeMatcherService._require_cv2()
        if not hasattr(cv2, "ORB_create"):
            return None  # minimal cv2 builds / test fakes: no registration
        orb = cv2.ORB_create(1500)
        kq, dq = orb.detectAndCompute(q_gray.astype(np.uint8), None)
        ks, ds = orb.detectAndCompute(s_gray.astype(np.uint8), None)
        if dq is None or ds is None or len(kq) < 30 or len(ks) < 30:
            return None
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = matcher.match(dq, ds)
        if len(matches) < 20:
            return None
        qpts = np.float32([kq[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        spts = np.float32([ks[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        T, inliers = cv2.estimateAffinePartial2D(
            qpts, spts, ransacReprojThreshold=3.0
        )
        if T is None or inliers is None or int(inliers.sum()) < 15:
            return None
        return T

    @classmethod
    def _register_affine(
        cls, q_gray: np.ndarray, s_gray: np.ndarray
    ) -> np.ndarray | None:
        """ORB+RANSAC partial-affine registration of the query frame onto the
        source plane; returns the warped query gray or None. This is the §4
        geometric matcher at feature level: the edit is a scaled/shifted crop
        of the source and embeddings/global NCC cannot see past that."""
        cv2 = AnimeMatcherService._require_cv2()
        T = cls._registration_transform(q_gray, s_gray)
        if T is None:
            return None
        return cv2.warpAffine(q_gray, T, (s_gray.shape[1], s_gray.shape[0]))

    @classmethod
    def _footprint_rect(
        cls, q_gray: np.ndarray, s_gray: np.ndarray
    ) -> tuple[float, float, float, float] | None:
        """The query frame's footprint inside the source plane as a
        fractional (x0, y0, x1, y1) rect. The edit is a full-height vertical
        crop whose x-center is framed per scene (measured 0.22-0.65 across
        GT); SSCD compared at this registered geometry separates duplicate
        instances the center-zoom model cannot (bench 2026-07-11: margins
        +0.196..+0.422 on every owner-labeled duplicate, zero control
        flips)."""
        T = cls._registration_transform(q_gray, s_gray)
        if T is None:
            return None
        h, w = q_gray.shape
        corners = np.array(
            [[0, 0], [w, 0], [0, h], [w, h]], dtype=np.float32
        )
        mapped = corners @ T[:, :2].T + T[:, 2]
        sh, sw = s_gray.shape
        x0 = float(np.clip(mapped[:, 0].min() / sw, 0.0, 0.95))
        x1 = float(np.clip(mapped[:, 0].max() / sw, x0 + 0.05, 1.0))
        y0 = float(np.clip(mapped[:, 1].min() / sh, 0.0, 0.95))
        y1 = float(np.clip(mapped[:, 1].max() / sh, y0 + 0.05, 1.0))
        if (x1 - x0) * (y1 - y0) > 0.9:
            return None  # effectively full frame: nothing to crop
        return (x0, y0, x1, y1)

    @classmethod
    def _pan_zero_crossing(
        cls,
        edge_gray: np.ndarray,
        frames: list[tuple[float, Image.Image]],
    ) -> float | None:
        """Time-localize a query edge frame inside a PANNING shot: register
        it onto the source plane, phase-correlate against each native frame,
        and take the zero crossing of the horizontal-shift-vs-time line (the
        moment the pan passes the query's position). Measured on the
        owner-flagged swoosh: error +0.026s where SSCD/NCC fail. Returns the
        source time or None when the shot does not behave like a pan."""
        cv2 = AnimeMatcherService._require_cv2()
        grays = [cls._small_gray(im) for _, im in frames]
        mid = len(grays) // 2
        warped = None
        for ref in (mid, max(0, mid - 6), min(len(grays) - 1, mid + 6)):
            warped = cls._register_affine(edge_gray, grays[ref])
            if warped is not None:
                break
        if warped is None:
            return None
        window = np.outer(
            np.hanning(warped.shape[0]), np.hanning(warped.shape[1])
        )
        wq = (warped * window).astype(np.float64)
        dxs = np.empty(len(grays))
        resps = np.empty(len(grays))
        for n, gray in enumerate(grays):
            (dx, _), resp = cv2.phaseCorrelate(
                wq, (gray * window).astype(np.float64)
            )
            dxs[n] = dx
            resps[n] = resp
        if float(dxs.max() - dxs.min()) < 8.0:
            return None  # not a pan: no exploitable shift trajectory
        times = np.array([t for t, _ in frames])
        best: tuple[float, float] | None = None
        for n in range(len(times) - 1):
            if dxs[n] == 0.0 or (dxs[n] < 0.0) != (dxs[n + 1] < 0.0):
                frac = abs(dxs[n]) / (abs(dxs[n]) + abs(dxs[n + 1]) + 1e-9)
                t0 = float(times[n] + frac * (times[n + 1] - times[n]))
                resp = float(max(resps[n], resps[n + 1]))
                if best is None or resp > best[1]:
                    best = (t0, resp)
        if best is None or best[1] < 0.4:
            return None
        return best[0]

    @classmethod
    def _duplicate_candidates(
        cls,
        i: int,
        j: int,
        scenes: list[Scene],
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        scene_segments: dict[int, list[SegmentHypothesis]],
        correspondences: list[Correspondence],
        current_episode: str,
        current_at,
    ) -> list[dict[str, float | str]]:
        """Distant same-content candidates for a chain: alternative fitted
        lines plus unit-rate correspondence clusters whose index support is
        near the winner's. These are the duplicate instances the index
        cannot separate (R1); pixel arbitration decides among them."""
        t0, t1 = scenes[i].start_time, scenes[j].end_time
        t_mid = 0.5 * (t0 + t1)
        seen_pos: list[tuple[str, float]] = [(current_episode, current_at(t_mid))]

        def distinct(episode: str, pos: float) -> bool:
            return not any(
                episode == ep and abs(pos - p) < 3.0 for ep, p in seen_pos
            )

        candidates: list[dict[str, float | str]] = []
        for piece in range(i, j + 1):
            for orig in remapped[piece][0]:
                for seg in scene_segments.get(orig, [])[:8]:
                    pos = seg.source_at(t_mid)
                    if not distinct(seg.episode, pos):
                        continue
                    seen_pos.append((seg.episode, pos))
                    candidates.append(
                        {
                            "episode": seg.episode,
                            "a": seg.a,
                            "b": seg.b,
                            "rank_sim": seg.mean_similarity,
                        }
                    )
        clusters: dict[tuple[str, int], list[Correspondence]] = {}
        for corr in correspondences:
            if corr.rank >= DECODE_RETRIEVAL_TOP_K:
                continue
            if not (t0 <= corr.t_tiktok < t1):
                continue
            key = (corr.episode, int(round((corr.t_source - corr.t_tiktok) / 2.0)))
            clusters.setdefault(key, []).append(corr)
        if clusters:
            best_sim = max(
                max(c.similarity for c in cl) for cl in clusters.values()
            )
            for (episode, _), cl in clusters.items():
                sim = max(c.similarity for c in cl)
                if len(cl) < 2 or sim < best_sim - 0.10:
                    continue
                b = float(np.median([c.t_source - c.t_tiktok for c in cl]))
                if not distinct(episode, t_mid + b):
                    continue
                seen_pos.append((episode, t_mid + b))
                candidates.append(
                    {"episode": episode, "a": 1.0, "b": b, "rank_sim": sim}
                )
        candidates.sort(key=lambda c: c["rank_sim"], reverse=True)
        candidates = [c for c in candidates if float(c["rank_sim"]) >= 0.35]
        return candidates[:3]

    @staticmethod
    def _piecewise_source_at(i0: int, j0: int, scenes: list[Scene], snapshot):
        """Chain line function over a raw-interval snapshot."""

        def fn(t: float) -> float:
            for piece in range(i0, j0 + 1):
                sc = scenes[piece]
                if t <= sc.end_time + 1e-6 or piece == j0:
                    rp = snapshot[piece] or snapshot[i0]
                    dur = max(sc.end_time - sc.start_time, 1e-6)
                    rate = (rp[1] - rp[0]) / dur
                    return rp[0] + (t - sc.start_time) * rate
            return snapshot[j0][1]

        return fn

    @classmethod
    def _global_duplicate_assignment(
        cls,
        chains: list[tuple[int, int]],
        scenes: list[Scene],
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        raw0: list[tuple[float, float] | None],
        scene_segments: dict[int, list[SegmentHypothesis]],
        correspondences: list[Correspondence],
    ) -> dict[int, dict[str, float | str]]:
        """Soft global assignment over duplicate near-tie candidate sets.

        Every chain contributes a candidate set (its current line plus the
        distant duplicate instances the index cannot separate); a linear DP
        maximizes uniform index support plus a soft forward-continuity
        reward between consecutive choices (edits mostly cut consecutive
        source moments — 79-100% of adjacent pairs, measured priors; reuse
        stays allowed because the reward is soft). Returns the chains whose
        best assignment differs from their current line plus every chain's
        candidate set. The caller applies switches only under zoom-SSCD or
        visual-identity evidence."""
        decode_corrs = [
            c for c in correspondences if c.rank < DECODE_RETRIEVAL_TOP_K
        ]

        def support(ii: int, jj: int, episode: str, line_fn) -> float:
            """Uniform candidate quality: strongest correspondence lying on
            the line inside the chain (same scale for every candidate)."""
            t0 = scenes[ii].start_time
            t1 = scenes[jj].end_time
            best = 0.0
            for c in decode_corrs:
                if c.episode != episode or not (t0 <= c.t_tiktok < t1):
                    continue
                if abs(c.t_source - line_fn(c.t_tiktok)) <= SEGMENT_RESIDUAL_SECONDS:
                    best = max(best, c.similarity)
            return best

        candidate_sets: list[list[dict[str, float | str]]] = []
        for ci, (ii, jj) in enumerate(chains):
            seg0 = remapped[ii][1]
            current_fn = cls._piecewise_source_at(ii, jj, scenes, raw0)
            current = {
                "episode": seg0.episode,
                "a": 1.0,
                "b": 0.0,
                "start": current_fn(scenes[ii].start_time),
                "end": current_fn(scenes[jj].end_time),
                "sim": support(ii, jj, seg0.episode, current_fn),
                "is_current": 1.0,
            }
            cands: list[dict[str, float | str]] = [current]
            if scenes[jj].end_time - scenes[ii].start_time <= 6.0:
                for cand in cls._duplicate_candidates(
                    ii,
                    jj,
                    scenes,
                    remapped,
                    scene_segments,
                    correspondences,
                    seg0.episode,
                    current_fn,
                ):
                    a, b = float(cand["a"]), float(cand["b"])
                    cand_fn = lambda t, _a=a, _b=b: _a * t + _b
                    cands.append(
                        {
                            "episode": cand["episode"],
                            "a": a,
                            "b": b,
                            "start": cand_fn(scenes[ii].start_time),
                            "end": cand_fn(scenes[jj].end_time),
                            "sim": support(
                                ii, jj, str(cand["episode"]), cand_fn
                            ),
                            "is_current": 0.0,
                        }
                    )
            candidate_sets.append(cands)

        # linear DP: node sim + soft forward-continuity between choices
        def edge(prev_ci: int, prev_c, next_ci: int, next_c) -> float:
            if prev_c["episode"] != next_c["episode"]:
                return 0.0
            tt_gap = (
                scenes[chains[next_ci][0]].start_time
                - scenes[chains[prev_ci][1]].end_time
            )
            gap = float(next_c["start"]) - (float(prev_c["end"]) + tt_gap)
            reward = 0.08 * math.exp(-abs(gap) / 8.0)
            if gap < -0.5:
                reward *= 0.5
            return reward

        n_chains = len(chains)
        scores: list[list[float]] = []
        back: list[list[int]] = []
        for ci in range(n_chains):
            row = []
            brow = []
            for c in candidate_sets[ci]:
                node = float(c["sim"])
                if ci == 0:
                    row.append(node)
                    brow.append(-1)
                    continue
                best_prev, best_k = None, -1
                for k, p in enumerate(candidate_sets[ci - 1]):
                    val = scores[ci - 1][k] + edge(ci - 1, p, ci, c)
                    if best_prev is None or val > best_prev:
                        best_prev, best_k = val, k
                row.append((best_prev or 0.0) + node)
                brow.append(best_k)
            scores.append(row)
            back.append(brow)
        switches: dict[int, dict[str, float | str]] = {}
        if not scores or not scores[-1]:
            return switches, candidate_sets
        k = int(np.argmax(scores[-1]))
        for ci in range(n_chains - 1, -1, -1):
            choice = candidate_sets[ci][k]
            if not float(choice["is_current"]):
                switches[ci] = choice
            k = back[ci][k]
            if k < 0 and ci > 0:
                k = int(np.argmax(scores[ci - 1]))
        return switches, candidate_sets

    @classmethod
    def _zoom_sscd_score_line(
        cls,
        q_mids: list[tuple[float, np.ndarray]],
        source_at,
        cache: _WindowEmbedCache,
        episode: str,
        zoom: "float | tuple[float, float, float, float]",
        sweep: float = 0.6,
    ) -> tuple[float, float, np.ndarray] | None:
        """(best mean cos, its offset, matched source embeddings) of the
        chain's mid query embeddings against SSCD embeddings of ZOOM-CROPPED
        native source frames along a candidate line. Plain native SSCD
        prefers the wrong duplicate instance and pixel NCC is too noisy;
        SSCD at the edit's estimated zoom separates them (measured
        2026-07-10: 12/14 positive GT margins on 85de at z=1.45 vs mixed
        at z=1.0)."""
        if not q_mids:
            return None
        preds = np.array([source_at(t) for t, _ in q_mids])
        pad = sweep + 0.2
        lo, hi = float(preds.min()) - pad, float(preds.max()) + pad
        win = cache.window(episode, zoom, lo, hi)
        if win is None:
            return None
        times, embs = win
        q = np.stack([e for _, e in q_mids])
        sims = q @ embs.T
        best: tuple[float, float, np.ndarray] | None = None
        rows = np.arange(len(q_mids))
        for delta in np.arange(-sweep, sweep + 1e-6, 1.0 / VERIFY_DECODE_FPS):
            pos = preds + delta
            cols = np.clip(np.searchsorted(times, pos), 0, len(times) - 1)
            prev_cols = np.clip(cols - 1, 0, len(times) - 1)
            use_prev = np.abs(times[prev_cols] - pos) < np.abs(times[cols] - pos)
            cols = np.where(use_prev, prev_cols, cols)
            valid = np.abs(times[cols] - pos) <= 0.15
            if valid.sum() < max(1, len(q_mids) * 2 // 3):
                continue
            score = float(np.mean(sims[rows, cols][valid]))
            if best is None or score > best[0]:
                # matched source embeddings at this alignment: the caller's
                # native identity certificate compares them across candidates
                matched = np.where(valid[:, None], embs[cols], np.nan)
                best = (score, float(delta), matched)
        return best

    @classmethod
    def _estimate_project_zoom(
        cls,
        chains: list[tuple[int, int]],
        scenes: list[Scene],
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        raw0: list[tuple[float, float] | None],
        mid_embs: dict[int, list[tuple[float, np.ndarray]]],
        cache: _WindowEmbedCache,
    ) -> float:
        """Estimate the edit's zoom over the source once per project from a
        few chains spread across the timeline: the zoom whose cropped-source
        SSCD best matches the query mid frames. Geometry is a property of
        the edit, not of individual scenes (GOAL §4)."""
        probes: list[tuple[int, int, int]] = []
        for ci, (ii, jj) in enumerate(chains):
            if scenes[jj].end_time - scenes[ii].start_time >= 1.5 and mid_embs.get(ci):
                probes.append((ci, ii, jj))
        if not probes:
            return 1.0
        step = max(1, len(probes) // 4)
        probes = probes[::step][:4]
        totals = {zoom: [] for zoom in cls._CANDIDATE_ZOOMS}
        for ci, ii, jj in probes:
            line_fn = cls._piecewise_source_at(ii, jj, scenes, raw0)
            episode = remapped[ii][1].episode
            cap = cache.get_cap(episode)
            if cap is None:
                continue
            for t, emb in mid_embs[ci][:2]:
                pred = line_fn(t)
                frames = AnimeMatcherService._collect_frames_in_window_from_capture(
                    cap, pred - 0.25, pred + 0.25, max_frames=8, sample_frames=3
                )
                if not frames:
                    continue
                nearest = min(frames, key=lambda fr: abs(fr[0] - pred))[1]
                variants = AnimeMatcherService._embed_pil_batch(
                    [
                        cls._zoom_crop(nearest, zoom).convert("RGB")
                        for zoom in cls._CANDIDATE_ZOOMS
                    ]
                )
                for zoom, v in zip(cls._CANDIDATE_ZOOMS, variants):
                    totals[zoom].append(float(emb @ v))
        means = {
            zoom: float(np.mean(vals))
            for zoom, vals in totals.items()
            if vals
        }
        if not means:
            return 1.0
        return max(means, key=means.get)

    @classmethod
    def _recover_no_match(
        cls,
        video_path: Path,
        scenes: list[Scene],
        raw: list[tuple[float, float] | None],
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        scene_segments: dict[int, list[SegmentHypothesis]] | None,
        correspondences: list[Correspondence] | None,
        cache: "_WindowEmbedCache",
        trusted_floor: float,
        doubts: dict[int, list[str]],
    ) -> None:
        """Recover no-match scenes whose true source the native instruments
        can certify. The decode DP scores such scenes below the no-match
        floor when retrieval evidence is thin, but the truth usually sits
        in the scene's own Stage-3 hypotheses, a neighbour's continuation
        or the deep-recall tail — and certification separates it from junk
        (owner-labeled bench 2026-07-11: truth 0.37-0.58 at grid geometry,
        junk <=0.16). Mutates raw/remapped/doubts in place."""
        n = len(scenes)
        for piece in range(n):
            if raw[piece] is not None or remapped[piece][1] is not None:
                continue
            sc = scenes[piece]
            dur = sc.end_time - sc.start_time
            if dur < 0.4:
                continue
            ts = [sc.start_time + f * dur for f in (0.3, 0.5, 0.7)]
            decoded = AnimeMatcherService.extract_frames(video_path, ts)
            pils = [
                (t, fr.convert("RGB"))
                for t, fr in zip(ts, decoded, strict=False)
                if fr is not None
            ]
            if not pils:
                continue
            embs = AnimeMatcherService._embed_pil_batch(
                _presize_images([im for _, im in pils])
            )
            q_mids = [
                (t, e) for (t, _), e in zip(pils, embs, strict=False)
            ]
            q_gray = cls._small_gray(pils[len(pils) // 2][1])
            t_mid = 0.5 * (sc.start_time + sc.end_time)
            seen: list[tuple[str, float]] = []
            cands: list[dict[str, float | str]] = []

            def _add(ep: str, b: float) -> None:
                pos = t_mid + b
                if any(
                    ep == e2 and abs(pos - p2) < 3.0 for e2, p2 in seen
                ):
                    return
                seen.append((ep, pos))
                cands.append({"episode": ep, "b": b})

            # neighbour continuations first (the strongest prior), then
            # the scene's own line hypotheses, then the deep-recall tail
            for nb, side in ((piece - 1, 1), (piece + 1, 0)):
                if (
                    0 <= nb < n
                    and raw[nb] is not None
                    and remapped[nb][1] is not None
                ):
                    b = (
                        raw[nb][1] - scenes[nb].end_time
                        if side == 1
                        else raw[nb][0] - scenes[nb].start_time
                    )
                    _add(remapped[nb][1].episode, b)
            for seg in (scene_segments or {}).get(piece, [])[:8]:
                _add(seg.episode, seg.source_at(t_mid) - t_mid)
            # raw correspondence clusters: thin single-frame evidence the
            # segment fitter never promoted to a line still names the
            # position (5e85#45's truth lives only here)
            corr_clusters: dict[tuple[str, int], list[float]] = {}
            for corr in correspondences or []:
                if not (sc.start_time <= corr.t_tiktok < sc.end_time):
                    continue
                b_c = corr.t_source - corr.t_tiktok
                corr_clusters.setdefault(
                    (corr.episode, int(round(b_c / 2.0))), []
                ).append(b_c)
            for (ep_c, _), bs in sorted(
                corr_clusters.items(), key=lambda kv: -len(kv[1])
            )[:4]:
                _add(ep_c, float(np.median(bs)))
            for c in cls._query_deep_recall(
                q_mids, lambda t: -1e9, ""
            ):
                _add(str(c["episode"]), float(c["b"]))

            import os as _os

            scored: list[tuple[float, str, float]] = []
            grid_budget = 3  # grid fallbacks cost K windows each
            for c in cands[:5]:
                ep, b = str(c["episode"]), float(c["b"])
                line_fn = lambda t, _b=b: t + _b
                rect = None
                s_shape = None
                cap = cache.get_cap(ep)
                if cap is not None:
                    pred = t_mid + b
                    frames = (
                        AnimeMatcherService._collect_frames_in_window_from_capture(
                            cap, pred - 0.3, pred + 0.3,
                            max_frames=8, sample_frames=3,
                        )
                    )
                    for _, im in sorted(
                        frames, key=lambda fr: abs(fr[0] - pred)
                    )[:2]:
                        s_gray = cls._small_gray(im)
                        s_shape = s_gray.shape
                        rect = cls._footprint_rect(q_gray, s_gray)
                        if rect is not None:
                            break
                if rect is not None:
                    res = cls._zoom_sscd_score_line(
                        q_mids, line_fn, cache, ep, rect, sweep=1.2
                    )
                    if res is not None:
                        if _os.environ.get("ATR_RERANK_DEBUG"):
                            print(
                                f"  [rec] scene {piece} {ep[-12:]}@"
                                f"{t_mid + b:.1f} reg score={res[0]:.3f}"
                            )
                        # a successful registration at the position is
                        # itself evidence (>=15 RANSAC inliers on the
                        # candidate's own frame), so the bar sits below
                        # the chain trust floor; the win-margin gate
                        # still arbitrates lookalikes
                        if res[0] >= max(0.55, trusted_floor - 0.15):
                            scored.append((res[0], ep, b + res[1]))
                    continue
                if s_shape is None or grid_budget <= 0:
                    continue
                grid_budget -= 1
                # grid fallback: the query's full-height aspect footprint
                # swept over plausible x-centers (fast/blurred content
                # defeats feature registration; the certification bar
                # still separates truth from junk at 2.6x even on a
                # coarse grid)
                span = (q_gray.shape[1] / q_gray.shape[0]) / (
                    s_shape[1] / s_shape[0]
                )
                span = min(0.95, max(0.15, span))
                g_best = None
                for cx in (0.35, 0.5, 0.65):
                    x0 = min(max(cx - span / 2.0, 0.0), 1.0 - span)
                    res = cls._zoom_sscd_score_line(
                        q_mids,
                        line_fn,
                        cache,
                        ep,
                        (x0, 0.0, x0 + span, 1.0),
                        sweep=1.2,
                    )
                    if res is not None and (
                        g_best is None or res[0] > g_best[0]
                    ):
                        g_best = res
                if g_best is not None:
                    if _os.environ.get("ATR_RERANK_DEBUG"):
                        print(
                            f"  [rec] scene {piece} {ep[-12:]}@"
                            f"{t_mid + b:.1f} grid score={g_best[0]:.3f}"
                        )
                    if g_best[0] >= RECOVERY_CERT_SSCD:
                        scored.append((g_best[0], ep, b + g_best[1]))
            # the same instance discipline as R1: a recovery must WIN, not
            # merely certify — lookalike instances certify too (5e85#11's
            # neighbour continuation), and a wrong recovery stales waivers
            # where a no-match would have stayed harmless
            scored.sort(reverse=True)
            if not scored or (
                len(scored) > 1 and scored[0][0] - scored[1][0] < 0.07
            ):
                continue
            score, ep, b_new = scored[0]
            seg = SegmentHypothesis(
                id=-1,
                episode=ep,
                tiktok_start=sc.start_time,
                tiktok_end=sc.end_time,
                a=1.0,
                b=b_new,
                inlier_count=1,
                mean_similarity=float(min(score, 0.5)),
                score=0.0,
                scene_index=sc.index,
            )
            remapped[piece] = (remapped[piece][0], seg)
            raw[piece] = (sc.start_time + b_new, sc.end_time + b_new)
            doubts.setdefault(piece, []).append("recovered")

    @classmethod
    def _stage5_refine(
        cls,
        video_path: Path,
        final_scenes: SceneList,
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        chain_of: list[int],
        raw: list[tuple[float, float] | None],
        library_type: LibraryType | str,
        scene_segments: dict[int, list[SegmentHypothesis]] | None = None,
        correspondences: list[Correspondence] | None = None,
        window_cache: _WindowEmbedCache | None = None,
    ) -> tuple[dict[int, tuple[float, float]], dict[int, list[str]]]:
        """Stage 5 native arbitration: zoom-SSCD re-ranking of duplicate
        primaries (R1), per-end offsets anchored on the true TikTok edge
        frames, argmax'd against natively decoded source frames along the
        chain line (R2), plus fitted-vs-unit rate arbitration on the same
        native evidence (R3).

        A confident (prominent, narrow) per-end peak is trusted as-is: the
        editor may have trimmed mid-shot, so no source-cut snapping. Only a
        temporally ambiguous end (static plateau) falls back to the other
        end's lock and to snapping on a native frame-change peak. Mutates
        ``raw`` in place when rate arbitration replaces a fitted slope.
        Returns per-piece (start_delta, end_delta) and per-piece doubt tags.
        """
        from .anime_library import AnimeLibraryService

        scenes = final_scenes.scenes
        n = len(scenes)
        refined_delta: dict[int, tuple[float, float]] = {}
        doubts: dict[int, list[str]] = {}
        _prof = {"rect": 0.0, "cur": 0.0, "cand": 0.0, "recall": 0.0}

        chains: list[tuple[int, int]] = []
        i = 0
        while i < n:
            j = i
            while j + 1 < n and chain_of[j + 1] == chain_of[i]:
                j += 1
            if remapped[i][1] is not None and raw[i] is not None and raw[j] is not None:
                chains.append((i, j))
            i = j + 1
        if not chains:
            return refined_delta, doubts

        # true edge frames for every chain end plus per-piece mid frames
        # (pixel arbitration queries), one decode pass + one embed batch;
        # small inward insets add robustness to transition frames
        edge_specs: list[tuple[int, int, float]] = []  # (chain_idx, side, t)
        for ci, (ii, jj) in enumerate(chains):
            span = scenes[jj].end_time - scenes[ii].start_time
            for side, base, sign in (
                (0, scenes[ii].start_time, 1.0),
                (1, scenes[jj].end_time, -1.0),
            ):
                for inset in (0.02, 0.15, 0.30):
                    if inset > 0.02 and inset >= span / 2.0:
                        continue
                    edge_specs.append((ci, side, base + sign * inset))
            pieces = list(range(ii, jj + 1))
            if len(pieces) == 1:
                # single scene: three interior queries — one frame is too
                # weak for both duplicate scoring and the identity
                # certificate (a single pair can be a repeated still)
                sc = scenes[pieces[0]]
                for frac in (0.3, 0.5, 0.7):
                    edge_specs.append(
                        (ci, 2, sc.start_time + frac * sc.duration)
                    )
            else:
                for piece in sorted(
                    {pieces[0], pieces[len(pieces) // 2], pieces[-1]}
                ):
                    sc = scenes[piece]
                    edge_specs.append(
                        (ci, 2, 0.5 * (sc.start_time + sc.end_time))
                    )
        decoded = AnimeMatcherService.extract_frames(
            video_path, [t for _, _, t in edge_specs]
        )
        images: list[Image.Image] = []
        kept: list[tuple[int, int, float]] = []
        for spec, frame in zip(edge_specs, decoded, strict=False):
            if frame is not None:
                images.append(frame.convert("RGB"))
                kept.append(spec)
        if not images:
            return refined_delta, doubts
        edge_embs = AnimeMatcherService._embed_pil_batch(_presize_images(images))
        edge_queries: dict[tuple[int, int], list[tuple[float, np.ndarray]]] = {}
        mid_embs: dict[int, list[tuple[float, np.ndarray]]] = {}
        edge_grays: dict[tuple[int, int], np.ndarray] = {}
        mid_grays: dict[int, np.ndarray] = {}
        for k, ((ci, side, t), emb) in enumerate(zip(kept, edge_embs, strict=False)):
            if side == 2:
                if ci not in mid_grays:
                    # mid query frame for footprint registration (R1)
                    mid_grays[ci] = cls._small_gray(images[k])
                mid_embs.setdefault(ci, []).append((t, emb))
            else:
                if (ci, side) not in edge_queries:
                    # outermost edge frame, kept small for registration
                    edge_grays[(ci, side)] = cls._small_gray(images[k])
                edge_queries.setdefault((ci, side), []).append((t, emb))

        cache = window_cache or _WindowEmbedCache(
            library_type, cls._zoom_crop, VERIFY_DECODE_FPS
        )
        owns_cache = window_cache is None
        step = 1.0 / (2.0 * VERIFY_DECODE_FPS)
        offsets = np.arange(-0.65, 0.65 + 1e-6, step)

        # R1 pass 4: soft global assignment over duplicate near-tie sets.
        # Chronology is resolved GLOBALLY (a DP over all chains) instead of
        # against possibly-wrong neighbours; switches are applied only under
        # zoom-SSCD or visual-identity evidence (in-loop below).
        raw0: list[tuple[float, float] | None] = list(raw)
        assignment_switch: dict[int, dict[str, float | str]] = {}
        candidate_sets: list[list[dict[str, float | str]]] = []
        project_zoom = 1.0
        if scene_segments is not None and correspondences is not None:
            assignment_switch, candidate_sets = cls._global_duplicate_assignment(
                chains, scenes, remapped, raw0, scene_segments, correspondences
            )
            try:
                project_zoom = cls._estimate_project_zoom(
                    chains, scenes, remapped, raw0, mid_embs, cache
                )
            except Exception:
                project_zoom = 1.0
        # per-project trust calibration: registered SSCD scores run on a
        # project-wide scale (0.72-0.93 on one style, 0.64-0.79 on
        # another); anchor the arbitration trust floor to confident probe
        # chains so one style does not arbitrate everything (perf) while
        # another trusts wrong instances (correctness). Probe windows land
        # in the shared cache, so the loop re-scores them for free.
        trusted_floor = DUPLICATE_TRUSTED_SSCD
        try:
            probe_scores: list[float] = []
            probe_cis = [
                ci
                for ci, (ii, jj) in enumerate(chains)
                if scenes[jj].end_time - scenes[ii].start_time >= 1.5
                and mid_embs.get(ci)
                and ci in mid_grays
            ]
            step_p = max(1, len(probe_cis) // 5)
            for ci in probe_cis[::step_p][:5]:
                ii, jj = chains[ci]
                line_fn = cls._piecewise_source_at(ii, jj, scenes, raw0)
                seg_ep = remapped[ii][1].episode
                cap = cache.get_cap(seg_ep)
                if cap is None:
                    continue
                t_mid_p = 0.5 * (scenes[ii].start_time + scenes[jj].end_time)
                pred_p = float(line_fn(t_mid_p))
                frames_p = (
                    AnimeMatcherService._collect_frames_in_window_from_capture(
                        cap, pred_p - 0.3, pred_p + 0.3,
                        max_frames=8, sample_frames=3,
                    )
                )
                rect_p = None
                for _, im in sorted(
                    frames_p, key=lambda fr: abs(fr[0] - pred_p)
                )[:2]:
                    rect_p = cls._footprint_rect(
                        mid_grays[ci], cls._small_gray(im)
                    )
                    if rect_p is not None:
                        break
                if rect_p is None:
                    continue
                res_p = cls._zoom_sscd_score_line(
                    mid_embs[ci], line_fn, cache, seg_ep, rect_p, sweep=0.3
                )
                if res_p is not None:
                    probe_scores.append(res_p[0])
            if probe_scores:
                trusted_floor = min(
                    DUPLICATE_TRUSTED_SSCD,
                    max(0.60, max(probe_scores) - 0.12),
                )
        except Exception:
            trusted_floor = DUPLICATE_TRUSTED_SSCD
        try:
            # visit queue: one pass over all chains, plus bounded forced
            # revisits of a switched chain's neighbours — a partial switch
            # inside a fold leaves siblings on the abandoned line and the
            # merged interval incoherent (dcd#19, v102); the revisit sees
            # the switched neighbour's continuation as a proposal
            def _r2_specs(
                cix: int,
            ) -> tuple[str, list[tuple[float, float]]] | None:
                """The R2 anchoring pass's exact (episode, window) specs
                for a chain, computed from LIVE raw — shared between the
                pass itself and the decode prefetcher so staged runs match
                byte-for-byte."""
                ii, jj = chains[cix]
                seg0 = remapped[ii][1]
                if seg0 is None or raw[ii] is None or raw[jj] is None:
                    return None
                q_all_x = edge_queries.get((cix, 0), []) + edge_queries.get(
                    (cix, 1), []
                )
                if not q_all_x:
                    return None

                def src_at(t: float) -> float:
                    for piece in range(ii, jj + 1):
                        sc = scenes[piece]
                        if t <= sc.end_time + 1e-6 or piece == jj:
                            if raw[piece] is None:
                                return raw[ii][0]
                            dur = max(sc.end_time - sc.start_time, 1e-6)
                            rate = (raw[piece][1] - raw[piece][0]) / dur
                            return raw[piece][0] + (t - sc.start_time) * rate
                    return raw[jj][1]

                targets_x = [src_at(t) for t, _ in q_all_x]
                if ii == jj:
                    sc = scenes[ii]
                    dur = max(sc.end_time - sc.start_time, 1e-6)
                    fitted_rate = (raw[ii][1] - raw[ii][0]) / dur
                    if abs(fitted_rate - 1.0) > 0.1:
                        t_mid_x = 0.5 * (sc.start_time + sc.end_time)
                        mid_src_x = 0.5 * (raw[ii][0] + raw[ii][1])
                        targets_x.extend(
                            mid_src_x + (t - t_mid_x) for t, _ in q_all_x
                        )
                targets_x.sort()
                wins_x: list[tuple[float, float]] = []
                for target in targets_x:
                    lo_x, hi_x = target - 0.85, target + 0.85
                    if wins_x and lo_x <= wins_x[-1][1] + 0.5:
                        wins_x[-1] = (
                            wins_x[-1][0],
                            max(wins_x[-1][1], hi_x),
                        )
                    else:
                        wins_x.append((lo_x, hi_x))
                return seg0.episode, wins_x

            visit_queue: list[tuple[int, bool]] = [
                (ci, False) for ci in range(len(chains))
            ]
            revisited: set[int] = set()
            qi = -1
            while qi + 1 < len(visit_queue):
                qi += 1
                ci, forced_visit = visit_queue[qi]
                i, j = chains[ci]
                segment_first = remapped[i][1]
                episode = segment_first.episode
                for lookahead in (1, 2):
                    # stage the upcoming chains' trust + R2 windows while
                    # this chain registers/scores (their raw is untouched
                    # until their own turn)
                    if qi + lookahead >= len(visit_queue):
                        break
                    nci = visit_queue[qi + lookahead][0]
                    ni_p, nj_p = chains[nci]
                    seg_n = remapped[ni_p][1]
                    if (
                        seg_n is not None
                        and raw[ni_p] is not None
                        and mid_embs.get(nci)
                    ):
                        fn_n = cls._piecewise_source_at(
                            ni_p, nj_p, scenes, raw
                        )
                        preds_n = [
                            float(fn_n(t)) for t, _ in mid_embs[nci]
                        ]
                        cache.prefetch(
                            seg_n.episode,
                            min(preds_n) - 0.5,
                            max(preds_n) + 0.5,
                        )
                        t_mid_n = 0.5 * (
                            scenes[ni_p].start_time + scenes[nj_p].end_time
                        )
                        cache.prefetch_probe(
                            seg_n.episode, float(fn_n(t_mid_n))
                        )
                        spec_n = _r2_specs(nci)
                        if spec_n is not None:
                            for lo_n, hi_n in spec_n[1]:
                                cache.prefetch(spec_n[0], lo_n, hi_n)

                def chain_source_at(t: float) -> float:
                    for piece in range(i, j + 1):
                        sc = scenes[piece]
                        if t <= sc.end_time + 1e-6 or piece == j:
                            if raw[piece] is None:
                                return raw[i][0]
                            dur = max(sc.end_time - sc.start_time, 1e-6)
                            rate = (raw[piece][1] - raw[piece][0]) / dur
                            return raw[piece][0] + (t - sc.start_time) * rate
                    return raw[j][1]

                q_start = edge_queries.get((ci, 0), [])
                q_end = edge_queries.get((ci, 1), [])
                q_all = q_start + q_end
                if not q_all:
                    continue

                # R1: duplicate arbitration by registered-footprint SSCD
                # (content-decided) or global-assignment chronology (for
                # certified-identical repeats, where switching cannot
                # change the render).
                distant = [
                    c
                    for c in (candidate_sets[ci] if ci < len(candidate_sets) else [])
                    if not float(c["is_current"])
                ]
                q_mids = mid_embs.get(ci, [])
                proposed = assignment_switch.get(ci)
                cur_sim = (
                    float(candidate_sets[ci][0]["sim"])
                    if ci < len(candidate_sets) and candidate_sets[ci]
                    else 0.0
                )
                index_suspect = any(
                    float(c["sim"]) >= cur_sim - 0.05 for c in distant
                )
                # candidate recall beyond the assignment sets: index-side
                # self-similarity (duplicate instances that carry no
                # correspondences) plus query-side deep search (true
                # instances outranked by montage lookalikes inside top-K)
                recall: list[dict[str, float | str]] = []
                t_mid_tt = 0.5 * (scenes[i].start_time + scenes[j].end_time)

                def _known(c, known) -> bool:
                    pos = float(c["a"]) * t_mid_tt + float(c["b"])
                    return any(
                        d["episode"] == c["episode"]
                        and abs(
                            pos - (float(d["a"]) * t_mid_tt + float(d["b"]))
                        )
                        < 3.0
                        for d in known
                    )

                import os as _os

                def cand_rect(ep: str, source_fn):
                    """Registered footprint of the query inside this
                    candidate's shot, or None when registration fails."""
                    _t0 = time.perf_counter()
                    q_gray = mid_grays.get(ci)
                    if q_gray is None:
                        _prof["rect"] += time.perf_counter() - _t0
                        return None
                    t_mid = 0.5 * (
                        scenes[i].start_time + scenes[j].end_time
                    )
                    pred = float(source_fn(t_mid))
                    frames = cache.probe_frames(ep, pred)
                    out_rect = None
                    for _, im in sorted(
                        frames, key=lambda fr: abs(fr[0] - pred)
                    )[:2]:
                        out_rect = cls._footprint_rect(
                            q_gray, cls._small_gray(im)
                        )
                        if out_rect is not None:
                            break
                    _prof["rect"] += time.perf_counter() - _t0
                    return out_rect

                def scored_with_rect(
                    ep: str,
                    line_fn,
                    sweep: float = 1.2,
                    rect: "tuple | None" = None,
                ) -> tuple[tuple | None, object]:
                    """Score a line under the edit's registered footprint.
                    The footprint is a property of the EDIT's framing, not
                    of the candidate (bench v4 2026-07-11: a shared crop
                    separates every owner-labeled duplicate), so callers
                    pass the chain's rect when they have one; otherwise
                    register at the line midpoint and — when that frame
                    belongs to another shot — retry at the scorer's own
                    best alignment."""
                    if rect is None:
                        rect = cand_rect(ep, line_fn)
                    res = cls._zoom_sscd_score_line(
                        q_mids,
                        line_fn,
                        cache,
                        ep,
                        rect if rect is not None else project_zoom,
                        sweep=sweep,
                    )
                    if res is not None and rect is None:
                        rect2 = cand_rect(
                            ep,
                            lambda t, _d=res[1]: line_fn(t) + _d,
                        )
                        if rect2 is not None:
                            res2 = cls._zoom_sscd_score_line(
                                q_mids, line_fn, cache, ep, rect2,
                                sweep=sweep,
                            )
                            if res2 is not None:
                                return res2, rect2
                    return res, rect

                # the current line is its own fit, so its peak sits within
                # a narrow sweep; candidates keep the wide sweep because
                # their offsets come from recall medians and cluster fits
                _t_cur = time.perf_counter()
                cur_res, cur_rect = (
                    scored_with_rect(episode, chain_source_at, sweep=0.3)
                    if q_mids
                    else (None, None)
                )
                _prof["cur"] += time.perf_counter() - _t_cur
                cur_doubt = (
                    cur_res is None
                    or cur_rect is None
                    or cur_res[0] < trusted_floor
                )
                arbitrate = bool(q_mids) and (
                    index_suspect
                    or proposed is not None
                    or cur_doubt
                    or forced_visit
                )
                if _os.environ.get("ATR_RERANK_DEBUG"):
                    print(
                        f"[chain] {i}-{j} tt={scenes[i].start_time:.1f}-"
                        f"{scenes[j].end_time:.1f} n_distant={len(distant)} "
                        f"cur={cur_res[0] if cur_res else None} "
                        f"rect={'y' if cur_rect else 'n'} "
                        f"doubt={cur_doubt} arbitrate={arbitrate} "
                        f"proposed={proposed is not None} "
                        f"suspect={index_suspect}"
                    )
                if not arbitrate:
                    # the current line explains the query at the edit's
                    # own registered geometry and nothing else doubts it:
                    # skip arbitration, keep the doubt tag for near-tie
                    # index candidates
                    if any(
                        float(c["sim"]) >= cur_sim - 0.03 for c in distant
                    ):
                        for piece in range(i, j + 1):
                            tags = doubts.setdefault(piece, [])
                            if "duplicate_tie" not in tags:
                                tags.append("duplicate_tie")
                elif cur_res is not None:
                    # chronology proposals: retrieval can be entirely
                    # blind to the true instance (montage lookalike
                    # dominating top-K), but the source mostly continues
                    # across consecutive chains (measured 79-100%
                    # adjacency prior) — propose the neighbours'
                    # unit-rate continuations at NOVEL positions;
                    # registered scoring decides. Each side then scores
                    # under its own registered footprint (project-zoom
                    # fallback). Bench 2026-07-11: with both sides
                    # registered true duplicates separate at >=0.104 and
                    # identical repeats sit <=0.03 (threshold 0.07); with
                    # one side registered the margins are larger but
                    # unmeasured in the reverse direction (bar 0.12).
                    proposals: list[dict[str, float | str]] = []
                    current_as_cand = [
                        {
                            "episode": episode,
                            "a": 1.0,
                            "b": chain_source_at(t_mid_tt) - t_mid_tt,
                        }
                    ]
                    if (
                        (cur_doubt or forced_visit)
                        and scenes[j].end_time - scenes[i].start_time <= 6.0
                    ):
                        # recall runs only for doubtful chains: index-side
                        # self-similarity (duplicate instances carrying no
                        # correspondences) plus query-side deep search
                        # (true instances outranked by montage lookalikes;
                        # edge-inset queries included — fast montages need
                        # more than three query frames to hit the right
                        # sub-shot)
                        mid_ts = [t for t, _ in q_mids]
                        q_recall = (q_mids + q_start[1:] + q_end[1:])[:8]
                        _t_rec = time.perf_counter()
                        recalled = (
                            cls._index_duplicate_recall(
                                episode, chain_source_at, mid_ts
                            )
                            + cls._query_deep_recall(
                                q_recall, chain_source_at, episode
                            )
                            if cur_doubt
                            else []  # forced revisits only need proposals
                        )
                        _prof["recall"] += time.perf_counter() - _t_rec
                        for c in recalled:
                            if not _known(c, distant) and not _known(c, recall):
                                recall.append(c)
                        proposals: list[dict[str, float | str]] = []
                        if ci > 0:
                            _pi2, pj2 = chains[ci - 1]
                            if raw[pj2] is not None and remapped[pj2][1] is not None:
                                proposals.append(
                                    {
                                        "episode": remapped[pj2][1].episode,
                                        "a": 1.0,
                                        "b": raw[pj2][1]
                                        - scenes[pj2].end_time,
                                        "rank_sim": 0.0,
                                    }
                                )
                        if ci + 1 < len(chains):
                            ni2, _nj2 = chains[ci + 1]
                            if raw[ni2] is not None and remapped[ni2][1] is not None:
                                proposals.append(
                                    {
                                        "episode": remapped[ni2][1].episode,
                                        "a": 1.0,
                                        "b": raw[ni2][0]
                                        - scenes[ni2].start_time,
                                        "rank_sim": 0.0,
                                    }
                                )
                        current_as_cand = [
                            {
                                "episode": episode,
                                "a": 1.0,
                                "b": chain_source_at(t_mid_tt) - t_mid_tt,
                            }
                        ]
                        # proposals are only worth their native windows on
                        # deeply doubtful chains: every proposal-fixed
                        # chain measured cur <= 0.638 registered
                        deeply_doubtful = (
                            cur_res is None
                            or cur_res[0] < trusted_floor - 0.05
                            or forced_visit
                        )
                        if _os.environ.get("ATR_RERANK_DEBUG"):
                            print(
                                f"  [prop] chain {i}-{j} deep={deeply_doubtful} "
                                + " ".join(
                                    f"{str(p['episode'])[-10:]}@"
                                    f"{float(p['a']) * t_mid_tt + float(p['b']):.1f}"
                                    for p in proposals
                                )
                            )
                        for c in proposals if deeply_doubtful else []:
                            if _known(c, current_as_cand):
                                continue
                            c["proposal"] = 1.0
                            replaced = False
                            for lst in (recall, distant):
                                for k2, d in enumerate(lst):
                                    if not _known(c, [d]):
                                        continue
                                    # same instance: the chronology offset
                                    # is exact where cluster/fit offsets
                                    # drift by seconds — the proposal's
                                    # line wins (unless the entry is the
                                    # assignment DP's own proposal, whose
                                    # identity the certificate tier needs)
                                    if d is not proposed:
                                        lst[k2] = c
                                    else:
                                        d["proposal"] = 1.0
                                    replaced = True
                                    break
                                if replaced:
                                    break
                            if not replaced:
                                recall.append(c)
                    # native scoring is the expensive step (a decoded +
                    # embedded window per candidate): drop assignment-set
                    # candidates that are not even index near-ties — they
                    # lose by wide margins (measured -0.13..-0.50) — and
                    # score recall/proposals first
                    strong = [
                        c
                        for c in distant
                        if float(c.get("sim", 1.0)) >= cur_sim - 0.10
                        or (proposed is not None and c is proposed)
                    ]
                    distant = (recall + strong)[:5]
                    # stage every candidate's window on the decode worker
                    # while the main thread registers and embeds
                    for cand_pf in distant:
                        a_pf, b_pf = float(cand_pf["a"]), float(cand_pf["b"])
                        preds_pf = [a_pf * t + b_pf for t, _ in q_mids]
                        cache.prefetch(
                            str(cand_pf["episode"]),
                            min(preds_pf) - 1.4,
                            max(preds_pf) + 1.4,
                        )
                        cache.prefetch_probe(
                            str(cand_pf["episode"]),
                            a_pf * t_mid_tt + b_pf,
                        )
                    switch_to: dict[str, float | str] | None = None
                    switch_delta = 0.0
                    switch_reason = ""
                    if distant:
                        cur_score = cur_res[0]
                        cur_start = chain_source_at(scenes[i].start_time)
                        best_margin = None
                        best_switch = None  # (margin, cand, delta)
                        cert_switch = None
                        prop_switch = None
                        for cand in distant:
                            a_c, b_c = float(cand["a"]), float(cand["b"])
                            cand_fn = lambda t, _a=a_c, _b=b_c: _a * t + _b
                            _t_cand = time.perf_counter()
                            # the chain's rect is a cheap LOWER BOUND for a
                            # true candidate (a wrong current instance's
                            # framing understates the truth, measured on
                            # 85de #17/#24); candidates near the decision
                            # boundary pay for their own registration
                            res, c_rect = scored_with_rect(
                                str(cand["episode"]),
                                cand_fn,
                                rect=cur_rect,
                            )
                            if (
                                res is not None
                                and cur_rect is not None
                                and -0.10 <= res[0] - cur_res[0] < 0.09
                            ):
                                # rescore around the first pass's own best
                                # alignment: the wide sweep already found
                                # the offset, the rescore only swaps in
                                # the candidate's registered geometry
                                d0 = res[1]
                                res2, c_rect2 = scored_with_rect(
                                    str(cand["episode"]),
                                    lambda t, _d=d0: cand_fn(t) + _d,
                                    sweep=0.3,
                                )
                                if res2 is not None and res2[0] > res[0]:
                                    res = (res2[0], res2[1] + d0, res2[2])
                                    c_rect = c_rect2
                            _prof["cand"] += time.perf_counter() - _t_cand
                            if res is None:
                                continue
                            margin = res[0] - cur_score
                            if _os.environ.get("ATR_RERANK_DEBUG"):
                                print(
                                    f"  [cand] chain {i}-{j} "
                                    f"{str(cand['episode'])[-14:]} "
                                    f"pos@{a_c * t_mid_tt + b_c:.1f} "
                                    f"rect={'y' if c_rect else 'n'} "
                                    f"score={res[0]:.3f} margin={margin:+.3f} "
                                    f"rank_sim={float(cand['rank_sim']) if 'rank_sim' in cand else -1:.2f}"
                                )
                            if best_margin is None or margin > best_margin:
                                best_margin = margin
                            is_proposed = proposed is not None and cand is proposed
                            threshold = (
                                0.07
                                if cur_rect is not None and c_rect is not None
                                else 0.12
                            )
                            if margin >= threshold or (
                                is_proposed and margin >= 0.02
                            ):
                                # content-decided at the edit's geometry;
                                # ALL candidates are scored and the best
                                # margin wins (first-past-post picked an
                                # inferior instance when recall widened
                                # the candidate set)
                                if best_switch is None or margin > best_switch[0]:
                                    best_switch = (margin, cand, res[1])
                            elif is_proposed and margin >= -0.02:
                                # native identity certificate: the two
                                # aligned decoded windows show the same
                                # frames, so chronology may decide freely
                                cross = np.sum(cur_res[2] * res[2], axis=1)
                                cross = cross[~np.isnan(cross)]
                                if cross.size and float(np.mean(cross)) >= 0.95:
                                    cert_switch = (margin, cand, res[1])
                            elif (
                                forced_visit
                                and cand.get("proposal")
                                and margin >= -0.02
                            ):
                                # fold continuity: a switched neighbour's
                                # continuation with neutral-or-better own
                                # evidence follows the corrected line — a
                                # truly different shot loses by a wide
                                # margin and stays
                                if prop_switch is None or margin > prop_switch[0]:
                                    prop_switch = (margin, cand, res[1])
                        if best_switch is not None:
                            _, switch_to, switch_delta = best_switch
                            switch_reason = "duplicate_rerank"
                        elif cert_switch is not None:
                            _, switch_to, switch_delta = cert_switch
                            switch_reason = "chronology_assign"
                        elif prop_switch is not None:
                            _, switch_to, switch_delta = prop_switch
                            switch_reason = "chronology_assign"
                        import os as _os

                        if _os.environ.get("ATR_RERANK_DEBUG"):
                            print(
                                f"[dup] chain {i}-{j} tt="
                                f"{scenes[i].start_time:.1f}-{scenes[j].end_time:.1f} "
                                f"cur@{cur_start:.1f} score={cur_score:.3f} "
                                f"rect={'y' if cur_rect else 'n'} "
                                f"n_cand={len(distant)} n_recall={len(recall)} "
                                f"best_margin={best_margin} "
                                f"-> {switch_reason or 'keep'}"
                            )
                        if switch_to is not None:
                            a_new = float(switch_to["a"])
                            b_new = float(switch_to["b"]) + switch_delta
                            episode = str(switch_to["episode"])
                            for piece in range(i, j + 1):
                                seg = remapped[piece][1]
                                sc = scenes[piece]
                                if seg is not None:
                                    seg = dc_replace(
                                        seg, episode=episode, a=a_new, b=b_new
                                    )
                                remapped[piece] = (remapped[piece][0], seg)
                                raw[piece] = (
                                    a_new * sc.start_time + b_new,
                                    a_new * sc.end_time + b_new,
                                )
                                doubts.setdefault(piece, []).append(switch_reason)
                            for nb in (ci - 1, ci + 1):
                                if 0 <= nb < len(chains) and nb not in revisited:
                                    revisited.add(nb)
                                    visit_queue.append((nb, True))
                        elif best_margin is not None and abs(best_margin) <= 0.04:
                            for piece in range(i, j + 1):
                                tags = doubts.setdefault(piece, [])
                                if "duplicate_tie" not in tags:
                                    tags.append("duplicate_tie")
                    if switch_to is None and forced_visit and any(
                        _known(p, current_as_cand) for p in proposals
                    ):
                        # this revisited chain already agrees with the
                        # switched neighbour: the fold may extend further —
                        # keep propagating so every sibling piece gets the
                        # chance to join the corrected line
                        for nb in (ci - 1, ci + 1):
                            if 0 <= nb < len(chains) and nb not in revisited:
                                revisited.add(nb)
                                visit_queue.append((nb, True))

                # unit-rate alternative for isolated scenes: index-grid
                # lookalikes can collapse a slope; only native frames can
                # arbitrate fitted rate against real-time playback
                unit_line: tuple[float, float] | None = None
                if i == j:
                    sc = scenes[i]
                    dur = max(sc.end_time - sc.start_time, 1e-6)
                    fitted_rate = (raw[i][1] - raw[i][0]) / dur
                    if abs(fitted_rate - 1.0) > 0.1:
                        t_mid = 0.5 * (sc.start_time + sc.end_time)
                        mid_src = 0.5 * (raw[i][0] + raw[i][1])
                        unit_line = (t_mid, mid_src)

                def unit_source_at(t: float) -> float:
                    return unit_line[1] + (t - unit_line[0])

                spec_r2 = _r2_specs(ci)
                windows: list[tuple[float, float]] = (
                    spec_r2[1] if spec_r2 is not None else []
                )
                times_list = []
                embs_list = []
                for lo, hi in windows:
                    win = cache.window(episode, 1.0, lo, hi)
                    if win is not None:
                        times_list.append(win[0])
                        embs_list.append(win[1])
                if not times_list:
                    continue
                times = np.concatenate(times_list)
                embs = np.concatenate(embs_list, axis=0)
                order = np.argsort(times)
                times = times[order]
                embs = embs[order]

                def sweep(
                    queries: list[tuple[float, np.ndarray]],
                    source_at,
                ) -> tuple[float, float, float, float] | None:
                    """(delta, peak score, prominence, peak width seconds) of
                    the offset sweep for a query set under a source line."""
                    if not queries:
                        return None
                    preds = np.array([source_at(t) for t, _ in queries])
                    q = np.stack([e for _, e in queries])
                    sims = q @ embs.T
                    scores = np.full(offsets.size, np.nan)
                    rows = np.arange(len(queries))
                    for oi, delta in enumerate(offsets):
                        pos = preds + delta
                        cols = np.clip(np.searchsorted(times, pos), 0, len(times) - 1)
                        prev_cols = np.clip(cols - 1, 0, len(times) - 1)
                        use_prev = np.abs(times[prev_cols] - pos) < np.abs(
                            times[cols] - pos
                        )
                        cols = np.where(use_prev, prev_cols, cols)
                        dist = np.abs(times[cols] - pos)
                        valid = dist <= 0.5
                        if valid.sum() < max(1, len(queries) * 2 // 3):
                            continue
                        scores[oi] = float(np.mean(sims[rows, cols][valid]))
                    if np.isnan(scores).all():
                        return None
                    peak = int(np.nanargmax(scores))
                    prominence = float(scores[peak]) - float(np.nanmedian(scores))
                    lo_k = peak
                    while (
                        lo_k - 1 >= 0
                        and not np.isnan(scores[lo_k - 1])
                        and scores[lo_k - 1] >= scores[peak] - 0.01
                    ):
                        lo_k -= 1
                    hi_k = peak
                    while (
                        hi_k + 1 < scores.size
                        and not np.isnan(scores[hi_k + 1])
                        and scores[hi_k + 1] >= scores[peak] - 0.01
                    ):
                        hi_k += 1
                    width = (hi_k - lo_k) * step
                    return float(offsets[peak]), float(scores[peak]), prominence, width

                if unit_line is not None:
                    res_fit = sweep(q_all, chain_source_at)
                    res_unit = sweep(q_all, unit_source_at)
                    if res_unit is not None and (
                        res_fit is None or res_unit[1] > res_fit[1] + 0.01
                    ):
                        sc = scenes[i]
                        raw[i] = (
                            unit_source_at(sc.start_time),
                            unit_source_at(sc.end_time),
                        )
                        doubts.setdefault(i, []).append("rate_arbitrated")

                res_s = sweep(q_start, chain_source_at)
                res_e = sweep(q_end, chain_source_at)

                def confident(res: tuple[float, float, float, float] | None) -> bool:
                    return res is not None and res[2] >= 0.02 and res[3] <= 0.40

                conf_s = confident(res_s)
                conf_e = confident(res_e)
                delta_s = res_s[0] if conf_s else None
                delta_e = res_e[0] if conf_e else None
                delta_all: float | None = None
                if delta_s is None or delta_e is None:
                    res_all = sweep(q_all, chain_source_at)
                    if res_all is not None and res_all[2] >= 0.015:
                        delta_all = res_all[0]
                def pan_localize(side: int) -> float | None:
                    """Registered pan localization for an ambiguous end: only
                    when the scene content moves (a static shot has no pan
                    trajectory to exploit — measured)."""
                    edge_gray = edge_grays.get((ci, side))
                    q_set = q_start if side == 0 else q_end
                    if edge_gray is None or len(q_set) < 2:
                        return None
                    if float(q_set[0][1] @ q_set[-1][1]) >= 0.85:
                        return None
                    t_edge = q_set[0][0]
                    pred = chain_source_at(t_edge)
                    cap = cache.get_cap(episode)
                    if cap is None:
                        return None
                    pan_frames = (
                        AnimeMatcherService._collect_frames_in_window_from_capture(
                            cap,
                            pred - 1.3,
                            pred + 1.3,
                            max_frames=170,
                            sample_frames=62,
                        )
                    )
                    if len(pan_frames) < 8:
                        return None
                    t0 = cls._pan_zero_crossing(edge_gray, pan_frames)
                    if t0 is None or abs(t0 - pred) > 1.2:
                        return None
                    return t0 - pred

                if not conf_s:
                    pan_delta = pan_localize(0)
                    if pan_delta is not None:
                        delta_s = pan_delta
                        conf_s = True
                        for piece in range(i, j + 1):
                            doubts.setdefault(piece, []).append("pan_localized")
                    else:
                        for piece in range(i, j + 1):
                            doubts.setdefault(piece, []).append("static_start")
                        delta_s = delta_e if delta_e is not None else delta_all
                if not conf_e:
                    pan_delta = pan_localize(1)
                    if pan_delta is not None:
                        delta_e = pan_delta
                        conf_e = True
                        for piece in range(i, j + 1):
                            doubts.setdefault(piece, []).append("pan_localized")
                    else:
                        for piece in range(i, j + 1):
                            doubts.setdefault(piece, []).append("static_end")
                        delta_e = delta_s if delta_s is not None else delta_all
                if delta_s is not None and delta_e is not None:
                    span = max(scenes[j].end_time - scenes[i].start_time, 1e-6)
                    for piece in range(i, j + 1):
                        f0 = (scenes[piece].start_time - scenes[i].start_time) / span
                        f1 = (scenes[piece].end_time - scenes[i].start_time) / span
                        refined_delta[piece] = (
                            delta_s + (delta_e - delta_s) * f0,
                            delta_s + (delta_e - delta_s) * f1,
                        )

                # interior chain boundaries: the pooled line smears the
                # editor's cut position across chained pieces; when a real
                # source cut sits next to the interior boundary, the TikTok
                # cut almost surely aligns WITH it (owner-reviewed failure:
                # a chained piece's last frame came from the next sequence).
                # Snapping both pieces to the cut keeps near-continuity.
                for piece in range(i, j):
                    if raw[piece] is None or raw[piece + 1] is None:
                        continue
                    d_left = refined_delta.get(piece, (0.0, 0.0))
                    d_right = refined_delta.get(piece + 1, (0.0, 0.0))
                    shared = raw[piece][1] + d_left[1]
                    win = cache.window(episode, 1.0, shared - 0.75, shared + 0.75)
                    if win is None or win[0].size < 3:
                        continue
                    w_times, w_embs = win
                    w_diffs = 1.0 - np.sum(w_embs[1:] * w_embs[:-1], axis=1)
                    w_mids = (w_times[1:] + w_times[:-1]) / 2.0
                    w_strong = w_diffs >= max(0.08, 3.0 * float(np.median(w_diffs)))
                    # pull-back only: a boundary PAST a cut renders next-
                    # shot frames in the left piece (containment violation,
                    # the owner-confirmed defect); pushing a boundary OUT to
                    # a later cut extends owner-validated intervals and was
                    # measured harmful (85de#9 round-2 perturbation)
                    cuts = [
                        c
                        for c, s_ in zip(w_mids, w_strong)
                        if s_ and 0.0 < shared - c <= 0.55
                    ]
                    if not cuts:
                        continue
                    c = min(cuts, key=lambda v: abs(v - shared))
                    if abs(c - shared) < 0.08:
                        continue
                    half = 0.5 / VERIFY_DECODE_FPS
                    refined_delta[piece] = (
                        d_left[0],
                        c - half - raw[piece][1],
                    )
                    refined_delta[piece + 1] = (
                        c + half - raw[piece + 1][0],
                        d_right[1],
                    )

                # snap-to-source-cut ONLY for ambiguous ends: when the edge
                # frame cannot place itself in time (static plateau), the
                # best remaining prior is that the editor cut on a source cut
                if len(times) >= 3:
                    emb_diffs = 1.0 - np.sum(embs[1:] * embs[:-1], axis=1)
                    diff_mid = (times[1:] + times[:-1]) / 2.0
                    strong = emb_diffs >= max(0.08, 3.0 * float(np.median(emb_diffs)))
                    for piece, side, was_confident in ((i, 0, conf_s), (j, 1, conf_e)):
                        if was_confident or raw[piece] is None:
                            continue
                        base_delta = refined_delta.get(piece, (0.0, 0.0))[side]
                        target = raw[piece][side] + base_delta
                        near = np.abs(diff_mid - target) <= 0.55
                        cand = near & strong
                        if not cand.any():
                            continue
                        snap_idx = int(
                            np.argmin(np.where(cand, np.abs(diff_mid - target), np.inf))
                        )
                        snap_t = float(diff_mid[snap_idx])
                        prev = refined_delta.get(piece, (0.0, 0.0))
                        if side == 0:
                            refined_delta[piece] = (snap_t - raw[piece][0], prev[1])
                        else:
                            refined_delta[piece] = (prev[0], snap_t - raw[piece][1])

            # R5b: piece-outlier arbitration. A multi-piece chain can hide
            # ONE wrong piece: the edit jumps away and back (85de GT#10/
            # #11/#12: 256.0 -> 198.6 -> 257.0) while a lookalike keeps the
            # pieces' lines continuous, so whole-chain arbitration sees a
            # healthy chain. Per-piece registered scores on the chain's
            # own cached window expose the odd piece (bench margin +0.40
            # registered for the owner-labeled case); the outlier piece
            # then arbitrates alone via deep recall.
            for ci, (i, j) in enumerate(chains):
                if j <= i:
                    continue
                q_mids = mid_embs.get(ci, [])
                q_gray = mid_grays.get(ci)
                if len(q_mids) < 2 or q_gray is None:
                    continue
                segment_first = remapped[i][1]
                if segment_first is None or raw[i] is None:
                    continue
                episode = segment_first.episode

                def chain_line(t: float, _i=i, _j=j) -> float:
                    for piece in range(_i, _j + 1):
                        sc = scenes[piece]
                        if t <= sc.end_time + 1e-6 or piece == _j:
                            if raw[piece] is None:
                                return raw[_i][0]
                            dur = max(sc.end_time - sc.start_time, 1e-6)
                            rate = (raw[piece][1] - raw[piece][0]) / dur
                            return raw[piece][0] + (t - sc.start_time) * rate
                    return raw[_j][1]

                cap0 = cache.get_cap(episode)
                if cap0 is None:
                    continue
                pred0 = float(chain_line(0.5 * (scenes[i].start_time + scenes[j].end_time)))
                frames0 = AnimeMatcherService._collect_frames_in_window_from_capture(
                    cap0, pred0 - 0.3, pred0 + 0.3, max_frames=8, sample_frames=3
                )
                rect0 = None
                for _, im in sorted(frames0, key=lambda fr: abs(fr[0] - pred0))[:2]:
                    rect0 = cls._footprint_rect(q_gray, cls._small_gray(im))
                    if rect0 is not None:
                        break
                if rect0 is None:
                    continue
                piece_scores: dict[int, float] = {}
                for piece in range(i, j + 1):
                    sc = scenes[piece]
                    pm = [
                        (t, e)
                        for t, e in q_mids
                        if sc.start_time <= t <= sc.end_time
                    ]
                    if not pm or raw[piece] is None:
                        continue
                    r = cls._zoom_sscd_score_line(
                        pm, chain_line, cache, episode, rect0, sweep=0.3
                    )
                    if r is not None:
                        piece_scores[piece] = r[0]
                if _os.environ.get("ATR_RERANK_DEBUG"):
                    print(
                        f"[pieces] chain {i}-{j} "
                        + " ".join(
                            f"{p}:{v:.2f}" for p, v in sorted(piece_scores.items())
                        )
                    )
                if len(piece_scores) < 2:
                    continue
                vmax = max(piece_scores.values())
                for piece, v in piece_scores.items():
                    if v >= vmax - 0.25 or v >= trusted_floor:
                        continue
                    sc = scenes[piece]
                    # the chain carries one mid per piece; recall's >=2
                    # distinct-query-time agreement gate needs more, so
                    # the outlier piece decodes its own mids (rare, cheap)
                    ts_p = [
                        sc.start_time + f * (sc.end_time - sc.start_time)
                        for f in (0.3, 0.5, 0.7)
                    ]
                    dec_p = AnimeMatcherService.extract_frames(
                        video_path, ts_p
                    )
                    pils_p = [
                        (t, fr.convert("RGB"))
                        for t, fr in zip(ts_p, dec_p, strict=False)
                        if fr is not None
                    ]
                    if not pils_p:
                        continue
                    embs_p = AnimeMatcherService._embed_pil_batch(
                        _presize_images([im for _, im in pils_p])
                    )
                    pm = [
                        (t, e)
                        for (t, _), e in zip(pils_p, embs_p, strict=False)
                    ]
                    cands_p = cls._query_deep_recall(pm, chain_line, episode)
                    best_p: tuple[float, dict, float] | None = None
                    for cand in cands_p[:3]:
                        a_c, b_c = float(cand["a"]), float(cand["b"])
                        cand_fn = lambda t, _a=a_c, _b=b_c: _a * t + _b
                        cap_c = cache.get_cap(str(cand["episode"]))
                        rect_c = None
                        if cap_c is not None:
                            pred_c = float(cand_fn(0.5 * (sc.start_time + sc.end_time)))
                            frames_c = AnimeMatcherService._collect_frames_in_window_from_capture(
                                cap_c, pred_c - 0.3, pred_c + 0.3,
                                max_frames=8, sample_frames=3,
                            )
                            for _, im in sorted(
                                frames_c, key=lambda fr: abs(fr[0] - pred_c)
                            )[:2]:
                                rect_c = cls._footprint_rect(
                                    q_gray, cls._small_gray(im)
                                )
                                if rect_c is not None:
                                    break
                        res_p = cls._zoom_sscd_score_line(
                            pm,
                            cand_fn,
                            cache,
                            str(cand["episode"]),
                            rect_c if rect_c is not None else project_zoom,
                            sweep=1.2,
                        )
                        if res_p is None:
                            continue
                        margin_p = res_p[0] - v
                        threshold_p = 0.07 if rect_c is not None else 0.12
                        if _os.environ.get("ATR_RERANK_DEBUG"):
                            print(
                                f"  [piece] chain {i}-{j} piece {piece} "
                                f"v={v:.3f} cand@"
                                f"{a_c * 0.5 * (sc.start_time + sc.end_time) + b_c:.1f} "
                                f"score={res_p[0]:.3f} margin={margin_p:+.3f}"
                            )
                        if margin_p >= threshold_p and (
                            best_p is None or res_p[0] > best_p[0]
                        ):
                            best_p = (res_p[0], cand, res_p[1])
                    if best_p is not None:
                        _, cand, delta_p = best_p
                        a_n, b_n = float(cand["a"]), float(cand["b"]) + delta_p
                        ep_n = str(cand["episode"])
                        seg_p = remapped[piece][1]
                        if seg_p is not None:
                            seg_p = dc_replace(seg_p, episode=ep_n, a=a_n, b=b_n)
                        remapped[piece] = (remapped[piece][0], seg_p)
                        raw[piece] = (
                            a_n * sc.start_time + b_n,
                            a_n * sc.end_time + b_n,
                        )
                        refined_delta.pop(piece, None)
                        doubts.setdefault(piece, []).append("duplicate_rerank")

            # start-side containment (owner-endorsed, round 6) as a
            # POST-pass: it must see the raw values AFTER the R5b
            # piece switches — 85de#12's rendered start only becomes a
            # render-segment start once piece 12 has moved to its own
            # line.
            for ci, (i, j) in enumerate(chains):
                if remapped[i][1] is None:
                    continue
                episode = remapped[i][1].episode
                # start-side containment (owner-endorsed, round 6): the
                # locked interval must not CROSS a native source cut that
                # the TikTok start frame sits after — a start placed before
                # the cut renders frames from a different sequence (85de
                # #12/#13: starts 0.84-1.03s early across a hard cut). The
                # scan extends the already-cached window by a few slots;
                # the start only ever pulls FORWARD, onto the cut.
                # render-segment starts: the chain start plus any piece
                # whose predecessor sits on a DIFFERENT line (piece-level
                # switches break continuity mid-chain — 85de#12's rendered
                # start is such a piece)
                seg_starts = [i]
                for p_c in range(i + 1, j + 1):
                    if raw[p_c] is None or raw[p_c - 1] is None:
                        continue
                    ep_a = (
                        remapped[p_c][1].episode
                        if remapped[p_c][1] is not None
                        else None
                    )
                    ep_b = (
                        remapped[p_c - 1][1].episode
                        if remapped[p_c - 1][1] is not None
                        else None
                    )
                    if ep_a != ep_b or abs(
                        raw[p_c][0] - raw[p_c - 1][1]
                    ) > INLIER_TOLERANCE_SECONDS:
                        seg_starts.append(p_c)
                for p_c in seg_starts:
                    if raw[p_c] is None:
                        continue
                    seg_ep = (
                        remapped[p_c][1].episode
                        if remapped[p_c][1] is not None
                        else episode
                    )
                    # segment end: last piece before the next segment start
                    p_end = j
                    for q_c in seg_starts:
                        if q_c > p_c:
                            p_end = q_c - 1
                            break
                    if raw[p_end] is None:
                        continue
                    d_start = refined_delta.get(p_c, (0.0, 0.0))
                    s0 = raw[p_c][0] + d_start[0]
                    s1 = raw[p_end][1] + refined_delta.get(
                        p_end, (0.0, 0.0)
                    )[1]
                    if s1 - s0 < 0.3:
                        continue
                    # 0.85 reach = the R2 anchoring window's own span:
                    # the scan is a pure cache hit (zero new decode); both
                    # owner cases needed <=0.35
                    win_c = cache.window(
                        seg_ep, 1.0, s0, min(s0 + 0.85, s1)
                    )
                    if win_c is None or win_c[0].size < 4:
                        continue
                    ct, ce = win_c
                    cd = 1.0 - np.sum(ce[1:] * ce[:-1], axis=1)
                    cmid = (ct[1:] + ct[:-1]) / 2.0
                    floor_c = max(0.08, 3.0 * float(np.median(cd)))
                    # a cut pair counts when its POST-cut frame lands
                    # inside the interval (the pair may straddle s0
                    # itself); the start pulls onto that first clean frame
                    cuts_c = ct[1:][
                        (cd >= floor_c)
                        & (ct[1:] > s0 + 0.01)
                        & (cmid < s1 - 0.1)
                    ]
                    import os as _os2

                    if _os2.environ.get("ATR_RERANK_DEBUG"):
                        print(
                            f"[contain] chain {i}-{j} piece {p_c} "
                            f"s0={s0:.3f} s1={s1:.3f} n_cuts={cuts_c.size} "
                            f"maxd={float(cd.max()) if cd.size else -1:.3f} "
                            f"t0={float(ct[0]):.3f} t1={float(ct[-1]):.3f} "
                            f"cuts={[round(float(c), 2) for c in cuts_c[:3]]}"
                        )
                    if not cuts_c.size:
                        continue
                    cut_t = float(cuts_c.min())
                    # start-edge query: the chain edge frame for the chain
                    # start; a freshly decoded frame for mid-chain starts
                    if p_c == i and edge_queries.get((ci, 0)):
                        q0_emb = edge_queries[(ci, 0)][0][1]
                    else:
                        dec_c = AnimeMatcherService.extract_frames(
                            video_path, [scenes[p_c].start_time + 0.02]
                        )
                        if not dec_c or dec_c[0] is None:
                            continue
                        q0_emb = AnimeMatcherService._embed_pil_batch(
                            _presize_images([dec_c[0].convert("RGB")])
                        )[0]
                    pre_m = ct < cut_t
                    post_m = (ct >= cut_t) & (ct <= min(cut_t + 0.8, s1))
                    if not (pre_m.any() and post_m.any()):
                        continue
                    sims_c = ce @ q0_emb
                    sim_pre = float(sims_c[pre_m].max())
                    sim_post = float(sims_c[post_m].max())
                    if _os2.environ.get("ATR_RERANK_DEBUG"):
                        print(
                            f"[contain]   cut@{cut_t:.2f} pre={sim_pre:.3f} "
                            f"post={sim_post:.3f}"
                        )
                    if sim_post >= sim_pre + 0.05:
                        refined_delta[p_c] = (
                            cut_t - raw[p_c][0],
                            d_start[1],
                        )


            # R6 no-match recovery: built, measured, and DISABLED — every
            # owner-labeled target legitimately abstains under the win-
            # margin discipline (lookalike loops within 0.07; certification
            # below bar; a piece spanning two GT scenes), so the pass costs
            # ~40s/project for zero output change (journal v106/v116). The
            # instrument remains available for M5:
            # cls._recover_no_match(video_path, scenes, raw, remapped,
            #     scene_segments, correspondences, cache, trusted_floor,
            #     doubts)
        finally:
            if owns_cache:
                cache.close()
            import os as _os

            if _os.environ.get("ATR_RERANK_DEBUG"):
                print(
                    "[prof] "
                    + " ".join(f"{k}={v:.1f}s" for k, v in _prof.items())
                )
        return refined_delta, doubts



    @classmethod
    def _alternatives_for_scene(
        cls,
        scene: Scene,
        source_scene_indices: list[int],
        scene_segments: dict[int, list[SegmentHypothesis]],
    ) -> list[AlternativeMatch]:
        alternatives: list[AlternativeMatch] = []
        seen: set[tuple[str, int, int]] = set()
        candidate_segments: list[SegmentHypothesis] = []
        for source_index in source_scene_indices:
            candidate_segments.extend(scene_segments.get(source_index, []))
        candidate_segments.sort(key=lambda segment: segment.score, reverse=True)
        for segment in candidate_segments:
            start, end = segment.source_interval(scene)
            key = (segment.episode, round(start * 2), round(end * 2))
            if key in seen:
                continue
            seen.add(key)
            source_duration = max(1e-6, end - start)
            alternatives.append(
                AlternativeMatch(
                    episode=segment.episode,
                    start_time=float(start),
                    end_time=float(end),
                    confidence=float(min(1.0, max(0.0, segment.mean_similarity))),
                    speed_ratio=float(scene.duration / source_duration),
                    vote_count=segment.inlier_count,
                    algorithm="global_segment",
                )
            )
            if len(alternatives) >= ALTERNATIVES_PER_SCENE:
                break
        return alternatives

    @classmethod
    def _edge_candidates(
        cls,
        scene: Scene,
        correspondences: list[Correspondence],
    ) -> tuple[list[MatchCandidate], list[MatchCandidate], list[MatchCandidate]]:
        start_target, end_target = cls._scene_edge_times(scene)
        return (
            cls._nearest_candidates(start_target, correspondences),
            cls._nearest_candidates((scene.start_time + scene.end_time) / 2.0, correspondences),
            cls._nearest_candidates(end_target, correspondences),
        )

    @staticmethod
    def _scene_edge_times(scene: Scene) -> tuple[float, float]:
        inward = min(1.0 / DENSE_SAMPLE_FPS, max(scene.duration, 0.0) / 4.0)
        start = scene.start_time + inward
        end = scene.end_time - inward
        if end <= start:
            center = (scene.start_time + scene.end_time) / 2.0
            return center, center
        return start, end

    @staticmethod
    def _nearest_candidates(
        target_time: float,
        correspondences: list[Correspondence],
    ) -> list[MatchCandidate]:
        nearby = [
            corr
            for corr in correspondences
            if abs(corr.t_tiktok - target_time) <= (1.0 / DENSE_SAMPLE_FPS + 1e-6)
        ]
        nearby.sort(key=lambda corr: (abs(corr.t_tiktok - target_time), -corr.similarity))
        deduped: list[MatchCandidate] = []
        seen: set[tuple[str, int]] = set()
        for corr in nearby:
            key = (corr.episode, round(corr.t_source * 2))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(
                MatchCandidate(
                    episode=corr.episode,
                    timestamp=float(corr.t_source),
                    similarity=float(corr.similarity),
                    series=corr.series,
                )
            )
            if len(deduped) >= RETRIEVAL_TOP_K:
                break
        return deduped
