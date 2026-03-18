from __future__ import annotations

import json
import sys
from pathlib import Path

import faiss
import numpy as np
import pytest
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
SEARCHER_ROOT = REPO_ROOT / "modules" / "anime_searcher"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(SEARCHER_ROOT) not in sys.path:
    sys.path.insert(0, str(SEARCHER_ROOT))

from app.services.anime_library import AnimeLibraryService, IndexProgress
from anime_searcher import cli as cli_module
from anime_searcher.config import EMBEDDING_DIM
from anime_searcher.indexer.index_manager import IndexManager


RUNNER = CliRunner()


def _write_manifest(tmp_path: Path, *paths: Path) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"files": [str(path) for path in paths]}),
        encoding="utf-8",
    )
    return manifest_path


def _dummy_model(tmp_path: Path) -> Path:
    model_path = tmp_path / "dummy_model.pt"
    model_path.write_bytes(b"model")
    return model_path


def _make_vectors(count: int, offset: int) -> np.ndarray:
    vectors = np.zeros((count, EMBEDDING_DIM), dtype=np.float32)
    for i in range(count):
        vectors[i, (offset + i) % EMBEDDING_DIM] = 1.0
    return vectors


def _seed_v3_series(library_path: Path, file_vectors: dict[Path, np.ndarray]) -> None:
    manager = IndexManager(library_path)
    manager.load_or_create(2.0)
    manager.set_series_fps("Demo", 2.0)
    for file_path, vectors in file_vectors.items():
        timestamps = [float(i) for i in range(len(vectors))]
        manager.add_embeddings(
            vectors,
            "Demo",
            file_path.stem,
            file_path,
            timestamps,
        )
        manager.update_file_state(file_path, len(vectors))
    manager.save()


