"""OpenTimelineIO-based timing utilities for frame-perfect calculations.

This module provides frame-accurate timing calculations using OTIO's
RationalTime primitives, avoiding float arithmetic drift that can occur
when converting between seconds and frames.
"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

from opentimelineio.opentime import RationalTime, TimeRange


class FrameRateInfo(NamedTuple):
    """Frame rate information with proper rational representation.

    Uses Fraction for exact frame rate representation, avoiding
    floating-point precision issues with rates like 23.976fps.
    """

    timebase: int  # e.g., 24, 30, 60
    ntsc: bool  # TRUE for 23.976, 29.97, 59.94 etc.

    @property
    def rate(self) -> Fraction:
        """Get the exact frame rate as a Fraction."""
        if self.ntsc:
            return Fraction(self.timebase * 1000, 1001)
        return Fraction(self.timebase, 1)

    @property
    def rate_float(self) -> float:
        """Get the frame rate as a float (for display/logging)."""
        return float(self.rate)

    def to_rational_time(self, seconds: float) -> RationalTime:
        """Convert seconds to RationalTime at this frame rate."""
        # Use the exact rate for conversion
        rate = float(self.rate)
        return RationalTime.from_seconds(seconds, rate)

    def frames_from_seconds(self, seconds: float) -> int:
        """Convert seconds to frame count at this frame rate."""
        rt = self.to_rational_time(seconds)
        return int(rt.to_frames())

    def seconds_from_frames(self, frames: int) -> float:
        """Convert frame count to seconds at this frame rate."""
        rate = float(self.rate)
        rt = RationalTime(frames, rate)
        return rt.to_seconds()

    @classmethod
    def from_fps(cls, fps: float) -> "FrameRateInfo":
        """Create FrameRateInfo from an approximate FPS value."""
        # Map common FPS values to timebase/ntsc pairs
        fps_mapping = {
            23.976: (24, True),
            24.0: (24, False),
            25.0: (25, False),
            29.97: (30, True),
            30.0: (30, False),
            50.0: (50, False),
            59.94: (60, True),
            60.0: (60, False),
        }

        for target_fps, (timebase, ntsc) in fps_mapping.items():
            if abs(fps - target_fps) < 0.05:
                return cls(timebase=timebase, ntsc=ntsc)

        # Fallback: use rounded fps, assume not NTSC
        return cls(timebase=round(fps), ntsc=False)


@dataclass
class ClipTiming:
    """Frame-perfect timing for a single clip.

    All timing values are stored as RationalTime for precision,
    with convenience properties for frame/second access.
    """

    scene_index: int
    source_path: Path
    bundle_filename: str

    # Source timings (in source media's frame rate)
    source_in: RationalTime
    source_out: RationalTime
    source_rate: FrameRateInfo

    # Timeline timings (in sequence frame rate)
    timeline_start: RationalTime
    timeline_end: RationalTime
    timeline_rate: FrameRateInfo

    # Speed information
    speed_ratio: Fraction  # source_duration / target_duration
    effective_speed: Fraction  # capped at 0.75 minimum
    leaves_gap: bool  # True if clip ends before next marker due to speed floor

    @property
    def source_in_seconds(self) -> float:
        return self.source_in.to_seconds()

    @property
    def source_out_seconds(self) -> float:
        return self.source_out.to_seconds()

    @property
    def source_in_frames(self) -> int:
        return int(self.source_in.to_frames())

    @property
    def source_out_frames(self) -> int:
        return int(self.source_out.to_frames())

    @property
    def timeline_start_seconds(self) -> float:
        return self.timeline_start.to_seconds()

    @property
    def timeline_end_seconds(self) -> float:
        return self.timeline_end.to_seconds()

    @property
    def timeline_start_frames(self) -> int:
        return int(self.timeline_start.to_frames())

    @property
    def timeline_end_frames(self) -> int:
        return int(self.timeline_end.to_frames())

    @property
    def source_duration(self) -> RationalTime:
        """Duration in source media's frame rate."""
        return self.source_out - self.source_in

    @property
    def target_duration(self) -> RationalTime:
        """Target duration on timeline (before speed adjustment)."""
        return self.timeline_end - self.timeline_start

    @property
    def actual_duration_seconds(self) -> float:
        """Actual clip duration after speed adjustment."""
        source_dur = self.source_duration.to_seconds()
        return source_dur / float(self.effective_speed)

    @property
    def actual_end_seconds(self) -> float:
        """Actual end position on timeline after speed adjustment."""
        return self.timeline_start_seconds + self.actual_duration_seconds

    @property
    def speed_percent(self) -> float:
        """Speed as a percentage (100 = normal speed)."""
        return float(self.effective_speed) * 100


@dataclass
class ContinuityIssue:
    """Represents a timing continuity issue (gap or overlap)."""

    issue_type: str  # 'gap' or 'overlap'
    between_scenes: tuple[int, int]  # (scene_a_index, scene_b_index)
    duration_seconds: float
    position_seconds: float  # Where the issue occurs


