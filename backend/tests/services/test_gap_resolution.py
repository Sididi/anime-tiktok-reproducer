import json
from fractions import Fraction
from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.models.match import MatchList, SceneMatch
from app.services.anime_library import AnimeLibraryService
from app.services.gap_resolution import GapCandidate, GapInfo, GapResolutionService


PROJECT_ID = "3c6cbee7ce0c"
PROJECT_DIR = Path(__file__).resolve().parents[2] / "data" / "projects" / PROJECT_ID
DEFAULT_FPS = Fraction(24000, 1001)
DEFAULT_FRAME_OFFSET = float(GapResolutionService.SAFETY_FRAMES / float(DEFAULT_FPS))
DEFAULT_TOLERANCE = float(Fraction(1, 1) / DEFAULT_FPS)


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


def _load_pre_gap_project_gap(scene_index: int) -> tuple[GapInfo, list[SceneMatch]]:
    match_list = MatchList.model_validate_json((PROJECT_DIR / "matches_before_gaps.json").read_text())
    transcription = json.loads((PROJECT_DIR / "gap_detection_transcription.json").read_text())
    gaps = GapResolutionService.calculate_gaps(match_list.matches, transcription["scenes"])
    return next(gap for gap in gaps if gap.scene_index == scene_index), match_list.matches


def _select_matches(matches: list[SceneMatch], *scene_indices: int) -> list[SceneMatch]:
    scene_index_set = set(scene_indices)
    return [match for match in matches if match.scene_index in scene_index_set]


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


def test_fixture_project_gap_output_is_stable():
    match_list = MatchList.model_validate_json((PROJECT_DIR / "matches_before_gaps.json").read_text())
    transcription = json.loads((PROJECT_DIR / "gap_detection_transcription.json").read_text())

    gaps = GapResolutionService.calculate_gaps(match_list.matches, transcription["scenes"])

    assert [gap.scene_index for gap in gaps] == [0, 2, 3, 10, 11, 16, 18, 25, 26, 41, 46]
    assert round(sum(gap.gap_duration for gap in gaps), 6) == pytest.approx(5.738692, abs=1e-6)
    assert [round(gap.gap_duration, 6) for gap in gaps] == [
        1.383333,
        0.7,
        1.066667,
        0.8,
        0.883333,
        0.1,
        0.35,
        0.333333,
        0.016667,
        0.072025,
        0.033333,
    ]


@pytest.mark.asyncio
async def test_project_scene0_surfaces_clean_backward_candidate_and_autofill_uses_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap, project_matches = _load_pre_gap_project_gap(0)
    matches = _select_matches(project_matches, 0, 1)

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
        matches=matches,
        max_candidates=20,
    )

    clean_backward = [
        candidate
        for candidate in candidates
        if candidate.is_clean and candidate.start_time < gap.current_start and candidate.end_time <= gap.current_end
    ]
    assert clean_backward

    selection = await GapResolutionService.select_autofill_candidates_overlap_aware(
        matches=matches,
        gaps=[gap],
        candidates_by_scene={gap.scene_index: candidates},
    )
    selected = selection.selected_candidates_by_scene[gap.scene_index]
    next_scene = next(match for match in matches if match.scene_index == 1)

    assert set(selection.selected_candidates_by_scene) == {gap.scene_index}
    assert selected.is_clean is True
    assert selected.is_cut_aligned is False
    assert selected.start_time < gap.current_start
    assert selected.end_time <= gap.current_end
    assert selected.end_time < next_scene.start_time
    assert selection.total_overlap_count == 0
    assert selection.total_overlap_seconds == 0.0


@pytest.mark.asyncio
async def test_project_scene10_prefers_forward_continuation_inside_tie_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap, project_matches = _load_pre_gap_project_gap(10)
    matches = _select_matches(project_matches, 9, 10, 11)

    _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={
            27.0: [
                0.0,
                490.07291666666663,
                491.24075,
                492.9090833333333,
                493.9935,
                1495.035,
            ],
        },
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert candidates
    assert candidates[0].extend_type == "extend_end"
    assert candidates[0].snap_description == "Extend end to next cut (+1.37s)"
    assert candidates[0].continuation_bias_applied is True
    assert candidates[0].clearance_side == "right"
    assert candidates[0].side_clearance_seconds is None

    competing_start = next(
        candidate
        for candidate in candidates
        if candidate.extend_type == "extend_start"
        and candidate.snap_description == "Extend start to previous cut (-1.30s)"
    )
    assert competing_start.continuation_bias_applied is False

    selection = await GapResolutionService.select_autofill_candidates_overlap_aware(
        matches=matches,
        gaps=[gap],
        candidates_by_scene={gap.scene_index: candidates},
    )
    assert selection.selected_candidates_by_scene[gap.scene_index].extend_type == "extend_end"