def _write_v2_sharded_index(library_path: Path, file_path: Path, vectors: np.ndarray) -> None:
    state_path = library_path / ".index" / "state.json"
    shard_dir = library_path / ".index" / "series" / "Demo_key"
    shard_dir.mkdir(parents=True)

    index = faiss.IndexFlatIP(EMBEDDING_DIM)
    index.add(vectors)
    faiss.write_index(index, str(shard_dir / "faiss.index"))

    metadata = {
        "series": "Demo",
        "frames": [
            {
                "id": i + 1,
                "series": "Demo",
                "episode": file_path.stem,
                "timestamp": float(i),
            }
            for i in range(len(vectors))
        ],
    }
    (shard_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (library_path / ".index" / "manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "config": {"fps": 2.0, "default_fps": 2.0},
                "series": {
                    "Demo": {
                        "key": "Demo_key",
                        "frames": len(vectors),
                        "fps": 2.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps(
            {
                "files": {
                    "Demo/ep1.mp4": {
                        "mtime": file_path.stat().st_mtime,
                        "size": file_path.stat().st_size,
                        "frame_count": len(vectors),
                        "index_start": 0,
                        "index_end": len(vectors) - 1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _install_fake_pipeline(monkeypatch: pytest.MonkeyPatch, batches_by_name: dict[str, tuple[np.ndarray, list[float]]]) -> None:
    class DummyEmbedder:
        device = "cpu"

    class FakePipeline:
        def embed_video(self, video_path: Path, fps: float):
            yield batches_by_name[video_path.name]

    monkeypatch.setattr(
        cli_module,
        "_create_pipeline",
        lambda *args, **kwargs: (DummyEmbedder(), None, FakePipeline()),
    )


def test_update_only_indexes_new_files_and_skips_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    file1 = series_dir / "ep1.mp4"
    file2 = series_dir / "ep2.mp4"
    file1.write_bytes(b"ep1")
    file2.write_bytes(b"ep2")

    _seed_v3_series(library_path, {file1: _make_vectors(2, 0)})
    manifest_path = _write_manifest(tmp_path, file2)
    model_path = _dummy_model(tmp_path)

    _install_fake_pipeline(monkeypatch, {file2.name: (_make_vectors(3, 10), [0.0, 1.0, 2.0])})
    result = RUNNER.invoke(
        cli_module.app,
        [
            "update",
            str(library_root),
            "--type",
            "anime",
            "--series",
            "Demo",
            "--manifest",
            str(manifest_path),
            "--model",
            str(model_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    manager = IndexManager(library_path)
    manager.load_or_create()
    assert manager.total_files == 2
    assert manager.total_frames == 5
    assert manager.state["Demo/ep1.mp4"].frame_count == 2
    assert manager.state["Demo/ep2.mp4"].frame_count == 3

    monkeypatch.setattr(
        cli_module,
        "_create_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pipeline should not run")),
    )
    second = RUNNER.invoke(
        cli_module.app,
        [
            "update",
            str(library_root),
            "--type",
            "anime",
            "--series",
            "Demo",
            "--manifest",
            str(manifest_path),
            "--model",
            str(model_path),
        ],
    )
    assert second.exit_code == 0, second.stdout
    assert "unchanged" in second.stdout.lower()

    manager = IndexManager(library_path)
    manager.load_or_create()
    assert manager.total_files == 2
    assert manager.total_frames == 5


def test_update_replaces_existing_file_without_duplicate_frames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    file1 = series_dir / "ep1.mp4"
    file1.write_bytes(b"old")

    _seed_v3_series(library_path, {file1: _make_vectors(2, 0)})
    file1.write_bytes(b"changed-and-longer")
    manifest_path = _write_manifest(tmp_path, file1)
    model_path = _dummy_model(tmp_path)

    _install_fake_pipeline(monkeypatch, {file1.name: (_make_vectors(3, 20), [0.0, 1.0, 2.0])})
    result = RUNNER.invoke(
        cli_module.app,
        [
            "update",
            str(library_root),
            "--type",
            "anime",
            "--series",
            "Demo",
            "--manifest",
            str(manifest_path),
            "--model",
            str(model_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    manager = IndexManager(library_path)
    manager.load_or_create()
    assert manager.total_files == 1
    assert manager.total_frames == 3
    assert manager.state["Demo/ep1.mp4"].frame_count == 3

    results = manager.search(_make_vectors(1, 20)[0], top_k=10, series="Demo")
    assert len(results) == 3
    assert all(meta.file_path == "Demo/ep1.mp4" for _, meta in results)


def test_remove_command_only_deletes_listed_files(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    file1 = series_dir / "ep1.mp4"
    file2 = series_dir / "ep2.mp4"
    file1.write_bytes(b"ep1")
    file2.write_bytes(b"ep2")

    _seed_v3_series(
        library_path,
        {
            file1: _make_vectors(2, 0),
            file2: _make_vectors(3, 10),
        },
    )
    manifest_path = _write_manifest(tmp_path, file2)

    result = RUNNER.invoke(
        cli_module.app,
        [
            "remove",
            str(library_root),
            "--type",
            "anime",
            "--series",
            "Demo",
            "--manifest",
            str(manifest_path),
        ],
    )
    assert result.exit_code == 0, result.stdout

    manager = IndexManager(library_path)
    manager.load_or_create()
    assert manager.total_files == 1
    assert manager.total_frames == 2
    assert "Demo/ep1.mp4" in manager.state
    assert "Demo/ep2.mp4" not in manager.state


def test_migrate_converts_v2_and_mutating_commands_refuse_first(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    file1 = series_dir / "ep1.mp4"
    file1.write_bytes(b"ep1")

    _write_v2_sharded_index(library_path, file1, _make_vectors(2, 0))
    manifest_path = _write_manifest(tmp_path, file1)

    remove_before_migration = RUNNER.invoke(
        cli_module.app,
        [
            "remove",
            str(library_root),
            "--type",
            "anime",
            "--series",
            "Demo",
            "--manifest",
            str(manifest_path),
        ],
    )
    assert remove_before_migration.exit_code == 1
    assert "migrate" in remove_before_migration.stdout.lower()

    migrate = RUNNER.invoke(
        cli_module.app,
        ["migrate", str(library_root), "--type", "anime"],
    )
    assert migrate.exit_code == 0, migrate.stdout

    manager = IndexManager(library_path)
    manager.load_or_create()
    assert manager.format_version == 3
    assert manager.total_files == 1
    assert manager.total_frames == 2

    results = manager.search(_make_vectors(1, 0)[0], top_k=1, series="Demo")
    assert results
    assert results[0][1].file_path == "Demo/ep1.mp4"


def test_mutating_and_read_commands_require_library_type(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)

    list_result = RUNNER.invoke(cli_module.app, ["list", str(library_root)])
    assert list_result.exit_code != 0
    assert "--type" in list_result.output

    migrate_result = RUNNER.invoke(cli_module.app, ["migrate", str(library_root)])
    assert migrate_result.exit_code != 0
    assert "--type" in migrate_result.output


def test_migrate_layout_moves_legacy_entries_into_anime_bucket(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    series_dir = library_root / "Demo"
    index_dir = library_root / ".index"
    series_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    episode_path = series_dir / "ep1.mp4"
    episode_path.write_bytes(b"demo")
    (index_dir / "state.json").write_text("{}", encoding="utf-8")

    migrate_result = RUNNER.invoke(
        cli_module.app,
        ["migrate-layout", str(library_root)],
    )
    assert migrate_result.exit_code == 0, migrate_result.stdout
    assert (library_root / "anime" / "Demo" / "ep1.mp4").exists()
    assert (library_root / "anime" / ".index" / "state.json").exists()

    second_result = RUNNER.invoke(
        cli_module.app,
        ["migrate-layout", str(library_root)],
    )
    assert second_result.exit_code == 0, second_result.stdout


@pytest.mark.asyncio
async def test_backend_update_anime_returns_prepared_library_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    searcher_path = tmp_path / "searcher"
    source_path = tmp_path / "incoming" / "ep3.mkv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    prepared_path = library_path / "Demo" / "ep3.mp4"

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_anime_searcher_path",
        classmethod(lambda cls: searcher_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "_get_indexed_series_fps_sync",
        classmethod(lambda cls, anime_name, library_type=None: 2.0),
    )

    async def fake_prepare_single_source_for_library(*, source_path: Path, dest_dir: Path):
        prepared_path.parent.mkdir(parents=True, exist_ok=True)
        prepared_path.write_bytes(b"prepared")
        return prepared_path, "Copying", True

    async def fake_stream_searcher_command(**kwargs):
        yield IndexProgress(status="indexing", message="updating", progress=0.6)

    async def fake_ensure_episode_manifest(
        *,
        force_refresh: bool = False,
        library_type=None,
    ):
        return {}

    async def fake_verify_prepared_library_files(files: list[Path]) -> str | None:
        return None

    monkeypatch.setattr(
        AnimeLibraryService,
        "_prepare_single_source_for_library",
        classmethod(lambda cls, **kwargs: fake_prepare_single_source_for_library(**kwargs)),
    )
    monkeypatch.setattr(AnimeLibraryService, "_verify_prepared_library_files", classmethod(lambda cls, files: fake_verify_prepared_library_files(files)))
    monkeypatch.setattr(AnimeLibraryService, "_stream_searcher_command", classmethod(lambda cls, **kwargs: fake_stream_searcher_command(**kwargs)))
    monkeypatch.setattr(
        AnimeLibraryService,
        "ensure_episode_manifest",
        classmethod(
            lambda cls, force_refresh=False, library_type=None: fake_ensure_episode_manifest(
                force_refresh=force_refresh,
                library_type=library_type,
            )
        ),
    )

    progress_events = [
        progress
        async for progress in AnimeLibraryService.update_anime(
            anime_name="Demo",
            source_paths=[source_path],
            require_gpu=False,
            library_type="anime",
        )
    ]

    assert progress_events[-1].status == "complete"
    assert progress_events[-1].prepared_library_paths == [str(prepared_path)]


@pytest.mark.asyncio
async def test_backend_remove_anime_files_deletes_file_and_sidecars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    searcher_path = tmp_path / "searcher"
    source_path = library_path / "Demo" / "ep1.mp4"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"indexed")

    sidecar_dir = AnimeLibraryService.get_subtitle_sidecar_dir(source_path)
    sidecar_dir.mkdir(parents=True)
    (sidecar_dir / "manifest.json").write_text("{}", encoding="utf-8")
    AnimeLibraryService.get_source_import_manifest_path(source_path).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_path),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_anime_searcher_path",
        classmethod(lambda cls: searcher_path),
    )

    async def fake_stream_searcher_command(**kwargs):
        yield IndexProgress(status="indexing", message="removing", progress=0.5)

    async def fake_ensure_episode_manifest(
        *,
        force_refresh: bool = False,
        library_type=None,
    ):
        return {}

    monkeypatch.setattr(AnimeLibraryService, "_stream_searcher_command", classmethod(lambda cls, **kwargs: fake_stream_searcher_command(**kwargs)))
    monkeypatch.setattr(
        AnimeLibraryService,
        "ensure_episode_manifest",
        classmethod(
            lambda cls, force_refresh=False, library_type=None: fake_ensure_episode_manifest(
                force_refresh=force_refresh,
                library_type=library_type,
            )
        ),
    )

    progress_events = [
        progress
        async for progress in AnimeLibraryService.remove_anime_files(
            anime_name="Demo",
            library_paths=[source_path],
            library_type="anime",
        )
    ]

    assert progress_events[-1].status == "complete"
    assert not source_path.exists()
    assert not sidecar_dir.exists()
    assert not AnimeLibraryService.get_source_import_manifest_path(source_path).exists()
