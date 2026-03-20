from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import torch
from PIL import Image
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
from anime_searcher.config import EMBEDDING_DIM, INDEX_ENGINE_PROFILE, INDEX_FORMAT_VERSION
from anime_searcher.indexer import embedder as embedder_module
from anime_searcher.indexer.frame_extractor import MediaBinaryProfile
from anime_searcher.indexer.index_manager import IndexManager
from anime_searcher.indexer.pipeline import FileStartedEvent, IndexedFileResult, IndexingJob, ParallelFileIndexer


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


def _seed_v4_series(library_path: Path, file_vectors: dict[Path, np.ndarray]) -> None:
    manager = IndexManager(library_path)
    manager.load_or_create(2.0)
    manager.set_series_fps("Demo", 2.0)
    for file_path, vectors in file_vectors.items():
        timestamps = [float(i) for i in range(len(vectors))]
        manager.add_file_embeddings(
            vectors,
            "Demo",
            file_path.stem,
            file_path,
            timestamps,
        )
        manager.update_file_state(file_path, len(vectors))
    manager.save()


def _write_legacy_manifest(library_path: Path) -> None:
    index_dir = library_path / ".index"
    index_dir.mkdir(parents=True)
    (index_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "engine_profile": "legacy",
                "config": {"default_fps": 2.0},
                "series": {},
            }
        ),
        encoding="utf-8",
    )


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    batches_by_name: dict[str, list[tuple[np.ndarray, list[float]]] | tuple[np.ndarray, list[float]]],
) -> None:
    class DummyEmbedder:
        device = "cpu"

    class FakePipeline:
        file_workers = 1

        def close(self) -> None:
            return None

        def run(self, jobs: list[IndexingJob]):
            for job in jobs:
                yield FileStartedEvent(job=job)
                raw_batches = batches_by_name[job.video_path.name]
                normalized_batches = raw_batches if isinstance(raw_batches, list) else [raw_batches]
                embeddings = np.concatenate([batch[0] for batch in normalized_batches], axis=0)
                timestamps: list[float] = []
                for _, batch_timestamps in normalized_batches:
                    timestamps.extend(batch_timestamps)
                yield IndexedFileResult(job=job, timestamps=timestamps, embeddings=embeddings)

    monkeypatch.setattr(
        cli_module,
        "_create_pipeline",
        lambda *args, **kwargs: (
            DummyEmbedder(),
            FakePipeline(),
            MediaBinaryProfile("test", "/usr/bin/ffmpeg", "/usr/bin/ffprobe"),
        ),
    )


