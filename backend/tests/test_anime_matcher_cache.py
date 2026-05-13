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
