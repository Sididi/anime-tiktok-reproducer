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
import time
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from scenedetect import open_video, SceneManager, ContentDetector

from ..config import settings
from ..models import SceneMatch
from ..utils.media_binaries import is_media_binary_override_error
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
    speed_diff: float  # Legacy/debug metric: difference from 100% speed
    extend_type: str   # 'extend_start', 'extend_end', 'extend_both'
    snap_description: str  # Human-readable description of what scene cuts were used
    overlap_count: int = 0
    overlap_seconds: float = 0.0
    is_cut_aligned: bool = False
    is_clean: bool = True
    added_duration: float = 0.0
    detector_threshold: float | None = None
    direction_priority: int = 0
    clearance_side: str | None = None
    side_clearance_seconds: float | None = None
    continuation_priority: int = 1
    continuation_bias_applied: bool = False

    def to_dict(self) -> dict:
        return {
            "start_time": round(self.start_time, 6),
            "end_time": round(self.end_time, 6),
            "duration": round(self.duration, 6),
            "effective_speed": round(float(self.effective_speed), 6),
            "speed_diff": round(self.speed_diff, 6),
            "extend_type": self.extend_type,
            "snap_description": self.snap_description,
            "overlap_count": self.overlap_count,
            "overlap_seconds": round(self.overlap_seconds, 6),
            "is_cut_aligned": self.is_cut_aligned,
            "is_clean": self.is_clean,
            "added_duration": round(self.added_duration, 6),
            "detector_threshold": self.detector_threshold,
            "direction_priority": self.direction_priority,
            "clearance_side": self.clearance_side,
            "side_clearance_seconds": (
                None
                if self.side_clearance_seconds is None
                else round(self.side_clearance_seconds, 6)
            ),
            "continuation_priority": self.continuation_priority,
            "continuation_bias_applied": self.continuation_bias_applied,
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


@dataclass(frozen=True)
class _AutoFillState:
    """Internal candidate state used by overlap-aware autofill DP."""

    scene_index: int
    episode_key: str
    start_time: float
    end_time: float
    cut_penalty: int
    continuation_penalty: int
    added_duration_micro: int
    candidate_rank: int
    candidate: GapCandidate | None


@dataclass(frozen=True)
class _NeighborWindow:
    """Immediate adjacent source occupancy for overlap-aware candidate ranking."""

    relation: str
    start_time: float
    end_time: float
    tolerance: float


@dataclass(frozen=True)
class _NeighborContext:
    """Neighbor windows used to prefer clean candidate directions."""

    previous: _NeighborWindow | None = None
    next: _NeighborWindow | None = None


@dataclass(frozen=True)
class _SideClearance:
    """Nearest occupied source window on one side of the current match."""

    has_blocker: bool
    clearance_seconds: float


@dataclass
class AutoFillSelectionResult:
    """Overlap-aware candidate selection output for autofill."""

    selected_candidates_by_scene: dict[int, GapCandidate]
    overlap_seconds_by_scene: dict[int, float]
    total_overlap_count: int
    total_overlap_seconds: float


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
    # Analyze every other frame for a large speedup with negligible UX impact.
    SCENE_DETECTION_FRAME_SKIP = 1

    # Safety frames offset (number of frames to stay away from scene boundaries)
    SAFETY_FRAMES = 3
    DEFAULT_FPS = Fraction(24000, 1001)  # 23.976fps as exact fraction

    # Prevent stampedes when many gap cards request candidates at once.
    # 1) `_scene_cut_inflight` deduplicates concurrent detection for same episode.
    # 2) `_scene_cut_semaphore` limits concurrent heavy scene detections globally.
    _scene_cut_inflight: dict[str, asyncio.Task[list[float]]] = {}
    _scene_cut_inflight_lock = asyncio.Lock()
    _scene_cut_semaphore = asyncio.Semaphore(6)
    _scene_cut_cache: dict[str, list[float]] = {}
    _candidate_batch_inflight: dict[str, asyncio.Task[dict[int, list["GapCandidate"]]]] = {}
    _candidate_batch_lock = asyncio.Lock()
    _candidate_batch_result_cache: OrderedDict[
        str,
        tuple[float, dict[int, list["GapCandidate"]]],
    ] = OrderedDict()

    # FPS cache: avoids redundant ffprobe calls for the same video file.
    _fps_cache: dict[str, Fraction] = {}

    # Default timeline rate (60fps for TikTok)
    TIMELINE_RATE = FrameRateInfo(timebase=60, ntsc=False)
    # Default source rate (23.976fps for most anime)
    SOURCE_RATE = FrameRateInfo(timebase=24, ntsc=True)
    OVERLAP_COST_SCALE = 1_000_000
    CONTINUATION_TIE_WINDOW = 0.15
    CANDIDATE_BATCH_CACHE_TTL_SECONDS = 15 * 60
    CANDIDATE_BATCH_CACHE_MAX_ENTRIES = 16

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
        except CommandTimeoutError:
            result = Fraction(24, 1)
        except FileNotFoundError as exc:
            if is_media_binary_override_error(exc):
                raise
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
    def _normalize_scene_cut_params(
        cls,
        threshold: float | None,
        min_scene_len: int | None,
        frame_skip: int | None,
    ) -> tuple[float, int, int]:
        """Normalize scene detection parameters used by caching/inflight dedupe."""
        threshold_val = cls.SCENE_THRESHOLD if threshold is None else float(threshold)
        min_scene_len_val = cls.MIN_SCENE_LEN if min_scene_len is None else int(min_scene_len)
        frame_skip_val = (
            cls.SCENE_DETECTION_FRAME_SKIP if frame_skip is None else int(frame_skip)
        )
        return threshold_val, min_scene_len_val, frame_skip_val

    @classmethod
    def _scene_cut_runtime_key(
        cls,
        episode_path: str,
        threshold: float,
        min_scene_len: int,
        frame_skip: int,
    ) -> str:
        resolved = str(Path(episode_path).resolve())
        return f"{resolved}|{threshold:.3f}|{min_scene_len}|{frame_skip}"

    @classmethod
    def _legacy_scene_cache_path(cls, episode_path: str) -> Path:
        """Get the legacy cache file path used before parameter-aware caching."""
        # Hash the episode path to create a unique cache filename
        import hashlib

        path_hash = hashlib.md5(episode_path.encode()).hexdigest()[:16]
        episode_name = Path(episode_path).stem
        return settings.cache_dir / "scene_cuts" / f"{episode_name}_{path_hash}.json"

    @classmethod
    def get_scene_cache_path(
        cls,
        episode_path: str,
        threshold: float | None = None,
        min_scene_len: int | None = None,
        frame_skip: int | None = None,
    ) -> Path:
        """Get the parameter-aware cache file path for scene cuts of an episode."""
        import hashlib

        threshold_val, min_scene_len_val, frame_skip_val = cls._normalize_scene_cut_params(
            threshold,
            min_scene_len,
            frame_skip,
        )
        path_hash = hashlib.md5(episode_path.encode()).hexdigest()[:16]
        params_hash = hashlib.md5(
            f"{threshold_val:.3f}|{min_scene_len_val}|{frame_skip_val}".encode()
        ).hexdigest()[:8]
        episode_name = Path(episode_path).stem
        return (
            settings.cache_dir
            / "scene_cuts"
            / f"{episode_name}_{path_hash}_{params_hash}.json"
        )

    @classmethod
    def load_cached_scene_cuts(
        cls,
        episode_path: str,
        threshold: float | None = None,
        min_scene_len: int | None = None,
        frame_skip: int | None = None,
    ) -> list[float] | None:
        """Load cached scene cut times for an episode.

        Returns:
            List of cut times in seconds, or None if cache doesn't exist.
        """
        threshold_val, min_scene_len_val, frame_skip_val = cls._normalize_scene_cut_params(
            threshold,
            min_scene_len,
            frame_skip,
        )
        cache_key = cls._scene_cut_runtime_key(
            episode_path,
            threshold_val,
            min_scene_len_val,
            frame_skip_val,
        )
        in_memory = cls._scene_cut_cache.get(cache_key)
        if in_memory is not None:
            return in_memory

        cache_path = cls.get_scene_cache_path(
            episode_path,
            threshold=threshold_val,
            min_scene_len=min_scene_len_val,
            frame_skip=frame_skip_val,
        )
        cache_paths = [cache_path]
        if (
            threshold_val == cls.SCENE_THRESHOLD
            and min_scene_len_val == cls.MIN_SCENE_LEN
            and frame_skip_val == cls.SCENE_DETECTION_FRAME_SKIP
        ):
            cache_paths.append(cls._legacy_scene_cache_path(episode_path))

        for cache_candidate in cache_paths:
            if not cache_candidate.exists():
                continue
            try:
                data = json.loads(cache_candidate.read_text())
                cuts = data.get("cuts", [])
                cls._scene_cut_cache[cache_key] = cuts
                return cuts
            except (json.JSONDecodeError, KeyError):
                continue
        return None

    @classmethod
    def save_scene_cuts_cache(
        cls,
        episode_path: str,
        cuts: list[float],
        threshold: float | None = None,
        min_scene_len: int | None = None,
        frame_skip: int | None = None,
    ) -> None:
        """Save scene cut times to cache."""
        threshold_val, min_scene_len_val, frame_skip_val = cls._normalize_scene_cut_params(
            threshold,
            min_scene_len,
            frame_skip,
        )
        cache_key = cls._scene_cut_runtime_key(
            episode_path,
            threshold_val,
            min_scene_len_val,
            frame_skip_val,
        )
        cls._scene_cut_cache[cache_key] = cuts
        cache_path = cls.get_scene_cache_path(
            episode_path,
            threshold=threshold_val,
            min_scene_len=min_scene_len_val,
            frame_skip=frame_skip_val,
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "episode_path": episode_path,
            "cuts": cuts,
            "threshold": threshold_val,
            "min_scene_len": min_scene_len_val,
            "frame_skip": frame_skip_val,
        }
        cache_path.write_text(json.dumps(data, indent=2))

    @classmethod
    async def detect_scene_cuts(
        cls,
        episode_path: str,
        threshold: float | None = None,
        min_scene_len: int | None = None,
        frame_skip: int | None = None,
    ) -> list[float]:
        """Detect scene cuts in an anime episode.

        Uses cache if available, otherwise runs pyscenedetect.

        Args:
            episode_path: Path to the video file
            threshold: ContentDetector threshold (optional)
            min_scene_len: Minimum scene length in frames (optional)
            frame_skip: Number of frames to skip during detection (optional)

        Returns:
            List of cut times in seconds (start of each scene)
        """
        threshold_val, min_scene_len_val, frame_skip_val = cls._normalize_scene_cut_params(
            threshold,
            min_scene_len,
            frame_skip,
        )

        # Check cache first
        cached = cls.load_cached_scene_cuts(
            episode_path,
            threshold=threshold_val,
            min_scene_len=min_scene_len_val,
            frame_skip=frame_skip_val,
        )
        if cached is not None:
            return cached

        abs_episode_key = cls._scene_cut_runtime_key(
            episode_path,
            threshold_val,
            min_scene_len_val,
            frame_skip_val,
        )

        async with cls._scene_cut_inflight_lock:
            task = cls._scene_cut_inflight.get(abs_episode_key)
            if task is None:
                task = asyncio.create_task(
                    cls._detect_and_cache_scene_cuts(
                        episode_path=episode_path,
                        threshold=threshold_val,
                        min_scene_len=min_scene_len_val,
                        frame_skip=frame_skip_val,
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
        frame_skip: int,
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
                frame_skip,
            )

        cls.save_scene_cuts_cache(
            episode_path,
            cuts,
            threshold=threshold,
            min_scene_len=min_scene_len,
            frame_skip=frame_skip,
        )
        return cuts

    @staticmethod
    def _detect_scene_cuts_sync(
        video_path: str,
        threshold: float,
        min_scene_len: int,
        frame_skip: int,
    ) -> list[float]:
        """Synchronous scene cut detection using pyscenedetect."""
        video = open_video(video_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(
            ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
        )

        scene_manager.detect_scenes(video, show_progress=False, frame_skip=frame_skip)
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
            scene_timings: Authoritative playback scene timings. When present,
                start_time/end_time are preferred over word-derived timing,
                including for raw scenes.

        Returns:
            List of GapInfo for scenes that have gaps
        """
        gaps = []

        # Create calculator for frame-perfect timing (same as processing.py)
        calculator = OTIOTimingCalculator(
            sequence_rate=cls.TIMELINE_RATE,
            source_rate=cls.SOURCE_RATE,
        )

        # Older saved states may not have authoritative scene bounds yet.
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
            if not scene_timing:
                continue

            timeline_start_raw = scene_timing.get("start_time")
            timeline_end_raw = scene_timing.get("end_time")

            has_authoritative_bounds = (
                isinstance(timeline_start_raw, (int, float))
                and isinstance(timeline_end_raw, (int, float))
                and float(timeline_end_raw) > float(timeline_start_raw)
            )

            if has_authoritative_bounds:
                timeline_start_raw = float(timeline_start_raw)
                timeline_end_raw = float(timeline_end_raw)
            else:
                words = scene_timing.get("words") or []
                if not words:
                    continue
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
        matches: list[SceneMatch] | None = None,
        max_candidates: int = 6,
    ) -> list[GapCandidate]:
        """Generate AI candidates for extending a clip to fill a gap.

        Uses pyscenedetect to find nearby scene cuts and proposes timings
        that snap to these cuts. Candidates are ranked by:
        1) Avoiding same-episode adjacent overlap
        2) Preferring cut-aligned candidates over fallback windows
        3) Minimizing added source duration (closer to the 75% floor)

        Uses Fraction-based arithmetic for frame-perfect precision.

        Args:
            gap: Gap information for the scene
            matches: Full project match list used for neighbor-aware ranking
            max_candidates: Maximum number of candidates to return

        Returns:
            List of GapCandidate objects sorted by the /gaps ranking policy
        """
        candidates_by_scene = await cls.generate_candidates_batch(
            [gap],
            matches=matches,
            max_candidates=max_candidates,
        )
        return candidates_by_scene.get(gap.scene_index, [])

    @classmethod
    async def _analyze_episode(
        cls,
        episode_path: Path,
        threshold: float | None = None,
    ) -> tuple[list[float], float]:
        """Load per-episode analysis data once (scene cuts + frame offset)."""
        cuts_task = asyncio.create_task(
            cls.detect_scene_cuts(str(episode_path), threshold=threshold)
        )
        frame_offset_task = asyncio.create_task(cls.get_frame_offset(episode_path))

        cuts: list[float]
        try:
            cuts = await cuts_task
        except Exception:
            cuts = []

        try:
            frame_offset = await frame_offset_task
        except Exception:
            frame_offset = float(cls.SAFETY_FRAMES / float(cls.DEFAULT_FPS))
        return cuts, frame_offset

    @classmethod
    def _build_gap_batch_key(
        cls,
        gaps: list[GapInfo],
        matches: list[SceneMatch] | None,
        max_candidates: int,
    ) -> str:
        """Build a stable key to dedupe concurrent all-candidates requests."""
        import hashlib

        gap_payload = [
            {
                "scene_index": gap.scene_index,
                "episode": gap.episode,
                "current_start": round(gap.current_start, 6),
                "current_end": round(gap.current_end, 6),
                "target_duration": round(gap.target_duration, 6),
            }
            for gap in gaps
        ]
        match_payload = []
        if matches:
            match_payload = [
                {
                    "scene_index": match.scene_index,
                    "episode": match.episode,
                    "start_time": round(match.start_time, 6),
                    "end_time": round(match.end_time, 6),
                }
                for match in matches
            ]
        payload = {
            "gaps": gap_payload,
            "matches": match_payload,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha1(encoded).hexdigest()
        return f"{max_candidates}:{digest}"

    @classmethod
    def _clone_candidate_batch_result(
        cls,
        candidates_by_scene: dict[int, list[GapCandidate]],
    ) -> dict[int, list[GapCandidate]]:
        return {
            scene_index: list(candidates)
            for scene_index, candidates in candidates_by_scene.items()
        }

    @classmethod
    def _prune_candidate_batch_cache_locked(cls, now: float | None = None) -> None:
        current_time = time.monotonic() if now is None else now
        expired_keys = [
            cache_key
            for cache_key, (stored_at, _) in cls._candidate_batch_result_cache.items()
            if current_time - stored_at > cls.CANDIDATE_BATCH_CACHE_TTL_SECONDS
        ]
        for cache_key in expired_keys:
            cls._candidate_batch_result_cache.pop(cache_key, None)

        while len(cls._candidate_batch_result_cache) > cls.CANDIDATE_BATCH_CACHE_MAX_ENTRIES:
            cls._candidate_batch_result_cache.popitem(last=False)

    @classmethod
    def _get_candidate_batch_cache_locked(
        cls,
        cache_key: str,
    ) -> dict[int, list[GapCandidate]] | None:
        now = time.monotonic()
        cls._prune_candidate_batch_cache_locked(now)
        entry = cls._candidate_batch_result_cache.get(cache_key)
        if entry is None:
            return None

        cls._candidate_batch_result_cache.move_to_end(cache_key)
        _, cached_result = entry
        return cls._clone_candidate_batch_result(cached_result)

    @classmethod
    def _store_candidate_batch_cache_locked(
        cls,
        cache_key: str,
        candidates_by_scene: dict[int, list[GapCandidate]],
    ) -> None:
        cls._candidate_batch_result_cache[cache_key] = (
            time.monotonic(),
            cls._clone_candidate_batch_result(candidates_by_scene),
        )
        cls._candidate_batch_result_cache.move_to_end(cache_key)
        cls._prune_candidate_batch_cache_locked()

    @classmethod
    async def _generate_and_cache_candidates_batch(
        cls,
        cache_key: str,
        gaps: list[GapInfo],
        matches: list[SceneMatch] | None,
        max_candidates: int,
    ) -> dict[int, list[GapCandidate]]:
        result = await cls.generate_candidates_batch(
            gaps,
            matches=matches,
            max_candidates=max_candidates,
        )
        async with cls._candidate_batch_lock:
            cls._store_candidate_batch_cache_locked(cache_key, result)
        return cls._clone_candidate_batch_result(result)

    @classmethod
    async def generate_candidates_batch_dedup(
        cls,
        gaps: list[GapInfo],
        matches: list[SceneMatch] | None = None,
        max_candidates: int = 6,
    ) -> dict[int, list[GapCandidate]]:
        """Deduplicate concurrent batch-generation calls for the same gap set."""
        if not gaps:
            return {}

        key = cls._build_gap_batch_key(gaps, matches, max_candidates)
        async with cls._candidate_batch_lock:
            cached = cls._get_candidate_batch_cache_locked(key)
            if cached is not None:
                return cached

            task = cls._candidate_batch_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    cls._generate_and_cache_candidates_batch(
                        key,
                        gaps,
                        matches=matches,
                        max_candidates=max_candidates,
                    )
                )
                cls._candidate_batch_inflight[key] = task

        try:
            result = await task
            return cls._clone_candidate_batch_result(result)
        finally:
            if task.done():
                async with cls._candidate_batch_lock:
                    current = cls._candidate_batch_inflight.get(key)
                    if current is task:
                        cls._candidate_batch_inflight.pop(key, None)

    @classmethod
    async def generate_candidates_batch(
        cls,
        gaps: list[GapInfo],
        matches: list[SceneMatch] | None = None,
        max_candidates: int = 6,
    ) -> dict[int, list[GapCandidate]]:
        """Generate candidates for many gaps with deduplicated per-episode analysis."""
        if not gaps:
            return {}

        manifest = await AnimeLibraryService.ensure_episode_manifest()
        resolved_by_scene: dict[int, Path] = {}
        unresolved: list[GapInfo] = []

        for gap in gaps:
            resolved = AnimeLibraryService.resolve_episode_path(gap.episode, manifest)
            if resolved and resolved.exists():
                resolved_by_scene[gap.scene_index] = resolved
            else:
                unresolved.append(gap)

        if unresolved:
            refreshed_manifest = await AnimeLibraryService.ensure_episode_manifest(force_refresh=True)
            for gap in unresolved:
                resolved = AnimeLibraryService.resolve_episode_path(gap.episode, refreshed_manifest)
                if resolved and resolved.exists():
                    resolved_by_scene[gap.scene_index] = resolved

        match_list = matches or []
        _, episode_key_by_scene, tolerance_by_episode = await cls._build_episode_overlap_context(
            match_list,
            gaps,
        )
        neighbor_contexts = cls._build_neighbor_contexts(
            match_list,
            episode_key_by_scene,
            tolerance_by_episode,
        )

        unique_episodes = sorted({str(path.resolve()) for path in resolved_by_scene.values()})
        analysis_tasks = {
            episode_path: asyncio.create_task(cls._analyze_episode(Path(episode_path)))
            for episode_path in unique_episodes
        }

        episode_analysis: dict[str, tuple[list[float], float]] = {}
        for episode_path, task in analysis_tasks.items():
            try:
                episode_analysis[episode_path] = await task
            except Exception:
                continue

        candidates_by_scene: dict[int, list[GapCandidate]] = {}
        for gap in gaps:
            resolved = resolved_by_scene.get(gap.scene_index)
            if not resolved:
                candidates_by_scene[gap.scene_index] = []
                continue

            analysis = episode_analysis.get(str(resolved.resolve()))
            if not analysis:
                candidates_by_scene[gap.scene_index] = []
                continue

            cuts, frame_offset = analysis
            candidates_by_scene[gap.scene_index] = await cls._generate_candidates_for_gap(
                gap=gap,
                episode_path=resolved,
                base_cuts=cuts,
                frame_offset=frame_offset,
                neighbor_context=neighbor_contexts.get(gap.scene_index, _NeighborContext()),
                max_candidates=max_candidates,
            )

        return candidates_by_scene

    @staticmethod
    def _scene_episode_hint(match: SceneMatch, gap: GapInfo | None) -> str:
        """Resolve the best available episode identifier for overlap checks."""
        if gap and gap.episode:
            return gap.episode
        if match.episode:
            return match.episode
        alternative = next((alt for alt in match.alternatives if alt.episode), None)
        return alternative.episode if alternative else ""

    @classmethod
    async def _normalize_episode_keys_for_overlap(
        cls,
        episode_hints: set[str],
    ) -> dict[str, tuple[str, Path | None]]:
        """Normalize episode hints to stable comparison keys.

        Returns:
            Mapping of raw episode hint -> (normalized_key, resolved_path_or_none).
            - normalized_key is absolute path string when resolution succeeds.
            - otherwise normalized_key falls back to the raw hint.
        """
        if not episode_hints:
            return {}

        try:
            manifest = await AnimeLibraryService.ensure_episode_manifest()
        except Exception:
            manifest = {}

        resolved_map: dict[str, tuple[str, Path | None]] = {}
        unresolved: list[str] = []

        for episode in sorted(episode_hints):
            if not episode:
                continue
            resolved = AnimeLibraryService.resolve_episode_path(episode, manifest)
            if resolved:
                resolved_path = resolved.resolve()
                resolved_map[episode] = (str(resolved_path), resolved_path)
            else:
                unresolved.append(episode)

        if unresolved:
            try:
                refreshed_manifest = await AnimeLibraryService.ensure_episode_manifest(force_refresh=True)
            except Exception:
                refreshed_manifest = manifest
            for episode in unresolved:
                resolved = AnimeLibraryService.resolve_episode_path(episode, refreshed_manifest)
                if resolved:
                    resolved_path = resolved.resolve()
                    resolved_map[episode] = (str(resolved_path), resolved_path)
                else:
                    resolved_map[episode] = (episode, None)

        return resolved_map

    @classmethod
    async def _build_episode_overlap_context(
        cls,
        matches: list[SceneMatch],
        gaps: list[GapInfo],
    ) -> tuple[list[int], dict[int, str], dict[str, float]]:
        """Build normalized episode/tolerance context shared by ranking and DP."""
        match_by_scene = {match.scene_index: match for match in matches}
        gap_by_scene = {gap.scene_index: gap for gap in gaps}
        sorted_scene_indices = sorted(match_by_scene)

        episode_hint_by_scene: dict[int, str] = {}
        for scene_index in sorted_scene_indices:
            match = match_by_scene[scene_index]
            gap = gap_by_scene.get(scene_index)
            episode_hint_by_scene[scene_index] = cls._scene_episode_hint(match, gap)

        normalized_episode = await cls._normalize_episode_keys_for_overlap(
            {episode for episode in episode_hint_by_scene.values() if episode}
        )

        default_tolerance = float(Fraction(1, 1) / cls.DEFAULT_FPS)
        tolerance_by_episode: dict[str, float] = {}
        for normalized_key, resolved_path in normalized_episode.values():
            if normalized_key in tolerance_by_episode:
                continue
            fps_fraction = cls.DEFAULT_FPS
            if resolved_path is not None:
                try:
                    fps_fraction = await cls.detect_video_fps(resolved_path)
                except Exception:
                    fps_fraction = cls.DEFAULT_FPS
            fps = float(fps_fraction)
            tolerance_by_episode[normalized_key] = (1.0 / fps) if fps > 0 else default_tolerance

        episode_key_by_scene: dict[int, str] = {}
        for scene_index in sorted_scene_indices:
            raw_episode = episode_hint_by_scene.get(scene_index, "")
            if raw_episode:
                episode_key_by_scene[scene_index] = normalized_episode.get(
                    raw_episode,
                    (raw_episode, None),
                )[0]
            else:
                episode_key_by_scene[scene_index] = f"__unknown_scene_{scene_index}"

        return sorted_scene_indices, episode_key_by_scene, tolerance_by_episode

    @classmethod
    def _build_neighbor_contexts(
        cls,
        matches: list[SceneMatch],
        episode_key_by_scene: dict[int, str],
        tolerance_by_episode: dict[str, float],
    ) -> dict[int, _NeighborContext]:
        """Build immediate previous/next same-episode occupancy windows."""
        match_by_scene = {match.scene_index: match for match in matches}
        sorted_scene_indices = sorted(match_by_scene)
        default_tolerance = float(Fraction(1, 1) / cls.DEFAULT_FPS)
        contexts: dict[int, _NeighborContext] = {}

        for position, scene_index in enumerate(sorted_scene_indices):
            episode_key = episode_key_by_scene.get(scene_index, f"__unknown_scene_{scene_index}")
            previous_window: _NeighborWindow | None = None
            next_window: _NeighborWindow | None = None

            if position > 0:
                previous_scene_index = sorted_scene_indices[position - 1]
                if episode_key_by_scene.get(previous_scene_index) == episode_key:
                    previous_match = match_by_scene[previous_scene_index]
                    previous_window = _NeighborWindow(
                        relation="previous",
                        start_time=previous_match.start_time,
                        end_time=previous_match.end_time,
                        tolerance=tolerance_by_episode.get(episode_key, default_tolerance),
                    )

            if position < len(sorted_scene_indices) - 1:
                next_scene_index = sorted_scene_indices[position + 1]
                if episode_key_by_scene.get(next_scene_index) == episode_key:
                    next_match = match_by_scene[next_scene_index]
                    next_window = _NeighborWindow(
                        relation="next",
                        start_time=next_match.start_time,
                        end_time=next_match.end_time,
                        tolerance=tolerance_by_episode.get(episode_key, default_tolerance),
                    )

            contexts[scene_index] = _NeighborContext(previous=previous_window, next=next_window)

        return contexts

    @classmethod
    def _threshold_rank(cls, detector_threshold: float | None) -> int:
        """Prefer baseline cuts before lower-threshold cascade results in ties."""
        if detector_threshold is None:
            return 0
        cascade = (cls.SCENE_THRESHOLD, 18.0, 12.0)
        for index, threshold in enumerate(cascade):
            if abs(detector_threshold - threshold) < 1e-6:
                return index
        return len(cascade)

    @classmethod
    def _candidate_sort_key(
        cls,
        candidate: GapCandidate,
    ) -> tuple[int, int, int, int, int, int, int, str, int, int]:
        """Sort candidates according to the /gaps ranking policy."""
        return (
            candidate.overlap_count,
            int(round(candidate.overlap_seconds * cls.OVERLAP_COST_SCALE)),
            0 if candidate.is_cut_aligned else 1,
            candidate.continuation_priority,
            int(round(candidate.added_duration * cls.OVERLAP_COST_SCALE)),
            candidate.direction_priority,
            cls._threshold_rank(candidate.detector_threshold),
            candidate.extend_type,
            int(round(candidate.start_time * cls.OVERLAP_COST_SCALE)),
            int(round(candidate.end_time * cls.OVERLAP_COST_SCALE)),
        )

    @classmethod
    def _merge_candidates(
        cls,
        candidates: list[GapCandidate],
        additional_candidates: list[GapCandidate],
    ) -> list[GapCandidate]:
        """Merge timing-unique candidates while keeping the better-ranked copy."""
        merged: dict[tuple[float, float], GapCandidate] = {}
        for candidate in [*candidates, *additional_candidates]:
            timing_key = (round(candidate.start_time, 6), round(candidate.end_time, 6))
            existing = merged.get(timing_key)
            if existing is None or cls._candidate_sort_key(candidate) < cls._candidate_sort_key(existing):
                merged[timing_key] = candidate
        return sorted(merged.values(), key=cls._candidate_sort_key)

    @classmethod
    async def _generate_candidates_for_gap(
        cls,
        gap: GapInfo,
        episode_path: Path,
        base_cuts: list[float],
        frame_offset: float,
        neighbor_context: _NeighborContext,
        max_candidates: int,
    ) -> list[GapCandidate]:
        """Generate ranked candidates for one gap using cuts, cascade, and fallbacks."""
        cut_candidates = cls._generate_cut_candidates_from_cuts(
            gap=gap,
            cuts=base_cuts,
            frame_offset=frame_offset,
            neighbor_context=neighbor_context,
            detector_threshold=cls.SCENE_THRESHOLD,
        )

        if not any(candidate.is_clean for candidate in cut_candidates):
            for threshold in (18.0, 12.0):
                cuts = await cls.detect_scene_cuts(str(episode_path), threshold=threshold)
                threshold_candidates = cls._generate_cut_candidates_from_cuts(
                    gap=gap,
                    cuts=cuts,
                    frame_offset=frame_offset,
                    neighbor_context=neighbor_context,
                    detector_threshold=threshold,
                )
                cut_candidates = cls._merge_candidates(cut_candidates, threshold_candidates)
                if any(candidate.is_clean for candidate in cut_candidates):
                    break

        fallback_candidates = cls._generate_fallback_candidates(
            gap=gap,
            neighbor_context=neighbor_context,
        )

        ranked = cls._merge_candidates(cut_candidates, fallback_candidates)
        ranked = cls._apply_continuation_bias(ranked, gap, neighbor_context)
        return ranked[:max_candidates]

    @classmethod
    async def select_autofill_candidates_overlap_aware(
        cls,
        matches: list[SceneMatch],
        gaps: list[GapInfo],
        candidates_by_scene: dict[int, list[GapCandidate]],
    ) -> AutoFillSelectionResult:
        """Pick one candidate per gap by minimizing overlaps first, then source stretch.

        Objective order (lexicographic):
        1) Number of overlaps across adjacent timeline scenes (same episode only)
        2) Total overlap duration
        3) Prefer cut-aligned candidates over fallback windows
        4) Prefer the continuation/open-side clean cut candidate inside the tie window
        5) Minimize added source duration (closer to the 75% floor)
        6) Candidate rank sum (stable deterministic tie-break)
        """
        if not matches:
            return AutoFillSelectionResult(
                selected_candidates_by_scene={},
                overlap_seconds_by_scene={},
                total_overlap_count=0,
                total_overlap_seconds=0.0,
            )

        match_by_scene = {match.scene_index: match for match in matches}
        if not match_by_scene:
            return AutoFillSelectionResult(
                selected_candidates_by_scene={},
                overlap_seconds_by_scene={},
                total_overlap_count=0,
                total_overlap_seconds=0.0,
            )

        gap_by_scene = {gap.scene_index: gap for gap in gaps}
        sorted_scene_indices, episode_key_by_scene, tolerance_by_episode = (
            await cls._build_episode_overlap_context(matches, gaps)
        )
        default_tolerance = float(Fraction(1, 1) / cls.DEFAULT_FPS)

        states_by_scene: list[list[_AutoFillState]] = []
        for scene_index in sorted_scene_indices:
            match = match_by_scene[scene_index]
            episode_key = episode_key_by_scene.get(scene_index, f"__unknown_scene_{scene_index}")

            scene_candidates = candidates_by_scene.get(scene_index, [])
            if gap_by_scene.get(scene_index) and scene_candidates:
                scene_states = [
                    _AutoFillState(
                        scene_index=scene_index,
                        episode_key=episode_key,
                        start_time=candidate.start_time,
                        end_time=candidate.end_time,
                        cut_penalty=0 if candidate.is_cut_aligned else 1,
                        continuation_penalty=candidate.continuation_priority,
                        added_duration_micro=int(
                            round(candidate.added_duration * cls.OVERLAP_COST_SCALE)
                        ),
                        candidate_rank=rank,
                        candidate=candidate,
                    )
                    for rank, candidate in enumerate(scene_candidates)
                ]
            else:
                scene_states = [
                    _AutoFillState(
                        scene_index=scene_index,
                        episode_key=episode_key,
                        start_time=match.start_time,
                        end_time=match.end_time,
                        cut_penalty=0,
                        continuation_penalty=0,
                        added_duration_micro=0,
                        candidate_rank=0,
                        candidate=None,
                    )
                ]

            states_by_scene.append(scene_states)

        def overlap_penalty(prev_state: _AutoFillState, next_state: _AutoFillState) -> tuple[int, int]:
            if prev_state.episode_key != next_state.episode_key:
                return (0, 0)
            tolerance = tolerance_by_episode.get(prev_state.episode_key, default_tolerance)
            overlap_effective = cls._interval_overlap_seconds(
                prev_state.start_time,
                prev_state.end_time,
                next_state.start_time,
                next_state.end_time,
                tolerance,
            )
            if overlap_effective <= 0:
                return (0, 0)
            overlap_micro = max(1, int(round(overlap_effective * cls.OVERLAP_COST_SCALE)))
            return (1, overlap_micro)

        dp_costs: list[list[tuple[int, int, int, int, int, int] | None]] = [
            [None for _ in scene_states] for scene_states in states_by_scene
        ]
        dp_prev: list[list[int | None]] = [
            [None for _ in scene_states] for scene_states in states_by_scene
        ]

        for state_index, state in enumerate(states_by_scene[0]):
            dp_costs[0][state_index] = (
                0,
                0,
                state.cut_penalty,
                state.continuation_penalty,
                state.added_duration_micro,
                state.candidate_rank,
            )

        for scene_pos in range(1, len(states_by_scene)):
            previous_states = states_by_scene[scene_pos - 1]
            current_states = states_by_scene[scene_pos]
            for current_index, current_state in enumerate(current_states):
                best_cost: tuple[int, int, int, int, int, int] | None = None
                best_previous_index: int | None = None
                for previous_index, previous_state in enumerate(previous_states):
                    previous_cost = dp_costs[scene_pos - 1][previous_index]
                    if previous_cost is None:
                        continue
                    overlap_count, overlap_micro = overlap_penalty(previous_state, current_state)
                    candidate_cost = (
                        previous_cost[0] + overlap_count,
                        previous_cost[1] + overlap_micro,
                        previous_cost[2] + current_state.cut_penalty,
                        previous_cost[3] + current_state.continuation_penalty,
                        previous_cost[4] + current_state.added_duration_micro,
                        previous_cost[5] + current_state.candidate_rank,
                    )
                    if best_cost is None or candidate_cost < best_cost:
                        best_cost = candidate_cost
                        best_previous_index = previous_index
                dp_costs[scene_pos][current_index] = best_cost
                dp_prev[scene_pos][current_index] = best_previous_index

        last_costs = dp_costs[-1]
        valid_last_indices = [index for index, cost in enumerate(last_costs) if cost is not None]
        if not valid_last_indices:
            return AutoFillSelectionResult(
                selected_candidates_by_scene={},
                overlap_seconds_by_scene={},
                total_overlap_count=0,
                total_overlap_seconds=0.0,
            )
        best_last_index = min(valid_last_indices, key=lambda index: last_costs[index])

        chosen_state_indices: list[int | None] = [None] * len(states_by_scene)
        current_index: int | None = best_last_index
        for scene_pos in range(len(states_by_scene) - 1, -1, -1):
            if current_index is None:
                break
            chosen_state_indices[scene_pos] = current_index
            current_index = dp_prev[scene_pos][current_index]

        selected_states: list[_AutoFillState] = []
        for scene_pos, chosen_index in enumerate(chosen_state_indices):
            if chosen_index is None:
                chosen_index = 0
            selected_states.append(states_by_scene[scene_pos][chosen_index])

        selected_candidates: dict[int, GapCandidate] = {}
        for state in selected_states:
            if state.candidate is not None:
                selected_candidates[state.scene_index] = state.candidate

        overlap_seconds_by_scene: dict[int, float] = {}
        total_overlap_count = 0
        total_overlap_seconds = 0.0
        for scene_pos in range(len(selected_states) - 1):
            current_state = selected_states[scene_pos]
            next_state = selected_states[scene_pos + 1]
            overlap_count, overlap_micro = overlap_penalty(current_state, next_state)
            if overlap_count == 0:
                continue
            overlap_seconds = overlap_micro / cls.OVERLAP_COST_SCALE
            total_overlap_count += overlap_count
            total_overlap_seconds += overlap_seconds
            overlap_seconds_by_scene[current_state.scene_index] = (
                overlap_seconds_by_scene.get(current_state.scene_index, 0.0) + overlap_seconds
            )
            overlap_seconds_by_scene[next_state.scene_index] = (
                overlap_seconds_by_scene.get(next_state.scene_index, 0.0) + overlap_seconds
            )

        return AutoFillSelectionResult(
            selected_candidates_by_scene=selected_candidates,
            overlap_seconds_by_scene=overlap_seconds_by_scene,
            total_overlap_count=total_overlap_count,
            total_overlap_seconds=total_overlap_seconds,
        )

    @classmethod
    def _interval_overlap_seconds(
        cls,
        start_a: float,
        end_a: float,
        start_b: float,
        end_b: float,
        tolerance: float,
    ) -> float:
        """Measure interval overlap with a one-frame tolerance."""
        overlap_raw = min(end_a, end_b) - max(start_a, start_b)
        return max(0.0, overlap_raw - tolerance)

    @staticmethod
    def _is_single_sided_candidate(extend_type: str) -> bool:
        """Return True when a candidate only grows one side of the match."""
        return extend_type not in {"extend_both", "fill_scene", "fallback_extend_both"}

    @classmethod
    def _candidate_overlap_metrics(
        cls,
        new_start: float,
        new_end: float,
        neighbor_context: _NeighborContext,
    ) -> tuple[int, float]:
        """Compute overlap metrics against immediate same-episode neighbors."""
        overlap_count = 0
        overlap_seconds = 0.0
        for neighbor in (neighbor_context.previous, neighbor_context.next):
            if neighbor is None:
                continue
            overlap = cls._interval_overlap_seconds(
                new_start,
                new_end,
                neighbor.start_time,
                neighbor.end_time,
                neighbor.tolerance,
            )
            if overlap <= 0:
                continue
            overlap_count += 1
            overlap_seconds += overlap
        return overlap_count, overlap_seconds

    @classmethod
    def _source_side_clearances(
        cls,
        gap: GapInfo,
        neighbor_context: _NeighborContext,
    ) -> dict[str, _SideClearance]:
        """Measure nearest occupied source-space clearance on each side."""
        left_clearance = float("inf")
        right_clearance = float("inf")
        left_has_blocker = False
        right_has_blocker = False

        for neighbor in (neighbor_context.previous, neighbor_context.next):
            if neighbor is None:
                continue

            if neighbor.end_time <= gap.current_start + neighbor.tolerance:
                left_has_blocker = True
                left_clearance = min(
                    left_clearance,
                    max(0.0, gap.current_start - neighbor.end_time - neighbor.tolerance),
                )

            if neighbor.start_time >= gap.current_end - neighbor.tolerance:
                right_has_blocker = True
                right_clearance = min(
                    right_clearance,
                    max(0.0, neighbor.start_time - gap.current_end - neighbor.tolerance),
                )

        return {
            "left": _SideClearance(
                has_blocker=left_has_blocker,
                clearance_seconds=left_clearance,
            ),
            "right": _SideClearance(
                has_blocker=right_has_blocker,
                clearance_seconds=right_clearance,
            ),
        }

    @staticmethod
    def _candidate_clearance_side(extend_type: str) -> str | None:
        """Map single-sided candidate types to the source side they extend."""
        if extend_type in {
            "extend_start",
            "extend_to_scene_start",
            "fallback_extend_start",
        }:
            return "left"
        if extend_type in {
            "extend_end",
            "extend_to_scene_end",
            "fallback_extend_end",
        }:
            return "right"
        return None

    @classmethod
    def _preferred_single_side_order(
        cls,
        gap: GapInfo,
        neighbor_context: _NeighborContext,
    ) -> tuple[str, str]:
        """Choose which extension direction to try first based on blocked neighbors."""
        side_clearances = cls._source_side_clearances(gap, neighbor_context)
        left_clearance = side_clearances["left"]
        right_clearance = side_clearances["right"]

        if not left_clearance.has_blocker and right_clearance.has_blocker:
            return ("start", "end")
        if left_clearance.has_blocker and not right_clearance.has_blocker:
            return ("end", "start")
        if left_clearance.has_blocker and right_clearance.has_blocker:
            if left_clearance.clearance_seconds >= right_clearance.clearance_seconds:
                return ("start", "end")
            return ("end", "start")
        return ("end", "start")

    @classmethod
    def _apply_continuation_bias(
        cls,
        candidates: list[GapCandidate],
        gap: GapInfo,
        neighbor_context: _NeighborContext,
    ) -> list[GapCandidate]:
        """Boost the better continuation side when clean cut candidates are near-tied."""
        if not candidates:
            return []

        for candidate in candidates:
            candidate.continuation_priority = 1
            candidate.continuation_bias_applied = False

        best_by_side: dict[str, GapCandidate] = {}
        for candidate in candidates:
            if not (
                candidate.is_clean
                and candidate.is_cut_aligned
                and cls._is_single_sided_candidate(candidate.extend_type)
            ):
                continue
            if candidate.clearance_side not in {"left", "right"}:
                continue
            existing = best_by_side.get(candidate.clearance_side)
            if existing is None or cls._candidate_sort_key(candidate) < cls._candidate_sort_key(existing):
                best_by_side[candidate.clearance_side] = candidate

        left_candidate = best_by_side.get("left")
        right_candidate = best_by_side.get("right")
        if left_candidate is None or right_candidate is None:
            return sorted(candidates, key=cls._candidate_sort_key)

        added_duration_delta = abs(left_candidate.added_duration - right_candidate.added_duration)
        if added_duration_delta > cls.CONTINUATION_TIE_WINDOW:
            return sorted(candidates, key=cls._candidate_sort_key)

        side_clearances = cls._source_side_clearances(gap, neighbor_context)
        left_clearance = side_clearances["left"]
        right_clearance = side_clearances["right"]

        preferred_side: str | None = None
        if left_clearance.has_blocker and not right_clearance.has_blocker:
            preferred_side = "right"
        elif right_clearance.has_blocker and not left_clearance.has_blocker:
            preferred_side = "left"
        elif left_clearance.has_blocker and right_clearance.has_blocker:
            if left_clearance.clearance_seconds > right_clearance.clearance_seconds:
                preferred_side = "left"
            elif right_clearance.clearance_seconds > left_clearance.clearance_seconds:
                preferred_side = "right"

        if preferred_side is None:
            return sorted(candidates, key=cls._candidate_sort_key)

        preferred_candidate = best_by_side[preferred_side]
        preferred_candidate.continuation_priority = 0
        preferred_candidate.continuation_bias_applied = True
        return sorted(candidates, key=cls._candidate_sort_key)

    @classmethod
    def _build_candidate(
        cls,
        gap: GapInfo,
        new_start: float,
        new_end: float,
        extend_type: str,
        description: str,
        neighbor_context: _NeighborContext,
        *,
        is_cut_aligned: bool,
        detector_threshold: float | None,
        direction_priority: int,
    ) -> GapCandidate | None:
        """Build and score a candidate if it satisfies the speed bounds."""
        if new_start < 0:
            return None

        new_start_frac = Fraction(new_start).limit_denominator(100000)
        new_end_frac = Fraction(new_end).limit_denominator(100000)
        new_duration_frac = new_end_frac - new_start_frac
        if new_duration_frac <= 0:
            return None

        target_duration_frac = Fraction(gap.target_duration).limit_denominator(100000)
        if target_duration_frac > 0:
            speed_frac = new_duration_frac / target_duration_frac
        else:
            speed_frac = Fraction(1, 1)

        if speed_frac < cls.MIN_SPEED or speed_frac > cls.MAX_SPEED:
            return None

        new_duration = float(new_duration_frac)
        overlap_count, overlap_seconds = cls._candidate_overlap_metrics(
            new_start,
            new_end,
            neighbor_context,
        )
        clearance_side = cls._candidate_clearance_side(extend_type)
        side_clearance_seconds = None
        if clearance_side is not None:
            side_clearance = cls._source_side_clearances(gap, neighbor_context)[clearance_side]
            if side_clearance.has_blocker:
                side_clearance_seconds = side_clearance.clearance_seconds

        return GapCandidate(
            start_time=new_start,
            end_time=new_end,
            duration=new_duration,
            effective_speed=speed_frac,
            speed_diff=abs(float(speed_frac) - float(cls.TARGET_SPEED)),
            extend_type=extend_type,
            snap_description=description,
            overlap_count=overlap_count,
            overlap_seconds=overlap_seconds,
            is_cut_aligned=is_cut_aligned,
            is_clean=overlap_count == 0,
            added_duration=max(0.0, new_duration - gap.current_duration),
            detector_threshold=detector_threshold if is_cut_aligned else None,
            direction_priority=direction_priority,
            clearance_side=clearance_side,
            side_clearance_seconds=side_clearance_seconds,
        )

    @classmethod
    def _generate_cut_candidates_from_cuts(
        cls,
        gap: GapInfo,
        cuts: list[float],
        frame_offset: float,
        neighbor_context: _NeighborContext,
        detector_threshold: float,
    ) -> list[GapCandidate]:
        """Generate cut-aligned candidates from detected cuts for one scene."""
        if not cuts or len(cuts) < 2:
            return []

        current_start = gap.current_start
        current_end = gap.current_end
        direction_order = cls._preferred_single_side_order(gap, neighbor_context)
        direction_priority = {
            direction_order[0]: 0,
            direction_order[1]: 1,
        }

        candidates: list[GapCandidate] = []
        seen_timings: set[tuple[float, float]] = set()

        def add_candidate(
            new_start: float,
            new_end: float,
            extend_type: str,
            description: str,
            direction: str | None = None,
        ) -> bool:
            candidate = cls._build_candidate(
                gap,
                new_start,
                new_end,
                extend_type,
                description,
                neighbor_context,
                is_cut_aligned=True,
                detector_threshold=detector_threshold,
                direction_priority=2 if direction is None else direction_priority[direction],
            )
            if candidate is None:
                return False
            timing_key = (round(candidate.start_time, 6), round(candidate.end_time, 6))
            if timing_key in seen_timings:
                return False
            seen_timings.add(timing_key)
            candidates.append(candidate)
            return True

        current_scene_start = None
        current_scene_end = None
        for i in range(len(cuts) - 1):
            scene_start = cuts[i]
            scene_end = cuts[i + 1]
            if scene_start <= current_start and current_end <= scene_end:
                current_scene_start = scene_start
                current_scene_end = scene_end
                break

        safe_start = None
        safe_end = None
        if current_scene_start is not None and current_scene_end is not None:
            safe_start = current_scene_start + frame_offset
            safe_end = current_scene_end - frame_offset

        cuts_before = [c for c in cuts if c < current_start]
        cuts_after = [c for c in cuts if c > current_end]
        cuts_before.sort(key=lambda c: current_start - c)
        cuts_after.sort(key=lambda c: c - current_end)

        start_specs: list[tuple[float, float, str, str, str]] = []
        end_specs: list[tuple[float, float, str, str, str]] = []

        if safe_start is not None and safe_start < current_start:
            start_specs.append(
                (
                    safe_start,
                    current_end,
                    "extend_to_scene_start",
                    f"Extend to scene start (-{current_start - safe_start:.2f}s)",
                    "start",
                )
            )
        if safe_end is not None and safe_end > current_end:
            end_specs.append(
                (
                    current_start,
                    safe_end,
                    "extend_to_scene_end",
                    f"Extend to scene end (+{safe_end - current_end:.2f}s)",
                    "end",
                )
            )

        for cut_start in cuts_before[:5]:
            new_start = cut_start + frame_offset
            start_specs.append(
                (
                    new_start,
                    current_end,
                    "extend_start",
                    f"Extend start to previous cut (-{current_start - new_start:.2f}s)",
                    "start",
                )
            )

        for cut_end in cuts_after[:5]:
            new_end = cut_end - frame_offset
            end_specs.append(
                (
                    current_start,
                    new_end,
                    "extend_end",
                    f"Extend end to next cut (+{new_end - current_end:.2f}s)",
                    "end",
                )
            )

        specs_by_direction = {
            "start": start_specs,
            "end": end_specs,
        }
        for direction in direction_order:
            for spec in specs_by_direction[direction]:
                add_candidate(*spec)

        if not any(
            candidate.is_clean and cls._is_single_sided_candidate(candidate.extend_type)
            for candidate in candidates
        ):
            if (
                safe_start is not None
                and safe_end is not None
                and (safe_start < current_start or safe_end > current_end)
            ):
                add_candidate(
                    safe_start,
                    safe_end,
                    "fill_scene",
                    f"Fill current scene ({current_scene_end - current_scene_start:.2f}s scene)",
                )

            for cut_start in cuts_before[:3]:
                for cut_end in cuts_after[:3]:
                    new_start = cut_start + frame_offset
                    new_end = cut_end - frame_offset
                    add_candidate(
                        new_start,
                        new_end,
                        "extend_both",
                        (
                            "Extend both "
                            f"(-{current_start - new_start:.2f}s, +{new_end - current_end:.2f}s)"
                        ),
                    )

        return sorted(candidates, key=cls._candidate_sort_key)

    @classmethod
    def _generate_fallback_candidates(
        cls,
        gap: GapInfo,
        neighbor_context: _NeighborContext,
    ) -> list[GapCandidate]:
        """Generate minimal-duration fallback windows when cut alignment is insufficient."""
        current_duration_frac = Fraction(gap.current_duration).limit_denominator(100000)
        target_duration_frac = Fraction(gap.target_duration).limit_denominator(100000)
        minimum_duration_frac = target_duration_frac * cls.MIN_SPEED
        extra_needed_frac = minimum_duration_frac - current_duration_frac
        if extra_needed_frac <= 0:
            return []

        extra_needed = float(extra_needed_frac)
        direction_order = cls._preferred_single_side_order(gap, neighbor_context)
        direction_priority = {
            direction_order[0]: 0,
            direction_order[1]: 1,
        }

        candidates: list[GapCandidate] = []
        seen_timings: set[tuple[float, float]] = set()

        def add_candidate(
            new_start: float,
            new_end: float,
            extend_type: str,
            description: str,
            direction: str | None = None,
        ) -> bool:
            candidate = cls._build_candidate(
                gap,
                new_start,
                new_end,
                extend_type,
                description,
                neighbor_context,
                is_cut_aligned=False,
                detector_threshold=None,
                direction_priority=2 if direction is None else direction_priority[direction],
            )
            if candidate is None:
                return False
            timing_key = (round(candidate.start_time, 6), round(candidate.end_time, 6))
            if timing_key in seen_timings:
                return False
            seen_timings.add(timing_key)
            candidates.append(candidate)
            return True

        single_side_specs = {
            "start": (
                gap.current_start - extra_needed,
                gap.current_end,
                "fallback_extend_start",
                f"Extend start to 75% floor (-{extra_needed:.2f}s)",
                "start",
            ),
            "end": (
                gap.current_start,
                gap.current_end + extra_needed,
                "fallback_extend_end",
                f"Extend end to 75% floor (+{extra_needed:.2f}s)",
                "end",
            ),
        }

        for direction in direction_order:
            add_candidate(*single_side_specs[direction])

        if not any(
            candidate.is_clean and cls._is_single_sided_candidate(candidate.extend_type)
            for candidate in candidates
        ):
            half_extra = extra_needed / 2
            add_candidate(
                gap.current_start - half_extra,
                gap.current_end + half_extra,
                "fallback_extend_both",
                f"Extend both to 75% floor (-{half_extra:.2f}s, +{half_extra:.2f}s)",
            )

        return sorted(candidates, key=cls._candidate_sort_key)

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