def test_parallel_file_indexer_combines_multi_batch_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    frames = [
        (float(i), Image.new("RGB", (2, 2), color=(i * 10, 0, 0)))
        for i in range(5)
    ]

    monkeypatch.setattr(
        "anime_searcher.indexer.pipeline.extract_frames",
        lambda *args, **kwargs: iter(frames),
    )

    class DummyEmbedder:
        device = "cpu"
        embedding_dim = EMBEDDING_DIM

        def __init__(self) -> None:
            self._offset = 0

        def embed_preprocessed_batch(self, batch) -> np.ndarray:
            vectors = _make_vectors(batch.shape[0], self._offset)
            self._offset += batch.shape[0]
            return vectors

    pipeline = ParallelFileIndexer(
        DummyEmbedder(),
        batch_size=2,
        prefetch_batches=2,
        transform_workers=1,
        file_workers=1,
    )
    outputs = list(
        pipeline.run(
            [
                IndexingJob(
                    video_path=video_path,
                    series="Demo",
                    episode="episode",
                    fps=2.0,
                )
            ]
        )
    )
    pipeline.close()

    assert isinstance(outputs[0], FileStartedEvent)
    assert isinstance(outputs[1], IndexedFileResult)
    result = outputs[1]
    assert result.embeddings.shape == (5, EMBEDDING_DIM)
    assert result.timestamps == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_create_pipeline_warms_embedder_before_building_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.pt"
    model_path.write_bytes(b"model")
    sample_video = tmp_path / "episode.mp4"
    sample_video.write_bytes(b"video")

    profile = MediaBinaryProfile("test", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")
    call_order: list[str] = []

    class DummyEmbedder:
        def __init__(self, received_model_path: Path) -> None:
            self.device = "cpu"
            self.model_path = received_model_path
            self.warmup_calls = 0
            self.warmed = False
            call_order.append("embedder_init")

        def warmup(self) -> None:
            self.warmup_calls += 1
            self.warmed = True
            call_order.append("warmup")

    class DummyPipeline:
        def __init__(
            self,
            embedder,
            *,
            batch_size: int,
            prefetch_batches: int,
            transform_workers: int,
            file_workers: int,
            media_profile: MediaBinaryProfile | None = None,
        ) -> None:
            assert embedder.warmed is True
            self.embedder = embedder
            self.batch_size = batch_size
            self.prefetch_batches = prefetch_batches
            self.transform_workers = transform_workers
            self.file_workers = file_workers
            self.media_profile = media_profile
            call_order.append("pipeline_init")

    monkeypatch.setattr(cli_module, "SSCDEmbedder", DummyEmbedder)
    monkeypatch.setattr(cli_module, "select_indexing_media_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(cli_module, "ParallelFileIndexer", DummyPipeline)

    embedder, pipeline, selected_profile = cli_module._create_pipeline(
        model_path,
        sample_video=sample_video,
        sample_fps=2.0,
        batch_size=8,
        fast=True,
        prefetch_batches=3,
        transform_workers=2,
        require_gpu=False,
    )

    assert embedder.model_path == model_path
    assert embedder.warmup_calls == 1
    assert pipeline.embedder is embedder
    assert selected_profile is profile
    assert call_order == ["embedder_init", "warmup", "pipeline_init"]


def test_embedder_warmup_uses_scalar_resize_size_for_square_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = _dummy_model(tmp_path)
    seen_shapes: list[tuple[int, ...]] = []

    class DummyModel:
        def to(self, _device: str):
            return self

        def eval(self):
            return self

    def fake_embed_preprocessed_batch(batch: torch.Tensor) -> np.ndarray:
        seen_shapes.append(tuple(batch.shape))
        return np.zeros((batch.shape[0], EMBEDDING_DIM), dtype=np.float32)

    monkeypatch.setattr(embedder_module.torch.jit, "load", lambda *_args, **_kwargs: DummyModel())
    monkeypatch.setattr(embedder_module, "RESIZE_SIZE", 288)

    embedder = embedder_module.SSCDEmbedder(model_path, device="cpu")
    monkeypatch.setattr(embedder, "embed_preprocessed_batch", fake_embed_preprocessed_batch)

    embedder.warmup()

    assert seen_shapes == [(1, 3, 288, 288)]


@pytest.mark.parametrize("resize_size", [(288, 320), [288, 320]])
def test_embedder_warmup_accepts_sequence_resize_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resize_size: tuple[int, int] | list[int],
) -> None:
    model_path = _dummy_model(tmp_path)
    seen_shapes: list[tuple[int, ...]] = []

    class DummyModel:
        def to(self, _device: str):
            return self

        def eval(self):
            return self

    def fake_embed_preprocessed_batch(batch: torch.Tensor) -> np.ndarray:
        seen_shapes.append(tuple(batch.shape))
        return np.zeros((batch.shape[0], EMBEDDING_DIM), dtype=np.float32)

    monkeypatch.setattr(embedder_module.torch.jit, "load", lambda *_args, **_kwargs: DummyModel())
    monkeypatch.setattr(embedder_module, "RESIZE_SIZE", resize_size)

    embedder = embedder_module.SSCDEmbedder(model_path, device="cpu")
    monkeypatch.setattr(embedder, "embed_preprocessed_batch", fake_embed_preprocessed_batch)

    embedder.warmup()

    assert seen_shapes == [(1, 3, 288, 320)]


def test_index_progress_json_emits_structured_error_for_unexpected_pipeline_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    series_dir = library_root / "anime" / "Demo"
    series_dir.mkdir(parents=True)
    (series_dir / "ep1.mp4").write_bytes(b"ep1")
    model_path = _dummy_model(tmp_path)

    monkeypatch.setattr(
        cli_module,
        "_create_pipeline",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("unexpected pipeline failure")),
    )

    result = RUNNER.invoke(
        cli_module.app,
        [
            "index",
            str(library_root),
            "--type",
            "anime",
            "--series",
            "Demo",
            "--model",
            str(model_path),
            "--progress-json",
        ],
    )

    assert result.exit_code == 1
    payloads = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert payloads == [
        {
            "event": "error",
            "message": "unexpected pipeline failure",
            "error": "unexpected pipeline failure",
            "progress": 1.0,
        }
    ]
    assert "Traceback" not in result.output