def test_next_timeline_neighbor_earlier_in_source_counts_as_left_blocker():
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=3.0)
    matches = [
        _make_match(0, 10.0, 11.0),
        _make_match(1, 8.4, 8.6),
    ]
    neighbor_context = GapResolutionService._build_neighbor_contexts(
        matches,
        episode_key_by_scene={0: "episode-a", 1: "episode-a"},
        tolerance_by_episode={"episode-a": DEFAULT_TOLERANCE},
    )[0]

    side_clearances = GapResolutionService._source_side_clearances(gap, neighbor_context)

    assert side_clearances["left"].has_blocker is True
    assert side_clearances["right"].has_blocker is False
    assert GapResolutionService._preferred_single_side_order(gap, neighbor_context) == ("end", "start")


@pytest.mark.asyncio
async def test_continuation_bias_prefers_more_open_side_within_tie_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=3.0)
    matches = [
        _make_match(0, 10.0, 11.0),
        _make_match(1, 8.4, 8.6),
    ]

    _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={27.0: [0.0, 8.7, 12.35, 100.0]},
        frame_offset=0.0,
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert candidates[0].extend_type == "extend_to_scene_end"
    assert candidates[0].continuation_bias_applied is True
    assert pytest.approx(candidates[0].added_duration, abs=1e-6) == 1.35

    competing_start = next(
        candidate for candidate in candidates if candidate.extend_type == "extend_to_scene_start"
    )
    assert pytest.approx(competing_start.added_duration, abs=1e-6) == 1.3


@pytest.mark.asyncio
async def test_project_scene26_keeps_smaller_added_duration_outside_tie_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    gap, project_matches = _load_pre_gap_project_gap(26)
    matches = _select_matches(project_matches, 25, 26, 27)

    _install_fake_episode(
        monkeypatch,
        tmp_path,
        episode=gap.episode,
        cuts_by_threshold={
            27.0: [
                0.0,
                726.0586666666666,
                727.727,
                728.2275,
                729.97925,
                1495.035,
            ],
        },
    )

    candidates = await GapResolutionService.generate_candidates(
        gap,
        matches=matches,
        max_candidates=20,
    )

    assert candidates[0].extend_type == "extend_start"
    assert candidates[0].snap_description == "Extend start to previous cut (-0.15s)"
    assert candidates[0].continuation_bias_applied is False


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


@pytest.mark.asyncio
async def test_generate_candidates_batch_dedup_reuses_cached_result(
    monkeypatch: pytest.MonkeyPatch,
):
    gap = _make_gap(scene_index=0, current_start=10.0, current_end=11.0, target_duration=2.0)
    matches = [_make_match(0, 10.0, 11.0)]
    call_count = 0

    async def fake_generate_candidates_batch(
        cls,
        gaps: list[GapInfo],
        matches: list[SceneMatch] | None = None,
        max_candidates: int = 6,
    ) -> dict[int, list[GapCandidate]]:
        nonlocal call_count
        call_count += 1
        return {
            gaps[0].scene_index: [
                GapCandidate(
                    start_time=9.0,
                    end_time=11.0,
                    duration=2.0,
                    effective_speed=Fraction(1, 1),
                    speed_diff=0.0,
                    extend_type="extend_start",
                    snap_description="cached candidate",
                )
            ]
        }

    monkeypatch.setattr(
        GapResolutionService,
        "generate_candidates_batch",
        classmethod(fake_generate_candidates_batch),
    )
    GapResolutionService._candidate_batch_inflight.clear()
    GapResolutionService._candidate_batch_result_cache.clear()

    first = await GapResolutionService.generate_candidates_batch_dedup(
        [gap],
        matches=matches,
    )
    second = await GapResolutionService.generate_candidates_batch_dedup(
        [gap],
        matches=matches,
    )

    assert call_count == 1
    assert first == second
    assert first is not second
    assert first[gap.scene_index] is not second[gap.scene_index]
