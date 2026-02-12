"""Gap Resolution Service for extending clips that hit the 75% speed floor.

This service handles:
1. Detecting which scenes have gaps (speed < 75% required)
2. Running pyscenedetect on source anime episodes to find cut points
3. Generating AI candidates for extending clips to fill gaps
4. Caching scene cut data per episode

NOTE: Uses OTIOTimingCalculator for frame-perfect precision, ensuring
consistent speed calculations that match the Premiere Pro JSX output.
"""

import asyncio
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from scenedetect import open_video, SceneManager, ContentDetector

from ..config import settings
from ..models import SceneMatch
from ..utils.timing import compute_adjusted_scene_end_times
from ..utils.subprocess_runner import CommandTimeoutError, run_command
from .anime_library import AnimeLibraryService
from .otio_timing import OTIOTimingCalculator, FrameRateInfo


@dataclass
class GapInfo:
    """Information about a scene that has a gap (hit 75% speed floor).

    Uses Fraction-based arithmetic internally for frame-perfect precision,
    matching the OTIOTimingCalculator used in Premiere Pro JSX generation.
    """

    scene_index: int
    # Current match data
    episode: str
    current_start: float  # Current source start time (seconds)
    current_end: float    # Current source end time (seconds)
    current_duration: float  # Current source duration (seconds)

    # Timeline data (from TTS transcription) - frame-snapped at 60fps
    timeline_start: float  # Frame-snapped timeline start (seconds)
    timeline_end: float    # Frame-snapped timeline end (seconds)
    target_duration: float  # How long the clip needs to be on timeline (seconds)

    # Gap calculations - use Fraction for precision
    required_speed: Fraction  # Speed that would be needed (< 0.75)
    effective_speed: Fraction  # Capped at 0.75
    gap_duration: float    # Duration of gap in seconds

    def to_dict(self) -> dict:
        return {
            "scene_index": self.scene_index,
            "episode": self.episode,
            "current_start": round(self.current_start, 6),  # More precision
            "current_end": round(self.current_end, 6),
            "current_duration": round(self.current_duration, 6),
            "timeline_start": round(self.timeline_start, 6),
            "timeline_end": round(self.timeline_end, 6),
            "target_duration": round(self.target_duration, 6),
            "required_speed": round(float(self.required_speed), 6),
            "effective_speed": round(float(self.effective_speed), 6),
            "gap_duration": round(self.gap_duration, 6),
        }


@dataclass
class GapCandidate:
    """A candidate for extending a clip to fill a gap.

    Uses Fraction-based arithmetic for effective_speed to match OTIO precision.
    """

    start_time: float  # Proposed source start time (seconds)
    end_time: float    # Proposed source end time (seconds)
    duration: float    # Proposed source duration (seconds)
    effective_speed: Fraction  # Speed with this timing (Fraction for precision)
    speed_diff: float  # Difference from 100% (for ranking, lower is better)
    extend_type: str   # 'extend_start', 'extend_end', 'extend_both'
    snap_description: str  # Human-readable description of what scene cuts were used

    def to_dict(self) -> dict:
        return {
            "start_time": round(self.start_time, 6),
            "end_time": round(self.end_time, 6),
            "duration": round(self.duration, 6),
            "effective_speed": round(float(self.effective_speed), 6),
            "speed_diff": round(self.speed_diff, 6),
            "extend_type": self.extend_type,
            "snap_description": self.snap_description,
        }


@dataclass
class GapResolutionProgress:
    """Progress information for gap resolution operations."""

    status: str  # 'starting', 'detecting', 'complete', 'error'
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    gaps: list[GapInfo] | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "gaps": [g.to_dict() for g in self.gaps] if self.gaps else None,
        }


