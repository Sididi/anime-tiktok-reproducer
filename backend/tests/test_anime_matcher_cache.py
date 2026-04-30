from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.modules.setdefault("numpy", types.SimpleNamespace())
pil_module = sys.modules.setdefault("PIL", types.ModuleType("PIL"))
sys.modules.setdefault("PIL.Image", types.SimpleNamespace(Image=object))
sys.modules.setdefault("PIL.ImageOps", types.SimpleNamespace())
setattr(pil_module, "Image", sys.modules["PIL.Image"])
setattr(pil_module, "ImageOps", sys.modules["PIL.ImageOps"])

sys.modules.setdefault(
    "app.config",
    types.SimpleNamespace(settings=types.SimpleNamespace(anime_searcher_path=Path("."), sscd_model_path=None)),
)
sys.modules.setdefault(
    "app.library_types",
    types.SimpleNamespace(LibraryType=str, coerce_library_type=lambda value: value),
)
sys.modules.setdefault(
    "app.models",
    types.SimpleNamespace(
        AlternativeMatch=object,
        MatchCandidate=object,
        MatchList=object,
        Scene=object,
        SceneMatch=object,
        SceneList=object,
    ),
)
sys.modules.setdefault(
    "app.services.matcher_trajectory",
    types.SimpleNamespace(
        Pass2Prior=object,
        TrajectoryFit=object,
        confidence_from_fit=lambda *args, **kwargs: None,
        extract_probe_times=lambda *args, **kwargs: [],
        fit_trajectory_per_episode=lambda *args, **kwargs: {},
        is_static_shot=lambda *args, **kwargs: False,
        pick_best_fit=lambda *args, **kwargs: None,
        project_endpoints=lambda *args, **kwargs: None,
        static_shot_endpoints=lambda *args, **kwargs: None,
    ),
)

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
