"""Identity matcher for Pure projects.

Pure mode reproduces one of our own published TikToks from its downloaded
output: the tiktok video itself is the only source. Matching is therefore an
identity mapping — scene N of the tiktok maps to that same time range of the
tiktok file. No index, no searcher, no GPU, no hydration.

The matches carry the video's absolute path as ``episode``:
``AnimeLibraryService.resolve_episode_path`` passes absolute existing paths
through unchanged, so identity matches flow through processing, playback
preparation, gap resolution and export exactly like library matches.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models.match import MatchList, SceneMatch
from ..models.scene import SceneList

logger = logging.getLogger("uvicorn.error")

# Boundary borrowing (Pure gap resolution): a neighbour keeps at least this
# much headroom above its own speed-floor requirement after lending time.
BORROW_SAFETY_SECONDS = 0.05
# A scene's source window never shrinks below this.
MIN_SCENE_SOURCE_SECONDS = 0.2


class PureMatcherService:
    """Dependency-free identity matcher for Pure projects."""

    @staticmethod
    def _identity_match(episode: str, scene_index: int, start_time: float, end_time: float,
                        merged_from: list[int] | None = None) -> SceneMatch:
        return SceneMatch(
            scene_index=scene_index,
            episode=episode,
            start_time=start_time,
            end_time=end_time,
            confidence=1.0,
            speed_ratio=1.0,
            confirmed=True,
            merged_from=merged_from,
        )

    @classmethod
    def build_identity_matches(cls, video_path: Path, scenes: SceneList) -> MatchList:
        """One trivially-correct identity match per scene."""
        episode = str(video_path)
        return MatchList(
            matches=[
                cls._identity_match(
                    episode, scene.index, scene.start_time, scene.end_time
                )
                for scene in scenes.scenes
            ]
        )

    @classmethod
    def rematch_scene(
        cls,
        video_path: Path,
        scenes: SceneList,
        *,
        scene_index: int,
        existing_matches: MatchList,
        merged_from: list[int] | None = None,
    ) -> MatchList:
        """Pure branch of manual merge-with-previous.

        Replaces the merged scene's match with an identity match over the
        merged span; all other matches are kept as-is.
        """
        scene = next((s for s in scenes.scenes if s.index == scene_index), None)
        if scene is None:
            raise ValueError(f"Scene index {scene_index} not found")

        replacement = cls._identity_match(
            str(video_path), scene.index, scene.start_time, scene.end_time,
            merged_from=merged_from,
        )

        matches = [m for m in existing_matches.matches if m.scene_index != scene_index]
        matches.append(replacement)
        matches.sort(key=lambda m: m.scene_index)
        return MatchList(matches=matches)

    # ------------------------------------------------------------------
    # Gap resolution (fully automatic, Pure only)
    # ------------------------------------------------------------------

    @classmethod
    def resolve_gaps_by_borrowing(
        cls,
        matches: list[SceneMatch],
        gaps: list,  # list[GapInfo] — kept untyped to avoid a circular import
        scene_timings: list[dict],
        *,
        min_speed: float,
    ) -> list[str]:
        """Resolve Pure-mode gaps by moving cuts, not by adding footage.

        In Pure mode the source IS the tiktok: the footage adjacent to a
        gapped scene belongs to its neighbours, so extending a source window
        (the anime resolution) would show the same frames twice. Instead the
        cut itself moves — the gapped scene takes source time from a
        neighbour whose narration underfills its own window, subject to the
        neighbour keeping its speed floor. Mutates ``matches`` in place and
        returns human-readable log lines. Residual gaps (nothing left to
        borrow) stay at the floor and are reported loudly.
        """
        by_index: dict[int, SceneMatch] = {m.scene_index: m for m in matches}
        ordered = sorted(by_index)

        def narration_seconds(scene_index: int) -> float | None:
            timing = next(
                (s for s in scene_timings if s.get("scene_index") == scene_index),
                None,
            )
            if timing is None:
                return None
            if timing.get("is_raw"):
                # Raw scenes replay their own source audio 1:1 — their window
                # must not move.
                return None
            start, end = timing.get("start_time"), timing.get("end_time")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
                return float(end) - float(start)
            words = timing.get("words") or []
            if words:
                return float(words[-1]["end"]) - float(words[0]["start"])
            return None

        def slack(scene_index: int) -> float:
            """Source seconds this scene can lend and still hold the floor."""
            match = by_index.get(scene_index)
            if match is None:
                return 0.0
            narration = narration_seconds(scene_index)
            if narration is None:
                return 0.0
            duration = match.end_time - match.start_time
            required = min_speed * narration + BORROW_SAFETY_SECONDS
            return max(0.0, min(duration - required, duration - MIN_SCENE_SOURCE_SECONDS))

        report: list[str] = []
        for gap in sorted(gaps, key=lambda g: g.scene_index):
            match = by_index.get(gap.scene_index)
            if match is None:
                continue
            # target_duration is frame-snapped by GapResolutionService with
            # the same OTIO math as JSX generation — authoritative.
            narration = float(getattr(gap, "target_duration", 0.0) or 0.0)
            if narration <= 0:
                narration = narration_seconds(gap.scene_index) or 0.0
            if narration <= 0:
                continue
            duration = match.end_time - match.start_time
            needed = max(0.0, min_speed * narration - duration)
            if needed <= 0:
                continue

            position = ordered.index(gap.scene_index)
            prev_index = ordered[position - 1] if position > 0 else None
            next_index = (
                ordered[position + 1] if position + 1 < len(ordered) else None
            )

            borrowed_parts: list[str] = []
            # Borrow from the side with more slack first.
            for neighbour_index in sorted(
                [i for i in (prev_index, next_index) if i is not None],
                key=slack,
                reverse=True,
            ):
                if needed <= 0:
                    break
                available = slack(neighbour_index)
                take = min(needed, available)
                if take <= 1e-3:
                    continue
                neighbour = by_index[neighbour_index]
                if neighbour_index == prev_index:
                    neighbour.end_time -= take
                    match.start_time -= take
                else:
                    neighbour.start_time += take
                    match.end_time += take
                narration_n = narration_seconds(neighbour_index) or 0.0
                if narration_n > 0:
                    neighbour.speed_ratio = (
                        (neighbour.end_time - neighbour.start_time) / narration_n
                    )
                needed -= take
                borrowed_parts.append(
                    f"{take:.2f}s from scene {neighbour_index}"
                )

            match.speed_ratio = (match.end_time - match.start_time) / narration
            if borrowed_parts:
                report.append(
                    f"Scene {gap.scene_index}: borrowed "
                    + " + ".join(borrowed_parts)
                    + f" (speed now {match.speed_ratio:.2f}x)"
                )
            if needed > 1e-3:
                message = (
                    f"Scene {gap.scene_index}: {needed:.2f}s gap remains after "
                    f"borrowing — plays at the {min_speed:.2f}x floor with a "
                    f"shortfall"
                )
                logger.warning("Pure gap resolution: %s", message)
                report.append(message)
        return report
