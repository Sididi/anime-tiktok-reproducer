from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import AlternativeMatch, MatchCandidate, MatchList, Scene, SceneList, SceneMatch
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


def test_video_frame_embedding_cache_is_bounded_lru(monkeypatch) -> None:
    monkeypatch.setattr(
        AnimeMatcherService,
        "_video_frame_embedding_cache",
        type(AnimeMatcherService._video_frame_embedding_cache)(),
    )
    monkeypatch.setattr(
        AnimeMatcherService, "VIDEO_FRAME_EMBEDDING_CACHE_MAX", 4,
    )

    for idx in range(10):
        key = ("video", 0, 0, idx)
        AnimeMatcherService._store_video_frame_embedding(
            key, np.full((2,), idx, dtype=np.float32)
        )

    cache = AnimeMatcherService._video_frame_embedding_cache
    assert len(cache) == 4
    # Only the four most-recent inserts survive (LRU eviction of oldest).
    assert [k[3] for k in cache] == [6, 7, 8, 9]
    # A hit refreshes recency so the entry is not the next victim.
    assert AnimeMatcherService._get_cached_video_frame_embedding(
        ("video", 0, 0, 6)
    ) is not None
    AnimeMatcherService._store_video_frame_embedding(
        ("video", 0, 0, 10), np.zeros((2,), dtype=np.float32)
    )
    assert ("video", 0, 0, 6) in cache
    assert ("video", 0, 0, 7) not in cache


def test_partial_rematch_preserves_skipped_existing_matches() -> None:
    existing = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="Manual Episode",
                start_time=10.0,
                end_time=11.0,
                confidence=1.0,
                speed_ratio=1.0,
                confirmed=True,
            ),
            SceneMatch(
                scene_index=1,
                episode="Old Target",
                start_time=20.0,
                end_time=21.0,
                confidence=1.0,
                speed_ratio=1.0,
                confirmed=True,
            ),
            SceneMatch(
                scene_index=2,
                episode="Manual Tail",
                start_time=30.0,
                end_time=31.0,
                confidence=1.0,
                speed_ratio=1.0,
                confirmed=True,
                merged_from=[7, 8],
            ),
        ]
    )
    rematched = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="Mutated Episode",
                start_time=90.0,
                end_time=91.0,
                confidence=0.5,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=1,
                episode="New Target",
                start_time=22.0,
                end_time=23.0,
                confidence=0.8,
                speed_ratio=1.0,
            ),
            SceneMatch(
                scene_index=2,
                episode="Mutated Tail",
                start_time=99.0,
                end_time=100.0,
                confidence=0.5,
                speed_ratio=1.0,
                merged_from=[9],
            ),
        ]
    )

    result = AnimeMatcherService._preserve_skipped_partial_rematch_matches(
        rematched,
        existing,
        {1},
    )

    assert result.matches[0].episode == "Manual Episode"
    assert result.matches[0].start_time == 10.0
    assert result.matches[1].episode == "New Target"
    assert result.matches[1].start_time == 22.0
    assert result.matches[2].episode == "Manual Tail"
    assert result.matches[2].start_time == 30.0
    assert result.matches[2].merged_from == [7, 8]


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


class _BatchLimitedGpuEmbedder:
    def __init__(self, max_batch: int) -> None:
        self.max_batch = max_batch
        self.gpu_calls: list[int] = []
        self.cpu_calls: list[int] = []

    def embed_pil_batch_gpu(self, images: list[Image.Image]) -> np.ndarray:
        self.gpu_calls.append(len(images))
        if len(images) > self.max_batch:
            raise RuntimeError("CUDA out of memory while allocating tensor")
        return np.full((len(images), 2), len(images), dtype=np.float32)

    def embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        self.cpu_calls.append(len(images))
        return np.full((len(images), 2), -1.0, dtype=np.float32)


def test_embed_pil_batch_splits_gpu_batch_after_cuda_oom(monkeypatch) -> None:
    fake = _BatchLimitedGpuEmbedder(max_batch=2)
    monkeypatch.setattr(AnimeMatcherService, "_embedder", fake)
    AnimeMatcherService.reset_runtime_stats()

    images = [Image.new("RGB", (16, 16), "black") for _ in range(5)]
    embeddings = AnimeMatcherService._embed_pil_batch(images)

    assert embeddings.shape == (5, 2)
    assert fake.gpu_calls == [5, 2, 3, 1, 2]
    assert fake.cpu_calls == []
    assert AnimeMatcherService.get_runtime_stats()["sscd_embedding_oom_retries"] == 2


