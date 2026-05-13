from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import MatchCandidate, MatchList, Scene, SceneList, SceneMatch
from app.services import anime_matcher as matcher_module
from app.services.anime_matcher import AnimeMatcherService, MatchProposal
from app.services.scene_merger import SceneMergerService


def _write_index_fixture(library_path: Path, frame_count: int) -> None:
    shard_key = "Kill_Blue_658fd4d3"
    index_dir = library_path / ".index"
    shard_dir = index_dir / "series" / shard_key
    shard_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 4,
                "engine_profile": "sscd_exact_resize_v1",
                "config": {"default_fps": 2.0},
                "series": {
                    "Kill Blue": {
                        "key": shard_key,
                        "frames": frame_count,
                        "fps": 2.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (index_dir / "state.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    (shard_dir / "faiss.index").write_bytes(f"faiss-{frame_count}".encode())
    (shard_dir / "metadata.json").write_text(
        json.dumps({"series": "Kill Blue", "frames": list(range(frame_count))}),
        encoding="utf-8",
    )


def test_index_signature_tracks_requested_series_shard_changes(tmp_path: Path) -> None:
    library_path = tmp_path / "anime"
    _write_index_fixture(library_path, 2)
    before = AnimeMatcherService._index_signature(library_path, "Kill Blue")

    _write_index_fixture(library_path, 3)
    after = AnimeMatcherService._index_signature(library_path, "Kill Blue")

    assert before != after


class _FakeIndexManager:
    def __init__(self, frame_count: int) -> None:
        self.frame_count = frame_count

    def get_series_frame_count(self, series: str) -> int:
        return self.frame_count


class _FakeEmbedder:
    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        return np.ones((len(images), 2), dtype=np.float32)


def test_seeded_large_series_crop_search_uses_local_windows(monkeypatch) -> None:
    images = [Image.new("RGB", (9, 16), "black")]
    episode_paths = {f"ep{i}": Path(f"/tmp/ep{i}.mp4") for i in range(8)}
    local_calls: list[dict[str, object]] = []

    monkeypatch.setattr(AnimeMatcherService, "_embedder", _FakeEmbedder())
    monkeypatch.setattr(AnimeMatcherService, "_index_manager", _FakeIndexManager(50000))
    monkeypatch.setattr(
        AnimeMatcherService,
        "_series_episode_paths",
        classmethod(lambda cls, series, library_type: episode_paths),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_load_or_build_crop_index",
        classmethod(
            lambda cls, episode_path, *, target_aspect: (_ for _ in ()).throw(
                AssertionError("full-episode crop index should not be built")
            )
        ),
    )

    def fake_local(cls, images, **kwargs):
        local_calls.append(kwargs)
        return [
            [
                MatchCandidate(
                    episode="ep7",
                    timestamp=12.0,
                    similarity=0.9,
                    series="large-series",
                )
            ]
        ]

    monkeypatch.setattr(
        AnimeMatcherService,
        "_search_local_crop_windows_batch",
        classmethod(fake_local),
    )

    results = AnimeMatcherService._search_crop_index_batch(
        images,
        series="large-series",
        library_type="anime",
        episode_names=["ep7", "ep2"],
        anchor_candidates=(
            [MatchCandidate(episode="ep7", timestamp=12.0, similarity=0.8, series="s")],
            [],
            [],
        ),
        top_n=3,
    )

    assert [candidate.episode for candidate in results[0]] == ["ep7"]
    assert len(local_calls) == 1
    assert local_calls[0]["episode_names"] == ["ep7", "ep2"]


def test_unseeded_crop_search_keeps_large_series_conservative(monkeypatch) -> None:
    images = [Image.new("RGB", (9, 16), "black")]
    built: list[str] = []

    monkeypatch.setattr(AnimeMatcherService, "_embedder", _FakeEmbedder())
    monkeypatch.setattr(AnimeMatcherService, "_index_manager", _FakeIndexManager(50000))
    monkeypatch.setattr(
        AnimeMatcherService,
        "_series_episode_paths",
        classmethod(
            lambda cls, series, library_type: {
                f"ep{i}": Path(f"/tmp/ep{i}.mp4")
                for i in range(8)
            }
        ),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_load_or_build_crop_index",
        classmethod(
            lambda cls, episode_path, *, target_aspect: built.append(episode_path.stem)
        ),
    )

    results = AnimeMatcherService._search_crop_index_batch(
        images,
        series="large-series",
        library_type="anime",
        episode_names=None,
        top_n=3,
    )

    assert built == []
    assert results == [[]]


def test_crop_search_skips_failed_episode_index(monkeypatch) -> None:
    images = [Image.new("RGB", (9, 16), "black")]

    monkeypatch.setattr(AnimeMatcherService, "_embedder", _FakeEmbedder())
    monkeypatch.setattr(AnimeMatcherService, "_index_manager", _FakeIndexManager(100))
    monkeypatch.setattr(
        AnimeMatcherService,
        "_series_episode_paths",
        classmethod(
            lambda cls, series, library_type: {
                "bad": Path("/tmp/bad.mp4"),
                "good": Path("/tmp/good.mp4"),
            }
        ),
    )

    def fake_load(cls, episode_path: Path, *, target_aspect: float):
        if episode_path.stem == "bad":
            raise RuntimeError("cuda oom")
        return {
            "embeddings": np.ones((1, 2), dtype=np.float32),
            "timestamps": np.asarray([5.0], dtype=np.float32),
        }

    monkeypatch.setattr(
        AnimeMatcherService,
        "_load_or_build_crop_index",
        classmethod(fake_load),
    )

    results = AnimeMatcherService._search_crop_index_batch(
        images,
        series="large-series",
        library_type="anime",
        episode_names=["bad", "good"],
        top_n=3,
    )

    assert [candidate.episode for candidate in results[0]] == ["good"]


def test_local_crop_search_respects_source_crop_cap(monkeypatch) -> None:
    images = [Image.new("RGB", (9, 16), "black")]
    embed_batch_sizes: list[int] = []

    class CountingEmbedder:
        def embed_batch(self, batch_images: list[Image.Image]) -> np.ndarray:
            embed_batch_sizes.append(len(batch_images))
            return np.ones((len(batch_images), 2), dtype=np.float32)

    monkeypatch.setattr(AnimeMatcherService, "_embedder", CountingEmbedder())
    monkeypatch.setattr(AnimeMatcherService, "LOCAL_CROP_MAX_SOURCE_CROPS_PER_SCENE", 5)
    monkeypatch.setattr(AnimeMatcherService, "CROP_INDEX_BATCH_SIZE", 3)
    monkeypatch.setattr(
        AnimeMatcherService,
        "_series_episode_paths",
        classmethod(
            lambda cls, series, library_type: {
                "ep1": Path("/tmp/ep1.mp4"),
                "ep2": Path("/tmp/ep2.mp4"),
            }
        ),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_local_crop_sample_times",
        classmethod(lambda cls, anchors: [1.0, 1.5, 2.0]),
    )

    def fake_collect(
        cls,
        episode_path: Path,
        sample_times: list[float],
        *,
        target_aspect: float,
        remaining_crop_budget: int,
    ):
        return (
            [Image.new("RGB", (9, 16), "white") for _ in range(remaining_crop_budget)],
            [float(index) for index in range(remaining_crop_budget)],
        )

    monkeypatch.setattr(
        AnimeMatcherService,
        "_collect_local_crop_variants",
        classmethod(fake_collect),
    )

    results = AnimeMatcherService._search_local_crop_windows_batch(
        images,
        series="large-series",
        library_type="anime",
        episode_names=["ep1", "ep2"],
        anchor_candidates=(
            [
                MatchCandidate(episode="ep1", timestamp=10.0, similarity=0.9, series="s"),
                MatchCandidate(episode="ep2", timestamp=20.0, similarity=0.8, series="s"),
            ],
            [],
            [],
        ),
        top_n=3,
    )

    assert results[0]
    # Source crops embed in batches [3, 2], then the single query frame embeds.
    assert embed_batch_sizes == [3, 2, 1]


def test_crop_search_trigger_skips_high_confidence_direct_match() -> None:
    match = SceneMatch(
        scene_index=0,
        episode="E1",
        start_time=10.0,
        end_time=12.0,
        confidence=0.7,
        speed_ratio=1.0,
    )

    assert AnimeMatcherService._should_try_crop_search("Series", 2.0, match) is False
    assert AnimeMatcherService._should_try_crop_search("Series", 2.0, None) is True


def test_batched_probe_search_preserves_scene_positions(monkeypatch) -> None:
    probe_frames = {
        4: (
            Image.new("RGB", (8, 8), "red"),
            Image.new("RGB", (8, 8), "green"),
            Image.new("RGB", (8, 8), "blue"),
        ),
        9: (
            Image.new("RGB", (8, 8), "white"),
            Image.new("RGB", (8, 8), "gray"),
            Image.new("RGB", (8, 8), "black"),
        ),
    }
    calls: list[int] = []

    class Result:
        def __init__(self, episode: str, timestamp: float) -> None:
            self.episode = episode
            self.timestamp = timestamp
            self.similarity = 0.9
            self.series = "Series"

    def fake_search(cls, images, **kwargs):
        calls.append(len(images))
        return [
            [Result(f"ep-{len(calls)}-{index}", float(index))]
            for index, _ in enumerate(images)
        ]

    monkeypatch.setattr(
        AnimeMatcherService,
        "_search_image_batch",
        classmethod(fake_search),
    )

    results = AnimeMatcherService._search_scene_probe_candidates_batch(
        probe_frames,
        top_n=25,
        threshold=None,
        flip=False,
        series="Series",
        batch_size=4,
    )

    assert calls == [4, 2]
    assert results[4][0][0].episode == "ep-1-0"
    assert results[4][1][0].episode == "ep-1-1"
    assert results[4][2][0].episode == "ep-1-2"
    assert results[9][0][0].episode == "ep-1-3"
    assert results[9][1][0].episode == "ep-2-0"
    assert results[9][2][0].episode == "ep-2-1"


def test_batched_probe_search_skips_incomplete_frame_triples(monkeypatch) -> None:
    probe_frames = {
        1: (
            Image.new("RGB", (8, 8), "red"),
            None,
            Image.new("RGB", (8, 8), "blue"),
        )
    }

    monkeypatch.setattr(
        AnimeMatcherService,
        "_search_image_batch",
        classmethod(
            lambda cls, images, **kwargs: (_ for _ in ()).throw(
                AssertionError("incomplete triples should not be searched")
            )
        ),
    )

    results = AnimeMatcherService._search_scene_probe_candidates_batch(
        probe_frames,
        top_n=25,
        threshold=None,
        flip=False,
        series="Series",
    )

    assert results[1] == ([], [], [])


def test_proposal_ranking_uses_reliability_adjusted_selection_score() -> None:
    high_similarity = MatchProposal(
        episode="E1",
        start_time=10.0,
        end_time=11.0,
        confidence=0.80,
        selection_score=0.80,
        source="direct",
    )
    lower_similarity_with_bonus = MatchProposal(
        episode="E2",
        start_time=20.0,
        end_time=21.0,
        confidence=0.79,
        selection_score=0.95,
        source="refined",
    )

    ranked = AnimeMatcherService._dedupe_proposals(
        [lower_similarity_with_bonus, high_similarity]
    )

    assert ranked[0] is lower_similarity_with_bonus


def test_proposal_ranking_ties_prefer_votes_then_refined_then_crop() -> None:
    direct = MatchProposal(
        episode="E1",
        start_time=10.0,
        end_time=11.0,
        confidence=0.80,
        selection_score=0.80,
        source="direct",
        vote_count=1,
    )
    crop = MatchProposal(
        episode="E2",
        start_time=20.0,
        end_time=21.0,
        confidence=0.80,
        selection_score=0.80,
        source="crop",
        vote_count=1,
    )
    refined = MatchProposal(
        episode="E3",
        start_time=30.0,
        end_time=31.0,
        confidence=0.80,
        selection_score=0.80,
        source="refined",
        vote_count=1,
    )
    voted = MatchProposal(
        episode="E4",
        start_time=40.0,
        end_time=41.0,
        confidence=0.80,
        selection_score=0.80,
        source="direct",
        vote_count=3,
    )

    ranked = AnimeMatcherService._dedupe_proposals([direct, crop, refined, voted])

    assert [proposal.episode for proposal in ranked] == ["E4", "E3", "E2", "E1"]


def _short_scenes(count: int) -> SceneList:
    return SceneList(
        scenes=[
            Scene(index=index, start_time=float(index), end_time=float(index + 1))
            for index in range(count)
        ]
    )


def _empty_matches(count: int) -> MatchList:
    return MatchList(
        matches=[
            SceneMatch(
                scene_index=index,
                episode="",
                start_time=0.0,
                end_time=0.0,
                confidence=0.0,
                speed_ratio=1.0,
                was_no_match=True,
            )
            for index in range(count)
        ]
    )


def test_stabilizer_keeps_empty_placeholders_as_no_match() -> None:
    scenes = _short_scenes(35)
    matches = _empty_matches(35)
    matches.matches[0] = SceneMatch(
        scene_index=0,
        episode="E1",
        start_time=10.0,
        end_time=11.0,
        confidence=0.8,
        speed_ratio=1.0,
    )

    result = AnimeMatcherService._stabilize_short_scene_sequence(scenes, matches)

    assert result.matches[26].episode == ""
    assert result.matches[26].start_time == 0.0
    assert result.matches[26].end_time == 0.0
    assert result.matches[26].confidence == 0.0
    assert result.matches[26].was_no_match is True


def test_stabilizer_can_apply_real_projected_candidates() -> None:
    scenes = _short_scenes(35)
    matches = _empty_matches(35)
    matches.matches[0] = SceneMatch(
        scene_index=0,
        episode="E1",
        start_time=10.0,
        end_time=11.0,
        confidence=0.8,
        speed_ratio=1.0,
    )
    matches.matches[26].start_candidates = [
        MatchCandidate(episode="E1", timestamp=20.0, similarity=0.75, series="Series")
    ]

    result = AnimeMatcherService._stabilize_short_scene_sequence(scenes, matches)

    assert result.matches[26].episode == "E1"
    assert result.matches[26].start_time == 20.0
    assert result.matches[26].end_time == 21.0
    assert result.matches[26].was_no_match is False
    assert any(
        alt.episode == "E1"
        and alt.start_time == 20.0
        and alt.end_time == 21.0
        and alt.algorithm == "continuity"
        for alt in result.matches[26].alternatives
    )


def test_monotonic_source_stabilizer_prefers_consistent_alternative() -> None:
    scenes = _short_scenes(12)
    matches = MatchList()
    for index in range(12):
        start_time = 10.0 + index
        matches.matches.append(
            SceneMatch(
                scene_index=index,
                episode="E1",
                start_time=start_time,
                end_time=start_time + 1.0,
                confidence=0.75,
                speed_ratio=1.0,
            )
        )

    matches.matches[5].start_time = 80.0
    matches.matches[5].end_time = 81.0
    matches.matches[5].confidence = 0.82
    matches.matches[5].alternatives = [
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=15.0,
            end_time=16.0,
            confidence=0.74,
            speed_ratio=1.0,
            vote_count=3,
            algorithm="direct",
        )
    ]

    result = AnimeMatcherService._stabilize_monotonic_source_sequence(scenes, matches)

    assert result.matches[5].start_time == 15.0
    assert result.matches[5].end_time == 16.0
    assert result.matches[5].alternatives[0].algorithm == "continuity"


def test_monotonic_boundary_recovery_prefers_direct_then_weighted() -> None:
    scenes = _short_scenes(2)
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=20.5,
                end_time=21.5,
                confidence=0.82,
                speed_ratio=1.0,
                alternatives=[
                    matcher_module.AlternativeMatch(
                        episode="E1",
                        start_time=20.5,
                        end_time=21.5,
                        confidence=0.82,
                        speed_ratio=1.0,
                        vote_count=3,
                        algorithm="continuity",
                    ),
                    matcher_module.AlternativeMatch(
                        episode="E1",
                        start_time=20.0,
                        end_time=21.0,
                        confidence=0.50,
                        speed_ratio=1.0,
                        vote_count=3,
                        algorithm="direct",
                    ),
                ],
            ),
            SceneMatch(
                scene_index=1,
                episode="E1",
                start_time=30.5,
                end_time=31.5,
                confidence=0.74,
                speed_ratio=1.0,
                alternatives=[
                    matcher_module.AlternativeMatch(
                        episode="E1",
                        start_time=30.5,
                        end_time=31.5,
                        confidence=0.74,
                        speed_ratio=1.0,
                        vote_count=3,
                        algorithm="continuity",
                    ),
                    matcher_module.AlternativeMatch(
                        episode="E1",
                        start_time=30.0,
                        end_time=31.0,
                        confidence=0.66,
                        speed_ratio=1.0,
                        vote_count=12,
                        algorithm="weighted_avg",
                    ),
                ],
            ),
        ]
    )

    result = AnimeMatcherService._recover_monotonic_boundary_alternatives(
        scenes,
        matches,
    )

    assert result.matches[0].start_time == 20.0
    assert result.matches[0].end_time == 21.0
    assert result.matches[0].alternatives[0].algorithm == "monotonic_direct"
    assert result.matches[1].start_time == 30.0
    assert result.matches[1].end_time == 31.0
    assert result.matches[1].alternatives[0].algorithm == "monotonic_weighted_avg"