class GapResolutionService:
    """Service for resolving gaps in clips that hit the 75% speed floor.

    Uses OTIOTimingCalculator for frame-perfect precision, ensuring
    consistent results that match the Premiere Pro JSX automation.
    """

    # Speed constraints as Fractions for exact comparison
    MIN_SPEED = Fraction(75, 100)  # 0.75
    MAX_SPEED = Fraction(160, 100)  # 1.60
    TARGET_SPEED = Fraction(1, 1)  # 1.0

    # Scene detection settings for source anime
    SCENE_THRESHOLD = 27.0
    MIN_SCENE_LEN = 10  # Frames

    # Safety frames offset (number of frames to stay away from scene boundaries)
    SAFETY_FRAMES = 3
    DEFAULT_FPS = Fraction(24000, 1001)  # 23.976fps as exact fraction

    # Prevent stampedes when many gap cards request candidates at once.
    # 1) `_scene_cut_inflight` deduplicates concurrent detection for same episode.
    # 2) `_scene_cut_semaphore` limits concurrent heavy scene detections globally.
    _scene_cut_inflight: dict[str, asyncio.Task[list[float]]] = {}
    _scene_cut_inflight_lock = asyncio.Lock()
    _scene_cut_semaphore = asyncio.Semaphore(2)

    # FPS cache: avoids redundant ffprobe calls for the same video file.
    _fps_cache: dict[str, Fraction] = {}

    # Default timeline rate (60fps for TikTok)
    TIMELINE_RATE = FrameRateInfo(timebase=60, ntsc=False)
    # Default source rate (23.976fps for most anime)
    SOURCE_RATE = FrameRateInfo(timebase=24, ntsc=True)

    @classmethod
    async def detect_video_fps(cls, video_path: Path) -> Fraction:
        """Detect video frame rate using ffprobe, returning as a Fraction for precision.

        Results are cached by resolved path to avoid redundant ffprobe subprocesses
        (e.g. multiple gaps referencing the same episode).

        Args:
            video_path: Path to video file

        Returns:
            Frame rate as a Fraction (e.g., Fraction(24000, 1001) for 23.976fps)
        """
        cache_key = str(video_path.resolve())
        if cache_key in cls._fps_cache:
            return cls._fps_cache[cache_key]

        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]

        try:
            result = await run_command(cmd, timeout_seconds=30.0)
        except (CommandTimeoutError, FileNotFoundError):
            result = Fraction(24, 1)
        else:
            if result.returncode != 0:
                result = Fraction(24, 1)
            else:
                fps_str = result.stdout.decode().strip()
                if "/" in fps_str:
                    num, den = fps_str.split("/")
                    result = Fraction(int(num), int(den))
                else:
                    fps_float = float(fps_str)
                    if abs(fps_float - 23.976) < 0.01:
                        result = Fraction(24000, 1001)
                    elif abs(fps_float - 29.97) < 0.01:
                        result = Fraction(30000, 1001)
                    elif abs(fps_float - 59.94) < 0.01:
                        result = Fraction(60000, 1001)
                    else:
                        result = Fraction(int(round(fps_float)), 1)

        cls._fps_cache[cache_key] = result
        return result

    @classmethod
    async def get_frame_offset(cls, video_path: Path) -> float:
        """Get the safety frame offset in seconds based on video FPS.

        We use 3 frames at source FPS to avoid accidentally including
        frames from adjacent scenes or transitions.

        Args:
            video_path: Path to the video file to detect FPS from

        Returns:
            Frame offset in seconds as float
        """
        try:
            fps_fraction = await cls.detect_video_fps(video_path)
            fps = float(fps_fraction)
        except Exception:
            fps = float(cls.DEFAULT_FPS)

        return float(cls.SAFETY_FRAMES / fps)

    @classmethod
    def resolve_episode_path(cls, episode_name: str) -> Path | None:
        """Resolve an episode name to its full path in the anime library.

        Args:
            episode_name: Episode name (e.g., "[9volt] Hanebado! - 03 [D0B8F455]")
                         or a full path

        Returns:
            Full path to the episode file, or None if not found.
        """
        return AnimeLibraryService.resolve_episode_path(episode_name)

    @classmethod
    def get_scene_cache_path(cls, episode_path: str) -> Path:
        """Get the cache file path for scene cuts of an episode."""
        # Hash the episode path to create a unique cache filename
        import hashlib
        path_hash = hashlib.md5(episode_path.encode()).hexdigest()[:16]
        episode_name = Path(episode_path).stem
        return settings.cache_dir / "scene_cuts" / f"{episode_name}_{path_hash}.json"

    @classmethod
    def load_cached_scene_cuts(cls, episode_path: str) -> list[float] | None:
        """Load cached scene cut times for an episode.

        Returns:
            List of cut times in seconds, or None if cache doesn't exist.
        """
        cache_path = cls.get_scene_cache_path(episode_path)
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                return data.get("cuts", [])
            except (json.JSONDecodeError, KeyError):
                return None
        return None

    @classmethod
    def save_scene_cuts_cache(cls, episode_path: str, cuts: list[float]) -> None:
        """Save scene cut times to cache."""
        cache_path = cls.get_scene_cache_path(episode_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "episode_path": episode_path,
            "cuts": cuts,
        }
        cache_path.write_text(json.dumps(data, indent=2))

    @classmethod
    async def detect_scene_cuts(
        cls,
        episode_path: str,
        threshold: float | None = None,
        min_scene_len: int | None = None,
    ) -> list[float]:
        """Detect scene cuts in an anime episode.

        Uses cache if available, otherwise runs pyscenedetect.

        Args:
            episode_path: Path to the video file
            threshold: ContentDetector threshold (optional)
            min_scene_len: Minimum scene length in frames (optional)

        Returns:
            List of cut times in seconds (start of each scene)
        """
        # Check cache first
        cached = cls.load_cached_scene_cuts(episode_path)
        if cached is not None:
            return cached

        abs_episode_key = str(Path(episode_path).resolve())
        threshold_val = threshold or cls.SCENE_THRESHOLD
        min_scene_len_val = min_scene_len or cls.MIN_SCENE_LEN

        async with cls._scene_cut_inflight_lock:
            task = cls._scene_cut_inflight.get(abs_episode_key)
            if task is None:
                task = asyncio.create_task(
                    cls._detect_and_cache_scene_cuts(
                        episode_path=episode_path,
                        threshold=threshold_val,
                        min_scene_len=min_scene_len_val,
                    )
                )
                cls._scene_cut_inflight[abs_episode_key] = task

        try:
            return await task
        finally:
            if task.done():
                async with cls._scene_cut_inflight_lock:
                    current = cls._scene_cut_inflight.get(abs_episode_key)
                    if current is task:
                        cls._scene_cut_inflight.pop(abs_episode_key, None)

    @classmethod
    async def _detect_and_cache_scene_cuts(
        cls,
        episode_path: str,
        threshold: float,
        min_scene_len: int,
    ) -> list[float]:
        """Run scene detection once and persist cache for future requests."""
        loop = asyncio.get_event_loop()
        async with cls._scene_cut_semaphore:
            cuts = await loop.run_in_executor(
                None,
                cls._detect_scene_cuts_sync,
                episode_path,
                threshold,
                min_scene_len,
            )

        cls.save_scene_cuts_cache(episode_path, cuts)
        return cuts

    @staticmethod
    def _detect_scene_cuts_sync(
        video_path: str,
        threshold: float,
        min_scene_len: int,
    ) -> list[float]:
        """Synchronous scene cut detection using pyscenedetect."""
        video = open_video(video_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(
            ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
        )

        scene_manager.detect_scenes(video, show_progress=False)
        scene_list = scene_manager.get_scene_list()

        # Return the start time of each scene (these are the cut points)
        cuts = [0.0]  # Include start of video
        for start, end in scene_list:
            cuts.append(start.get_seconds())

        # Add video duration as final "cut"
        cuts.append(video.duration.get_seconds())

        return sorted(set(cuts))  # Remove duplicates and sort

    @classmethod
    def calculate_gaps(
        cls,
        matches: list[SceneMatch],
        scene_timings: list[dict],  # From transcription, each has words with start/end
    ) -> list[GapInfo]:
        """Calculate which scenes have gaps due to 75% speed floor.

        Uses Fraction-based arithmetic and frame-snapping for precision,
        matching the OTIOTimingCalculator used in Premiere Pro JSX generation.

        Args:
            matches: Scene matches with source timings
            scene_timings: Scene timings from TTS transcription (each has scene_index, words)

        Returns:
            List of GapInfo for scenes that have gaps
        """
        gaps = []

        # Create calculator for frame-perfect timing (same as processing.py)
        calculator = OTIOTimingCalculator(
            sequence_rate=cls.TIMELINE_RATE,
            source_rate=cls.SOURCE_RATE,
        )

        # Compute adjusted end times to eliminate gaps between scenes
        # Each scene's end is extended to the next scene's start
        adjusted_ends = compute_adjusted_scene_end_times(
            scenes=scene_timings,
            get_scene_index=lambda s: s.get("scene_index"),
            get_first_word_start=lambda s: s["words"][0]["start"] if s.get("words") else None,
            get_last_word_end=lambda s: s["words"][-1]["end"] if s.get("words") else None,
        )

        for match in matches:
            # Resolve match (fallback to best alternative if missing)
            episode = match.episode
            source_start = match.start_time
            source_end = match.end_time
            if not episode:
                alternative = next((alt for alt in match.alternatives if alt.episode), None)
                if alternative:
                    episode = alternative.episode
                    source_start = alternative.start_time
                    source_end = alternative.end_time
                else:
                    continue

            # Find corresponding scene timing
            scene_timing = next(
                (s for s in scene_timings if s.get("scene_index") == match.scene_index),
                None,
            )
            if not scene_timing or not scene_timing.get("words"):
                continue

            words = scene_timing["words"]
            timeline_start_raw = words[0]["start"]
            # Use adjusted end time to eliminate gaps between scenes
            timeline_end_raw = adjusted_ends.get(match.scene_index, words[-1]["end"])

            # Snap timeline positions to 60fps frame grid (matching processing.py)
            # This ensures we use the exact same values as JSX generation
            timeline_start_rt = calculator.seconds_to_timeline_time(timeline_start_raw)
            timeline_end_rt = calculator.seconds_to_timeline_time(timeline_end_raw)

            # Get frame-snapped seconds
            timeline_start_frames = int(timeline_start_rt.to_frames())
            timeline_end_frames = int(timeline_end_rt.to_frames())
            timeline_start = timeline_start_frames / 60.0  # Frame-snapped
            timeline_end = timeline_end_frames / 60.0  # Frame-snapped

            # Calculate target duration using Fraction for exact arithmetic
            target_duration_frac = Fraction(timeline_end).limit_denominator(100000) - \
                                   Fraction(timeline_start).limit_denominator(100000)
            target_duration = float(target_duration_frac)

            # Source timing from match (resolved)
            source_duration_frac = Fraction(source_end).limit_denominator(100000) - \
                                   Fraction(source_start).limit_denominator(100000)
            source_duration = float(source_duration_frac)

            # Calculate speed using Fraction (matching otio_timing.py logic)
            if target_duration_frac > 0:
                speed_ratio = source_duration_frac / target_duration_frac
            else:
                continue

            # Check if this scene hits the 75% floor
            if speed_ratio < cls.MIN_SPEED:
                # Calculate gap using Fraction arithmetic
                effective_speed = cls.MIN_SPEED
                actual_duration_frac = source_duration_frac / effective_speed
                gap_duration_frac = target_duration_frac - actual_duration_frac
                gap_duration = float(gap_duration_frac)

                gaps.append(GapInfo(
                    scene_index=match.scene_index,
                    episode=episode,
                    current_start=source_start,
                    current_end=source_end,
                    current_duration=source_duration,
                    timeline_start=timeline_start,
                    timeline_end=timeline_end,
                    target_duration=target_duration,
                    required_speed=speed_ratio,
                    effective_speed=effective_speed,
                    gap_duration=gap_duration,
                ))

        return gaps

    @classmethod
    async def generate_candidates(
        cls,
        gap: GapInfo,
        max_candidates: int = 6,
    ) -> list[GapCandidate]:
        """Generate AI candidates for extending a clip to fill a gap.

        Uses pyscenedetect to find nearby scene cuts and proposes timings
        that snap to these cuts. Candidates are ranked by closeness to 100% speed.

        Uses Fraction-based arithmetic for frame-perfect precision.

        Strategies:
        1. Push to current scene boundaries (if clip is inside a scene)
        2. Extend end to next scene cut
        3. Extend start to previous scene cut
        4. Extend both directions

        All candidates include a 2-3 frame safety offset to avoid
        accidentally including frames from transitions.

        Args:
            gap: Gap information for the scene
            max_candidates: Maximum number of candidates to return

        Returns:
            List of GapCandidate objects, sorted by speed_diff (closest to 100% first)
        """
        await AnimeLibraryService.ensure_episode_manifest()

        # Resolve episode path using indexed manifest (no recursive scan).
        episode_path = cls.resolve_episode_path(gap.episode)
        if not episode_path:
            manifest = await AnimeLibraryService.ensure_episode_manifest(force_refresh=True)
            episode_path = AnimeLibraryService.resolve_episode_path(gap.episode, manifest)
        if not episode_path:
            return []

        # Get scene cuts for this episode
        cuts = await cls.detect_scene_cuts(str(episode_path))

        if not cuts or len(cuts) < 2:
            return []

        current_start = gap.current_start
        current_end = gap.current_end
        # Use the Fraction-based target_duration from GapInfo
        target_duration_frac = Fraction(gap.target_duration).limit_denominator(100000)
        target_duration = gap.target_duration
        frame_offset = await cls.get_frame_offset(episode_path)

        candidates = []
        seen_timings = set()  # Avoid duplicates

        def add_candidate(new_start: float, new_end: float, extend_type: str, description: str) -> bool:
            """Helper to add a candidate if valid. Uses Fraction arithmetic."""
            # Use Fraction for precise duration calculation
            new_start_frac = Fraction(new_start).limit_denominator(100000)
            new_end_frac = Fraction(new_end).limit_denominator(100000)
            new_duration_frac = new_end_frac - new_start_frac
            new_duration = float(new_duration_frac)

            if new_duration <= 0:
                return False

            # Calculate speed using Fraction
            if target_duration_frac > 0:
                speed_frac = new_duration_frac / target_duration_frac
            else:
                speed_frac = Fraction(1, 1)

            # Check if speed is in valid range (using Fraction comparison)
            if speed_frac < cls.MIN_SPEED or speed_frac > cls.MAX_SPEED:
                return False

            timing_key = (round(new_start, 6), round(new_end, 6))
            if timing_key in seen_timings:
                return False
            seen_timings.add(timing_key)

            speed_diff = abs(float(speed_frac) - float(cls.TARGET_SPEED))
            candidates.append(GapCandidate(
                start_time=new_start,
                end_time=new_end,
                duration=new_duration,
                effective_speed=speed_frac,
                speed_diff=speed_diff,
                extend_type=extend_type,
                snap_description=description,
            ))
            return True

        # Find the scene that contains our current clip
        # A scene is defined by two consecutive cuts
        current_scene_start = None
        current_scene_end = None

        for i in range(len(cuts) - 1):
            scene_start = cuts[i]
            scene_end = cuts[i + 1]

            # Check if our clip is within this scene
            # (clip start and end are both within scene boundaries)
            if scene_start <= current_start and current_end <= scene_end:
                current_scene_start = scene_start
                current_scene_end = scene_end
                break

        # Strategy 0: Push to current scene boundaries (if inside a scene)
        # This is often the best option as it uses the full scene without crossing cuts
        if current_scene_start is not None and current_scene_end is not None:
            # Apply safety offset: add frames to start (move inward), subtract from end (move inward)
            safe_start = current_scene_start + frame_offset
            safe_end = current_scene_end - frame_offset

            if safe_start < current_start or safe_end > current_end:  # Only if we're extending
                add_candidate(
                    safe_start,
                    safe_end,
                    "fill_scene",
                    f"Fill current scene ({current_scene_end - current_scene_start:.2f}s scene)",
                )

            # Also try just extending to end of current scene
            if safe_end > current_end:
                add_candidate(
                    current_start,
                    safe_end,
                    "extend_to_scene_end",
                    f"Extend to scene end (+{safe_end - current_end:.2f}s)",
                )

            # And just extending to start of current scene
            if safe_start < current_start:
                add_candidate(
                    safe_start,
                    current_end,
                    "extend_to_scene_start",
                    f"Extend to scene start (-{current_start - safe_start:.2f}s)",
                )

        # Find cuts before and after current timing (for adjacent scenes)
        cuts_before = [c for c in cuts if c < current_start]
        cuts_after = [c for c in cuts if c > current_end]

        # Sort by distance from current position
        cuts_before.sort(key=lambda c: current_start - c)  # Closest first
        cuts_after.sort(key=lambda c: c - current_end)  # Closest first

        # Strategy 1: Extend end to next scene cut
        # When extending end: the cut marks the START of the next scene,
        # so we subtract offset to stay in current scene
        for cut_end in cuts_after[:5]:
            new_start = current_start
            new_end = cut_end - frame_offset  # Subtract to stay before the cut

            add_candidate(
                new_start,
                new_end,
                "extend_end",
                f"Extend end to next cut (+{new_end - current_end:.2f}s)",
            )

        # Strategy 2: Extend start to previous scene cut
        # When extending start: the cut marks the START of current scene,
        # so we add offset to stay in current scene
        for cut_start in cuts_before[:5]:
            new_start = cut_start + frame_offset  # Add to stay after the cut
            new_end = current_end

            add_candidate(
                new_start,
                new_end,
                "extend_start",
                f"Extend start to previous cut (-{current_start - new_start:.2f}s)",
            )

        # Strategy 3: Extend both directions to adjacent scene cuts
        for cut_start in cuts_before[:3]:
            for cut_end in cuts_after[:3]:
                new_start = cut_start + frame_offset  # Add to stay after the cut
                new_end = cut_end - frame_offset  # Subtract to stay before the cut

                add_candidate(
                    new_start,
                    new_end,
                    "extend_both",
                    f"Extend both (-{current_start - new_start:.2f}s, +{new_end - current_end:.2f}s)",
                )

        # Sort by speed_diff (closest to 100% first) and take top candidates
        candidates.sort(key=lambda c: c.speed_diff)

        # Fallback Strategy 4: If no candidates found, try to calculate exact timing for 100% speed
        # This extends outward (both backward and forward) to hit exactly 100%
        if not candidates:
            # To get 100% speed, we need source_duration = target_duration
            needed_duration = target_duration
            current_duration = current_end - current_start
            extra_needed = needed_duration - current_duration

            if extra_needed > 0:
                # Split the extra time: try extending backward from current start
                # and calculate how much forward extension would be needed for 100%

                # Try 1: Extend only backward (add all extra to start)
                new_start = current_start - extra_needed
                if new_start >= 0:  # Don't go before start of video
                    add_candidate(
                        new_start,
                        current_end,
                        "fallback_extend_start",
                        f"Extend start for 100% speed (-{extra_needed:.2f}s)",
                    )

                # Try 2: Extend only forward (add all extra to end)
                new_end = current_end + extra_needed
                add_candidate(
                    current_start,
                    new_end,
                    "fallback_extend_end",
                    f"Extend end for 100% speed (+{extra_needed:.2f}s)",
                )

                # Try 3: Extend both ways (split extra evenly)
                half_extra = extra_needed / 2
                new_start = current_start - half_extra
                new_end = current_end + half_extra
                if new_start >= 0:
                    add_candidate(
                        new_start,
                        new_end,
                        "fallback_extend_both",
                        f"Extend both for 100% speed (-{half_extra:.2f}s, +{half_extra:.2f}s)",
                    )

                # Sort fallback candidates by speed_diff too
                candidates.sort(key=lambda c: c.speed_diff)

        return candidates[:max_candidates]

    @classmethod
    def compute_speed_for_timing(
        cls,
        source_start: float,
        source_end: float,
        target_duration: float,
    ) -> Fraction:
        """Compute the effective speed for given timing using Fraction arithmetic.

        Uses the same precision as OTIOTimingCalculator for consistency.

        Args:
            source_start: Source in point (seconds)
            source_end: Source out point (seconds)
            target_duration: Target duration on timeline (seconds)

        Returns:
            Effective speed as Fraction (clamped to 0.75-1.60)
        """
        # Use Fraction for exact calculation
        source_start_frac = Fraction(source_start).limit_denominator(100000)
        source_end_frac = Fraction(source_end).limit_denominator(100000)
        target_frac = Fraction(target_duration).limit_denominator(100000)

        source_duration_frac = source_end_frac - source_start_frac

        if target_frac <= 0:
            return Fraction(1, 1)

        speed_frac = source_duration_frac / target_frac

        # Clamp to valid range using Fraction comparison
        if speed_frac < cls.MIN_SPEED:
            return cls.MIN_SPEED
        elif speed_frac > cls.MAX_SPEED:
            return cls.MAX_SPEED
        return speed_frac

    @classmethod
    def compute_raw_speed_for_timing(
        cls,
        source_start: float,
        source_end: float,
        target_duration: float,
    ) -> Fraction:
        """Compute the raw (unclamped) speed for given timing using Fraction arithmetic.

        Args:
            source_start: Source in point (seconds)
            source_end: Source out point (seconds)
            target_duration: Target duration on timeline (seconds)

        Returns:
            Raw speed as Fraction (may be outside 0.75-1.60 range)
        """
        # Use Fraction for exact calculation
        source_start_frac = Fraction(source_start).limit_denominator(100000)
        source_end_frac = Fraction(source_end).limit_denominator(100000)
        target_frac = Fraction(target_duration).limit_denominator(100000)

        source_duration_frac = source_end_frac - source_start_frac

        if target_frac <= 0:
            return Fraction(1, 1)

        return source_duration_frac / target_frac