def test_embed_pil_batch_falls_back_to_cpu_path_for_single_image_oom(
    monkeypatch,
) -> None:
    fake = _BatchLimitedGpuEmbedder(max_batch=0)
    monkeypatch.setattr(AnimeMatcherService, "_embedder", fake)
    AnimeMatcherService.reset_runtime_stats()

    embeddings = AnimeMatcherService._embed_pil_batch(
        [Image.new("RGB", (4096, 4096), "black")]
    )

    assert embeddings.tolist() == [[-1.0, -1.0]]
    assert fake.gpu_calls == [1]
    assert fake.cpu_calls == [1]
    assert AnimeMatcherService.get_runtime_stats()["sscd_embedding_oom_retries"] == 1


def test_extract_frames_seeks_across_large_frame_gaps(monkeypatch) -> None:
    class FakeCV2:
        CAP_PROP_FPS = 1
        CAP_PROP_POS_FRAMES = 2
        COLOR_BGR2RGB = 3

        @staticmethod
        def cvtColor(frame, code):
            return frame

    class FakeCapture:
        def __init__(self) -> None:
            self.position = 0
            self.set_calls: list[tuple[int, int]] = []
            self.grab_calls = 0

        def get(self, prop: int) -> float:
            if prop == FakeCV2.CAP_PROP_FPS:
                return 30.0
            return 0.0

        def set(self, prop: int, value: int) -> None:
            self.set_calls.append((prop, int(value)))
            self.position = int(value)

        def grab(self) -> bool:
            self.grab_calls += 1
            self.position += 1
            return True

        def read(self):
            self.position += 1
            return True, np.zeros((2, 2, 3), dtype=np.uint8)

    cap = FakeCapture()
    monkeypatch.setattr(
        AnimeMatcherService,
        "_require_cv2",
        classmethod(lambda cls: FakeCV2),
    )

    frames = AnimeMatcherService._extract_frames_from_capture(cap, [0.0, 100.0])

    assert all(frame is not None for frame in frames)
    assert (FakeCV2.CAP_PROP_POS_FRAMES, 3000) in cap.set_calls
    assert cap.grab_calls < AnimeMatcherService.MAX_SEQUENTIAL_GRAB_FRAMES


def test_extract_frames_uses_presentation_timestamps_for_vfr(monkeypatch) -> None:
    class FakeCV2:
        CAP_PROP_FPS = 1
        CAP_PROP_POS_FRAMES = 2
        CAP_PROP_POS_MSEC = 3
        COLOR_BGR2RGB = 4

        @staticmethod
        def cvtColor(frame, code):
            return frame

    class FakeCapture:
        # Four frames spread across ten seconds: timestamp * average FPS would
        # map 2s to frame 1, while its actual PTS belongs to frame 2.
        pts = [0.0, 1.0, 2.0, 10.0]

        def __init__(self) -> None:
            self.next_index = 0
            self.last_index: int | None = None

        def get(self, prop: int) -> float:
            if prop == FakeCV2.CAP_PROP_FPS:
                return 0.4
            if prop == FakeCV2.CAP_PROP_POS_FRAMES:
                return float(self.next_index)
            if prop == FakeCV2.CAP_PROP_POS_MSEC:
                return (
                    self.pts[self.last_index] * 1000.0
                    if self.last_index is not None
                    else 0.0
                )
            return 0.0

        def set(self, prop: int, value: float) -> bool:
            if prop == FakeCV2.CAP_PROP_POS_MSEC:
                target = float(value) / 1000.0
                self.next_index = next(
                    (i for i, pts in enumerate(self.pts) if pts >= target),
                    len(self.pts),
                )
                self.last_index = None
            return True

        def read(self):
            if self.next_index >= len(self.pts):
                return False, None
            self.last_index = self.next_index
            self.next_index += 1
            frame = np.full((2, 2, 3), self.last_index, dtype=np.uint8)
            return True, frame

    monkeypatch.setattr(
        AnimeMatcherService,
        "_require_cv2",
        classmethod(lambda cls: FakeCV2),
    )

    frames = AnimeMatcherService._extract_frames_from_capture(
        FakeCapture(),
        [0.0, 2.0, 10.0],
    )

    assert [int(np.asarray(frame)[0, 0, 0]) for frame in frames] == [0, 2, 3]