def test_dense_non_monotonic_merge_requires_visual_support() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=0.6),
            Scene(index=1, start_time=0.6, end_time=1.45),
            Scene(index=2, start_time=1.45, end_time=2.8),
        ]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=20.0,
                end_time=20.6,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=1,
                episode="E1",
                start_time=300.0,
                end_time=300.85,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=2,
                episode="E1",
                start_time=500.0,
                end_time=501.35,
                confidence=0.8,
                speed_ratio=1.0,
            ),
        ]
    )

    assert SceneMergerService._build_non_monotonic_dense_chains(
        scenes,
        matches,
        {0: 0.41},
    ) == []
    assert SceneMergerService._build_non_monotonic_dense_chains(
        scenes,
        matches,
        {0: 0.85},
    ) == [[0, 1]]


def test_dense_non_monotonic_merge_absorbs_short_backward_jump() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.35),
            Scene(index=1, start_time=1.35, end_time=2.25),
            Scene(index=2, start_time=2.25, end_time=3.5),
        ]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=680.0,
                end_time=681.4,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=1,
                episode="E1",
                start_time=640.0,
                end_time=640.9,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=2,
                episode="E1",
                start_time=700.0,
                end_time=701.2,
                confidence=0.8,
                speed_ratio=1.0,
            ),
        ]
    )

    assert SceneMergerService._build_non_monotonic_dense_chains(
        scenes,
        matches,
        {},
    ) == [[0, 1]]


