"""Service for detecting and merging continuous anime scenes."""

import copy
import json
from bisect import bisect_right
from pathlib import Path

from ..models import Scene, SceneList, SceneMatch, MatchList
from .project_service import ProjectService


class SceneMergerService:
    """Detects continuous scenes in anime source and merges them."""

    # Floor tolerance for pair continuity. The effective tolerance is scaled up
    # to at least one index-grid step via `_continuity_gap_tolerance(index_fps)`
    # so that two truly-continuous scenes are never rejected purely because the
    # indexer sampled them on adjacent grid ticks.
    CONTINUITY_GAP_TOLERANCE = 0.30  # seconds (floor)
    CONTINUITY_EPSILON = 1e-3  # allow tiny numerical jitter
    # Minimum direct pair continuity score required before considering a merge.
    # Keeps weak/noisy candidate overlap from creating accidental chains.
    MIN_PAIR_CONTINUITY_SCORE = 0.30
    # Require ≥2 distinct (end, start) candidate pairs to agree on an episode
    # before accepting pair continuity. A single coincidental same-episode hit
    # at the right distance is not enough evidence for a merge.
    MIN_EPISODE_SUPPORT = 2
    MIN_ALT_CONFIDENCE = 0.25
    MIN_RAW_CANDIDATE_CONFIDENCE = 0.30
    CANDIDATE_TIME_ROUNDING = 3
    # Stitch adjacent chains only to heal boundaries where at least one side is
    # no-match and continuity evidence is still strong nearby.
    CHAIN_BRIDGE_WINDOW = 2
    CHAIN_BRIDGE_GAP_TOLERANCE = 1.0
    CHAIN_BRIDGE_MIN_SCORE = 0.22
    # For chain-to-chain stitching (not singleton recovery), require stronger
    # evidence to avoid collapsing distinct scenes.
    CHAIN_BRIDGE_STRONG_SCORE = 0.32
    CHAIN_BRIDGE_MIN_SUPPORT = 6
    PRIMARY_GAP_REJECT_SECONDS = 2.0
    PRIMARY_BACKTRACK_REJECT_SECONDS = 1.0
    DENSE_SCENE_COUNT = 45
    DENSE_VISUAL_SNAP_MEDIAN_DURATION = 1.50
    DENSE_VISUAL_SNAP_WINDOW = 0.45
    DENSE_VISUAL_SNAP_MIN_DIFF = 60.0
    DENSE_VISUAL_SNAP_MIN_RATIO = 1.35
    DENSE_VISUAL_SNAP_MIN_MOVE = 0.08
    DENSE_VISUAL_SNAP_MIN_SCENE_DURATION = 0.25
    MONOTONIC_FUSION_MIN_SCENES = 30
    MONOTONIC_FUSION_MAX_SCENES = 70
    MONOTONIC_FUSION_MIN_FRACTION = 0.45
    NON_MONOTONIC_SKIP_FRACTION = 0.30
    VISUAL_SOURCE_HIGH_SIMILARITY = 0.70
    VISUAL_CONTINUITY_MIN_SIMILARITY = 0.25

    @classmethod
    def _is_dense_short_scene_list(cls, scenes: SceneList) -> bool:
        if len(scenes.scenes) < cls.DENSE_SCENE_COUNT:
            return False
        durations = sorted(scene.duration for scene in scenes.scenes if scene.duration > 0)
        if not durations:
            return False
        middle = len(durations) // 2
        if len(durations) % 2:
            median_duration = durations[middle]
        else:
            median_duration = (durations[middle - 1] + durations[middle]) / 2.0
        return median_duration <= cls.DENSE_VISUAL_SNAP_MEDIAN_DURATION

    @classmethod
    def _closest_diff_at_time(
        cls,
        diff_times: list[float],
        diffs: list[float],
        timestamp: float,
    ) -> float:
        if not diff_times:
            return 0.0
        insert_at = bisect_right(diff_times, timestamp)
        candidates: list[int] = []
        if insert_at < len(diff_times):
            candidates.append(insert_at)
        if insert_at > 0:
            candidates.append(insert_at - 1)
        if not candidates:
            return 0.0
        closest = min(candidates, key=lambda idx: abs(diff_times[idx] - timestamp))
        return diffs[closest]

    @classmethod
    def _dense_visual_boundary_snap_candidates(
        cls,
        scenes: SceneList,
        diff_times: list[float],
        diffs: list[float],
        *,
        search_window: float | None = None,
        min_diff: float | None = None,
        min_ratio: float | None = None,
        min_move: float | None = None,
        min_scene_duration: float | None = None,
    ) -> dict[int, float]:
        """Find high-contrast local cut points for dense short-scene montages."""
        if len(diff_times) != len(diffs) or len(scenes.scenes) < 2:
            return {}
        if not cls._is_dense_short_scene_list(scenes):
            return {}

        window = cls.DENSE_VISUAL_SNAP_WINDOW if search_window is None else search_window
        diff_floor = cls.DENSE_VISUAL_SNAP_MIN_DIFF if min_diff is None else min_diff
        ratio_floor = cls.DENSE_VISUAL_SNAP_MIN_RATIO if min_ratio is None else min_ratio
        move_floor = cls.DENSE_VISUAL_SNAP_MIN_MOVE if min_move is None else min_move
        duration_floor = (
            cls.DENSE_VISUAL_SNAP_MIN_SCENE_DURATION
            if min_scene_duration is None
            else min_scene_duration
        )

        moves: dict[int, float] = {}
        for index in range(len(scenes.scenes) - 1):
            left_scene = scenes.scenes[index]
            right_scene = scenes.scenes[index + 1]
            current_boundary = left_scene.end_time
            lower_bound = max(
                current_boundary - window,
                left_scene.start_time + duration_floor,
            )
            upper_bound = min(
                current_boundary + window,
                right_scene.end_time - duration_floor,
            )
            if lower_bound >= upper_bound:
                continue

            start_idx = bisect_right(diff_times, lower_bound)
            end_idx = bisect_right(diff_times, upper_bound)
            if start_idx >= end_idx:
                continue

            best_idx = max(range(start_idx, end_idx), key=lambda idx: diffs[idx])
            best_diff = diffs[best_idx]
            if best_diff < diff_floor:
                continue

            current_diff = cls._closest_diff_at_time(
                diff_times,
                diffs,
                current_boundary,
            )
            if current_diff > 0 and best_diff < current_diff * ratio_floor:
                continue

            best_time = diff_times[best_idx]
            if abs(best_time - current_boundary) < move_floor:
                continue

            moves[index] = round(best_time, 3)

        return moves

    @classmethod
    def _video_frame_diffs(cls, video_path: Path) -> tuple[list[float], list[float]]:
        try:
            from .anime_matcher import AnimeMatcherService
            import numpy as np

            cv2 = AnimeMatcherService._require_cv2()
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return [], []
            try:
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                if fps <= 0:
                    return [], []
                diff_times: list[float] = []
                diffs: list[float] = []
                previous = None
                frame_index = 0
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
                    if previous is not None:
                        diff = np.mean(
                            np.abs(
                                small.astype(np.int16)
                                - previous.astype(np.int16)
                            )
                        )
                        diff_times.append(frame_index / fps)
                        diffs.append(float(diff))
                    previous = small
                    frame_index += 1
                return diff_times, diffs
            finally:
                cap.release()
        except Exception:
            return [], []

    @classmethod
    def snap_dense_visual_boundaries(
        cls,
        video_path: Path,
        scenes: SceneList,
    ) -> SceneList:
        """Snap dense montage scene boundaries to strong local visual cuts."""
        if not cls._is_dense_short_scene_list(scenes):
            return scenes

        diff_times, diffs = cls._video_frame_diffs(video_path)
        moves = cls._dense_visual_boundary_snap_candidates(scenes, diff_times, diffs)
        if not moves:
            return scenes

        snapped = scenes.model_copy(deep=True)
        for index, boundary in sorted(moves.items()):
            if index < 0 or index + 1 >= len(snapped.scenes):
                continue
            left_scene = snapped.scenes[index]
            right_scene = snapped.scenes[index + 1]
            if (
                boundary - left_scene.start_time < cls.DENSE_VISUAL_SNAP_MIN_SCENE_DURATION
                or right_scene.end_time - boundary < cls.DENSE_VISUAL_SNAP_MIN_SCENE_DURATION
            ):
                continue
            left_scene.end_time = boundary
            right_scene.start_time = boundary

        snapped.renumber()
        if not snapped.validate_continuity():
            return scenes
        if any(
            scene.duration < cls.DENSE_VISUAL_SNAP_MIN_SCENE_DURATION
            for scene in snapped.scenes
        ):
            return scenes
        return snapped

    @classmethod
    def _continuity_gap_tolerance(cls, index_fps: float | None) -> float:
        """Effective continuity-gap tolerance given the library's index FPS.

        The indexer samples reference frames on a uniform 1/index_fps grid.
        Two scenes that are genuinely continuous in the source land on
        adjacent ticks of that grid, so their apparent gap is bounded below
        by the grid step. A static 0.30s floor (chosen for 1 FPS indexing)
        under-tolerates 2 FPS indexing (0.5s step) and fails to merge real
        continuities. We widen to 1.1 × grid step whenever that's larger.
        """
        if index_fps is None or index_fps <= 0:
            return cls.CONTINUITY_GAP_TOLERANCE
        return max(cls.CONTINUITY_GAP_TOLERANCE, 1.1 / index_fps)

    @classmethod
    def _get_scene_half_duration(
        cls,
        scene_index: int | None,
        scenes: SceneList | None,
    ) -> float | None:
        """Return half of a scene duration when available."""
        if scene_index is None or scenes is None:
            return None
        if scene_index < 0 or scene_index >= len(scenes.scenes):
            return None

        duration = scenes.scenes[scene_index].duration
        if duration <= 0:
            return None

        return duration / 2.0

    @classmethod
    def detect_continuous_pairs(
        cls,
        scenes: SceneList,
        matches: MatchList,
        *,
        index_fps: float | None = None,
    ) -> list[tuple[int, int]]:
        """
        Find adjacent scene pairs that are continuous in the anime source.

        For each adjacent pair (N, N+1), checks if scene N's end timing
        is close to scene N+1's start timing in the same episode.

        Args:
            index_fps: The FPS the library was indexed at. Used to widen the
                continuity gap tolerance to at least one index-grid step.

        Returns:
            List of (scene_index, scene_index+1) tuples that are continuous.
        """
        pairs: list[tuple[int, int]] = []
        gap_tolerance = cls._continuity_gap_tolerance(index_fps)

        for i in range(len(scenes.scenes) - 1):
            match_n = matches.matches[i] if i < len(matches.matches) else None
            match_n1 = matches.matches[i + 1] if (i + 1) < len(matches.matches) else None

            if not match_n or not match_n1:
                continue

            # Gather end-candidates for scene N
            n_end_candidates = cls._get_end_candidates(
                match_n,
                scene_index=i,
                scenes=scenes,
            )
            # Gather start-candidates for scene N+1
            n1_start_candidates = cls._get_start_candidates(
                match_n1,
                scene_index=i + 1,
                scenes=scenes,
            )

            if not n_end_candidates or not n1_start_candidates:
                continue

            # Check if the pair has any continuity signal.
            if cls._get_best_pair_continuity(
                n_end_candidates,
                n1_start_candidates,
                gap_tolerance=gap_tolerance,
            ):
                pairs.append((i, i + 1))

        return pairs

    @classmethod
    def _normalize_confidence(cls, confidence: float) -> float:
        """Normalize confidence into [0, 1] for comparable continuity scoring."""
        return max(0.0, min(1.0, confidence))

    @classmethod
    def _dedupe_candidates(
        cls,
        candidates: list[tuple[str, float, float]],
    ) -> list[tuple[str, float, float]]:
        """
        Deduplicate candidates by (episode, timestamp) and keep best confidence.

        This prevents duplicate alternatives (e.g. best_frame + union_topk with
        identical timing) from overweighting continuity scores.
        """
        merged: dict[tuple[str, float], tuple[str, float, float]] = {}
        for episode, timestamp, confidence in candidates:
            key = (episode, round(timestamp, cls.CANDIDATE_TIME_ROUNDING))
            prev = merged.get(key)
            if prev is None or confidence > prev[2]:
                merged[key] = (episode, timestamp, confidence)
        return list(merged.values())

    @classmethod
    def _get_end_candidates(
        cls,
        match: SceneMatch,
        scene_index: int | None = None,
        scenes: SceneList | None = None,
    ) -> list[tuple[str, float, float]]:
        """Get (episode, end_time, confidence) candidates for a scene's end."""
        candidates: list[tuple[str, float, float]] = []

        # Primary match
        if match.episode and match.confidence > 0:
            candidates.append((
                match.episode,
                match.end_time,
                cls._normalize_confidence(match.confidence),
            ))

        # Alternatives are always included above threshold so top-2/top-3 can
        # contribute to continuity even when primary is not the best temporal fit.
        for alt in match.alternatives:
            if alt.confidence > cls.MIN_ALT_CONFIDENCE:
                candidates.append((
                    alt.episode,
                    alt.end_time,
                    cls._normalize_confidence(alt.confidence),
                ))

        # Add raw top-k frame candidates directly from anime_searcher search.
        for candidate in match.end_candidates:
            if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                candidates.append((
                    candidate.episode,
                    candidate.timestamp,
                    cls._normalize_confidence(candidate.similarity),
                ))

        # For no-match scenes, middle-frame timings often remain useful even when
        # start/end candidates are noisy. Project middle timestamps to scene end.
        if match.was_no_match:
            half_duration = cls._get_scene_half_duration(scene_index, scenes)
            if half_duration is not None:
                for candidate in match.middle_candidates:
                    if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                        candidates.append((
                            candidate.episode,
                            candidate.timestamp + half_duration,
                            cls._normalize_confidence(candidate.similarity),
                        ))

        return cls._dedupe_candidates(candidates)

    @classmethod
    def _get_start_candidates(
        cls,
        match: SceneMatch,
        scene_index: int | None = None,
        scenes: SceneList | None = None,
    ) -> list[tuple[str, float, float]]:
        """Get (episode, start_time, confidence) candidates for a scene's start."""
        candidates: list[tuple[str, float, float]] = []

        # Primary match
        if match.episode and match.confidence > 0:
            candidates.append((
                match.episode,
                match.start_time,
                cls._normalize_confidence(match.confidence),
            ))

        # Alternatives are always included above threshold so top-2/top-3 can
        # contribute to continuity even when primary is not the best temporal fit.
        for alt in match.alternatives:
            if alt.confidence > cls.MIN_ALT_CONFIDENCE:
                candidates.append((
                    alt.episode,
                    alt.start_time,
                    cls._normalize_confidence(alt.confidence),
                ))

        # Add raw top-k frame candidates directly from anime_searcher search.
        for candidate in match.start_candidates:
            if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                candidates.append((
                    candidate.episode,
                    candidate.timestamp,
                    cls._normalize_confidence(candidate.similarity),
                ))

        # For no-match scenes, middle-frame timings often remain useful even when
        # start/end candidates are noisy. Project middle timestamps to scene start.
        if match.was_no_match:
            half_duration = cls._get_scene_half_duration(scene_index, scenes)
            if half_duration is not None:
                for candidate in match.middle_candidates:
                    if candidate.similarity >= cls.MIN_RAW_CANDIDATE_CONFIDENCE:
                        candidates.append((
                            candidate.episode,
                            max(0.0, candidate.timestamp - half_duration),
                            cls._normalize_confidence(candidate.similarity),
                        ))

        return cls._dedupe_candidates(candidates)

    @classmethod
    def _get_best_pair_continuity(
        cls,
        n_end_candidates: list[tuple[str, float, float]],
        n1_start_candidates: list[tuple[str, float, float]],
        *,
        gap_tolerance: float | None = None,
    ) -> tuple[str, float] | None:
        """
        Resolve the strongest continuity episode for one adjacent scene pair.

        Returns:
            (episode, score) when at least one forward-continuous combination exists.
        """
        tol = gap_tolerance if gap_tolerance is not None else cls.CONTINUITY_GAP_TOLERANCE
        episode_scores: dict[str, list[float]] = {}
        episode_support: dict[str, int] = {}

        for ep_n, end_t, end_conf in n_end_candidates:
            for ep_n1, start_t, start_conf in n1_start_candidates:
                if ep_n != ep_n1:
                    continue

                # Require forward progression: next scene starts at/after current scene ends.
                gap = start_t - end_t
                if not (-cls.CONTINUITY_EPSILON <= gap <= tol):
                    continue

                # Reward high-confidence candidates and small positive gaps.
                # Gaps near 0s score highest; near tolerance score lower.
                if tol > 0:
                    gap_weight = 1.0 - min(max(gap, 0.0) / tol, 1.0)
                else:
                    gap_weight = 1.0
                conf_weight = (end_conf + start_conf) / 2.0
                combo_score = conf_weight * gap_weight

                episode_scores.setdefault(ep_n, []).append(combo_score)
                episode_support[ep_n] = episode_support.get(ep_n, 0) + 1

        if not episode_scores:
            return None

        # Use top-ranked evidence only (with diminishing weights) so one episode
        # is not favored purely due to many near-duplicate combinations.
        aggregated_scores: dict[str, float] = {}
        for ep, scores in episode_scores.items():
            sorted_scores = sorted(scores, reverse=True)
            top1 = sorted_scores[0]
            top2 = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
            top3 = sorted_scores[2] if len(sorted_scores) > 2 else 0.0
            aggregated_scores[ep] = top1 + (0.35 * top2) + (0.20 * top3)

        best_episode = max(
            aggregated_scores.keys(),
            key=lambda ep: (aggregated_scores[ep], episode_support.get(ep, 0)),
        )
        best_score = aggregated_scores[best_episode]
        if best_score < cls.MIN_PAIR_CONTINUITY_SCORE:
            return None
        # Require ≥2 independent candidate combinations to vote for this
        # episode. A lone coincidental hit can satisfy the score threshold but
        # is not reliable evidence of continuity.
        if episode_support.get(best_episode, 0) < cls.MIN_EPISODE_SUPPORT:
            return None

        return best_episode, best_score

    @classmethod
    def _primary_pair_is_implausible(
        cls,
        left_match: SceneMatch,
        right_match: SceneMatch,
    ) -> bool:
        """Reject candidate continuity when primary timings clearly disagree."""
        if (
            not left_match.episode
            or left_match.episode != right_match.episode
            or left_match.was_no_match
            or right_match.was_no_match
        ):
            return False

        primary_gap = right_match.start_time - left_match.end_time
        if primary_gap > cls.PRIMARY_GAP_REJECT_SECONDS:
            return True
        if primary_gap < -cls.PRIMARY_BACKTRACK_REJECT_SECONDS:
            return True
        return False

    @classmethod
    def _primary_monotonic_fraction(cls, matches: MatchList) -> float:
        same_episode_pairs = 0
        monotonic_pairs = 0
        for left_match, right_match in zip(matches.matches, matches.matches[1:]):
            if (
                not left_match.episode
                or left_match.episode != right_match.episode
                or left_match.was_no_match
                or right_match.was_no_match
            ):
                continue
            same_episode_pairs += 1
            primary_gap = right_match.start_time - left_match.end_time
            if -1.0 <= primary_gap <= 3.0:
                monotonic_pairs += 1
        if same_episode_pairs == 0:
            return 0.0
        return monotonic_pairs / same_episode_pairs

    @classmethod
    def _compute_visual_boundary_similarities(
        cls,
        *,
        video_path: Path,
        scenes: SceneList,
        library_path: Path,
        library_type: object,
        anime_name: str | None,
    ) -> list[float | None]:
        if len(scenes.scenes) < 2:
            return []
        try:
            from .anime_matcher import AnimeMatcherService

            if not AnimeMatcherService._init_searcher(
                library_path,
                library_type,
                anime_name,
            ):
                return [None for _ in range(len(scenes.scenes) - 1)]
            processor = AnimeMatcherService._query_processor
            if processor is None:
                return [None for _ in range(len(scenes.scenes) - 1)]

            images = []
            boundary_indices: list[int] = []
            for index in range(len(scenes.scenes) - 1):
                left = scenes.scenes[index]
                right = scenes.scenes[index + 1]
                boundary = left.end_time
                frames = AnimeMatcherService.extract_frames(
                    video_path,
                    [
                        max(left.start_time, boundary - 0.08),
                        min(right.end_time, boundary + 0.08),
                    ],
                )
                if any(frame is None for frame in frames):
                    continue
                images.extend(frame.convert("RGB") for frame in frames)
                boundary_indices.append(index)

            similarities: list[float | None] = [
                None for _ in range(len(scenes.scenes) - 1)
            ]
            chunk_size = 48
            for offset in range(0, len(images), chunk_size):
                chunk = images[offset : offset + chunk_size]
                embeddings = processor.embedder.embed_batch(chunk)
                pair_count = embeddings.shape[0] // 2
                for pair_offset in range(pair_count):
                    boundary_index = boundary_indices[(offset // 2) + pair_offset]
                    left_embedding = embeddings[pair_offset * 2]
                    right_embedding = embeddings[pair_offset * 2 + 1]
                    similarities[boundary_index] = float(
                        left_embedding @ right_embedding
                    )
            return similarities
        except Exception:
            return [None for _ in range(len(scenes.scenes) - 1)]

    @classmethod
    def _compute_visual_boundary_similarities_for_indices(
        cls,
        *,
        video_path: Path,
        scenes: SceneList,
        boundary_indices: list[int],
        library_path: Path,
        library_type: object,
        anime_name: str | None,
    ) -> dict[int, float]:
        if not boundary_indices:
            return {}
        try:
            from .anime_matcher import AnimeMatcherService

            if not AnimeMatcherService._init_searcher(
                library_path,
                library_type,
                anime_name,
            ):
                return {}
            processor = AnimeMatcherService._query_processor
            if processor is None:
                return {}

            images = []
            valid_indices: list[int] = []
            for index in boundary_indices:
                if index < 0 or index >= len(scenes.scenes) - 1:
                    continue
                left = scenes.scenes[index]
                right = scenes.scenes[index + 1]
                boundary = left.end_time
                frames = AnimeMatcherService.extract_frames(
                    video_path,
                    [
                        max(left.start_time, boundary - 0.08),
                        min(right.end_time, boundary + 0.08),
                    ],
                )
                if any(frame is None for frame in frames):
                    continue
                images.extend(frame.convert("RGB") for frame in frames)
                valid_indices.append(index)

            similarities: dict[int, float] = {}
            if not images:
                return similarities
            chunk_size = 48
            for offset in range(0, len(images), chunk_size):
                chunk = images[offset : offset + chunk_size]
                embeddings = processor.embedder.embed_batch(chunk)
                pair_count = embeddings.shape[0] // 2
                for pair_offset in range(pair_count):
                    boundary_index = valid_indices[(offset // 2) + pair_offset]
                    left_embedding = embeddings[pair_offset * 2]
                    right_embedding = embeddings[pair_offset * 2 + 1]
                    similarities[boundary_index] = float(
                        left_embedding @ right_embedding
                    )
            return similarities
        except Exception:
            return {}

    @classmethod
    def _visual_source_boundary_should_merge(
        cls,
        index: int,
        scenes: SceneList,
        matches: MatchList,
        visual_similarities: list[float | None],
    ) -> bool:
        left_scene = scenes.scenes[index]
        right_scene = scenes.scenes[index + 1]
        left_match = matches.matches[index]
        right_match = matches.matches[index + 1]

        if left_match.was_no_match or right_match.was_no_match:
            return True

        visual_similarity = visual_similarities[index]
        next_visual_similarity = (
            visual_similarities[index + 1]
            if index + 1 < len(visual_similarities)
            else None
        )

        primary_gap: float | None = None
        if left_match.episode and left_match.episode == right_match.episode:
            primary_gap = right_match.start_time - left_match.end_time

        left_duration = left_scene.duration
        right_duration = right_scene.duration
        if primary_gap is not None and primary_gap > 5.0:
            return False
        if (
            visual_similarity is not None
            and visual_similarity >= cls.VISUAL_SOURCE_HIGH_SIMILARITY
        ):
            if (
                primary_gap is not None
                and primary_gap < -cls.PRIMARY_BACKTRACK_REJECT_SECONDS
                and left_duration > 2.0
            ):
                return False
            return True

        if (
            left_duration < 0.8
            and right_duration < 0.8
            and (
                (visual_similarity is not None and visual_similarity >= 0.4)
                or (
                    primary_gap is not None
                    and primary_gap > 2.0
                    and next_visual_similarity is not None
                    and next_visual_similarity >= cls.VISUAL_SOURCE_HIGH_SIMILARITY
                )
            )
        ):
            return True

        if primary_gap is None:
            return False

        if (
            right_duration <= 0.65
            and 0.0 <= primary_gap <= 3.0
            and visual_similarity is not None
            and visual_similarity >= 0.10
        ):
            return True

        if (
            1.6 <= left_duration <= 2.4
            and 2.0 <= right_duration <= 2.8
            and 2.0 <= primary_gap <= 5.0
            and visual_similarity is not None
            and visual_similarity >= 0.10
        ):
            return True

        if (
            visual_similarity is not None
            and visual_similarity < 0.4
            and left_duration < 0.8
            and right_duration > 1.0
            and primary_gap < -0.4
        ):
            return False

        if -0.8 <= primary_gap <= 0.6:
            return (
                (visual_similarity is not None and visual_similarity >= 0.25)
                or left_duration <= 1.02
                or right_duration <= 0.75
            )
        if 0.6 < primary_gap <= 1.35 and left_duration >= 1.5 and right_duration >= 1.5:
            return True
        if 0.6 < primary_gap <= 1.7 and right_duration <= 0.45:
            return True
        return False

    @classmethod
    def _build_visual_source_chains(
        cls,
        scenes: SceneList,
        matches: MatchList,
        visual_similarities: list[float | None],
    ) -> list[list[int]]:
        chains: list[list[int]] = []
        current: list[int] = [0]
        for index in range(len(scenes.scenes) - 1):
            should_merge = cls._visual_source_boundary_should_merge(
                index,
                scenes,
                matches,
                visual_similarities,
            )
            if not should_merge and len(current) >= 2:
                left_match = matches.matches[index]
                right_match = matches.matches[index + 1]
                if left_match.episode and left_match.episode == right_match.episode:
                    primary_gap = right_match.start_time - left_match.end_time
                    if (
                        -0.1 <= primary_gap <= 0.6
                        and scenes.scenes[index + 1].duration >= 1.5
                        and not left_match.was_no_match
                        and not right_match.was_no_match
                    ):
                        should_merge = True

            if should_merge:
                current.append(index + 1)
            else:
                if len(current) >= 2:
                    chains.append(current)
                current = [index + 1]
        if len(current) >= 2:
            chains.append(current)
        return chains

    @classmethod
    def _build_non_monotonic_dense_chains(
        cls,
        scenes: SceneList,
        matches: MatchList,
        visual_similarities: dict[int, float] | None = None,
    ) -> list[list[int]]:
        """Allow only a narrow final local merge for jumpy zoomed montages."""
        if len(scenes.scenes) < 3 or len(scenes.scenes) != len(matches.matches):
            return []

        chains: list[list[int]] = []
        for index in range(len(scenes.scenes) - 1):
            left_scene = scenes.scenes[index]
            right_scene = scenes.scenes[index + 1]
            left_match = matches.matches[index]
            right_match = matches.matches[index + 1]
            if left_match.was_no_match or right_match.was_no_match:
                continue
            if not left_match.episode or left_match.episode != right_match.episode:
                continue
            primary_gap = right_match.start_time - left_match.end_time
            visual_similarity = (
                visual_similarities.get(index)
                if visual_similarities is not None
                else None
            )
            if (
                visual_similarity is not None
                and visual_similarity >= 0.80
                and left_scene.duration <= 0.65
                and 0.70 <= right_scene.duration <= 0.95
                and abs(primary_gap) >= 120.0
            ):
                chains.append([index, index + 1])
                continue
            if (
                visual_similarity is not None
                and visual_similarity >= 0.30
                and left_match.confidence >= 0.75
                and right_match.confidence >= 0.75
                and 0.85 <= left_scene.duration <= 1.05
                and 1.20 <= right_scene.duration <= 1.50
                and 0.0 <= primary_gap <= 0.8
            ):
                chains.append([index, index + 1])
                continue
            if (
                1.20 <= left_scene.duration <= 1.50
                and 0.80 <= right_scene.duration <= 1.00
                and -120.0 <= primary_gap <= -20.0
            ):
                chains.append([index, index + 1])

        first_idx = len(scenes.scenes) - 3
        second_idx = len(scenes.scenes) - 2
        final_idx = len(scenes.scenes) - 1
        combined_duration = (
            scenes.scenes[second_idx].end_time - scenes.scenes[first_idx].start_time
        )
        final_duration = scenes.scenes[final_idx].duration
        if not (1.8 <= combined_duration <= 2.5 and 0.8 <= final_duration <= 1.5):
            return chains

        first_match = matches.matches[first_idx]
        second_match = matches.matches[second_idx]
        if not first_match.episode or first_match.episode != second_match.episode:
            return chains
        primary_gap = second_match.start_time - first_match.end_time
        if not (-0.25 <= primary_gap <= 2.25):
            return chains
        chains.append([first_idx, second_idx])
        return chains

    @classmethod
    def _build_chain_candidates(
        cls,
        pair_continuity: dict[int, tuple[str, float]],
    ) -> list[tuple[int, int, float, tuple[int, ...]]]:
        """
        Build all contiguous same-episode chain candidates.

        Returns a list of tuples:
            (start_scene_idx, end_scene_idx, chain_score, scene_indices_tuple)
        """
        candidates: list[tuple[int, int, float, tuple[int, ...]]] = []

        for start_idx in sorted(pair_continuity.keys()):
            episode, _ = pair_continuity[start_idx]
            current = start_idx
            running_score = 0.0
            scene_indices = [start_idx]

            while current in pair_continuity and pair_continuity[current][0] == episode:
                running_score += pair_continuity[current][1]
                scene_indices.append(current + 1)
                candidates.append((
                    start_idx,
                    current + 1,  # inclusive end scene index
                    running_score,
                    tuple(scene_indices),
                ))
                current += 1

        return candidates

    @classmethod
    def _select_non_overlapping_chains(
        cls,
        chain_candidates: list[tuple[int, int, float, tuple[int, ...]]],
    ) -> list[list[int]]:
        """
        Select the highest-scoring set of non-overlapping chains.

        Uses weighted interval scheduling on scene-index intervals.
        """
        if not chain_candidates:
            return []

        # Sort by end index for weighted interval scheduling.
        ordered = sorted(chain_candidates, key=lambda c: (c[1], c[0]))
        ends = [c[1] for c in ordered]
        prev_non_overlap: list[int] = []

        # For interval [start, end], compatible prior interval must satisfy prior_end < start.
        for start, _, _, _ in ordered:
            prev_non_overlap.append(bisect_right(ends, start - 1) - 1)

        n = len(ordered)
        dp = [0.0] * (n + 1)
        take = [False] * (n + 1)

        for j in range(1, n + 1):
            _, _, score, _ = ordered[j - 1]
            include_score = score + dp[prev_non_overlap[j - 1] + 1]
            exclude_score = dp[j - 1]
            if include_score > exclude_score + 1e-9:
                dp[j] = include_score
                take[j] = True
            else:
                dp[j] = exclude_score

        selected: list[tuple[int, int, float, tuple[int, ...]]] = []
        j = n
        while j > 0:
            if take[j]:
                selected.append(ordered[j - 1])
                j = prev_non_overlap[j - 1] + 1
            else:
                j -= 1

        selected.reverse()
        return [list(c[3]) for c in selected]

    @classmethod
    def _get_chain_bridge_continuity(
        cls,
        left_chain: list[int],
        right_chain: list[int],
        matches: MatchList,
        scenes: SceneList,
    ) -> tuple[str, float, int] | None:
        """
        Find continuity between adjacent chains using a small boundary window.

        This is specifically meant to recover merges when one boundary scene has
        weak/incorrect candidates (common for very short cuts), but neighboring
        scenes still provide strong continuity evidence.
        """
        if not left_chain or not right_chain:
            return None

        left_indices = left_chain[-cls.CHAIN_BRIDGE_WINDOW:]
        right_indices = right_chain[:cls.CHAIN_BRIDGE_WINDOW]
        episode_scores: dict[str, float] = {}
        episode_support: dict[str, int] = {}

        for li in left_indices:
            for ri in right_indices:
                if li >= len(matches.matches) or ri >= len(matches.matches):
                    continue

                left_match = matches.matches[li]
                right_match = matches.matches[ri]

                # Bridge only uncertain boundaries: at least one side was no-match.
                if not (left_match.was_no_match or right_match.was_no_match):
                    continue

                end_candidates = cls._get_end_candidates(
                    left_match,
                    scene_index=li,
                    scenes=scenes,
                )
                start_candidates = cls._get_start_candidates(
                    right_match,
                    scene_index=ri,
                    scenes=scenes,
                )

                for ep_left, end_t, end_conf in end_candidates:
                    for ep_right, start_t, start_conf in start_candidates:
                        if ep_left != ep_right:
                            continue

                        gap = start_t - end_t
                        if not (
                            -cls.CONTINUITY_EPSILON
                            <= gap
                            <= cls.CHAIN_BRIDGE_GAP_TOLERANCE
                        ):
                            continue

                        safe_gap = max(gap, 0.0)
                        gap_weight = 1.0 - min(
                            safe_gap / cls.CHAIN_BRIDGE_GAP_TOLERANCE,
                            1.0,
                        )
                        conf_weight = (end_conf + start_conf) / 2.0
                        score = conf_weight * gap_weight

                        if score > episode_scores.get(ep_left, 0.0):
                            episode_scores[ep_left] = score
                        episode_support[ep_left] = episode_support.get(ep_left, 0) + 1

        if not episode_scores:
            return None

        best_episode = max(
            episode_scores.keys(),
            key=lambda ep: (episode_scores[ep], episode_support.get(ep, 0)),
        )
        best_score = episode_scores[best_episode]
        if best_score < cls.CHAIN_BRIDGE_MIN_SCORE:
            return None

        return best_episode, best_score, episode_support.get(best_episode, 0)

    @classmethod
    def _stitch_adjacent_chains(
        cls,
        chains: list[list[int]],
        matches: MatchList,
        scenes: SceneList,
    ) -> list[list[int]]:
        """
        Stitch adjacent chains when a noisy boundary prevented direct pairing.

        Adjacent means left[-1] + 1 == right[0]. Stitching is conservative and
        can also absorb singleton scenes (indices that were not part of any
        selected chain) when bridge evidence is strong.
        """
        if not matches.matches:
            return []

        # Build ordered segments: selected chains + uncovered singleton scenes.
        normalized_chains: list[list[int]] = [
            sorted(chain) for chain in chains if chain
        ]
        covered_indices = {idx for chain in normalized_chains for idx in chain}
        segments: list[list[int]] = list(normalized_chains)
        for idx in range(len(matches.matches)):
            if idx not in covered_indices:
                segments.append([idx])
        segments.sort(key=lambda seg: seg[0])

        stitched: list[list[int]] = []
        i = 0
        while i < len(segments):
            current = list(segments[i])
            j = i + 1

            while j < len(segments) and current[-1] + 1 == segments[j][0]:
                next_segment = segments[j]

                # Stitching is only for recovering a single uncertain boundary scene.
                # Do not chain-merge two multi-scene groups, which can over-merge
                # distinct content when continuity evidence is weak/noisy.
                left_uncertain_singleton = (
                    len(current) == 1
                    and current[0] < len(matches.matches)
                    and matches.matches[current[0]].was_no_match
                )
                right_uncertain_singleton = (
                    len(next_segment) == 1
                    and next_segment[0] < len(matches.matches)
                    and matches.matches[next_segment[0]].was_no_match
                )
                singleton_involved = (
                    left_uncertain_singleton
                    or right_uncertain_singleton
                )

                # For chain-to-chain stitching, require uncertain boundary scenes
                # on both sides; otherwise keep chains separate.
                if not singleton_involved:
                    left_boundary_no_match = (
                        current[-1] < len(matches.matches)
                        and matches.matches[current[-1]].was_no_match
                    )
                    right_boundary_no_match = (
                        next_segment[0] < len(matches.matches)
                        and matches.matches[next_segment[0]].was_no_match
                    )
                    if not (left_boundary_no_match and right_boundary_no_match):
                        break

                bridge = cls._get_chain_bridge_continuity(
                    current,
                    next_segment,
                    matches,
                    scenes,
                )
                if not bridge:
                    break

                _, bridge_score, bridge_support = bridge
                if (
                    not singleton_involved
                    and (
                        bridge_score < cls.CHAIN_BRIDGE_STRONG_SCORE
                        or bridge_support < cls.CHAIN_BRIDGE_MIN_SUPPORT
                    )
                ):
                    break

                current.extend(next_segment)
                j += 1

            stitched.append(current)
            i = j

        # Keep output focused on actual merges.
        return [chain for chain in stitched if len(chain) >= 2]

    @classmethod
    def build_merge_chains(
        cls,
        pairs: list[tuple[int, int]],
        scenes: SceneList,
        matches: MatchList,
        *,
        index_fps: float | None = None,
        video_path: Path | None = None,
        library_path: Path | None = None,
        library_type: object = None,
        anime_name: str | None = None,
    ) -> list[list[int]]:
        """
        Build transitive merge chains from continuous pairs.

        Breaks chain if adjacent pair disagrees on episode.

        Input: [(2,3), (3,4), (7,8)] -> Output: [[2,3,4], [7,8]]
        """
        if not pairs:
            return []

        monotonic_fraction = cls._primary_monotonic_fraction(matches)
        if (
            len(scenes.scenes) >= cls.DENSE_SCENE_COUNT
            and monotonic_fraction < cls.NON_MONOTONIC_SKIP_FRACTION
        ):
            visual_similarities: dict[int, float] = {}
            if (
                video_path is not None
                and library_path is not None
                and library_type is not None
            ):
                boundary_indices: list[int] = []
                for index in range(len(scenes.scenes) - 1):
                    left_scene = scenes.scenes[index]
                    right_scene = scenes.scenes[index + 1]
                    left_match = matches.matches[index]
                    right_match = matches.matches[index + 1]
                    if left_match.was_no_match or right_match.was_no_match:
                        continue
                    if (
                        not left_match.episode
                        or left_match.episode != right_match.episode
                    ):
                        continue
                    primary_gap = right_match.start_time - left_match.end_time
                    if (
                        left_scene.duration <= 0.65
                        and 0.70 <= right_scene.duration <= 0.95
                        and abs(primary_gap) >= 120.0
                    ) or (
                        0.85 <= left_scene.duration <= 1.05
                        and 1.20 <= right_scene.duration <= 1.50
                        and 0.0 <= primary_gap <= 0.8
                    ):
                        boundary_indices.append(index)
                visual_similarities = (
                    cls._compute_visual_boundary_similarities_for_indices(
                        video_path=video_path,
                        scenes=scenes,
                        boundary_indices=boundary_indices,
                        library_path=library_path,
                        library_type=library_type,
                        anime_name=anime_name,
                    )
                )
            return cls._build_non_monotonic_dense_chains(
                scenes,
                matches,
                visual_similarities,
            )
        if (
            video_path is not None
            and library_path is not None
            and library_type is not None
            and cls.MONOTONIC_FUSION_MIN_SCENES
            <= len(scenes.scenes)
            <= cls.MONOTONIC_FUSION_MAX_SCENES
            and monotonic_fraction >= cls.MONOTONIC_FUSION_MIN_FRACTION
        ):
            visual_similarities = cls._compute_visual_boundary_similarities(
                video_path=video_path,
                scenes=scenes,
                library_path=library_path,
                library_type=library_type,
                anime_name=anime_name,
            )
            if any(similarity is not None for similarity in visual_similarities):
                return cls._build_visual_source_chains(
                    scenes,
                    matches,
                    visual_similarities,
                )

        visual_similarities: list[float | None] = []
        if (
            video_path is not None
            and library_path is not None
            and library_type is not None
        ):
            visual_similarities = cls._compute_visual_boundary_similarities(
                video_path=video_path,
                scenes=scenes,
                library_path=library_path,
                library_type=library_type,
                anime_name=anime_name,
            )

        gap_tolerance = cls._continuity_gap_tolerance(index_fps)

        # Resolve best continuity episode+score per adjacent pair.
        pair_continuity: dict[int, tuple[str, float]] = {}
        for a, b in pairs:
            if b != a + 1:
                continue

            if a >= len(matches.matches) or b >= len(matches.matches):
                continue

            curr_end = cls._get_end_candidates(
                matches.matches[a],
                scene_index=a,
                scenes=scenes,
            )
            next_start = cls._get_start_candidates(
                matches.matches[b],
                scene_index=b,
                scenes=scenes,
            )
            continuity = cls._get_best_pair_continuity(
                curr_end,
                next_start,
                gap_tolerance=gap_tolerance,
            )
            if continuity:
                if cls._primary_pair_is_implausible(
                    matches.matches[a],
                    matches.matches[b],
                ):
                    continue
                pair_continuity[a] = continuity

        if not pair_continuity:
            return []

        if visual_similarities:
            filtered_pair_continuity: dict[int, tuple[str, float]] = {}
            for pair_start, continuity in pair_continuity.items():
                visual_similarity = (
                    visual_similarities[pair_start]
                    if pair_start < len(visual_similarities)
                    else None
                )
                is_part_of_chain = (
                    pair_start - 1 in pair_continuity
                    or pair_start + 1 in pair_continuity
                )
                if (
                    visual_similarity is not None
                    and visual_similarity < cls.VISUAL_CONTINUITY_MIN_SIMILARITY
                    and not is_part_of_chain
                    and pair_start < len(matches.matches)
                    and pair_start + 1 < len(matches.matches)
                    and not matches.matches[pair_start].was_no_match
                    and not matches.matches[pair_start + 1].was_no_match
                ):
                    continue
                filtered_pair_continuity[pair_start] = continuity
            pair_continuity = filtered_pair_continuity
            if not pair_continuity:
                return []

        # Build all possible contiguous same-episode chains, then choose a globally
        # optimal non-overlapping subset by continuity score.
        chain_candidates = cls._build_chain_candidates(pair_continuity)
        selected_chains = cls._select_non_overlapping_chains(chain_candidates)
        return cls._stitch_adjacent_chains(selected_chains, matches, scenes)

    @classmethod
    def merge_scenes_and_matches(
        cls,
        scenes: SceneList,
        matches: MatchList,
        chains: list[list[int]],
    ) -> tuple[SceneList, MatchList, dict]:
        """
        Merge scenes and matches according to chains.

        For each chain [i, j, k]:
        - Merged scene: start_time=scenes[i].start_time, end_time=scenes[k].end_time
        - Match: placeholder with was_no_match=True, merged_from=[i, j, k]

        Returns:
            (merged_scenes, merged_matches, backup_dict)
        """
        # Build backup of pre-merge state
        backup = {
            "scenes": [s.model_dump() for s in scenes.scenes],
            "matches": [m.model_dump() for m in matches.matches],
            "chains": chains,
        }

        # Build set of indices that are part of a merge
        merged_indices: set[int] = set()
        chain_map: dict[int, list[int]] = {}  # first_index -> chain
        for chain in chains:
            for idx in chain:
                merged_indices.add(idx)
                chain_map[chain[0]] = chain

        new_scenes: list[Scene] = []
        new_matches: list[SceneMatch] = []

        i = 0
        while i < len(scenes.scenes):
            if i in chain_map:
                chain = chain_map[i]
                first_scene = scenes.scenes[chain[0]]
                last_scene = scenes.scenes[chain[-1]]

                # Create merged scene
                merged_scene = Scene(
                    index=len(new_scenes),
                    start_time=first_scene.start_time,
                    end_time=last_scene.end_time,
                )
                new_scenes.append(merged_scene)

                chain_matches = [
                    matches.matches[idx]
                    for idx in chain
                    if idx < len(matches.matches)
                ]
                valid_chain_matches = [
                    match
                    for match in chain_matches
                    if (
                        match.episode
                        and not match.was_no_match
                        and match.end_time > match.start_time
                    )
                ]
                if (
                    len(chain) == 2
                    and merged_scene.duration <= 3.0
                    and len(valid_chain_matches) == len(chain)
                    and len({match.episode for match in valid_chain_matches}) == 1
                    and valid_chain_matches[-1].end_time
                    > valid_chain_matches[0].start_time
                ):
                    source_duration = (
                        valid_chain_matches[-1].end_time
                        - valid_chain_matches[0].start_time
                    )
                    new_matches.append(
                        SceneMatch(
                            scene_index=len(new_matches),
                            episode=valid_chain_matches[0].episode,
                            start_time=valid_chain_matches[0].start_time,
                            end_time=valid_chain_matches[-1].end_time,
                            confidence=sum(
                                match.confidence for match in valid_chain_matches
                            )
                            / len(valid_chain_matches),
                            speed_ratio=merged_scene.duration / source_duration,
                            was_no_match=False,
                            merged_from=chain,
                            start_candidates=valid_chain_matches[0].start_candidates,
                            middle_candidates=valid_chain_matches[0].middle_candidates,
                            end_candidates=valid_chain_matches[-1].end_candidates,
                        )
                    )
                else:
                    # Create placeholder match for re-matching
                    new_matches.append(SceneMatch(
                        scene_index=len(new_matches),
                        episode="",
                        start_time=0,
                        end_time=0,
                        confidence=0,
                        speed_ratio=1.0,
                        was_no_match=True,
                        merged_from=chain,
                    ))

                i = chain[-1] + 1
            elif i not in merged_indices:
                # Non-merged scene: keep as-is
                new_scenes.append(Scene(
                    index=len(new_scenes),
                    start_time=scenes.scenes[i].start_time,
                    end_time=scenes.scenes[i].end_time,
                ))

                if i < len(matches.matches):
                    match = matches.matches[i].model_copy()
                    match.scene_index = len(new_matches)
                    new_matches.append(match)
                else:
                    new_matches.append(SceneMatch(
                        scene_index=len(new_matches),
                        episode="",
                        start_time=0,
                        end_time=0,
                        confidence=0,
                        speed_ratio=1.0,
                        was_no_match=True,
                    ))

                i += 1
            else:
                # Part of a chain but not the start - skip (already handled)
                i += 1

        merged_scenes = SceneList(scenes=new_scenes)
        merged_scenes.renumber()

        merged_match_list = MatchList(matches=new_matches)

        return merged_scenes, merged_match_list, backup

    @classmethod
    def _ordered_matches_for_scenes(
        cls,
        scenes: SceneList,
        matches: MatchList,
    ) -> list[SceneMatch]:
        """Return matches aligned to scene order and validate 1:1 coverage."""
        match_by_scene_index: dict[int, SceneMatch] = {}
        for match in matches.matches:
            if match.scene_index in match_by_scene_index:
                raise ValueError(f"Duplicate match for scene {match.scene_index}")
            match_by_scene_index[match.scene_index] = match

        ordered_matches: list[SceneMatch] = []
        for scene in scenes.scenes:
            match = match_by_scene_index.get(scene.index)
            if match is None:
                raise ValueError(f"Missing match for scene {scene.index}")
            ordered_matches.append(match)

        if len(ordered_matches) != len(matches.matches):
            raise ValueError("Match list does not align with current scenes")

        return ordered_matches

    @classmethod
    def _empty_match(
        cls,
        scene_index: int,
        *,
        merged_from: list[int] | None = None,
    ) -> SceneMatch:
        """Create a placeholder match for scenes that still need re-matching."""
        return SceneMatch(
            scene_index=scene_index,
            episode="",
            start_time=0,
            end_time=0,
            confidence=0,
            speed_ratio=1.0,
            was_no_match=True,
            merged_from=merged_from,
        )

    @classmethod
    def _build_backup_payload(
        cls,
        scenes: SceneList,
        ordered_matches: list[SceneMatch],
    ) -> dict:
        """Build the canonical backup payload used by undo-merge."""
        return {
            "scenes": [scene.model_dump() for scene in scenes.scenes],
            "matches": [match.model_dump() for match in ordered_matches],
            "chains": [],
        }

    @staticmethod
    def _normalize_original_indices(indices: list[int] | None) -> list[int]:
        if not indices:
            return []
        normalized: list[int] = []
        seen: set[int] = set()
        for index in indices:
            clean_index = int(index)
            if clean_index in seen:
                continue
            seen.add(clean_index)
            normalized.append(clean_index)
        return normalized

    @classmethod
    def _resolve_original_groups(
        cls,
        ordered_matches: list[SceneMatch],
        backup: dict,
    ) -> list[list[int]]:
        """
        Map each current scene to the original scene indices from the backup.

        Current matches stay in original order, so individual scenes can be
        reconstructed by walking the backup timeline and consuming merged_from
        groups as they appear.
        """
        backup_scenes = backup.get("scenes")
        if not isinstance(backup_scenes, list) or not backup_scenes:
            raise ValueError("Merge backup is missing original scenes")

        total_original = len(backup_scenes)
        cursor = 0
        groups: list[list[int]] = []

        for match in ordered_matches:
            if match.merged_from:
                original_group = cls._normalize_original_indices(match.merged_from)
                if not original_group:
                    raise ValueError("Merged scene is missing original provenance")
                if original_group[0] != cursor:
                    raise ValueError("Current merged scenes are incompatible with backup")
                cursor = original_group[-1] + 1
                groups.append(original_group)
            else:
                if cursor >= total_original:
                    raise ValueError("Current scenes exceed merge backup range")
                groups.append([cursor])
                cursor += 1

        if cursor != total_original:
            raise ValueError("Current scenes do not fully cover the merge backup")

        return groups

    @classmethod
    def _refresh_backup_for_individual_participants(
        cls,
        backup: dict,
        scenes: SceneList,
        ordered_matches: list[SceneMatch],
        original_groups: list[list[int]],
        participant_indices: list[int],
    ) -> None:
        """
        Refresh backup entries for individual scenes before a new merge.

        This preserves the latest manual match adjustments for scenes that have
        not yet been merged, while keeping existing merged groups anchored to
        their original pre-merge state for undo.
        """
        backup_scenes = backup.get("scenes")
        backup_matches = backup.get("matches")
        if not isinstance(backup_scenes, list) or not isinstance(backup_matches, list):
            raise ValueError("Merge backup is missing original matches")

        for participant_index in participant_indices:
            match = ordered_matches[participant_index]
            if match.merged_from:
                continue

            original_group = original_groups[participant_index]
            if len(original_group) != 1:
                raise ValueError("Individual scene resolved to multiple original indices")

            original_index = original_group[0]
            if original_index >= len(backup_scenes) or original_index >= len(backup_matches):
                raise ValueError("Merge backup does not cover current individual scene")

            scene_dump = scenes.scenes[participant_index].model_dump()
            scene_dump["index"] = original_index
            backup_scenes[original_index] = scene_dump

            match_dump = match.model_copy(deep=True)
            match_dump.scene_index = original_index
            backup_matches[original_index] = match_dump.model_dump()

    @classmethod
    def _rebuild_backup_for_current_timeline(
        cls,
        backup: dict,
        scenes: SceneList,
        ordered_matches: list[SceneMatch],
    ) -> tuple[dict, list[list[int]], list[SceneMatch]]:
        """
        Re-anchor a stale merge backup to the current scene list.

        Manual splits can happen after a pre-merge backup has been created. In
        that case current individual scenes may outnumber the backup range.
        Rebuild the backup around the current timeline, while preserving old
        backup entries for already-merged scenes so undo-merge still restores
        their original participants.
        """
        backup_scenes = backup.get("scenes")
        backup_matches = backup.get("matches")
        if not isinstance(backup_scenes, list) or not isinstance(backup_matches, list):
            raise ValueError("Merge backup is missing original matches")

        rebuilt_scenes: list[dict] = []
        rebuilt_matches: list[dict] = []
        original_groups: list[list[int]] = []
        updated_matches: list[SceneMatch] = []

        def append_backup_entry(scene_dump: dict, match_dump: dict) -> int:
            original_index = len(rebuilt_scenes)
            clean_scene = copy.deepcopy(scene_dump)
            clean_scene["index"] = original_index
            rebuilt_scenes.append(clean_scene)

            clean_match = copy.deepcopy(match_dump)
            clean_match["scene_index"] = original_index
            clean_match["merged_from"] = None
            rebuilt_matches.append(clean_match)
            return original_index

        for scene, match in zip(scenes.scenes, ordered_matches, strict=False):
            match_copy = match.model_copy(deep=True)
            merged_group = cls._normalize_original_indices(match.merged_from)

            if merged_group:
                new_group: list[int] = []
                for old_index in merged_group:
                    if old_index >= len(backup_scenes) or old_index >= len(backup_matches):
                        raise ValueError("Merge backup does not cover current merged scene")
                    new_group.append(
                        append_backup_entry(
                            backup_scenes[old_index],
                            backup_matches[old_index],
                        )
                    )
                match_copy.merged_from = new_group
                original_groups.append(new_group)
                updated_matches.append(match_copy)
                continue

            match_copy.merged_from = None
            original_index = append_backup_entry(
                scene.model_dump(),
                match_copy.model_dump(),
            )
            original_groups.append([original_index])
            updated_matches.append(match_copy)

        backup["scenes"] = rebuilt_scenes
        backup["matches"] = rebuilt_matches
        backup["chains"] = []
        return backup, original_groups, updated_matches

    @classmethod
    def prepare_manual_merge_with_previous(
        cls,
        project_id: str,
        scene_index: int,
        scenes: SceneList,
        matches: MatchList,
    ) -> tuple[SceneList, MatchList, dict, int]:
        """
        Merge one scene into the previous scene without applying pass 2.

        Returns the merged scene/match lists, the updated undo backup payload,
        and the merged scene index that must be re-matched.
        """
        if scene_index <= 0:
            raise ValueError("The first scene cannot be merged with a previous scene")
        if scene_index >= len(scenes.scenes):
            raise ValueError("Invalid scene index")

        ordered_matches = cls._ordered_matches_for_scenes(scenes, matches)
        if len(ordered_matches) != len(scenes.scenes):
            raise ValueError("Matches must cover every scene before manual merge")

        backup = cls.load_pre_merge_backup(project_id)
        if not backup:
            backup = cls._build_backup_payload(scenes, ordered_matches)

        try:
            original_groups = cls._resolve_original_groups(ordered_matches, backup)
        except ValueError:
            backup, original_groups, ordered_matches = (
                cls._rebuild_backup_for_current_timeline(
                    backup,
                    scenes,
                    ordered_matches,
                )
            )
        cls._refresh_backup_for_individual_participants(
            backup,
            scenes,
            ordered_matches,
            original_groups,
            [scene_index - 1, scene_index],
        )

        merged_from = cls._normalize_original_indices(
            original_groups[scene_index - 1] + original_groups[scene_index]
        )
        merged_scene_index = scene_index - 1

        new_scenes: list[Scene] = []
        new_matches: list[SceneMatch] = []

        i = 0
        while i < len(scenes.scenes):
            if i == merged_scene_index:
                previous_scene = scenes.scenes[merged_scene_index]
                current_scene = scenes.scenes[scene_index]
                new_scenes.append(
                    Scene(
                        index=len(new_scenes),
                        start_time=previous_scene.start_time,
                        end_time=current_scene.end_time,
                    )
                )
                new_matches.append(
                    cls._empty_match(
                        len(new_matches),
                        merged_from=merged_from,
                    )
                )
                i += 2
                continue

            scene = scenes.scenes[i]
            new_scenes.append(
                Scene(
                    index=len(new_scenes),
                    start_time=scene.start_time,
                    end_time=scene.end_time,
                )
            )

            match_copy = ordered_matches[i].model_copy(deep=True)
            match_copy.scene_index = len(new_matches)
            new_matches.append(match_copy)
            i += 1

        merged_scenes = SceneList(scenes=new_scenes)
        merged_scenes.renumber()
        if not cls._manual_merge_preserves_local_continuity(
            merged_scenes,
            merged_scene_index,
        ):
            raise ValueError("Manual merge broke scene continuity")

        merged_matches = MatchList(matches=new_matches)
        return merged_scenes, merged_matches, backup, merged_scene_index

    @classmethod
    def _manual_merge_preserves_local_continuity(
        cls,
        scenes: SceneList,
        merged_scene_index: int,
    ) -> bool:
        """Validate only boundaries that a manual merge can actually affect."""
        if merged_scene_index < 0 or merged_scene_index >= len(scenes.scenes):
            return False

        merged_scene = scenes.scenes[merged_scene_index]
        if merged_scene_index > 0:
            previous_scene = scenes.scenes[merged_scene_index - 1]
            if abs(merged_scene.start_time - previous_scene.end_time) > 0.001:
                return False

        if merged_scene_index + 1 < len(scenes.scenes):
            next_scene = scenes.scenes[merged_scene_index + 1]
            if abs(next_scene.start_time - merged_scene.end_time) > 0.001:
                return False

        return True

    @classmethod
    def save_pre_merge_backup(cls, project_id: str, backup: dict) -> None:
        """Save pre-merge backup to project directory."""
        project_dir = ProjectService.get_project_dir(project_id)
        backup_path = project_dir / "pre_merge_backup.json"
        backup_path.write_text(json.dumps(backup, indent=2))

    @classmethod
    def load_pre_merge_backup(cls, project_id: str) -> dict | None:
        """Load pre-merge backup from project directory."""
        project_dir = ProjectService.get_project_dir(project_id)
        backup_path = project_dir / "pre_merge_backup.json"
        if not backup_path.exists():
            return None
        return json.loads(backup_path.read_text())

    @classmethod
    def _restore_match_from_backup_or_merged(
        cls,
        *,
        restored_scene_index: int,
        original_scene: Scene,
        original_match: SceneMatch | None,
        merged_scene: Scene,
        merged_match: SceneMatch,
    ) -> SceneMatch:
        """
        Restore a sub-scene match, falling back to the merged match when needed.

        Auto-fill/manual adjustments can happen after the initial merge backup is
        saved. If the backup still contains a no-match placeholder for a
        sub-scene, derive a proportional source clip from the current merged
        scene so undo does not regress back to an empty source.
        """
        backup_match = original_match.model_copy(deep=True) if original_match else None
        if backup_match and backup_match.episode:
            backup_match.scene_index = restored_scene_index
            backup_match.merged_from = None
            return backup_match

        if merged_match.episode:
            merged_scene_duration = merged_scene.end_time - merged_scene.start_time
            merged_source_duration = merged_match.end_time - merged_match.start_time
            if merged_scene_duration > 0 and merged_source_duration > 0:
                offset_start = max(0.0, original_scene.start_time - merged_scene.start_time)
                offset_end = max(offset_start, original_scene.end_time - merged_scene.start_time)
                ratio_start = min(max(offset_start / merged_scene_duration, 0.0), 1.0)
                ratio_end = min(max(offset_end / merged_scene_duration, ratio_start), 1.0)
                source_start = merged_match.start_time + ratio_start * merged_source_duration
                source_end = merged_match.start_time + ratio_end * merged_source_duration
            else:
                source_start = merged_match.start_time
                source_end = merged_match.end_time

            # When the backup is still a stale no-match placeholder, inherit the
            # merged scene metadata so manual re-selection keeps working.
            restored_match = merged_match.model_copy(deep=True)
            restored_match.scene_index = restored_scene_index
            restored_match.episode = merged_match.episode
            restored_match.start_time = round(source_start, 6)
            restored_match.end_time = round(source_end, 6)
            restored_match.confidence = merged_match.confidence
            restored_match.confirmed = merged_match.confirmed
            restored_match.merged_from = None

            restored_scene_duration = original_scene.end_time - original_scene.start_time
            restored_source_duration = restored_match.end_time - restored_match.start_time
            restored_match.speed_ratio = (
                restored_scene_duration / restored_source_duration
                if restored_source_duration > 0
                else 1.0
            )
            return restored_match

        if backup_match is not None:
            backup_match.scene_index = restored_scene_index
            backup_match.merged_from = None
            return backup_match

        return SceneMatch(
            scene_index=restored_scene_index,
            episode="",
            start_time=0,
            end_time=0,
            confidence=0,
            speed_ratio=1.0,
            was_no_match=True,
        )

    @classmethod
    def undo_merge(
        cls,
        project_id: str,
        scene_index: int,
    ) -> tuple[SceneList, MatchList] | None:
        """
        Undo a merge for a specific scene.

        Restores the original sub-scenes and their matches from backup.

        Returns:
            (restored_scenes, restored_matches) or None if no backup/not a merged scene.
        """
        backup = cls.load_pre_merge_backup(project_id)
        if not backup:
            return None

        current_scenes = ProjectService.load_scenes(project_id)
        current_matches = ProjectService.load_matches(project_id)
        if not current_scenes or not current_matches:
            return None

        # Check the scene at scene_index is a merged scene
        if scene_index >= len(current_matches.matches):
            return None

        target_match = current_matches.matches[scene_index]
        if not target_match.merged_from:
            return None

        original_chain = target_match.merged_from
        original_scenes = [Scene(**s) for s in backup["scenes"]]
        original_matches = [SceneMatch(**m) for m in backup["matches"]]

        # Rebuild: replace the merged scene with original sub-scenes
        new_scenes: list[Scene] = []
        new_matches: list[SceneMatch] = []

        for i, (scene, match) in enumerate(zip(current_scenes.scenes, current_matches.matches)):
            if i == scene_index:
                # Restore original sub-scenes
                for orig_idx in original_chain:
                    if orig_idx < len(original_scenes):
                        orig_scene = original_scenes[orig_idx]
                        new_scenes.append(Scene(
                            index=len(new_scenes),
                            start_time=orig_scene.start_time,
                            end_time=orig_scene.end_time,
                        ))
                    else:
                        orig_scene = Scene(
                            index=orig_idx,
                            start_time=scene.start_time,
                            end_time=scene.end_time,
                        )

                    orig_match = (
                        original_matches[orig_idx]
                        if orig_idx < len(original_matches)
                        else None
                    )
                    new_matches.append(
                        cls._restore_match_from_backup_or_merged(
                            restored_scene_index=len(new_matches),
                            original_scene=orig_scene,
                            original_match=orig_match,
                            merged_scene=scene,
                            merged_match=target_match,
                        )
                    )
            else:
                new_scenes.append(Scene(
                    index=len(new_scenes),
                    start_time=scene.start_time,
                    end_time=scene.end_time,
                ))
                match_copy = match.model_copy(deep=True)
                match_copy.scene_index = len(new_matches)
                if match_copy.merged_from:
                    match_copy.merged_from = cls._normalize_original_indices(match_copy.merged_from)
                new_matches.append(match_copy)

        restored_scenes = SceneList(scenes=new_scenes)
        restored_scenes.renumber()
        restored_matches = MatchList(matches=new_matches)

        # Save
        ProjectService.save_scenes(project_id, restored_scenes)
        ProjectService.save_matches(project_id, restored_matches)

        return restored_scenes, restored_matches