def test_scene_merger_frame_diffs_use_presentation_timestamps(monkeypatch) -> None:
    class FakeCV2:
        CAP_PROP_FPS = 1
        CAP_PROP_POS_MSEC = 2
        COLOR_BGR2GRAY = 3
        INTER_AREA = 4

        @staticmethod
        def cvtColor(frame, code):
            return frame[:, :, 0]

        @staticmethod
        def resize(frame, size, interpolation):
            return frame

    class FakeCapture:
        pts = [0.0, 0.25, 2.0]

        def __init__(self) -> None:
            self.next_index = 0
            self.last_index: int | None = None

        def isOpened(self) -> bool:
            return True

        def get(self, prop: int) -> float:
            if prop == FakeCV2.CAP_PROP_FPS:
                return 1.5
            if prop == FakeCV2.CAP_PROP_POS_MSEC:
                return (
                    self.pts[self.last_index] * 1000.0
                    if self.last_index is not None
                    else 0.0
                )
            return 0.0

        def read(self):
            if self.next_index >= len(self.pts):
                return False, None
            self.last_index = self.next_index
            self.next_index += 1
            return True, np.full((2, 2, 3), self.last_index, dtype=np.uint8)

        def release(self) -> None:
            pass

    monkeypatch.setattr(
        FakeCV2,
        "VideoCapture",
        staticmethod(lambda path: FakeCapture()),
        raising=False,
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_require_cv2",
        classmethod(lambda cls: FakeCV2),
    )

    diff_times, diffs = SceneMergerService._video_frame_diffs(Path("vfr.mp4"))

    assert diff_times == [0.25, 2.0]
    assert diffs == [1.0, 1.0]


def test_tail_interval_candidates_include_exposed_alternatives() -> None:
    scene = Scene(index=0, start_time=0.0, end_time=2.0)
    match = SceneMatch(
        scene_index=0,
        episode="E1",
        start_time=10.0,
        end_time=12.0,
        confidence=0.8,
        speed_ratio=1.0,
        alternatives=[
            AlternativeMatch(
                episode="E1",
                start_time=20.0,
                end_time=22.0,
                confidence=0.7,
                speed_ratio=1.0,
                algorithm="weighted_avg",
            )
        ],
    )

    candidates = AnimeMatcherService._tail_interval_candidates(scene, match, "E1")

    assert any(
        candidate["start_time"] == 20.0 and candidate["end_time"] == 22.0
        for candidate in candidates
    )


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


def test_dense_visual_boundary_snap_skips_terminal_boundary() -> None:
    scenes = _short_scenes(45)
    terminal_boundary = scenes.scenes[-2].end_time
    moves = SceneMergerService._dense_visual_boundary_snap_candidates(
        scenes,
        [terminal_boundary + 0.25],
        [90.0],
    )

    assert moves == {}


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


def test_snap_dense_visual_boundaries_skips_adjacent_collapse(
    monkeypatch,
) -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=6.0, end_time=6.9),
            Scene(index=1, start_time=6.9, end_time=7.566),
            Scene(index=2, start_time=7.566, end_time=8.133),
            *[
                Scene(
                    index=index,
                    start_time=8.133 + (index - 3),
                    end_time=9.133 + (index - 3),
                )
                for index in range(3, 45)
            ],
        ]
    )
    monkeypatch.setattr(
        SceneMergerService,
        "_video_frame_diffs",
        classmethod(lambda cls, video_path: ([], [])),
    )
    monkeypatch.setattr(
        SceneMergerService,
        "_dense_visual_boundary_snap_candidates",
        classmethod(lambda cls, scenes, diff_times, diffs: {0: 7.15, 1: 7.15}),
    )

    snapped = SceneMergerService.snap_dense_visual_boundaries(
        Path("/tmp/source.mp4"),
        scenes,
    )

    assert snapped.scenes[0].end_time == 7.15
    assert snapped.scenes[1].start_time == 7.15
    assert snapped.scenes[1].end_time == 7.566
    assert snapped.validate_continuity()
    assert all(scene.duration > 0 for scene in snapped.scenes)