def test_dense_non_monotonic_merge_absorbs_visual_short_medium_fragment() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=0.95),
            Scene(index=1, start_time=0.95, end_time=2.35),
            Scene(index=2, start_time=2.35, end_time=3.6),
            Scene(index=3, start_time=3.6, end_time=4.9),
        ]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=953.70,
                end_time=954.65,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=1,
                episode="E1",
                start_time=955.20,
                end_time=956.60,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=2,
                episode="E1",
                start_time=962.0,
                end_time=963.2,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=3,
                episode="E1",
                start_time=1100.0,
                end_time=1101.3,
                confidence=0.8,
                speed_ratio=1.0,
            ),
        ]
    )

    assert SceneMergerService._build_non_monotonic_dense_chains(
        scenes,
        matches,
        {0: 0.29},
    ) == []
    assert SceneMergerService._build_non_monotonic_dense_chains(
        scenes,
        matches,
        {0: 0.33},
    ) == [[0, 1]]


def test_dense_visual_boundary_snap_selects_strong_local_cut() -> None:
    scenes = _short_scenes(45)
    moves = SceneMergerService._dense_visual_boundary_snap_candidates(
        scenes,
        [0.70, 1.00, 1.35],
        [20.0, 30.0, 82.0],
    )

    assert moves == {0: 1.35}


