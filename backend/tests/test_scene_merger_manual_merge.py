from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import MatchList, Scene, SceneList, SceneMatch
from app.services.scene_merger import SceneMergerService


def _match(index: int) -> SceneMatch:
    return SceneMatch(
        scene_index=index,
        episode="E1",
        start_time=100.0 + index,
        end_time=101.0 + index,
        confidence=0.8,
        speed_ratio=1.0,
    )


def test_manual_merge_preserves_unrelated_existing_overlap() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
            Scene(index=2, start_time=2.0, end_time=3.0),
            Scene(index=3, start_time=2.9, end_time=4.0),
        ]
    )
    matches = MatchList(matches=[_match(index) for index in range(4)])

    merged_scenes, merged_matches, _backup, merged_index = (
        SceneMergerService.prepare_manual_merge_with_previous(
            "missing-test-project",
            1,
            scenes,
            matches,
        )
    )

    assert merged_index == 0
    assert [(scene.start_time, scene.end_time) for scene in merged_scenes.scenes] == [
        (0.0, 2.0),
        (2.0, 3.0),
        (2.9, 4.0),
    ]
    assert [match.scene_index for match in merged_matches.matches] == [0, 1, 2]


def test_stale_merge_backup_is_reanchored_after_manual_split() -> None:
    backup_scenes = [
        Scene(index=0, start_time=0.0, end_time=1.0).model_dump(),
        Scene(index=1, start_time=1.0, end_time=2.0).model_dump(),
        Scene(index=2, start_time=2.0, end_time=3.0).model_dump(),
        Scene(index=3, start_time=3.0, end_time=4.0).model_dump(),
    ]
    backup_matches = [_match(index).model_dump() for index in range(4)]
    backup = {"scenes": backup_scenes, "matches": backup_matches, "chains": []}

    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=2.0),
            Scene(index=1, start_time=2.0, end_time=2.5),
            Scene(index=2, start_time=2.5, end_time=3.0),
            Scene(index=3, start_time=3.0, end_time=4.0),
        ]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=100.0,
                end_time=102.0,
                confidence=0.8,
                speed_ratio=1.0,
                merged_from=[0, 1],
            ),
            _match(1),
            _match(2),
            _match(3),
        ]
    )

    rebuilt_backup, original_groups, updated_matches = (
        SceneMergerService._rebuild_backup_for_current_timeline(
            backup,
            scenes,
            matches.matches,
        )
    )

    assert len(rebuilt_backup["scenes"]) == 5
    assert original_groups == [[0, 1], [2], [3], [4]]
    assert updated_matches[0].merged_from == [0, 1]
    assert rebuilt_backup["scenes"][2]["start_time"] == 2.0
    assert rebuilt_backup["scenes"][3]["start_time"] == 2.5