class OTIOTimingCalculator:
    """Calculator for frame-perfect timing using OpenTimelineIO.

    This class handles all timing calculations using OTIO's RationalTime
    to ensure frame-accurate positioning without floating-point drift.
    """

    # Default sequence rate: 60fps non-NTSC for smooth TikTok playback
    DEFAULT_SEQUENCE_RATE = FrameRateInfo(timebase=60, ntsc=False)
    # Default source rate: 23.976fps (common anime frame rate)
    DEFAULT_SOURCE_RATE = FrameRateInfo(timebase=24, ntsc=True)
    # Minimum speed (75% = 0.75)
    MIN_SPEED = Fraction(75, 100)

    def __init__(
        self,
        sequence_rate: FrameRateInfo | None = None,
        source_rate: FrameRateInfo | None = None,
    ):
        self.sequence_rate = sequence_rate or self.DEFAULT_SEQUENCE_RATE
        self.source_rate = source_rate or self.DEFAULT_SOURCE_RATE

    def seconds_to_timeline_time(self, seconds: float) -> RationalTime:
        """Convert seconds to RationalTime at sequence rate."""
        return self.sequence_rate.to_rational_time(seconds)

    def seconds_to_source_time(self, seconds: float) -> RationalTime:
        """Convert seconds to RationalTime at source rate."""
        return self.source_rate.to_rational_time(seconds)

    def calculate_speed(
        self,
        source_duration_seconds: float,
        target_duration_seconds: float,
    ) -> tuple[Fraction, Fraction, bool]:
        """Calculate speed ratio with 75% floor constraint.

        Args:
            source_duration_seconds: Duration of source clip
            target_duration_seconds: Desired duration on timeline

        Returns:
            Tuple of (speed_ratio, effective_speed, leaves_gap)
            - speed_ratio: raw calculation (source / target)
            - effective_speed: capped at MIN_SPEED
            - leaves_gap: True if speed was capped (clip won't fill target)
        """
        if target_duration_seconds <= 0:
            return Fraction(1), Fraction(1), False

        # Use Fraction for exact calculation
        # Limit denominator to avoid huge fractions from float conversion
        source_frac = Fraction(source_duration_seconds).limit_denominator(100000)
        target_frac = Fraction(target_duration_seconds).limit_denominator(100000)

        speed_ratio = source_frac / target_frac

        # Apply floor constraint
        leaves_gap = False
        if speed_ratio < self.MIN_SPEED:
            effective_speed = self.MIN_SPEED
            leaves_gap = True
        else:
            effective_speed = speed_ratio

        return speed_ratio, effective_speed, leaves_gap

    def calculate_clip_timing(
        self,
        scene_index: int,
        source_path: Path,
        bundle_filename: str,
        source_in_seconds: float,
        source_out_seconds: float,
        timeline_start_seconds: float,
        timeline_end_seconds: float,
    ) -> ClipTiming:
        """Calculate frame-perfect timing for a single clip.

        Args:
            scene_index: Scene number
            source_path: Path to source video file
            bundle_filename: Safe filename for the bundle
            source_in_seconds: Source in point (seconds)
            source_out_seconds: Source out point (seconds)
            timeline_start_seconds: Timeline position (seconds)
            timeline_end_seconds: Target end position (seconds)

        Returns:
            ClipTiming with all frame-accurate values
        """
        source_duration = source_out_seconds - source_in_seconds
        target_duration = timeline_end_seconds - timeline_start_seconds

        speed_ratio, effective_speed, leaves_gap = self.calculate_speed(
            source_duration, target_duration
        )

        return ClipTiming(
            scene_index=scene_index,
            source_path=source_path,
            bundle_filename=bundle_filename,
            source_in=self.seconds_to_source_time(source_in_seconds),
            source_out=self.seconds_to_source_time(source_out_seconds),
            source_rate=self.source_rate,
            timeline_start=self.seconds_to_timeline_time(timeline_start_seconds),
            timeline_end=self.seconds_to_timeline_time(timeline_end_seconds),
            timeline_rate=self.sequence_rate,
            speed_ratio=speed_ratio,
            effective_speed=effective_speed,
            leaves_gap=leaves_gap,
        )

    def validate_clip_continuity(
        self,
        clips: list[ClipTiming],
        tolerance_frames: int = 1,
    ) -> list[ContinuityIssue]:
        """Validate that clips form a continuous timeline.

        Checks for gaps and overlaps between consecutive clips,
        accounting for speed adjustments.

        Args:
            clips: List of ClipTiming objects (should be sorted by timeline_start)
            tolerance_frames: Number of frames of tolerance for gaps/overlaps

        Returns:
            List of ContinuityIssue objects describing any problems found
        """
        issues = []

        if len(clips) < 2:
            return issues

        # Sort by timeline start position
        sorted_clips = sorted(clips, key=lambda c: c.timeline_start_seconds)

        for i in range(len(sorted_clips) - 1):
            current = sorted_clips[i]
            next_clip = sorted_clips[i + 1]

            # Calculate where current clip actually ends (after speed adjustment)
            current_actual_end = current.actual_end_seconds
            next_start = next_clip.timeline_start_seconds

            # Calculate difference in frames
            diff_seconds = next_start - current_actual_end
            diff_frames = abs(self.sequence_rate.frames_from_seconds(diff_seconds))

            if diff_frames > tolerance_frames:
                if diff_seconds > 0:
                    # Gap between clips
                    issues.append(
                        ContinuityIssue(
                            issue_type="gap",
                            between_scenes=(current.scene_index, next_clip.scene_index),
                            duration_seconds=diff_seconds,
                            position_seconds=current_actual_end,
                        )
                    )
                else:
                    # Overlap between clips
                    issues.append(
                        ContinuityIssue(
                            issue_type="overlap",
                            between_scenes=(current.scene_index, next_clip.scene_index),
                            duration_seconds=abs(diff_seconds),
                            position_seconds=next_start,
                        )
                    )

        return issues

    def calculate_total_duration(self, clips: list[ClipTiming]) -> RationalTime:
        """Calculate total timeline duration from clips.

        Returns the end position of the last clip (accounting for speed).
        """
        if not clips:
            return RationalTime(0, float(self.sequence_rate.rate))

        # Find the clip that ends latest
        latest_end = 0.0
        for clip in clips:
            end = clip.actual_end_seconds
            if end > latest_end:
                latest_end = end

        return self.seconds_to_timeline_time(latest_end)