def test_dense_visual_boundary_snap_ignores_sparse_projects() -> None:
    scenes = _short_scenes(20)
    moves = SceneMergerService._dense_visual_boundary_snap_candidates(
        scenes,
        [0.70, 1.00, 1.35],
        [20.0, 30.0, 82.0],
    )

    assert moves == {}


def test_snap_dense_visual_boundaries_updates_adjacent_scene_edges(
    monkeypatch,
) -> None:
    scenes = _short_scenes(45)
    monkeypatch.setattr(
        SceneMergerService,
        "_video_frame_diffs",
        classmethod(lambda cls, video_path: ([0.70, 1.00, 1.35], [20.0, 30.0, 82.0])),
    )

    snapped = SceneMergerService.snap_dense_visual_boundaries(
        Path("/tmp/source.mp4"),
        scenes,
    )

    assert snapped.scenes[0].end_time == 1.35
    assert snapped.scenes[1].start_time == 1.35
    assert scenes.scenes[0].end_time == 1.0
    assert snapped.validate_continuity()


def test_merge_scenes_preserves_same_episode_source_seed() -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=10.0, end_time=11.0),
            Scene(index=1, start_time=11.0, end_time=12.4),
        ]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=953.70,
                end_time=954.65,
                confidence=0.79,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=1,
                episode="E1",
                start_time=955.20,
                end_time=956.62,
                confidence=0.84,
                speed_ratio=1.0,
            ),
        ]
    )

    merged_scenes, merged_matches, _ = SceneMergerService.merge_scenes_and_matches(
        scenes,
        matches,
        [[0, 1]],
    )

    assert [(scene.start_time, scene.end_time) for scene in merged_scenes.scenes] == [
        (10.0, 12.4)
    ]
    assert merged_matches.matches[0].episode == "E1"
    assert merged_matches.matches[0].start_time == 953.70
    assert merged_matches.matches[0].end_time == 956.62
    assert merged_matches.matches[0].was_no_match is False
    assert merged_matches.matches[0].merged_from == [0, 1]


