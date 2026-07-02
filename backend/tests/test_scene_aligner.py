from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Scene, SceneList
from app.services.scene_aligner import (
    Correspondence,
    MAX_EVIDENCE_SPEED,
    SceneAlignerService,
)


def _corr(
    sample_index: int,
    t_tiktok: float,
    t_source: float,
    episode: str = "episode-01.mkv",
    similarity: float = 0.82,
) -> Correspondence:
    return Correspondence(
        sample_index=sample_index,
        t_tiktok=t_tiktok,
        t_source=t_source,
        episode=episode,
        similarity=similarity,
        series="series",
        rank=0,
    )


def test_segment_extraction_recovers_fast_affine_segment() -> None:
    scene = Scene(index=0, start_time=10.0, end_time=11.0)
    speed = 4.07
    offset = 120.0
    correspondences = [
        _corr(i, t, speed * t + offset)
        for i, t in enumerate([10.0, 10.25, 10.5, 10.75])
    ]
    correspondences.extend(
        [
            _corr(10, 10.25, 45.0, episode="episode-02.mkv", similarity=0.91),
            _corr(11, 10.50, 300.0, episode="episode-01.mkv", similarity=0.50),
        ]
    )

    segments = SceneAlignerService.extract_scene_segments(
        SceneList(scenes=[scene]),
        correspondences,
    )[0]

    assert segments
    assert segments[0].episode == "episode-01.mkv"
    assert abs(segments[0].a - speed) < 0.05
    assert segments[0].a < MAX_EVIDENCE_SPEED
    assert segments[0].inlier_count == 4


def test_dp_keeps_non_monotonic_jump_when_emission_is_strong() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
        ]
    )
    correspondences = []
    for i, t in enumerate([0.0, 0.25, 0.5, 0.75]):
        correspondences.append(_corr(i, t, 200.0 + t, similarity=0.85))
    for i, t in enumerate([1.0, 1.25, 1.5, 1.75], start=4):
        correspondences.append(_corr(i, t, 118.0 + t, similarity=0.87))

    scene_segments = SceneAlignerService.extract_scene_segments(scenes, correspondences)
    decoded = SceneAlignerService.decode_scene_sequence(scenes, scene_segments)

    assert decoded[0] is not None
    assert decoded[1] is not None
    assert decoded[0].episode == decoded[1].episode
    assert decoded[1].source_at(scenes.scenes[1].start_time) < decoded[0].source_at(
        scenes.scenes[0].end_time
    )


def test_dp_allows_intruder_episode_with_coherent_support() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
            Scene(index=2, start_time=2.0, end_time=3.0),
        ]
    )
    correspondences = []
    for i, t in enumerate([0.0, 0.25, 0.5, 0.75]):
        correspondences.append(_corr(i, t, 50.0 + t, episode="episode-01.mkv"))
    for i, t in enumerate([1.0, 1.25, 1.5, 1.75], start=4):
        correspondences.append(_corr(i, t, 140.0 + t, episode="episode-03.mkv", similarity=0.90))
    for i, t in enumerate([2.0, 2.25, 2.5, 2.75], start=8):
        correspondences.append(_corr(i, t, 52.0 + t, episode="episode-01.mkv"))

    scene_segments = SceneAlignerService.extract_scene_segments(scenes, correspondences)
    decoded = SceneAlignerService.decode_scene_sequence(scenes, scene_segments)

    assert [segment.episode if segment else "" for segment in decoded] == [
        "episode-01.mkv",
        "episode-03.mkv",
        "episode-01.mkv",
    ]


def test_edge_pair_hypothesis_uses_actual_sample_times() -> None:
    scene = Scene(index=0, start_time=10.0, end_time=12.0)
    # Edge samples are inside the scene. The fitted line must extrapolate back
    # to the true scene boundaries rather than treating the edge samples as the
    # boundaries themselves.
    correspondences = [
        _corr(0, 10.125, 100.125, similarity=0.9),
        _corr(1, 11.875, 101.875, similarity=0.9),
    ]

    segments = SceneAlignerService._edge_pair_hypotheses(scene, correspondences)

    assert segments
    start, end = segments[0].source_interval(scene)
    assert abs(start - 100.0) < 1e-6
    assert abs(end - 102.0) < 1e-6
