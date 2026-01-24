"""Anime source matching service using anime_searcher module."""

import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import cv2
from PIL import Image

from ..config import settings
from ..models import AlternativeMatch, MatchCandidate, MatchList, SceneMatch, SceneList


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

    @classmethod
    def _init_searcher(cls, library_path: Path) -> bool:
        """
        Initialize the anime_searcher components.

        Args:
            library_path: Path to the anime library with index

        Returns:
            True if initialization succeeded
        """
        # Add anime_searcher to path if needed
        searcher_path = settings.anime_searcher_path / "anime_searcher"
        if str(searcher_path.parent) not in sys.path:
            sys.path.insert(0, str(searcher_path.parent))

        # Skip if already loaded for same library
        if cls._loaded_library_path == library_path and cls._query_processor is not None:
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
            cls._embedder = SSCDEmbedder(model_path)
            cls._query_processor = QueryProcessor(cls._index_manager, cls._embedder)
            cls._loaded_library_path = library_path

            return True

        except Exception as e:
            print(f"Failed to initialize anime_searcher: {e}")
            return False

    @staticmethod
    def extract_frame(video_path: Path, timestamp: float) -> Image.Image | None:
        """
        Extract a single frame from a video at the given timestamp.

        Args:
            video_path: Path to the video file
            timestamp: Time in seconds

        Returns:
            PIL Image or None if extraction failed
        """
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
        70% and 160% of original speed.

        Args:
            start_candidates: Top 5 matches for scene start frame
            middle_candidates: Top 5 matches for scene middle frame
            end_candidates: Top 5 matches for scene end frame
            scene_duration: Duration of the scene in the TikTok

        Returns:
            SceneMatch if a consistent match is found, None otherwise
        """
        MIN_SPEED = 0.70  # 70% - slowed down
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

                    # Calculate confidence based on similarities and temporal consistency
                    avg_similarity = (
                        start.similarity + middle.similarity + end.similarity
                    ) / 3

                    # Bonus for middle frame being roughly in the middle
                    expected_middle = start.timestamp + source_duration / 2
                    actual_middle = middle.timestamp
                    middle_deviation = abs(actual_middle - expected_middle) / source_duration
                    temporal_bonus = max(0, 1 - middle_deviation * 2) * 0.1

                    confidence = avg_similarity + temporal_bonus

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
        
        # ============ Algorithm 1: Weighted Average (up to 3) ============
        # Aggregate votes by episode across all frame positions
        seen_weighted_avg: set[str] = set()
        episode_votes: dict[str, dict] = defaultdict(lambda: {
            'total_similarity': 0.0,
            'vote_count': 0,
            'timestamps': [],
        })
        
        all_candidates = [
            ('start', start_candidates),
            ('middle', middle_candidates),
            ('end', end_candidates),
        ]
        
        for position, candidates in all_candidates:
            for candidate in candidates:
                ep = candidate.episode
                episode_votes[ep]['total_similarity'] += candidate.similarity
                episode_votes[ep]['vote_count'] += 1
                episode_votes[ep]['timestamps'].append((position, candidate.timestamp))
        
        weighted_avg_alts: list[tuple[float, AlternativeMatch]] = []
        for episode, data in episode_votes.items():
            if data['vote_count'] == 0:
                continue
                
            avg_similarity = data['total_similarity'] / data['vote_count']
            timestamps = sorted(data['timestamps'], key=lambda x: x[1])
            
            # Estimate start/end times from available timestamps
            min_ts = min(t[1] for t in timestamps)
            max_ts = max(t[1] for t in timestamps)
            
            if max_ts - min_ts > 0.5:
                start_time = min_ts
                end_time = max_ts
            else:
                mid_ts = (min_ts + max_ts) / 2
                start_time = mid_ts - scene_duration / 2
                end_time = mid_ts + scene_duration / 2
                
            source_duration = end_time - start_time
            speed_ratio = scene_duration / source_duration if source_duration > 0 else 1.0
            
            # Score: vote_count * 10 + avg_similarity (favor more votes)
            score = data['vote_count'] * 10 + avg_similarity
            weighted_avg_alts.append((score, AlternativeMatch(
                episode=episode,
                start_time=max(0, start_time),
                end_time=end_time,
                confidence=avg_similarity,
                speed_ratio=speed_ratio,
                vote_count=data['vote_count'],
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
            
            best_frame_alts.append((best.similarity, AlternativeMatch(
                episode=best.episode,
                start_time=max(0, start_time),
                end_time=end_time,
                confidence=best.similarity,
                speed_ratio=1.0,  # Assuming 1:1 speed since we estimate from duration
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
                
                alternatives.append(AlternativeMatch(
                    episode=c.episode,
                    start_time=max(0, start_time),
                    end_time=end_time,
                    confidence=c.similarity,
                    speed_ratio=1.0,
                    vote_count=1,
                    algorithm='union_topk',
                ))
                seen_union_topk.add(c.episode)
                utk_added += 1
        
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
        anime_name: str | None = None,
    ) -> AsyncIterator[MatchProgress]:
        """
        Match all scenes in a video to anime source episodes.

        Args:
            video_path: Path to the TikTok video
            scenes: List of detected scenes
            library_path: Path to the indexed anime library
            anime_name: Optional anime name to filter search results

        Yields:
            MatchProgress objects with status updates
        """
        total_scenes = len(scenes.scenes)
        yield MatchProgress(
            "starting",
            0,
            f"Initializing matcher for {total_scenes} scenes...",
            0,
            total_scenes,
        )

        # Initialize searcher in thread pool
        loop = asyncio.get_event_loop()
        init_success = await loop.run_in_executor(
            None, cls._init_searcher, library_path
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

        for i, scene in enumerate(scenes.scenes):
            yield MatchProgress(
                "matching",
                i / total_scenes,
                f"Matching scene {i + 1}/{total_scenes}",
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

                # Run frame extraction in thread pool
                frames = await loop.run_in_executor(
                    None,
                    lambda: (
                        cls.extract_frame(video_path, start_time),
                        cls.extract_frame(video_path, middle_time),
                        cls.extract_frame(video_path, end_time),
                    ),
                )

                start_frame, middle_frame, end_frame = frames

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
                        )
                    )
                    continue

                # Search for each frame
                def search_frames():
                    start_results = cls._query_processor.search_image(
                        start_frame, top_n=5, flip=True, series=anime_name
                    )
                    middle_results = cls._query_processor.search_image(
                        middle_frame, top_n=5, flip=True, series=anime_name
                    )
                    end_results = cls._query_processor.search_image(
                        end_frame, top_n=5, flip=True, series=anime_name
                    )
                    return start_results, middle_results, end_results

                start_results, middle_results, end_results = await loop.run_in_executor(
                    None, search_frames
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

                # Find temporal match
                match = cls._find_temporal_match(
                    start_candidates,
                    middle_candidates,
                    end_candidates,
                    scene.duration,
                )

                if match:
                    match.scene_index = scene.index
                    match.start_candidates = start_candidates
                    match.middle_candidates = middle_candidates
                    match.end_candidates = end_candidates
                    # Also compute alternatives for matched scenes (for editing)
                    match.alternatives = cls._compute_alternatives(
                        start_candidates,
                        middle_candidates,
                        end_candidates,
                        scene.duration,
                    )
                    matches.matches.append(match)
                else:
                    # No match found - compute alternatives for manual selection
                    alternatives = cls._compute_alternatives(
                        start_candidates,
                        middle_candidates,
                        end_candidates,
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
