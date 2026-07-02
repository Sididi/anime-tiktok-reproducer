"""Global scene alignment for TikTok edits against indexed anime sources."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
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
# Continuity at a cut is judged within roughly one 2 fps index grid step.
CONTINUITY_GRID_STEPS = 1.25
# Episode switches are rare intruders, so they pay a mild but survivable penalty.
EPISODE_SWITCH_PENALTY = 0.25
# Playback is centered near 1x, but this prior is deliberately weak for fast/slow edits.
UNIT_SOURCE_RATE_PRIOR_WEIGHT = 0.04
# Backward source jumps exist, so penalize instead of forbidding them.
BACKWARD_JUMP_PENALTY = 0.20
# No-match remains available but should lose to even modest coherent evidence.
NO_MATCH_PENALTY = 0.60
# Dense inlier support matters, but repeated anime stills make it weaker than similarity.
SUPPORT_PRIOR_WEIGHT = 0.15
# UI contract exposes a compact ranked list, matching the old frontend expectation.
ALTERNATIVES_PER_SCENE = 5
# Refinement is authoritative only if it stays near the indexed interval.
BOUNDARY_REFINE_MAX_SHIFT_SECONDS = 1.25
# Ten source samples trims refinement cost while preserving sub-grid boundary search.
ALIGNER_REFINE_FRAMES_PER_BOUNDARY = 10
# Query-side crop variants are searched when dense direct support stays weak.
QUERY_VARIANT_MIN_SCENE_SUPPORT = 4


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

    def stats(self) -> dict[str, float]:
        result = {
            "aligner_sample_count": float(self.sample_count),
            "aligner_correspondence_count": float(self.correspondence_count),
            "aligner_segment_count": float(self.segment_count),
            "aligner_weak_variant_sample_count": float(self.weak_variant_sample_count),
        }
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
        samples = cls.sample_query_video(video_path)
        diagnostics.phase_timings["sample"] = time.perf_counter() - started
        diagnostics.sample_count = len(samples)

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
        cls._add_global_path_segments(
            scenes,
            correspondences,
            scene_segments,
            max_segments=MAX_SEGMENTS_PER_SCENE,
        )
        decode_segments = cls.extract_scene_segments(
            scenes,
            decode_correspondences,
            max_segments=DECODE_SEGMENTS_PER_SCENE,
            line_limit=DECODE_RETRIEVAL_TOP_K,
        )
        cls._add_global_path_segments(
            scenes,
            decode_correspondences,
            decode_segments,
            max_segments=DECODE_SEGMENTS_PER_SCENE,
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
            cls._add_global_path_segments(
                scenes,
                correspondences,
                scene_segments,
                max_segments=MAX_SEGMENTS_PER_SCENE,
            )
            decode_segments = cls.extract_scene_segments(
                scenes,
                decode_correspondences,
                max_segments=DECODE_SEGMENTS_PER_SCENE,
                line_limit=DECODE_RETRIEVAL_TOP_K,
            )
            cls._add_global_path_segments(
                scenes,
                decode_correspondences,
                decode_segments,
                max_segments=DECODE_SEGMENTS_PER_SCENE,
            )
            cls._trim_scene_segments(decode_segments, DECODE_SEGMENTS_PER_SCENE)
            diagnostics.phase_timings["variant_retrieve"] = time.perf_counter() - started
            diagnostics.weak_variant_sample_count = len(variant_samples)

        diagnostics.correspondence_count = len(correspondences)
        diagnostics.segments = [segment for values in scene_segments.values() for segment in values]
        diagnostics.segment_count = len(diagnostics.segments)

        started = time.perf_counter()
        decoded = cls.decode_scene_sequence(scenes, decode_segments)
        diagnostics.phase_timings["decode"] = time.perf_counter() - started

        started = time.perf_counter()
        final_scenes, remapped = cls._remap_decoded_without_merge(scenes, decoded)
        diagnostics.phase_timings["merge"] = time.perf_counter() - started

        started = time.perf_counter()
        matches = cls._build_matches(
            video_path,
            final_scenes,
            remapped,
            scene_segments,
            correspondences,
            library_type,
        )
        diagnostics.phase_timings["refine_build"] = time.perf_counter() - started
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
    def _add_global_path_segments(
        cls,
        scenes: SceneList,
        correspondences: list[Correspondence],
        scene_segments: dict[int, list[SegmentHypothesis]],
        *,
        max_segments: int,
    ) -> None:
        path = cls._decode_sample_path(scenes, correspondences)
        if not path:
            return
        next_id = (
            max((segment.id for values in scene_segments.values() for segment in values), default=-1)
            + 1
        )
        by_scene: dict[int, list[Correspondence]] = {i: [] for i in range(len(scenes.scenes))}
        scene_index = 0
        for corr in path:
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

        for index, scene in enumerate(scenes.scenes):
            values = by_scene[index]
            if len(values) < MIN_SEGMENT_INLIER_TIMES:
                continue
            episode_counts: dict[str, int] = {}
            for corr in values:
                episode_counts[corr.episode] = episode_counts.get(corr.episode, 0) + 1
            episode = max(episode_counts, key=episode_counts.get)
            episode_values = [corr for corr in values if corr.episode == episode]
            if len(episode_values) < MIN_SEGMENT_INLIER_TIMES:
                continue
            seed = cls._refit_line_from_inliers(
                scene,
                episode,
                episode_values,
                1.0,
                episode_values[0].t_source - episode_values[0].t_tiktok,
            )
            if seed is None:
                continue
            segment = SegmentHypothesis(
                id=next_id,
                episode=seed.episode,
                tiktok_start=seed.tiktok_start,
                tiktok_end=seed.tiktok_end,
                a=seed.a,
                b=seed.b,
                inlier_count=seed.inlier_count,
                mean_similarity=seed.mean_similarity,
                score=seed.score,
                scene_index=seed.scene_index,
            )
            next_id += 1
            scene_segments.setdefault(index, []).append(segment)
            scene_segments[index].sort(key=lambda item: item.score, reverse=True)
            del scene_segments[index][max_segments:]

    @classmethod
    def _decode_sample_path(
        cls,
        scenes: SceneList,
        correspondences: list[Correspondence],
    ) -> list[Correspondence]:
        if not correspondences:
            return []
        by_time: dict[float, list[Correspondence]] = {}
        for corr in correspondences:
            by_time.setdefault(round(corr.t_tiktok, 3), []).append(corr)
        times = sorted(by_time)
        if not times:
            return []
        states: list[list[Correspondence]] = []
        for sample_time in times:
            values = by_time[sample_time]
            values.sort(key=lambda corr: corr.similarity, reverse=True)
            states.append(values[:RETRIEVAL_TOP_K])

        scores: list[list[float]] = [[corr.similarity for corr in states[0]]]
        back: list[list[int]] = [[-1 for _ in states[0]]]
        for time_index in range(1, len(times)):
            previous_time = times[time_index - 1]
            current_time = times[time_index]
            crosses_boundary = cls._crosses_scene_boundary(
                scenes,
                previous_time,
                current_time,
            )
            row: list[float] = []
            row_back: list[int] = []
            for current in states[time_index]:
                best_score = -math.inf
                best_index = 0
                for prev_index, previous in enumerate(states[time_index - 1]):
                    score = (
                        scores[-1][prev_index]
                        + current.similarity
                        + cls._sample_transition_score(
                            previous,
                            current,
                            current_time - previous_time,
                            crosses_boundary,
                        )
                    )
                    if score > best_score:
                        best_score = score
                        best_index = prev_index
                row.append(best_score)
                row_back.append(best_index)
            scores.append(row)
            back.append(row_back)

        state_index = int(np.argmax(scores[-1]))
        path: list[Correspondence] = [states[-1][state_index]]
        for time_index in range(len(times) - 1, 0, -1):
            state_index = back[time_index][state_index]
            path.append(states[time_index - 1][state_index])
        path.reverse()
        return path

    @classmethod
    def _crosses_scene_boundary(
        cls,
        scenes: SceneList,
        previous_time: float,
        current_time: float,
    ) -> bool:
        for scene in scenes.scenes:
            if previous_time < scene.end_time <= current_time:
                return True
            if scene.start_time > current_time:
                break
        return False

    @staticmethod
    def _sample_transition_score(
        previous: Correspondence,
        current: Correspondence,
        dt: float,
        crosses_boundary: bool,
    ) -> float:
        if dt <= 0:
            return 0.0
        if previous.episode != current.episode:
            return -EPISODE_SWITCH_PENALTY
        source_delta = current.t_source - previous.t_source
        if crosses_boundary:
            if source_delta < 0:
                return -BACKWARD_JUMP_PENALTY
            return 0.0
        speed = source_delta / dt
        if not (MIN_EVIDENCE_SPEED <= speed <= MAX_EVIDENCE_SPEED):
            return -BACKWARD_JUMP_PENALTY
        return EPISODE_SWITCH_PENALTY - min(
            EPISODE_SWITCH_PENALTY,
            SceneAlignerService._speed_prior_penalty(speed),
        )

    @classmethod
    def sample_query_video(cls, video_path: Path) -> list[QuerySample]:
        cv2 = AnimeMatcherService._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        try:
            native_fps = cap.get(cv2.CAP_PROP_FPS)
            if not native_fps or native_fps <= 0:
                native_fps = 30.0
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            duration = frame_count / native_fps if frame_count and frame_count > 0 else None
            if duration is None:
                return []

            sample_times = np.arange(0.0, max(0.0, duration), 1.0 / DENSE_SAMPLE_FPS)
            targets = {
                max(0, int(round(float(t) * native_fps))): float(t)
                for t in sample_times
            }
            if not targets:
                return []

            images: list[Image.Image] = []
            times: list[float] = []
            samples: list[QuerySample] = []

            def flush() -> None:
                if not images:
                    return
                embeddings = AnimeMatcherService._embed_pil_batch(
                    [image.convert("RGB") for image in images]
                )
                for sample_time, embedding in zip(times, embeddings, strict=False):
                    samples.append(QuerySample(sample_time, embedding, "plain"))
                images.clear()
                times.clear()

            next_target_iter = iter(sorted(targets.items()))
            try:
                target_frame, target_time = next(next_target_iter)
            except StopIteration:
                return []

            frame_index = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                while frame_index >= target_frame:
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    images.append(Image.fromarray(frame_rgb))
                    times.append(target_time)
                    if len(images) >= 96:
                        flush()
                    try:
                        target_frame, target_time = next(next_target_iter)
                    except StopIteration:
                        flush()
                        return samples
                frame_index += 1
            flush()
            return samples
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
        for index, image in enumerate(images):
            rgb = image.convert("RGB")
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

    @classmethod
    def decode_scene_sequence(
        cls,
        scenes: SceneList,
        scene_segments: dict[int, list[SegmentHypothesis]],
    ) -> list[SegmentHypothesis | None]:
        states: list[list[_DecodedState]] = []
        for index in range(len(scenes.scenes)):
            segments = scene_segments.get(index, [])
            scene_states = [
                _DecodedState(segment, cls._emission_score(segment))
                for segment in segments[:MAX_SEGMENTS_PER_SCENE]
            ]
            scene_states.append(_DecodedState(None, -NO_MATCH_PENALTY))
            states.append(scene_states)
        if not states:
            return []

        scores: list[list[float]] = [[state.emission for state in states[0]]]
        back: list[list[int]] = [[-1 for _ in states[0]]]
        for scene_index in range(1, len(states)):
            prev_scores = scores[-1]
            row: list[float] = []
            row_back: list[int] = []
            for state in states[scene_index]:
                best_score = -math.inf
                best_index = 0
                for prev_index, prev_state in enumerate(states[scene_index - 1]):
                    score = (
                        prev_scores[prev_index]
                        + state.emission
                        + cls._transition_score(
                            scenes.scenes[scene_index - 1],
                            scenes.scenes[scene_index],
                            prev_state.segment,
                            state.segment,
                        )
                    )
                    if score > best_score:
                        best_score = score
                        best_index = prev_index
                row.append(best_score)
                row_back.append(best_index)
            scores.append(row)
            back.append(row_back)

        last_index = int(np.argmax(scores[-1]))
        decoded: list[SegmentHypothesis | None] = [None] * len(states)
        for scene_index in range(len(states) - 1, -1, -1):
            decoded[scene_index] = states[scene_index][last_index].segment
            last_index = back[scene_index][last_index]
            if last_index < 0 and scene_index > 0:
                last_index = 0
        return decoded

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
    def _transition_score(
        cls,
        left_scene: Scene,
        right_scene: Scene,
        left: SegmentHypothesis | None,
        right: SegmentHypothesis | None,
    ) -> float:
        if left is None or right is None:
            return 0.0
        score = 0.0
        if left.episode != right.episode:
            score -= EPISODE_SWITCH_PENALTY
            return score
        gap = right.source_at(right_scene.start_time) - left.source_at(left_scene.end_time)
        if gap < 0:
            score -= BACKWARD_JUMP_PENALTY
        return score

    @classmethod
    def _remap_decoded_without_merge(
        cls,
        scenes: SceneList,
        decoded: list[SegmentHypothesis | None],
    ) -> tuple[SceneList, list[tuple[list[int], SegmentHypothesis | None]]]:
        copied = SceneList(
            scenes=[
                Scene(index=index, start_time=scene.start_time, end_time=scene.end_time)
                for index, scene in enumerate(scenes.scenes)
            ]
        )
        return copied, [
            ([index], decoded[index] if index < len(decoded) else None)
            for index in range(len(copied.scenes))
        ]

    @classmethod
    def _merge_decoded_continuities(
        cls,
        scenes: SceneList,
        decoded: list[SegmentHypothesis | None],
    ) -> tuple[SceneList, list[tuple[list[int], SegmentHypothesis | None]]]:
        merged_scenes: list[Scene] = []
        remapped: list[tuple[list[int], SegmentHypothesis | None]] = []
        index = 0
        while index < len(scenes.scenes):
            merged_indices = [index]
            current_segment = decoded[index] if index < len(decoded) else None
            start = scenes.scenes[index].start_time
            end = scenes.scenes[index].end_time
            cursor = index
            while cursor + 1 < len(scenes.scenes):
                next_segment = decoded[cursor + 1] if cursor + 1 < len(decoded) else None
                if not cls._should_merge_pair(
                    scenes.scenes[cursor],
                    scenes.scenes[cursor + 1],
                    current_segment,
                    next_segment,
                ):
                    break
                cursor += 1
                merged_indices.append(cursor)
                end = scenes.scenes[cursor].end_time
            merged_scenes.append(
                Scene(index=len(merged_scenes), start_time=start, end_time=end)
            )
            remapped.append((merged_indices, current_segment))
            index = cursor + 1
        return SceneList(scenes=merged_scenes), remapped

    @classmethod
    def _should_merge_pair(
        cls,
        left_scene: Scene,
        right_scene: Scene,
        left: SegmentHypothesis | None,
        right: SegmentHypothesis | None,
    ) -> bool:
        if left is None or right is None or left.episode != right.episode:
            return False
        index_step = 1.0 / max(AnimeMatcherService.get_index_fps(), 1e-3)
        gap = right.source_at(right_scene.start_time) - left.source_at(left_scene.end_time)
        speed_delta = abs(left.a - right.a)
        return abs(gap) <= (0.5 * index_step) and speed_delta <= 0.20

    @classmethod
    def _build_matches(
        cls,
        video_path: Path,
        final_scenes: SceneList,
        remapped: list[tuple[list[int], SegmentHypothesis | None]],
        scene_segments: dict[int, list[SegmentHypothesis]],
        correspondences: list[Correspondence],
        library_type: LibraryType | str,
    ) -> MatchList:
        matches = MatchList()
        query_boundaries = cls._query_boundary_embeddings(video_path, final_scenes.scenes)
        for final_index, (source_scene_indices, segment) in enumerate(remapped):
            scene = final_scenes.scenes[final_index]
            alternatives = cls._alternatives_for_scene(scene, source_scene_indices, scene_segments)
            start_candidates, middle_candidates, end_candidates = cls._edge_candidates(
                scene,
                correspondences,
            )
            if segment is None:
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

            start_time, end_time = segment.source_interval(scene)
            refined = cls._refine_boundaries_with_query_embeddings(
                scene,
                segment.episode,
                start_time,
                end_time,
                library_type,
                query_boundaries[final_index],
            )
            if refined is not None:
                refined_start, refined_end = refined
                source_duration = end_time - start_time
                refined_duration = refined_end - refined_start
                index_step = 1.0 / max(AnimeMatcherService.get_index_fps(), 1e-3)
                if (
                    refined_end > refined_start
                    and abs(refined_start - start_time) <= BOUNDARY_REFINE_MAX_SHIFT_SECONDS
                    and abs(refined_end - end_time) <= BOUNDARY_REFINE_MAX_SHIFT_SECONDS
                    and abs(refined_duration - source_duration) <= index_step
                ):
                    start_time, end_time = refined_start, refined_end

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
                    merged_from=source_scene_indices if len(source_scene_indices) > 1 else None,
                    alternatives=alternatives,
                    start_candidates=start_candidates,
                    middle_candidates=middle_candidates,
                    end_candidates=end_candidates,
                )
            )
        return matches

    @classmethod
    def _query_boundary_embeddings(
        cls,
        video_path: Path,
        scenes: list[Scene],
    ) -> list[tuple[np.ndarray, np.ndarray, float] | None]:
        timestamps: list[float] = []
        for scene in scenes:
            scene_duration = scene.end_time - scene.start_time
            tiny_offset = min(0.05, scene_duration / 10.0)
            start_time = scene.start_time + tiny_offset
            end_time = max(start_time + 1e-3, scene.end_time - tiny_offset)
            timestamps.extend([start_time, end_time])

        frames = AnimeMatcherService.extract_frames(video_path, timestamps)
        valid_images = [frame.convert("RGB") for frame in frames if frame is not None]
        embeddings = AnimeMatcherService._embed_pil_batch(valid_images)
        embedding_iter = iter(embeddings)

        result: list[tuple[np.ndarray, np.ndarray, float] | None] = []
        for index in range(0, len(frames), 2):
            start_frame = frames[index]
            end_frame = frames[index + 1]
            if start_frame is None or end_frame is None:
                if start_frame is not None:
                    next(embedding_iter, None)
                if end_frame is not None:
                    next(embedding_iter, None)
                result.append(None)
                continue
            start_embedding = next(embedding_iter)
            end_embedding = next(embedding_iter)
            target_aspect = start_frame.width / max(1, start_frame.height)
            result.append((start_embedding, end_embedding, target_aspect))
        return result

    @classmethod
    def _refine_boundaries_with_query_embeddings(
        cls,
        scene: Scene,
        matched_episode: str,
        matched_start_ts: float,
        matched_end_ts: float,
        library_type: LibraryType | str,
        query_boundary: tuple[np.ndarray, np.ndarray, float] | None,
    ) -> tuple[float, float] | None:
        started_at = time.perf_counter()
        try:
            AnimeMatcherService._record_runtime_stat("boundary_refine_calls")
            if query_boundary is None or AnimeMatcherService._embedder is None:
                return None

            from .anime_library import AnimeLibraryService

            episode_path = AnimeLibraryService.resolve_episode_path(
                matched_episode,
                library_type=library_type,
            )
            if episode_path is None or not episode_path.exists():
                return None

            scene_duration = scene.end_time - scene.start_time
            if scene_duration <= 0:
                return None

            index_step = 1.0 / max(AnimeMatcherService.get_index_fps(), 1e-3)
            window = max(0.5, index_step + 0.15)

            cv2 = AnimeMatcherService._require_cv2()
            cap = cv2.VideoCapture(str(episode_path))
            try:
                start_frames = AnimeMatcherService._collect_frames_in_window_from_capture(
                    cap,
                    matched_start_ts - window,
                    matched_start_ts + window,
                    sample_frames=ALIGNER_REFINE_FRAMES_PER_BOUNDARY,
                )
                end_frames = AnimeMatcherService._collect_frames_in_window_from_capture(
                    cap,
                    matched_end_ts - window,
                    matched_end_ts + window,
                    sample_frames=ALIGNER_REFINE_FRAMES_PER_BOUNDARY,
                )
            finally:
                cap.release()
            if not start_frames or not end_frames:
                return None

            q_start, q_end, target_aspect = query_boundary
            refined_start = AnimeMatcherService._best_boundary_timestamp(
                query_embedding=q_start,
                source_frames=start_frames,
                target_aspect=target_aspect,
            )
            refined_end = AnimeMatcherService._best_boundary_timestamp(
                query_embedding=q_end,
                source_frames=end_frames,
                target_aspect=target_aspect,
            )
            if refined_end - refined_start <= 0.1:
                return None

            AnimeMatcherService._record_runtime_stat("boundary_refine_successes")
            return refined_start, refined_end
        finally:
            AnimeMatcherService._record_runtime_stat(
                "boundary_refine_seconds",
                time.perf_counter() - started_at,
            )

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
