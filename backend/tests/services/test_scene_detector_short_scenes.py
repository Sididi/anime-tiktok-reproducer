from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))


def _scene_detector_service():
    scene_detector_module = pytest.importorskip("app.services.scene_detector")
    return scene_detector_module.SceneDetectorService


def test_extreme_short_threshold_adapts_to_fps():
    SceneDetectorService = _scene_detector_service()
    assert SceneDetectorService._extreme_short_threshold_seconds(60.0) == 0.08
    assert SceneDetectorService._extreme_short_threshold_seconds(24.0) == 0.125


def test_short_middle_scene_merges_with_next_when_right_boundary_disappears(monkeypatch):
    SceneDetectorService = _scene_detector_service()
    base_ranges = [(0.0, 2.0), (2.0, 2.05), (2.05, 5.0)]

    def fake_detect_ranges(video_path: Path, threshold: float, min_scene_len: int):
        return [(0.0, 2.0), (2.0, 5.0)], 5.0, 24.0

    monkeypatch.setattr(
        SceneDetectorService,
        "_detect_ranges",
        staticmethod(fake_detect_ranges),
    )

    sanitized = SceneDetectorService._sanitize_extreme_short_ranges(
        video_path=Path("dummy.mp4"),
        ranges=base_ranges,
        threshold=18.0,
        min_scene_len=10,
        fps=24.0,
    )

    assert sanitized == [(0.0, 2.0), (2.0, 5.0)]


def test_short_middle_scene_merges_with_previous_when_left_boundary_disappears(monkeypatch):
    SceneDetectorService = _scene_detector_service()
    base_ranges = [(0.0, 2.0), (2.0, 2.05), (2.05, 5.0)]

    def fake_detect_ranges(video_path: Path, threshold: float, min_scene_len: int):
        return [(0.0, 2.05), (2.05, 5.0)], 5.0, 24.0

    monkeypatch.setattr(
        SceneDetectorService,
        "_detect_ranges",
        staticmethod(fake_detect_ranges),
    )

    sanitized = SceneDetectorService._sanitize_extreme_short_ranges(
        video_path=Path("dummy.mp4"),
        ranges=base_ranges,
        threshold=18.0,
        min_scene_len=10,
        fps=24.0,
    )

    assert sanitized == [(0.0, 2.05), (2.05, 5.0)]
