import json
from fractions import Fraction
from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.models.match import SceneMatch
from app.services.anime_library import AnimeLibraryService
from app.services.gap_resolution import GapInfo, GapResolutionService
from app.services.project_service import ProjectService


PROJECT_ID = "3c6cbee7ce0c"
PROJECT_DIR = Path(__file__).resolve().parents[2] / "data" / "projects" / PROJECT_ID
DEFAULT_FPS = Fraction(24000, 1001)
DEFAULT_FRAME_OFFSET = float(GapResolutionService.SAFETY_FRAMES / float(DEFAULT_FPS))


def _make_match(
    scene_index: int,
    start_time: float,
    end_time: float,
    *,
    episode: str = "episode-a",
) -> SceneMatch:
    return SceneMatch(
        scene_index=scene_index,
        episode=episode,
        start_time=start_time,
        end_time=end_time,
        confidence=1.0,
        speed_ratio=1.0,
        confirmed=True,
    )


def _make_gap(
    scene_index: int,
    current_start: float,
    current_end: float,
    target_duration: float,
    *,
    episode: str = "episode-a",
) -> GapInfo:
    current_duration_frac = Fraction(current_end - current_start).limit_denominator(100000)
    target_duration_frac = Fraction(target_duration).limit_denominator(100000)
    required_speed = current_duration_frac / target_duration_frac
    actual_duration_frac = current_duration_frac / GapResolutionService.MIN_SPEED
    gap_duration = float(target_duration_frac - actual_duration_frac)
    return GapInfo(
        scene_index=scene_index,
        episode=episode,
        current_start=current_start,
        current_end=current_end,
        current_duration=float(current_duration_frac),
        timeline_start=0.0,
        timeline_end=target_duration,
        target_duration=target_duration,
        required_speed=required_speed,
        effective_speed=GapResolutionService.MIN_SPEED,
        gap_duration=gap_duration,
    )


def _install_fake_episode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    episode: str,
    cuts_by_threshold: dict[float, list[float]],
    fps: Fraction = DEFAULT_FPS,
    frame_offset: float = DEFAULT_FRAME_OFFSET,
) -> tuple[Path, list[float]]:
    episode_path = tmp_path / f"{episode}.mp4"
    episode_path.touch()
    seen_thresholds: list[float] = []

    async def fake_ensure_episode_manifest(cls, *, force_refresh: bool = False) -> dict:
        return {}

    def fake_resolve_episode_path(cls, episode_name: str, manifest: dict | None = None) -> Path | None:
        if episode_name == episode:
            return episode_path
        return None

    async def fake_detect_scene_cuts(
        cls,
        episode_path_arg: str,
        threshold: float | None = None,
        min_scene_len: int | None = None,
        frame_skip: int | None = None,
    ) -> list[float]:
        threshold_val = GapResolutionService.SCENE_THRESHOLD if threshold is None else float(threshold)
        seen_thresholds.append(threshold_val)
        return list(cuts_by_threshold.get(threshold_val, []))

    async def fake_get_frame_offset(cls, episode_path_arg: Path) -> float:
        return frame_offset

    async def fake_detect_video_fps(cls, episode_path_arg: Path) -> Fraction:
        return fps

    monkeypatch.setattr(
        AnimeLibraryService,
        "ensure_episode_manifest",
        classmethod(fake_ensure_episode_manifest),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(fake_resolve_episode_path),
    )
    monkeypatch.setattr(
        GapResolutionService,
        "detect_scene_cuts",
        classmethod(fake_detect_scene_cuts),
    )
    monkeypatch.setattr(
        GapResolutionService,
        "get_frame_offset",
        classmethod(fake_get_frame_offset),
    )
    monkeypatch.setattr(
        GapResolutionService,
        "detect_video_fps",
        classmethod(fake_detect_video_fps),
    )

    GapResolutionService._scene_cut_cache.clear()
    GapResolutionService._fps_cache.clear()

    return episode_path, seen_thresholds


