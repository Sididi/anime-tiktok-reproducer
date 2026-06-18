from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate_matching_against_ground_truth as evaluator
from app.models import (
    AlternativeMatch,
    MatchCandidate,
    MatchList,
    Scene,
    SceneList,
    SceneMatch,
)


def _scene(index: int, start: float, end: float) -> Scene:
    return Scene(index=index, start_time=start, end_time=end)


def _match(
    index: int,
    episode: str = "E1",
    start: float | None = None,
    end: float | None = None,
    *,
    alternatives: list[AlternativeMatch] | None = None,
    start_candidates: list[MatchCandidate] | None = None,
    end_candidates: list[MatchCandidate] | None = None,
) -> SceneMatch:
    start_time = float(index * 10 if start is None else start)
    end_time = float(start_time + 2.0 if end is None else end)
    return SceneMatch(
        scene_index=index,
        episode=episode,
        start_time=start_time,
        end_time=end_time,
        confidence=0.8,
        speed_ratio=1.0,
        alternatives=alternatives or [],
        start_candidates=start_candidates or [],
        end_candidates=end_candidates or [],
    )


def _candidate(index: int, timestamp: float, episode: str = "E1") -> MatchCandidate:
    return MatchCandidate(
        episode=episode,
        timestamp=timestamp,
        similarity=0.8,
        series="Series",
    )


def _generated(
    scenes: list[Scene],
    matches: list[SceneMatch],
) -> evaluator.GeneratedResult:
    return evaluator.GeneratedResult(
        scenes=SceneList(scenes=scenes),
        matches=MatchList(matches=matches),
        elapsed_seconds=1.0,
    )


def _patch_ground_truth(
    monkeypatch,
    scenes: list[Scene],
    matches: list[SceneMatch],
) -> None:
    monkeypatch.setattr(
        evaluator,
        "_load_required",
        lambda project_id: (object(), SceneList(scenes=scenes), MatchList(matches=matches)),
    )


def test_strict_validator_rejects_scene_count_mismatch(monkeypatch) -> None:
    _patch_ground_truth(
        monkeypatch,
        [_scene(0, 0.0, 1.0), _scene(1, 1.0, 2.0)],
        [_match(0), _match(1)],
    )

    result = evaluator._validate_strict(
        "project",
        _generated([_scene(0, 0.0, 2.0)], [_match(0)]),
    )

    assert not result.passed
    assert "scene count mismatch" in result.rows[0]


def test_strict_validator_allows_at_most_three_loose_scene_timings(monkeypatch) -> None:
    gt_scenes = [_scene(index, index * 2.0, index * 2.0 + 2.0) for index in range(4)]
    gt_matches = [_match(index) for index in range(4)]
    _patch_ground_truth(monkeypatch, gt_scenes, gt_matches)

    generated_scenes = [
        _scene(index, scene.start_time + 0.4, scene.end_time + 0.4)
        for index, scene in enumerate(gt_scenes)
    ]

    result = evaluator._validate_strict(
        "project",
        _generated(generated_scenes, gt_matches),
    )

    assert not result.passed
    assert result.scene_loose == 4
    assert any("too many loose scene timings" in row for row in result.rows)


def test_strict_validator_allows_two_wrong_primaries_with_exposed_candidates(
    monkeypatch,
) -> None:
    gt_scenes = [_scene(index, index * 2.0, index * 2.0 + 2.0) for index in range(3)]
    gt_matches = [_match(index) for index in range(3)]
    _patch_ground_truth(monkeypatch, gt_scenes, gt_matches)

    wrong_with_candidates = [
        _match(
            0,
            episode="wrong",
            start=99.0,
            end=101.0,
            alternatives=[
                AlternativeMatch(
                    episode="E1",
                    start_time=0.0,
                    end_time=2.0,
                    confidence=0.7,
                    speed_ratio=1.0,
                )
            ],
        ),
        _match(
            1,
            episode="wrong",
            start=109.0,
            end=111.0,
            start_candidates=[_candidate(1, 10.0)],
            end_candidates=[_candidate(1, 12.0)],
        ),
        _match(2),
    ]

    result = evaluator._validate_strict(
        "project",
        _generated(gt_scenes, wrong_with_candidates),
    )

    assert result.passed
    assert result.wrong_primary_with_candidate == 2


def test_strict_validator_rejects_wrong_primary_without_exposed_candidate(
    monkeypatch,
) -> None:
    gt_scenes = [_scene(0, 0.0, 2.0)]
    gt_matches = [_match(0)]
    _patch_ground_truth(monkeypatch, gt_scenes, gt_matches)

    result = evaluator._validate_strict(
        "project",
        _generated(gt_scenes, [_match(0, episode="wrong", start=99.0, end=101.0)]),
    )

    assert not result.passed
    assert result.source_failed == 1
    assert any("missing candidate" in row for row in result.rows)
