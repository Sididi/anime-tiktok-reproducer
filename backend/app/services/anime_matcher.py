"""Anime source matching service using anime_searcher module."""

import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import AsyncIterator

import numpy as np
from PIL import Image, ImageOps

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from ..models import AlternativeMatch, MatchCandidate, MatchList, Scene, SceneMatch, SceneList


@dataclass
class MatchProgress:
    """Progress information for anime matching."""

    status: str  # starting, matching, complete, error
    progress: float = 0.0  # 0-1
    message: str = ""
    current_scene: int = 0
    total_scenes: int = 0
    matches: MatchList | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "current_scene": self.current_scene,
            "total_scenes": self.total_scenes,
            "error": self.error,
        }
        if self.matches is not None:
            result["matches"] = self.matches.model_dump()
        return result


class AnimeMatcherService:
    """Service for matching TikTok scenes to anime source episodes."""

    # Singleton instances for the searcher components (expensive to load)
    _index_manager = None
    _embedder = None
    _query_processor = None
    _loaded_library_path: Path | None = None
    _loaded_library_type: LibraryType | None = None
    # Series that were updated on disk and require cache refresh before matching.
    _stale_series: dict[LibraryType, set[str]] = defaultdict(set)

    @classmethod
    def mark_series_updated(
        cls,
        library_type: LibraryType | str,
        series_name: str | None,
    ) -> None:
        """Mark one series as stale so next match for it reloads the index cache."""
        if not series_name:
            return
        cls._stale_series[coerce_library_type(library_type)].add(series_name)

    @staticmethod
    def _require_cv2():
        import cv2

        return cv2

    @classmethod
    def _init_searcher(
        cls,
        library_path: Path,
        library_type: LibraryType | str,
        anime_name: str | None = None,
    ) -> bool:
        """
        Initialize the anime_searcher components.

        Args:
            library_path: Path to the anime library with index
            anime_name: Optional series name currently being matched

        Returns:
            True if initialization succeeded
        """
        # Add anime_searcher to path if needed
        searcher_path = settings.anime_searcher_path / "anime_searcher"
        if str(searcher_path.parent) not in sys.path:
            sys.path.insert(0, str(searcher_path.parent))

        # Reuse cache unless current series was updated on disk.
        scoped_type = coerce_library_type(library_type)
        stale_series = cls._stale_series[scoped_type]
        cache_ready = (
            cls._loaded_library_path == library_path
            and cls._loaded_library_type == scoped_type
            and cls._query_processor is not None
            and cls._index_manager is not None
        )
        needs_refresh_for_series = anime_name is not None and anime_name in stale_series
        needs_refresh_for_unscoped_match = anime_name is None and bool(stale_series)
        missing_scoped_series = (
            cache_ready
            and anime_name is not None
            and anime_name not in cls._index_manager.get_series_list()
        )
        if (
            cache_ready
            and not (
                needs_refresh_for_series
                or needs_refresh_for_unscoped_match
                or missing_scoped_series
            )
        ):
            return True

        try:
            from anime_searcher.indexer.embedder import SSCDEmbedder
            from anime_searcher.indexer.index_manager import IndexManager
            from anime_searcher.searcher.query import QueryProcessor

            # Find model path
            model_path = settings.sscd_model_path
            if model_path is None:
                # Try default location in anime_searcher module
                model_path = settings.anime_searcher_path / "sscd_disc_mixup.torchscript.pt"

            if not model_path.exists():
                raise FileNotFoundError(f"SSCD model not found at {model_path}")

            cls._index_manager = IndexManager(library_path)
            cls._index_manager.load_or_create()
            cls._embedder = SSCDEmbedder(model_path, precision="fp32")
            cls._query_processor = QueryProcessor(cls._index_manager, cls._embedder)
            cls._loaded_library_path = library_path
            cls._loaded_library_type = scoped_type
            # Full reload brings all series up to date.
            stale_series.clear()

            return True

        except Exception as e:
            print(f"Failed to initialize anime_searcher: {e}")
            return False

    @classmethod
    def extract_frame(cls, video_path: Path, timestamp: float) -> Image.Image | None:
        """
        Extract a single frame from a video at the given timestamp.

        Args:
            video_path: Path to the video file
            timestamp: Time in seconds

        Returns:
            PIL Image or None if extraction failed
        """
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        try:
            # Seek to timestamp
            cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ret, frame = cap.read()
            if not ret:
                return None

            # Convert BGR to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(frame_rgb)
        finally:
            cap.release()

    @classmethod
    def extract_frames(cls, video_path: Path, timestamps: list[float]) -> list[Image.Image | None]:
        """
        Extract multiple frames in one pass using a single VideoCapture instance.

        Args:
            video_path: Path to the video file
            timestamps: List of times in seconds

        Returns:
            List of PIL images (or None on extraction failure), in input order.
        """
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        frames: list[Image.Image | None] = []
        try:
            for timestamp in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000)
                ret, frame = cap.read()
                if not ret:
                    frames.append(None)
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame_rgb))
            return frames
        finally:
            cap.release()

    @classmethod
    def get_index_fps(cls) -> float:
        """Return the FPS the loaded library was indexed at.

        Falls back to 1.0 (anime_searcher's DEFAULT_FPS) when no manifest FPS
        is available so callers that gate behavior on grid step stay safe.
        """
        if cls._index_manager is not None:
            try:
                fps = cls._index_manager.get_default_fps()
            except Exception:
                fps = None
            if fps is not None and float(fps) > 0:
                return float(fps)
        return 1.0

    @classmethod
    def _get_video_fps(cls, video_path: Path) -> float | None:
        cv2 = cls._require_cv2()
        cap = cv2.VideoCapture(str(video_path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS)
            return float(fps) if fps and fps > 0 else None
        finally:
            cap.release()

    @classmethod
    def _collect_frames_in_window(
        cls,
        video_path: Path,
        start_ts: float,
        end_ts: float,
        max_frames: int = 48,
    ) -> list[tuple[float, Image.Image]]:
        """Decode frames whose timestamps fall in [start_ts, end_ts].

        Uses OpenCV's keyframe-based seek then iterates forward frame-by-frame.
        Timestamps returned are the decoded frames' actual PTS (from
        CAP_PROP_POS_MSEC read before the decode advances the position).
        """
        cv2 = cls._require_cv2()
        start_ts = max(0.0, start_ts)
        cap = cv2.VideoCapture(str(video_path))
        frames: list[tuple[float, Image.Image]] = []
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, start_ts * 1000.0)
            while len(frames) < max_frames:
                pos_ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                if pos_ts > end_ts:
                    break
                ret, frame = cap.read()
                if not ret:
                    break
                if pos_ts < start_ts:
                    # Seek landed on an earlier keyframe; skip until we enter the window.
                    continue
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append((pos_ts, Image.fromarray(frame_rgb)))
        finally:
            cap.release()
        return frames

    @classmethod
    def _refine_boundaries(
        cls,
        video_path: Path,
        scene: Scene,
        matched_episode: str,
        matched_start_ts: float,
        matched_end_ts: float,
        library_type: LibraryType | str,
    ) -> tuple[float, float] | None:
        """Refine (start_ts, end_ts) to native source FPS using argmax cosine.

        The 2-FPS index grid caps boundary precision at 0.5s. Post-match we
        decode the matched source episode at its own native FPS in a small
        window around each boundary, re-embed those frames, and pick the one
        whose SSCD embedding best matches the TikTok scene's actual first /
        last frame. Reduces boundary error from ~250ms to ~1 source frame.

        Returns None on failure; caller should keep the unrefined timestamps.
        """
        if cls._embedder is None:
            return None

        # Resolve the source episode file. Import inline to avoid a top-level
        # cycle (AnimeLibraryService imports a lot).
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

        # Use a small inward offset so we sample actual content, not transitions.
        tiny_offset = min(0.05, scene_duration / 10.0)
        tiktok_start_t = scene.start_time + tiny_offset
        tiktok_end_t = max(tiktok_start_t + 1e-3, scene.end_time - tiny_offset)

        tiktok_frames = cls.extract_frames(video_path, [tiktok_start_t, tiktok_end_t])
        if not all(tiktok_frames):
            return None
        tiktok_start_frame, tiktok_end_frame = tiktok_frames

        # Widen the refinement window slightly beyond the 2-FPS half-grid so
        # the true boundary is definitely inside the search range even when
        # matched_*_ts landed on the wrong side of a cut.
        index_step = 1.0 / max(cls.get_index_fps(), 1e-3)
        window = max(0.5, index_step + 0.15)

        start_frames = cls._collect_frames_in_window(
            episode_path,
            matched_start_ts - window,
            matched_start_ts + window,
        )
        end_frames = cls._collect_frames_in_window(
            episode_path,
            matched_end_ts - window,
            matched_end_ts + window,
        )
        if not start_frames or not end_frames:
            return None

        embedder = cls._embedder
        query_embeddings = embedder.embed_batch([tiktok_start_frame, tiktok_end_frame])
        if query_embeddings.shape[0] < 2:
            return None
        q_start, q_end = query_embeddings[0], query_embeddings[1]

        start_imgs = [f[1] for f in start_frames]
        end_imgs = [f[1] for f in end_frames]
        start_embs = embedder.embed_batch(start_imgs)
        end_embs = embedder.embed_batch(end_imgs)

        # SSCD embeddings are L2-normalized — inner product == cosine.
        start_scores = start_embs @ q_start
        end_scores = end_embs @ q_end

        refined_start = float(start_frames[int(np.argmax(start_scores))][0])
        refined_end = float(end_frames[int(np.argmax(end_scores))][0])

        # If refinement collapses or reverses the interval, keep the original
        # timestamps — a degenerate pick is worse than the coarse grid.
        if refined_end - refined_start <= 0.1:
            return None

        return refined_start, refined_end

    @classmethod
    def _search_image_batch(
        cls,
        images: list[Image.Image],
        *,
        top_n: int = 5,
        threshold: float | None = None,
        flip: bool = False,
        series: str | None = None,
    ) -> list[list]:
        """
        Run batched embedding + search for a list of query images.

        Returns one search result list per input image.
        """
        processor = cls._query_processor
        prepared = [img.convert("RGB") for img in images]

        embeddings = processor.embedder.embed_batch(prepared)
        per_image_results = [
            processor.index_manager.search(
                embeddings[i],
                top_n,
                threshold,
                series=series,
            )
            for i in range(len(prepared))
        ]

        if flip:
            flipped = [ImageOps.mirror(img) for img in prepared]
            flip_embeddings = processor.embedder.embed_batch(flipped)
            per_image_flip_results = [
                processor.index_manager.search(
                    flip_embeddings[i],
                    top_n,
                    threshold,
                    series=series,
                )
                for i in range(len(prepared))
            ]
            merged_results = [
                processor._merge_results(per_image_results[i], per_image_flip_results[i], top_n)
                for i in range(len(prepared))
            ]
        else:
            merged_results = per_image_results

        return [
            [
                processor._format_result(rank + 1, similarity, metadata)
                for rank, (similarity, metadata) in enumerate(results)
            ]
            for results in merged_results
        ]

    @classmethod
    def _find_temporal_match(
        cls,
        start_candidates: list[MatchCandidate],
        middle_candidates: list[MatchCandidate],
        end_candidates: list[MatchCandidate],
        scene_duration: float,
    ) -> SceneMatch | None:
        """
        Find a temporally consistent match across start/middle/end candidates.

        The algorithm looks for candidates from the same episode where the timestamps
        follow each other in order (start < middle < end) with a speed ratio between
        the configured matcher floor and 160% of original speed.

        Args:
            start_candidates: Top 5 matches for scene start frame
            middle_candidates: Top 5 matches for scene middle frame
            end_candidates: Top 5 matches for scene end frame
            scene_duration: Duration of the scene in the TikTok

        Returns:
            SceneMatch if a consistent match is found, None otherwise
        """
        MIN_SPEED = settings.matcher_min_speed_factor
        MAX_SPEED = 1.60  # 160% - sped up

        best_match: SceneMatch | None = None
        best_confidence = 0.0

        for start in start_candidates:
            for middle in middle_candidates:
                for end in end_candidates:
                    # Must be same episode
                    if not (start.episode == middle.episode == end.episode):
                        continue

                    # Timestamps must be in order
                    if not (start.timestamp < middle.timestamp < end.timestamp):
                        continue

                    # Calculate source duration and speed ratio
                    source_duration = end.timestamp - start.timestamp
                    if source_duration <= 0:
                        continue

                    speed_ratio = scene_duration / source_duration

                    # Check if within acceptable speed range
                    if not (MIN_SPEED <= speed_ratio <= MAX_SPEED):
                        continue

                    # Confidence combines three signals (all on [0, 1]):
                    #   avg_similarity: raw retrieval quality across probes.
                    #   min_similarity: the weakest probe — penalizes triples where
                    #                   one frame is a bad match, even if the other
                    #                   two are strong (classic sequence-match fix).
                    #   temporal_score: how close middle is to the geometric center;
                    #                   rewards clean temporal geometry.
                    avg_similarity = (
                        start.similarity + middle.similarity + end.similarity
                    ) / 3
                    min_similarity = min(
                        start.similarity, middle.similarity, end.similarity
                    )

                    expected_middle = start.timestamp + source_duration / 2
                    middle_deviation = (
                        abs(middle.timestamp - expected_middle) / source_duration
                    )
                    temporal_score = max(0.0, 1.0 - middle_deviation * 2)

                    confidence = (
                        0.70 * avg_similarity
                        + 0.20 * min_similarity
                        + 0.10 * temporal_score
                    )

                    if confidence > best_confidence:
                        best_confidence = confidence
                        best_match = SceneMatch(
                            scene_index=0,  # Will be set later
                            episode=start.episode,
                            start_time=start.timestamp,
                            end_time=end.timestamp,
                            confidence=confidence,
                            speed_ratio=speed_ratio,
                            start_candidates=[start],
                            middle_candidates=[middle],
                            end_candidates=[end],
                        )

        return best_match

    @classmethod
    def _compute_alternatives(
        cls,
        start_candidates: list[MatchCandidate],
        middle_candidates: list[MatchCandidate],
        end_candidates: list[MatchCandidate],
        scene_duration: float,
    ) -> list[AlternativeMatch]:
        """
        Compute up to 7 alternative matches using three different algorithms:
        - Weighted Average: Up to 3 candidates (averages similarity across frame positions)
        - Best Frame Winner: Up to 2 candidates (single best match from any frame)
        - Union of Top-K: Up to 2 candidates (top matches from combined pool)

        Each algorithm maintains its own seen_episodes set to allow different algorithms
        to surface the same episode with different timing estimates. This provides more
        diverse alternatives for manual review.

        Args:
            start_candidates: Top 5 matches for scene start frame
            middle_candidates: Top 5 matches for scene middle frame
            end_candidates: Top 5 matches for scene end frame
            scene_duration: Duration of the scene in the TikTok

        Returns:
            List of up to 7 AlternativeMatch objects from different algorithms
        """
        alternatives: list[AlternativeMatch] = []

        MIN_SPEED = settings.matcher_min_speed_factor
        MAX_SPEED = 1.60

        all_candidates = [
            ('start', start_candidates),
            ('middle', middle_candidates),
            ('end', end_candidates),
        ]

        # ============ Algorithm 1: Weighted Average (up to 3) ============
        # Aggregate candidates per position per episode so we can verify a
        # temporally-consistent triple exists before proposing an interval.
        # Prior to this guard, an episode whose start-frame hit and middle-frame
        # hit landed in entirely different scenes (same character, different
        # moment) produced intervals spanning hundreds of seconds — the "long
        # clip" bug.
        episode_pos: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(
            lambda: {'start': [], 'middle': [], 'end': []}
        )
        episode_total_sim: dict[str, float] = defaultdict(float)
        episode_vote_count: dict[str, int] = defaultdict(int)

        for position, candidates in all_candidates:
            for candidate in candidates:
                ep = candidate.episode
                episode_pos[ep][position].append(
                    (candidate.timestamp, candidate.similarity)
                )
                episode_total_sim[ep] += candidate.similarity
                episode_vote_count[ep] += 1

        seen_weighted_avg: set[str] = set()
        weighted_avg_alts: list[tuple[float, AlternativeMatch]] = []

        for episode in episode_pos:
            pos = episode_pos[episode]
            vote_count = episode_vote_count[episode]
            if vote_count == 0:
                continue
            avg_similarity = episode_total_sim[episode] / vote_count

            # Search for the highest-scoring valid (s, m, e) triple, then a valid
            # (s, e) pair, then fall back to midpoint projection.
            best_interval: tuple[float, float, float] | None = None
            best_interval_score = -1.0

            if pos['start'] and pos['middle'] and pos['end']:
                for s_ts, s_sim in pos['start']:
                    for m_ts, m_sim in pos['middle']:
                        if s_ts >= m_ts:
                            continue
                        for e_ts, e_sim in pos['end']:
                            if m_ts >= e_ts:
                                continue
                            src_dur = e_ts - s_ts
                            if src_dur <= 0:
                                continue
                            sr = scene_duration / src_dur
                            if not (MIN_SPEED <= sr <= MAX_SPEED):
                                continue
                            score = s_sim + m_sim + e_sim
                            if score > best_interval_score:
                                best_interval_score = score
                                best_interval = (s_ts, e_ts, sr)

            if best_interval is None and pos['start'] and pos['end']:
                for s_ts, s_sim in pos['start']:
                    for e_ts, e_sim in pos['end']:
                        if s_ts >= e_ts:
                            continue
                        src_dur = e_ts - s_ts
                        sr = scene_duration / src_dur
                        if not (MIN_SPEED <= sr <= MAX_SPEED):
                            continue
                        score = s_sim + e_sim
                        if score > best_interval_score:
                            best_interval_score = score
                            best_interval = (s_ts, e_ts, sr)

            if best_interval is not None:
                start_time, end_time, speed_ratio = best_interval
            else:
                # No ordered pair/triple passes the speed bounds. Project from
                # the single best-similarity candidate, centering a scene-length
                # interval on it. Keeps speed_ratio honestly at 1.0.
                all_ts_sim: list[tuple[float, float]] = []
                for p in ('start', 'middle', 'end'):
                    all_ts_sim.extend(pos[p])
                if not all_ts_sim:
                    continue
                best_ts, _ = max(all_ts_sim, key=lambda x: x[1])
                start_time = max(0.0, best_ts - scene_duration / 2)
                end_time = start_time + scene_duration
                speed_ratio = 1.0

            # Score: vote_count * 10 + avg_similarity (favor more votes)
            score = vote_count * 10 + avg_similarity
            weighted_avg_alts.append((score, AlternativeMatch(
                episode=episode,
                start_time=max(0.0, start_time),
                end_time=end_time,
                confidence=avg_similarity,
                speed_ratio=speed_ratio,
                vote_count=vote_count,
                algorithm='weighted_avg',
            )))

        # Sort by score and take top 3
        weighted_avg_alts.sort(key=lambda x: -x[0])
        for score, alt in weighted_avg_alts[:3]:
            if alt.episode not in seen_weighted_avg:
                alternatives.append(alt)
                seen_weighted_avg.add(alt.episode)

        # ============ Algorithm 2: Best Frame Winner (up to 2) ============
        # Take the single highest-confidence match from each frame position
        seen_best_frame: set[str] = set()
        best_frame_alts: list[tuple[float, AlternativeMatch]] = []

        for position, candidates in all_candidates:
            if not candidates:
                continue
            # Get the best candidate from this position
            best = max(candidates, key=lambda c: c.similarity)

            # Estimate timing based on position
            if position == 'start':
                start_time = best.timestamp
                end_time = best.timestamp + scene_duration
            elif position == 'middle':
                start_time = best.timestamp - scene_duration / 2
                end_time = best.timestamp + scene_duration / 2
            else:  # end
                start_time = best.timestamp - scene_duration
                end_time = best.timestamp

            clamped_start = max(0.0, start_time)
            source_duration = max(1e-3, end_time - clamped_start)
            best_frame_alts.append((best.similarity, AlternativeMatch(
                episode=best.episode,
                start_time=clamped_start,
                end_time=end_time,
                confidence=best.similarity,
                speed_ratio=scene_duration / source_duration,
                vote_count=1,
                algorithm='best_frame',
            )))

        # Sort by similarity and take top 2 unique episodes
        best_frame_alts.sort(key=lambda x: -x[0])
        bf_added = 0
        for sim, alt in best_frame_alts:
            if alt.episode not in seen_best_frame and bf_added < 2:
                alternatives.append(alt)
                seen_best_frame.add(alt.episode)
                bf_added += 1

        # ============ Algorithm 3: Union of Top-K (up to 2) ============
        # Pool all candidates and take top K by raw similarity
        seen_union_topk: set[str] = set()
        all_pooled = []
        for position, candidates in all_candidates:
            for c in candidates:
                all_pooled.append((position, c))

        # Sort by similarity
        all_pooled.sort(key=lambda x: -x[1].similarity)

        utk_added = 0
        for position, c in all_pooled:
            if c.episode not in seen_union_topk and utk_added < 2:
                # Estimate timing
                if position == 'start':
                    start_time = c.timestamp
                    end_time = c.timestamp + scene_duration
                elif position == 'middle':
                    start_time = c.timestamp - scene_duration / 2
                    end_time = c.timestamp + scene_duration / 2
                else:
                    start_time = c.timestamp - scene_duration
                    end_time = c.timestamp

                clamped_start = max(0.0, start_time)
                source_duration = max(1e-3, end_time - clamped_start)
                alternatives.append(AlternativeMatch(
                    episode=c.episode,
                    start_time=clamped_start,
                    end_time=end_time,
                    confidence=c.similarity,
                    speed_ratio=scene_duration / source_duration,
                    vote_count=1,
                    algorithm='union_topk',
                ))
                seen_union_topk.add(c.episode)
                utk_added += 1

        # Deduplicate alternatives sharing identical (start_time, end_time):
        # the three algorithms independently propose intervals and routinely
        # converge on the same boundaries. Keep the highest-confidence entry
        # per interval so reviewers don't wade through redundant candidates.
        dedup: dict[tuple[float, float], AlternativeMatch] = {}
        for alt in alternatives:
            key = (alt.start_time, alt.end_time)
            existing = dedup.get(key)
            if existing is None or alt.confidence > existing.confidence:
                dedup[key] = alt
        alternatives = list(dedup.values())

        # Final sort: weighted_avg first, then best_frame, then union_topk
        # Within each algorithm, sort by confidence
        algorithm_order = {'weighted_avg': 0, 'best_frame': 1, 'union_topk': 2}
        alternatives.sort(key=lambda x: (algorithm_order.get(x.algorithm, 99), -x.confidence))

        return alternatives[:7]

    @classmethod
    async def match_scenes(
        cls,
        video_path: Path,
        scenes: SceneList,
        library_path: Path,
        library_type: LibraryType | str,
        anime_name: str | None = None,
        scene_indices_to_match: list[int] | None = None,
        existing_matches: MatchList | None = None,
        pass_label: str = "",
    ) -> AsyncIterator[MatchProgress]:
        """
        Match all scenes in a video to anime source episodes.

        Args:
            video_path: Path to the TikTok video
            scenes: List of detected scenes
            library_path: Path to the indexed anime library
            anime_name: Optional anime name to filter search results
            scene_indices_to_match: If set, only match these scene indices
            existing_matches: Pre-existing matches to copy for skipped scenes
            pass_label: Optional prefix for progress messages (e.g. "Pass 1: ")

        Yields:
            MatchProgress objects with status updates
        """
        total_scenes = len(scenes.scenes)
        scenes_to_process = (
            len(scene_indices_to_match) if scene_indices_to_match is not None
            else total_scenes
        )
        prefix = f"{pass_label}" if pass_label else ""

        yield MatchProgress(
            "starting",
            0,
            f"{prefix}Initializing matcher for {scenes_to_process} scenes...",
            0,
            total_scenes,
        )

        # Initialize searcher in thread pool
        loop = asyncio.get_event_loop()
        init_success = await loop.run_in_executor(
            None, cls._init_searcher, library_path, library_type, anime_name
        )

        if not init_success:
            yield MatchProgress(
                "error",
                0,
                "",
                error="Failed to initialize anime_searcher. Check library path and model.",
            )
            return

        matches = MatchList()
        processed_count = 0

        for i, scene in enumerate(scenes.scenes):
            # Skip scenes not in the target list
            if scene_indices_to_match is not None and i not in scene_indices_to_match:
                # Copy existing match
                if existing_matches and i < len(existing_matches.matches):
                    match_copy = existing_matches.matches[i].model_copy()
                    match_copy.scene_index = scene.index
                    matches.matches.append(match_copy)
                else:
                    matches.matches.append(SceneMatch(
                        scene_index=scene.index,
                        episode="",
                        start_time=0,
                        end_time=0,
                        confidence=0,
                        speed_ratio=1.0,
                        was_no_match=True,
                    ))
                continue

            processed_count += 1
            yield MatchProgress(
                "matching",
                processed_count / scenes_to_process,
                f"{prefix}Matching scene {processed_count}/{scenes_to_process}",
                i + 1,
                total_scenes,
            )

            try:
                # Extract frames at start, middle, end of scene
                # Add 125ms offset from boundaries to avoid transition artifacts
                FRAME_OFFSET = 0.125  # 125ms offset from scene boundaries

                scene_duration = scene.end_time - scene.start_time
                # Ensure we have enough duration for offset
                safe_offset = min(FRAME_OFFSET, scene_duration / 4)

                start_time = scene.start_time + safe_offset
                middle_time = (scene.start_time + scene.end_time) / 2
                end_time = scene.end_time - safe_offset

                # Run frame extraction in one capture pass.
                start_frame, middle_frame, end_frame = await loop.run_in_executor(
                    None,
                    cls.extract_frames,
                    video_path,
                    [start_time, middle_time, end_time],
                )

                if not all([start_frame, middle_frame, end_frame]):
                    # Create empty match for this scene
                    matches.matches.append(
                        SceneMatch(
                            scene_index=scene.index,
                            episode="",
                            start_time=0,
                            end_time=0,
                            confidence=0,
                            speed_ratio=1.0,
                            was_no_match=True,
                        )
                    )
                    continue

                # Retrieve deep top-K for the triple search so recycled animation
                # doesn't silently bury the correct reference frame below rank 5.
                # Alternatives are computed from the top-5 slice to keep their
                # noise profile stable.
                # Flip augmentation is off: anime isn't broadcast mirrored, so
                # mirroring only adds symmetry-coincidence false positives.
                search_batch = partial(
                    cls._search_image_batch,
                    [start_frame, middle_frame, end_frame],
                    top_n=25,
                    threshold=None,
                    flip=False,
                    series=anime_name,
                )
                start_results, middle_results, end_results = await loop.run_in_executor(
                    None,
                    search_batch,
                )

                # Convert to MatchCandidate objects
                def to_candidates(results) -> list[MatchCandidate]:
                    return [
                        MatchCandidate(
                            episode=r.episode,
                            timestamp=r.timestamp,
                            similarity=r.similarity,
                            series=r.series,
                        )
                        for r in results
                    ]

                start_candidates = to_candidates(start_results)
                middle_candidates = to_candidates(middle_results)
                end_candidates = to_candidates(end_results)

                # Find temporal match across the deep candidate pool.
                match = cls._find_temporal_match(
                    start_candidates,
                    middle_candidates,
                    end_candidates,
                    scene.duration,
                )

                # Alternatives operate on the top-5 slice per position.
                alt_start = start_candidates[:5]
                alt_middle = middle_candidates[:5]
                alt_end = end_candidates[:5]

                if match:
                    # Refine boundaries to native-frame precision (addresses the
                    # 2-FPS index Nyquist floor — the dominant timing failure).
                    refined = await loop.run_in_executor(
                        None,
                        cls._refine_boundaries,
                        video_path,
                        scene,
                        match.episode,
                        match.start_time,
                        match.end_time,
                        library_type,
                    )
                    if refined is not None:
                        refined_start, refined_end = refined
                        refined_duration = refined_end - refined_start
                        if refined_duration > 0:
                            match.start_time = refined_start
                            match.end_time = refined_end
                            match.speed_ratio = scene.duration / refined_duration

                    match.scene_index = scene.index
                    match.start_candidates = start_candidates
                    match.middle_candidates = middle_candidates
                    match.end_candidates = end_candidates
                    match.alternatives = cls._compute_alternatives(
                        alt_start,
                        alt_middle,
                        alt_end,
                        scene.duration,
                    )
                    matches.matches.append(match)
                else:
                    # No match found - compute alternatives for manual selection
                    alternatives = cls._compute_alternatives(
                        alt_start,
                        alt_middle,
                        alt_end,
                        scene.duration,
                    )
                    matches.matches.append(
                        SceneMatch(
                            scene_index=scene.index,
                            episode="",
                            start_time=0,
                            end_time=0,
                            confidence=0,
                            speed_ratio=1.0,
                            was_no_match=True,
                            alternatives=alternatives,
                            start_candidates=start_candidates,
                            middle_candidates=middle_candidates,
                            end_candidates=end_candidates,
                        )
                    )

            except Exception as e:
                # Store error match but continue processing
                matches.matches.append(
                    SceneMatch(
                        scene_index=scene.index,
                        episode="",
                        start_time=0,
                        end_time=0,
                        confidence=0,
                        speed_ratio=1.0,
                        was_no_match=True,
                    )
                )
                print(f"Error matching scene {i}: {e}")

        yield MatchProgress(
            "complete",
            1.0,
            f"Matched {len(matches.matches)} scenes",
            total_scenes,
            total_scenes,
            matches,
        )
