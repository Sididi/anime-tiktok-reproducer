from __future__ import annotations

import json

import pytest

from app.models import AlternativeMatch, MatchList, Scene, SceneList, SceneMatch
from app.services.matching_ground_truth_v2 import (
    GroundTruthSnapshot,
    acceptance_report,
    calibrate_confidence_leave_one_project_out,
    ensure_safe_output_dir,
    evaluate_timeline,
)


def _scenes(*ranges):
    return SceneList(
        scenes=[
            Scene(index=index, start_time=start, end_time=end)
            for index, (start, end) in enumerate(ranges)
        ]
    )


def _matches(*values):
    return MatchList(
        matches=[
            SceneMatch(
                scene_index=index,
                episode=episode,
                start_time=start,
                end_time=end,
                confidence=0.8,
                speed_ratio=1.0,
            )
            for index, (episode, start, end) in enumerate(values)
        ]
    )


def test_timeline_metric_is_invariant_to_continuous_merge():
    gt_scenes = _scenes((0.0, 1.0), (1.0, 2.0))
    gt_matches = _matches(("ep", 10.0, 11.0), ("ep", 11.0, 12.0))
    generated_scenes = _scenes((0.0, 2.0))
    generated_matches = _matches(("ep", 10.0, 12.0))
    metrics, _ = evaluate_timeline(
        gt_scenes, gt_matches, generated_scenes, generated_matches
    )
    assert metrics["exact_0_5_coverage"] == 1.0
    assert metrics["pass_1_0_coverage"] == 1.0
    assert metrics["fragmentation_ratio"] == 0.5


def test_wrong_episode_is_never_treated_as_timing_match():
    scenes = _scenes((0.0, 1.0))
    metrics, _ = evaluate_timeline(
        scenes,
        _matches(("ep-a", 10.0, 11.0)),
        scenes,
        _matches(("ep-b", 10.0, 11.0)),
    )
    assert metrics["wrong_episode_coverage"] == 1.0
    assert metrics["pass_1_0_coverage"] == 0.0


def test_half_second_is_exact_and_one_second_is_pass_only():
    scenes = _scenes((0.0, 1.0))
    gt = _matches(("ep", 10.0, 11.0))
    exact, _ = evaluate_timeline(scenes, gt, scenes, _matches(("ep", 10.5, 11.5)))
    loose, _ = evaluate_timeline(scenes, gt, scenes, _matches(("ep", 10.8, 11.8)))
    assert exact["exact_0_5_coverage"] == 1.0
    assert loose["exact_0_5_coverage"] == 0.0
    assert loose["pass_1_0_coverage"] == 1.0


def test_abstained_primary_can_recall_truth_in_top_seven():
    scenes = _scenes((0.0, 1.0))
    gt = _matches(("ep", 10.0, 11.0))
    generated = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="",
                start_time=0.0,
                end_time=0.0,
                confidence=0.0,
                speed_ratio=1.0,
                was_no_match=True,
                alternatives=[
                    AlternativeMatch(
                        episode="ep",
                        start_time=10.0,
                        end_time=11.0,
                        confidence=0.5,
                        speed_ratio=1.0,
                        vote_count=2,
                        algorithm="timeline_cluster",
                    )
                ],
            )
        ]
    )
    metrics, _ = evaluate_timeline(scenes, gt, scenes, generated)
    assert metrics["abstained_coverage"] == 1.0
    assert metrics["top7_pass_recall"] == 1.0


def test_snapshot_detects_mutation_and_output_rejects_project_tree(tmp_path):
    project = tmp_path / "project-id"
    project.mkdir()
    (project / "project.json").write_text(json.dumps({"id": "project-id"}))
    (project / "scenes.json").write_text(json.dumps({"scenes": []}))
    (project / "matches.json").write_text(json.dumps({"matches": []}))
    snapshot = GroundTruthSnapshot.capture(project)
    snapshot.assert_unchanged()
    with pytest.raises(ValueError):
        ensure_safe_output_dir(project / "evaluation", [snapshot])
    (project / "matches.json").write_text(json.dumps({"matches": [{"changed": True}]}))
    with pytest.raises(RuntimeError):
        snapshot.assert_unchanged()


def test_leave_one_project_out_uses_most_conservative_threshold():
    rows = {
        "one": [
            {"generated_source": 1.0, "generated_confidence": 0.9, "status": "exact"},
            {"generated_source": 2.0, "generated_confidence": 0.2, "status": "timing_error"},
        ],
        "two": [
            {"generated_source": 1.0, "generated_confidence": 0.8, "status": "pass"},
            {"generated_source": 2.0, "generated_confidence": 0.3, "status": "wrong_episode"},
        ],
    }
    result = calibrate_confidence_leave_one_project_out(rows)
    assert result["calibrated"] is True
    assert result["threshold"] == pytest.approx(0.9)


def test_acceptance_report_requires_speed_accuracy_baselines_and_three_runs():
    metrics = [
        {
            "project_id": "one",
            "duration_seconds": 60.0,
            "cold_seconds": 20.0,
            "warm_seconds": [10.0, 11.0],
            "samples": 100,
            "pass_1_0_coverage": 0.99,
            "exact_0_5_coverage": 0.91,
            "resolved_coverage": 1.0,
            "resolved_pass_precision": 0.99,
        }
    ]
    result = acceptance_report(
        metrics,
        exact_baselines={"one": 0.90},
        pass_baselines={"one": 0.99},
    )
    assert result["release_ready"] is True
    metrics[0]["warm_seconds"] = []
    assert acceptance_report(
        metrics,
        exact_baselines={"one": 0.90},
        pass_baselines={"one": 0.99},
    )["release_ready"] is False