def test_dense_promotion_prefers_tied_continuity_alternative() -> None:
    scenes = _short_scenes(45)
    matches = MatchList()
    for index in range(45):
        matches.matches.append(
            SceneMatch(
                scene_index=index,
                episode="E1",
                start_time=100.0 + index,
                end_time=101.0 + index,
                confidence=0.6,
                speed_ratio=1.0,
            )
        )

    target = matches.matches[40]
    target.start_time = 908.0
    target.end_time = 908.8
    target.confidence = 0.77
    target.alternatives = [
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=906.65,
            end_time=907.47,
            confidence=0.77,
            speed_ratio=1.0,
            vote_count=1,
            algorithm="continuity",
        )
    ]

    result = AnimeMatcherService._promote_dense_short_alternatives(scenes, matches)

    assert result.matches[40].start_time == 906.65
    assert result.matches[40].end_time == 907.47


def test_duration_consistent_weighted_alternative_can_adjust_nearby_bounds() -> None:
    scenes = SceneList(
        scenes=[Scene(index=0, start_time=0.0, end_time=0.62)]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=937.56,
                end_time=938.02,
                confidence=0.86,
                speed_ratio=1.0,
                alternatives=[
                    matcher_module.AlternativeMatch(
                        episode="E1",
                        start_time=937.69,
                        end_time=938.31,
                        confidence=0.79,
                        speed_ratio=1.0,
                        vote_count=6,
                        algorithm="weighted_avg",
                    )
                ],
            )
        ]
    )

    result = AnimeMatcherService._promote_duration_consistent_weighted_alternatives(
        scenes,
        matches,
    )

    assert result.matches[0].start_time == 937.69
    assert result.matches[0].end_time == 938.31


