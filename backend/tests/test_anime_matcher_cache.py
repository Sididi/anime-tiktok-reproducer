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
from app.services.anime_matcher import AnimeMatcherService


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


def test_seeded_crop_search_filters_before_large_series_limits(monkeypatch) -> None:
    images = [Image.new("RGB", (9, 16), "black")]
    episode_paths = {f"ep{i}": Path(f"/tmp/ep{i}.mp4") for i in range(8)}
    built: list[str] = []

    monkeypatch.setattr(AnimeMatcherService, "_embedder", _FakeEmbedder())
    monkeypatch.setattr(AnimeMatcherService, "_index_manager", _FakeIndexManager(50000))
    monkeypatch.setattr(
        AnimeMatcherService,
        "_series_episode_paths",
        classmethod(lambda cls, series, library_type: episode_paths),
    )

    def fake_load(cls, episode_path: Path, *, target_aspect: float):
        built.append(episode_path.stem)
        return {
            "embeddings": np.ones((1, 2), dtype=np.float32),
            "timestamps": np.asarray([12.0], dtype=np.float32),
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
        episode_names=["ep7", "ep2"],
        top_n=3,
    )

    assert built == ["ep7", "ep2"]
    assert [candidate.episode for candidate in results[0]] == ["ep7", "ep2"]


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
    monkeypatch.setattr(AnimeMatcherService, "_index_manager", _FakeIndexManager(50000))
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
