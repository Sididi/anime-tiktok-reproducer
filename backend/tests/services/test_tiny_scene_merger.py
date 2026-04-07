from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.models.scene import Scene, SceneList


def _make_scenes(*ranges: tuple[float, float]) -> SceneList:
    return SceneList(
        scenes=[
            Scene(index=i, start_time=s, end_time=e)
            for i, (s, e) in enumerate(ranges)
        ]
    )


THRESHOLD = 0.35


class TestMergeTinyScenes:
    def test_no_tiny_scenes_unchanged(self):
        sl = _make_scenes((0.0, 1.0), (1.0, 2.5), (2.5, 4.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 3
        assert log == []

    def test_tiny_scene_in_middle_merges_into_previous(self):
        sl = _make_scenes((0.0, 2.0), (2.0, 2.2), (2.2, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 2
        assert merged.scenes[0].start_time == 0.0
        assert merged.scenes[0].end_time == 2.2
        assert merged.scenes[1].start_time == 2.2
        assert merged.scenes[1].end_time == 5.0
        assert log == [(1, 0)]

    def test_tiny_scene_at_start_merges_forward(self):
        sl = _make_scenes((0.0, 0.2), (0.2, 3.0), (3.0, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 2
        assert merged.scenes[0].start_time == 0.0
        assert merged.scenes[0].end_time == 3.0
        assert log == [(0, 1)]

    def test_consecutive_tiny_scenes_at_start_merge_forward(self):
        sl = _make_scenes((0.0, 0.1), (0.1, 0.3), (0.3, 3.0), (3.0, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 2
        assert merged.scenes[0].start_time == 0.0
        assert merged.scenes[0].end_time == 3.0
        assert log == [(0, 2), (1, 2)]

    def test_tiny_scene_at_end_merges_into_previous(self):
        sl = _make_scenes((0.0, 2.0), (2.0, 4.0), (4.0, 4.15))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 2
        assert merged.scenes[1].end_time == 4.15
        assert log == [(2, 1)]

    def test_multiple_non_adjacent_tiny_scenes(self):
        sl = _make_scenes(
            (0.0, 2.0), (2.0, 2.1), (2.1, 4.0), (4.0, 4.2), (4.2, 6.0)
        )
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 3
        assert merged.scenes[0].end_time == 2.1
        assert merged.scenes[1].end_time == 4.2
        assert log == [(1, 0), (3, 2)]

    def test_adjacent_tiny_scenes_in_middle_cascade(self):
        sl = _make_scenes((0.0, 2.0), (2.0, 2.15), (2.15, 2.3), (2.3, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 2
        assert merged.scenes[0].start_time == 0.0
        assert merged.scenes[0].end_time == 2.3
        assert merged.scenes[1].start_time == 2.3
        # Both tiny scenes absorbed into scene 0
        assert (1, 0) in log
        assert (2, 0) in log

    def test_all_scenes_tiny_returns_unchanged(self):
        sl = _make_scenes((0.0, 0.1), (0.1, 0.2), (0.2, 0.3))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 3
        assert log == []

    def test_single_scene_returns_unchanged(self):
        sl = _make_scenes((0.0, 0.1))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 1
        assert log == []

    def test_continuity_preserved(self):
        sl = _make_scenes(
            (0.0, 0.1), (0.1, 2.0), (2.0, 2.2), (2.2, 4.0), (4.0, 4.1)
        )
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert merged.validate_continuity()

    def test_threshold_boundary_not_merged(self):
        """A scene exactly at the threshold should NOT be merged."""
        sl = _make_scenes((0.0, 2.0), (2.0, 2.35), (2.35, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(merged.scenes) == 3
        assert log == []

    def test_indices_renumbered_after_merge(self):
        sl = _make_scenes((0.0, 2.0), (2.0, 2.1), (2.1, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert [s.index for s in merged.scenes] == [0, 1]

    def test_original_not_mutated(self):
        sl = _make_scenes((0.0, 2.0), (2.0, 2.1), (2.1, 5.0))
        merged, log = sl.merge_tiny_scenes(THRESHOLD)
        assert len(sl.scenes) == 3
        assert len(merged.scenes) == 2