def test_underfilled_source_interval_can_extend_to_supported_end() -> None:
    scenes = SceneList(
        scenes=[Scene(index=0, start_time=0.0, end_time=5.16)]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=653.0,
                end_time=657.0,
                confidence=0.50,
                speed_ratio=1.29,
                start_candidates=[
                    MatchCandidate(
                        episode="E1",
                        timestamp=653.0,
                        similarity=0.46,
                        series="Series",
                    )
                ],
                end_candidates=[
                    MatchCandidate(
                        episode="E1",
                        timestamp=658.0,
                        similarity=0.44,
                        series="Series",
                    )
                ],
            )
        ]
    )

    result = AnimeMatcherService._extend_underfilled_source_end_candidates(
        scenes,
        matches,
    )

    assert result.matches[0].start_time == 653.0
    assert result.matches[0].end_time == 658.0
    assert result.matches[0].alternatives[0].algorithm == "duration_end"


def test_monotonic_speed_floor_can_extend_short_alternative() -> None:
    scenes = _short_scenes(12)
    scenes.scenes[5].start_time = 5.0
    scenes.scenes[5].end_time = 6.3
    matches = MatchList()
    for index in range(12):
        start_time = 10.0 + index * 3.0
        matches.matches.append(
            SceneMatch(
                scene_index=index,
                episode="E1",
                start_time=start_time,
                end_time=start_time + 1.3,
                confidence=0.70,
                speed_ratio=1.0,
            )
        )

    target = matches.matches[5]
    target.start_time = 696.5
    target.end_time = 697.8
    target.alternatives = [
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=698.5,
            end_time=700.0,
            confidence=0.56,
            speed_ratio=0.87,
            vote_count=12,
            algorithm="weighted_avg",
        )
    ]
    target.end_candidates = [
        MatchCandidate(
            episode="E1",
            timestamp=700.0,
            similarity=0.48,
            series="Series",
        )
    ]

    result = AnimeMatcherService._extend_monotonic_speed_floor_alternatives(
        scenes,
        matches,
    )

    assert result.matches[5].start_time == 698.5
    assert result.matches[5].end_time == 700.5
    assert result.matches[5].alternatives[0].algorithm == "monotonic_speed_floor"


def test_short_end_projection_can_use_strong_start_anchor() -> None:
    scenes = SceneList(
        scenes=[Scene(index=0, start_time=0.0, end_time=0.88)]
    )
    matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="E1",
                start_time=739.56,
                end_time=740.44,
                confidence=0.85,
                speed_ratio=1.0,
                alternatives=[
                    matcher_module.AlternativeMatch(
                        episode="E1",
                        start_time=739.56,
                        end_time=740.44,
                        confidence=0.85,
                        speed_ratio=1.0,
                        vote_count=1,
                        algorithm="end",
                    )
                ],
                start_candidates=[
                    MatchCandidate(
                        episode="E1",
                        timestamp=736.0,
                        similarity=0.83,
                        series="Series",
                    )
                ],
                middle_candidates=[
                    MatchCandidate(
                        episode="E1",
                        timestamp=736.5,
                        similarity=0.84,
                        series="Series",
                    )
                ],
            )
        ]
    )

    result = AnimeMatcherService._promote_short_end_projection_start_anchors(
        scenes,
        matches,
    )

    assert result.matches[0].start_time == 736.0
    assert result.matches[0].end_time == 736.88
    assert result.matches[0].alternatives[0].algorithm == "start_anchor"