def test_snap_dense_visual_boundaries_ignores_terminal_boundary_move(
    monkeypatch,
) -> None:
    scenes = _short_scenes(45)
    terminal_index = len(scenes.scenes) - 2
    terminal_boundary = scenes.scenes[terminal_index].end_time
    monkeypatch.setattr(
        SceneMergerService,
        "_video_frame_diffs",
        classmethod(lambda cls, video_path: ([], [])),
    )
    monkeypatch.setattr(
        SceneMergerService,
        "_dense_visual_boundary_snap_candidates",
        classmethod(
            lambda cls, scenes, diff_times, diffs: {
                terminal_index: terminal_boundary + 0.25
            }
        ),
    )

    snapped = SceneMergerService.snap_dense_visual_boundaries(
        Path("/tmp/source.mp4"),
        scenes,
    )

    assert snapped.scenes[terminal_index].end_time == terminal_boundary
    assert snapped.scenes[terminal_index + 1].start_time == terminal_boundary


def test_append_terminal_tiny_chain_adds_final_transition() -> None:
    scenes = _short_scenes(45)
    scenes.scenes[-2].end_time = 44.6
    scenes.scenes[-1].start_time = 44.6
    scenes.scenes[-1].end_time = 45.0

    chains = SceneMergerService._append_terminal_tiny_chain(scenes, [[2, 3]])

    assert chains == [[2, 3], [43, 44]]


def test_append_terminal_tiny_chain_extends_existing_penultimate_chain() -> None:
    scenes = _short_scenes(45)
    scenes.scenes[-2].end_time = 44.6
    scenes.scenes[-1].start_time = 44.6
    scenes.scenes[-1].end_time = 45.0

    chains = SceneMergerService._append_terminal_tiny_chain(scenes, [[41, 42, 43]])

    assert chains == [[41, 42, 43, 44]]


def test_visual_boundary_similarities_batch_frame_extraction(monkeypatch) -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
            Scene(index=2, start_time=2.0, end_time=3.0),
        ]
    )
    calls: list[list[float]] = []

    class FakeEmbedder:
        def embed_batch(self, images):
            assert len(images) == 4
            return np.array(
                [
                    [1.0, 0.0],
                    [0.0, 1.0],
                    [1.0, 0.0],
                    [1.0, 0.0],
                ],
                dtype=np.float32,
            )

    monkeypatch.setattr(
        AnimeMatcherService,
        "_init_searcher",
        classmethod(lambda cls, library_path, library_type, anime_name: True),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_query_processor",
        types.SimpleNamespace(embedder=FakeEmbedder()),
    )

    def fake_extract_frames(video_path, timestamps):
        calls.append(list(timestamps))
        return [Image.new("RGB", (4, 4), "black") for _ in timestamps]

    monkeypatch.setattr(AnimeMatcherService, "extract_frames", fake_extract_frames)

    similarities = SceneMergerService._compute_visual_boundary_similarities(
        video_path=Path("/tmp/video.mp4"),
        scenes=scenes,
        library_path=Path("/tmp/library"),
        library_type="local",
        anime_name="Series",
    )

    assert calls == [[0.92, 1.08, 1.92, 2.08]]
    assert similarities == [0.0, 1.0]


def test_visual_boundary_similarities_for_indices_batch_frame_extraction(
    monkeypatch,
) -> None:
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=1.0),
            Scene(index=1, start_time=1.0, end_time=2.0),
            Scene(index=2, start_time=2.0, end_time=3.0),
            Scene(index=3, start_time=3.0, end_time=4.0),
        ]
    )
    calls: list[list[float]] = []

    class FakeEmbedder:
        def embed_batch(self, images):
            assert len(images) == 4
            return np.array(
                [
                    [1.0, 0.0],
                    [1.0, 0.0],
                    [1.0, 0.0],
                    [0.0, 1.0],
                ],
                dtype=np.float32,
            )

    monkeypatch.setattr(
        AnimeMatcherService,
        "_init_searcher",
        classmethod(lambda cls, library_path, library_type, anime_name: True),
    )
    monkeypatch.setattr(
        AnimeMatcherService,
        "_query_processor",
        types.SimpleNamespace(embedder=FakeEmbedder()),
    )

    def fake_extract_frames(video_path, timestamps):
        calls.append(list(timestamps))
        return [Image.new("RGB", (4, 4), "black") for _ in timestamps]

    monkeypatch.setattr(AnimeMatcherService, "extract_frames", fake_extract_frames)

    similarities = SceneMergerService._compute_visual_boundary_similarities_for_indices(
        video_path=Path("/tmp/video.mp4"),
        scenes=scenes,
        boundary_indices=[0, 2],
        library_path=Path("/tmp/library"),
        library_type="local",
        anime_name="Series",
    )

    assert calls == [[0.92, 1.08, 2.92, 3.08]]
    assert similarities == {0: 1.0, 2: 0.0}


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
