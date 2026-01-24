"""Anime source matching service using anime_searcher module."""

import asyncio
import sys
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
    ) -> tuple[SceneMatch | None, list[AlternativeMatch]]:
        """
        Find a temporally consistent match across start/middle/end candidates.
        Returns the best match and up to 5 alternative matches using Weighted Voting.

        Weighted Voting Algorithm:
        - Score = sum of (similarity * position_weight) for each frame position
        - Position weights: start=1.0, middle=0.8, end=1.0 (edges more important)
        - Vote count = number of frame positions that include this episode
        - Alternatives ranked by score, with vote_count as tiebreaker

        Args:
            start_candidates: Top 5 matches for scene start frame
            middle_candidates: Top 5 matches for scene middle frame
            end_candidates: Top 5 matches for scene end frame
            scene_duration: Duration of the scene in the TikTok

        Returns:
            Tuple of (best match or None, list of up to 5 alternative matches)
        """
        MIN_SPEED = 0.70  # 70% - slowed down
        MAX_SPEED = 1.60  # 160% - sped up

        # Collect all valid temporal matches
        all_matches: list[tuple[float, int, SceneMatch]] = []  # (score, vote_count, match)

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

                    # Weighted Voting: edges (start/end) weighted more than middle
                    # Position weights: start=1.0, middle=0.8, end=1.0
                    weighted_score = (
                        start.similarity * 1.0 +
                        middle.similarity * 0.8 +
                        end.similarity * 1.0
                    ) / 2.8  # Normalize to 0-1 range

                    # Bonus for middle frame being roughly in the middle
                    expected_middle = start.timestamp + source_duration / 2
                    actual_middle = middle.timestamp
                    middle_deviation = abs(actual_middle - expected_middle) / source_duration
                    temporal_bonus = max(0, 1 - middle_deviation * 2) * 0.1

                    confidence = weighted_score + temporal_bonus

                    # Count how many frame positions voted for this episode
                    vote_count = 3  # All three frames match this episode

                    match = SceneMatch(
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
                    all_matches.append((confidence, vote_count, match))

        # Sort by confidence (desc), then vote_count (desc)
        all_matches.sort(key=lambda x: (x[0], x[1]), reverse=True)

        # Take top 5 unique episodes for alternatives
        alternatives: list[AlternativeMatch] = []
        seen_episodes: set[str] = set()
        
        for confidence, vote_count, match in all_matches:
            if match.episode not in seen_episodes:
                seen_episodes.add(match.episode)
                alternatives.append(
                    AlternativeMatch(
                        episode=match.episode,
                        start_time=match.start_time,
                        end_time=match.end_time,
                        confidence=confidence,
                        speed_ratio=match.speed_ratio,
                        vote_count=vote_count,
                    )
                )
                if len(alternatives) >= 5:
                    break

        # Best match is the first one (if any)
        best_match = all_matches[0][2] if all_matches else None

        return best_match, alternatives

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
                # Use 100ms (3 frames at 30fps) offset from boundaries to avoid
                # scene transition artifacts (cross-fades, motion blur, detection errors)
                FRAME_OFFSET = 0.10  # 100ms offset from scene boundaries
                
                start_time = scene.start_time + FRAME_OFFSET
                middle_time = (scene.start_time + scene.end_time) / 2
                end_time = scene.end_time - FRAME_OFFSET

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
                match, alternatives = cls._find_temporal_match(
                    start_candidates,
                    middle_candidates,
                    end_candidates,
                    scene.duration,
                )

                if match:
                    match.scene_index = scene.index
                    match.alternatives = alternatives
                    match.start_candidates = start_candidates
                    match.middle_candidates = middle_candidates
                    match.end_candidates = end_candidates
                    matches.matches.append(match)
                else:
                    # No match found - store candidates and alternatives for manual selection
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