def test_dense_local_source_outlier_promotes_bracketed_alternative() -> None:
    scenes = _short_scenes(45)
    matches = MatchList()
    for index in range(45):
        start_time = 900.0 + index * 3.0
        matches.matches.append(
            SceneMatch(
                scene_index=index,
                episode="E1",
                start_time=start_time,
                end_time=start_time + 1.0,
                confidence=0.70,
                speed_ratio=1.0,
            )
        )

    matches.matches[19].start_time = 705.45
    matches.matches[19].end_time = 706.55
    matches.matches[20].start_time = 628.5
    matches.matches[20].end_time = 630.0
    matches.matches[20].confidence = 0.68
    matches.matches[20].alternatives = [
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=628.5,
            end_time=630.0,
            confidence=0.68,
            speed_ratio=1.0,
            vote_count=1,
            algorithm="end",
        ),
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=716.0,
            end_time=717.5,
            confidence=0.42,
            speed_ratio=1.0,
            vote_count=1,
            algorithm="end",
        ),
    ]
    matches.matches[21].start_time = 718.22
    matches.matches[21].end_time = 718.80

    result = AnimeMatcherService._promote_dense_local_source_alternatives(
        scenes,
        matches,
    )

    assert result.matches[20].start_time == 716.0
    assert result.matches[20].end_time == 717.5
    assert result.matches[20].alternatives[0].algorithm == "local_bracket"


def test_dense_local_source_gap_prefers_high_vote_earlier_alternative() -> None:
    scenes = _short_scenes(45)
    scenes.scenes[30].start_time = 30.0
    scenes.scenes[30].end_time = 30.85
    matches = MatchList()
    for index in range(45):
        start_time = 900.0 + index * 3.0
        matches.matches.append(
            SceneMatch(
                scene_index=index,
                episode="E1",
                start_time=start_time,
                end_time=start_time + 1.0,
                confidence=0.70,
                speed_ratio=1.0,
            )
        )

    matches.matches[29].start_time = 747.58
    matches.matches[29].end_time = 748.46
    matches.matches[30].start_time = 750.50
    matches.matches[30].end_time = 751.35
    matches.matches[30].confidence = 0.97
    matches.matches[30].alternatives = [
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=750.50,
            end_time=751.35,
            confidence=0.97,
            speed_ratio=1.0,
            vote_count=1,
            algorithm="continuity",
        ),
        matcher_module.AlternativeMatch(
            episode="E1",
            start_time=749.58,
            end_time=750.43,
            confidence=0.57,
            speed_ratio=1.0,
            vote_count=14,
            algorithm="weighted_avg",
        ),
    ]
    matches.matches[31].start_time = 752.65
    matches.matches[31].end_time = 754.0

    result = AnimeMatcherService._promote_dense_local_source_alternatives(
        scenes,
        matches,
    )

    assert result.matches[30].start_time == 749.58
    assert result.matches[30].end_time == 750.43
    assert result.matches[30].alternatives[0].algorithm == "local_gap"


def test_finalized_primary_is_visible_as_candidate() -> None:
    scene = Scene(index=0, start_time=0.0, end_time=1.0)

    match = AnimeMatcherService._build_match_from_proposals(
        scene,
        [
            MatchProposal(
                episode="E1",
                start_time=10.0,
                end_time=11.0,
                confidence=0.9,
                selection_score=0.95,
                source="refined",
                vote_count=1,
            ),
            MatchProposal(
                episode="E1",
                start_time=9.5,
                end_time=10.5,
                confidence=0.88,
                selection_score=0.88,
                source="best_frame",
                vote_count=1,
            ),
        ],
    )

    assert match.episode == "E1"
    assert match.start_time == 10.0
    assert match.end_time == 11.0
    assert match.alternatives[0].algorithm == "refined"
    assert any(
        alt.episode == match.episode
        and alt.start_time == match.start_time
        and alt.end_time == match.end_time
        for alt in match.alternatives
    )