def test_update_only_indexes_new_files_and_skips_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    file1 = series_dir / "ep1.mp4"
    file2 = series_dir / "ep2.mp4"
    file1.write_bytes(b"ep1")
    file2.write_bytes(b"ep2")

    _seed_v4_series(library_path, {file1: _make_vectors(2, 0)})
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

    _seed_v4_series(library_path, {file1: _make_vectors(2, 0)})
    file1.write_bytes(b"changed-and-longer")
    manifest_path = _write_manifest(tmp_path, file1)
    model_path = _dummy_model(tmp_path)

    _install_fake_pipeline(
        monkeypatch,
        {
            file1.name: [
                (_make_vectors(2, 20), [0.0, 1.0]),
                (_make_vectors(1, 22), [2.0]),
            ]
        },
    )
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

    _seed_v4_series(
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


def test_v4_rejects_legacy_indices_with_rebuild_message(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    library_path.mkdir(parents=True)
    _write_legacy_manifest(library_path)

    result = RUNNER.invoke(
        cli_module.app,
        ["info", str(library_root), "--type", "anime"],
    )
    assert result.exit_code == 1
    assert "rebuild" in result.stdout.lower()


def test_migration_commands_are_removed(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)

    migrate_result = RUNNER.invoke(cli_module.app, ["migrate", str(library_root), "--type", "anime"])
    assert migrate_result.exit_code != 0
    assert "no such command" in migrate_result.output.lower()

    migrate_layout_result = RUNNER.invoke(cli_module.app, ["migrate-layout", str(library_root)])
    assert migrate_layout_result.exit_code != 0
    assert "no such command" in migrate_layout_result.output.lower()


def test_mutating_and_read_commands_require_library_type(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)

    list_result = RUNNER.invoke(cli_module.app, ["list", str(library_root)])
    assert list_result.exit_code != 0
    assert "--type" in list_result.output

    info_result = RUNNER.invoke(cli_module.app, ["info", str(library_root)])
    assert info_result.exit_code != 0
    assert "--type" in info_result.output


def test_backend_parses_structured_searcher_progress_events() -> None:
    progress = AnimeLibraryService._parse_searcher_progress_line(
        line=json.dumps(
            {
                "event": "file_completed",
                "message": "Indexed Demo/ep1.mp4",
                "current_file": "Demo/ep1.mp4",
                "progress": 0.5,
                "completed_files": 1,
                "total_files": 2,
            }
        ),
        status="indexing",
        total_files=2,
        progress_start=0.35,
        progress_span=0.60,
        text_line_index=1,
    )
    assert progress is not None
    assert progress.status == "indexing"
    assert progress.current_file == "Demo/ep1.mp4"
    assert progress.completed_files == 1
    assert progress.total_files == 2
    assert progress.progress == pytest.approx(0.65)

    error = AnimeLibraryService._parse_searcher_progress_line(
        line=json.dumps({"event": "error", "error": "boom"}),
        status="indexing",
        total_files=2,
        progress_start=0.35,
        progress_span=0.60,
        text_line_index=2,
    )
    assert error is not None
    assert error.status == "error"
    assert error.error == "boom"


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
    monkeypatch.setattr(
        AnimeLibraryService,
        "_verify_prepared_library_files",
        classmethod(lambda cls, files: fake_verify_prepared_library_files(files)),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "_stream_searcher_command",
        classmethod(lambda cls, **kwargs: fake_stream_searcher_command(**kwargs)),
    )
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

    monkeypatch.setattr(
        AnimeLibraryService,
        "_stream_searcher_command",
        classmethod(lambda cls, **kwargs: fake_stream_searcher_command(**kwargs)),
    )
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


def test_seeded_index_uses_v4_manifest(tmp_path: Path) -> None:
    library_path = tmp_path / "library" / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    file1 = series_dir / "ep1.mp4"
    file1.write_bytes(b"ep1")

    _seed_v4_series(library_path, {file1: _make_vectors(2, 0)})

    payload = json.loads((library_path / ".index" / "manifest.json").read_text())
    assert payload["version"] == INDEX_FORMAT_VERSION
    assert payload["engine_profile"] == INDEX_ENGINE_PROFILE