@pytest.mark.asyncio
async def test_project_scene0_surfaces_clean_backward_candidate_and_autofill_uses_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    matches = ProjectService.load_matches(PROJECT_ID)
    assert matches is not None
    transcription = json.loads((PROJECT_DIR / "gap_detection_transcription.json").read_text())
    gaps = GapResolutionService.calculate_gaps(matches.matches, transcription["scenes"])
    gap = next(gap for gap in gaps if gap.scene_index == 0)

    _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={
            27.0: [0.0, 381.13075, 382.0483333333333, 382.79908333333333, 385.8855, 1495.035],
            18.0: [0.0, 382.2985833333333, 382.79908333333333, 385.8855, 1495.035],
            12.0: [0.0, 382.79908333333333, 383.71666666666664, 384.4674166666666, 385.8855, 1495.035],
        },
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches.matches,
        max_candidates=20,
    )

    clean_backward = [
        candidate
        for candidate in candidates
        if candidate.is_clean and candidate.start_time < gap.current_start and candidate.end_time <= gap.current_end
    ]
    assert clean_backward

    selection = await GapResolutionService.select_autofill_candidates_overlap_aware(
        matches=[
            next(match for match in matches.matches if match.scene_index == 0),
            next(match for match in matches.matches if match.scene_index == 1),
        ],
        gaps=[gap],
        candidates_by_scene={gap.scene_index: candidates},
    )
    selected = selection.selected_candidates_by_scene[gap.scene_index]
    next_scene = next(match for match in matches.matches if match.scene_index == 1)

    assert set(selection.selected_candidates_by_scene) == {gap.scene_index}
    assert selected.is_clean is True
    assert selected.is_cut_aligned is False
    assert selected.start_time < gap.current_start
    assert selected.end_time <= gap.current_end
    assert selected.end_time < next_scene.start_time
    assert selection.total_overlap_count == 0
    assert selection.total_overlap_seconds == 0.0


@pytest.mark.asyncio
async def test_clean_cut_candidate_ranks_ahead_of_clean_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=2.0)
    matches = [
        _make_match(0, 10.0, 11.0),
        _make_match(1, 12.5, 13.5),
    ]

    _, seen_thresholds = _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={27.0: [0.0, 9.0, 12.0, 100.0]},
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert seen_thresholds == [27.0]
    assert candidates
    assert candidates[0].is_cut_aligned is True
    assert candidates[0].is_clean is True
    assert candidates[0].extend_type in {"extend_to_scene_start", "extend_to_scene_end"}


@pytest.mark.asyncio
async def test_clean_fallback_ranks_ahead_of_overlapping_cut_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=2.0)
    matches = [
        _make_match(0, 10.0, 11.0),
        _make_match(1, 11.5, 12.5),
    ]

    _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={
            27.0: [0.0, 11.8, 100.0],
            18.0: [0.0, 11.8, 100.0],
            12.0: [0.0, 11.8, 100.0],
        },
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert candidates
    assert candidates[0].is_cut_aligned is False
    assert candidates[0].is_clean is True
    assert candidates[0].extend_type == "fallback_extend_start"

    selection = await GapResolutionService.select_autofill_candidates_overlap_aware(
        matches=matches,
        gaps=[gap],
        candidates_by_scene={gap.scene_index: candidates},
    )
    selected = selection.selected_candidates_by_scene[gap.scene_index]
    assert selected.is_cut_aligned is False
    assert selected.is_clean is True
    assert selection.total_overlap_count == 0


@pytest.mark.asyncio
async def test_threshold_cascade_runs_only_after_baseline_clean_cut_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=2.0)
    matches = [
        _make_match(0, 10.0, 11.0),
        _make_match(1, 11.5, 12.5),
    ]

    _, seen_thresholds = _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={
            27.0: [0.0, 11.8, 100.0],
            18.0: [0.0, 9.2, 100.0],
            12.0: [0.0, 8.0, 100.0],
        },
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert seen_thresholds == [27.0, 18.0]
    assert any(
        candidate.is_cut_aligned and candidate.is_clean and candidate.detector_threshold == 18.0
        for candidate in candidates
    )


@pytest.mark.asyncio
async def test_different_episode_neighbors_do_not_block_candidate_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=2.0)
    matches = [
        _make_match(0, 10.0, 11.0, episode="episode-a"),
        _make_match(1, 11.5, 12.5, episode="episode-b"),
    ]

    _, seen_thresholds = _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode="episode-a",
        cuts_by_threshold={27.0: [0.0, 11.8, 100.0]},
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert seen_thresholds == [27.0]
    assert candidates
    assert candidates[0].is_cut_aligned is True
    assert all(candidate.overlap_count == 0 for candidate in candidates)