def test_validation_repairs_primary_absent_from_alternatives() -> None:
    scene = Scene(index=0, start_time=0.0, end_time=1.0)
    match = SceneMatch(
        scene_index=0,
        episode="wrong",
        start_time=100.0,
        end_time=101.0,
        confidence=0.7,
        speed_ratio=1.0,
        alternatives=[
            matcher_module.AlternativeMatch(
                episode="right",
                start_time=10.0,
                end_time=11.0,
                confidence=0.8,
                speed_ratio=1.0,
                vote_count=1,
                algorithm="best_frame",
            )
        ],
    )

    repaired = AnimeMatcherService._validate_and_repair_match(scene, match)

    assert repaired.episode == "right"
    assert repaired.start_time == 10.0
    assert repaired.end_time == 11.0
    assert any(
        alt.episode == repaired.episode
        and alt.start_time == repaired.start_time
        and alt.end_time == repaired.end_time
        for alt in repaired.alternatives
    )


def test_no_match_with_frame_candidates_gets_alternatives() -> None:
    scene = Scene(index=0, start_time=0.0, end_time=1.0)
    match = SceneMatch(
        scene_index=0,
        episode="",
        start_time=0.0,
        end_time=0.0,
        confidence=0.0,
        speed_ratio=1.0,
        was_no_match=True,
        start_candidates=[
            MatchCandidate(episode="E1", timestamp=10.0, similarity=0.7, series="S")
        ],
        middle_candidates=[
            MatchCandidate(episode="E1", timestamp=10.5, similarity=0.7, series="S")
        ],
        end_candidates=[
            MatchCandidate(episode="E1", timestamp=11.0, similarity=0.7, series="S")
        ],
    )

    repaired = AnimeMatcherService._validate_and_repair_match(scene, match)

    assert repaired.episode == ""
    assert repaired.was_no_match is True
    assert repaired.alternatives
    assert repaired.start_candidates


def test_init_searcher_preloads_cv2_before_searcher_import(
    monkeypatch,
    tmp_path: Path,
) -> None:
    order: list[str] = []
    model_path = tmp_path / "sscd.pt"
    model_path.write_bytes(b"model")

    class FakeIndexManager:
        def __init__(self, library_path: Path) -> None:
            order.append("index_manager_init")
            self.library_path = library_path

        def load_or_create(self) -> None:
            pass

        def get_series_list(self) -> list[str]:
            return []

    class FakeEmbedder:
        def __init__(self, model_path: Path, precision: str) -> None:
            order.append("embedder_init")

    class FakeQueryProcessor:
        def __init__(self, index_manager, embedder) -> None:
            order.append("query_processor_init")

    fake_modules = {
        "anime_searcher": types.ModuleType("anime_searcher"),
        "anime_searcher.indexer": types.ModuleType("anime_searcher.indexer"),
        "anime_searcher.searcher": types.ModuleType("anime_searcher.searcher"),
        "anime_searcher.indexer.embedder": types.ModuleType(
            "anime_searcher.indexer.embedder"
        ),
        "anime_searcher.indexer.index_manager": types.ModuleType(
            "anime_searcher.indexer.index_manager"
        ),
        "anime_searcher.searcher.query": types.ModuleType("anime_searcher.searcher.query"),
    }
    fake_modules["anime_searcher.indexer.embedder"].SSCDEmbedder = FakeEmbedder
    fake_modules["anime_searcher.indexer.index_manager"].IndexManager = FakeIndexManager
    fake_modules["anime_searcher.searcher.query"].QueryProcessor = FakeQueryProcessor
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    original_import = builtins.__import__

    def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("anime_searcher"):
            order.append(f"import:{name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    monkeypatch.setattr(
        AnimeMatcherService,
        "_require_cv2",
        staticmethod(lambda: order.append("cv2") or object()),
    )
    monkeypatch.setattr(matcher_module.settings, "sscd_model_path", model_path)
    monkeypatch.setattr(matcher_module.settings, "anime_searcher_path", tmp_path)
    monkeypatch.setattr(AnimeMatcherService, "_index_signature", classmethod(lambda *args: ()))
    monkeypatch.setattr(AnimeMatcherService, "_loaded_library_path", None)
    monkeypatch.setattr(AnimeMatcherService, "_loaded_library_type", None)
    monkeypatch.setattr(AnimeMatcherService, "_query_processor", None)
    monkeypatch.setattr(AnimeMatcherService, "_index_manager", None)

    assert AnimeMatcherService._init_searcher(tmp_path / "library", "anime", "Series")

    assert order[0] == "cv2"
    assert order.index("cv2") < order.index("import:anime_searcher.indexer.embedder")
