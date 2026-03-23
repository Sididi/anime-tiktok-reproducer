from __future__ import annotations

import io
import json
import sys
from collections import defaultdict
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

from anime_searcher import benchmark as benchmark_module
from app.services.anime_library import AnimeLibraryService, IndexProgress
from app.services.anime_matcher import AnimeMatcherService
from anime_searcher import cli as cli_module
from anime_searcher.config import EMBEDDING_DIM, INDEX_ENGINE_PROFILE, INDEX_FORMAT_VERSION
from anime_searcher.indexer import embedder as embedder_module
from anime_searcher.indexer import frame_extractor as frame_extractor_module
from anime_searcher.indexer import index_manager as index_manager_module
from anime_searcher.indexer.frame_extractor import DecodedFrameBatch, MediaBinaryProfile
from anime_searcher.indexer.index_manager import IndexManager
from anime_searcher.indexer.pipeline import (
    FileStartedEvent,
    IndexedFileResult,
    IndexingJob,
    ParallelFileIndexer,
    SkippedFileResult,
)
from anime_searcher.searcher import query as query_module


RUNNER = CliRunner()
FAULTY_PROJECT_SERIES = {
    "Bokura no Ameiro Protocol (Protocol Rain)": "Bokura_no_Ameiro_Protocol__Protocol_Rain_d6306a28",
    "Tougen Anki": "Tougen_Anki_9f8b2809",
    "Yamada-kun to Lv999 no Koi wo Suru (My Love Story)": "Yamada-kun_to_Lv999_no_Koi_wo_Suru__My_Love_Story_c136df76",
}


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


def test_parallel_file_indexer_torchcodec_backend_embeds_decoded_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    decoded_batches = [
        DecodedFrameBatch(
            timestamps=[0.0, 1.0],
            data=torch.full((2, 3, 8, 8), 255, dtype=torch.uint8),
        ),
        DecodedFrameBatch(
            timestamps=[2.0],
            data=torch.full((1, 3, 8, 8), 255, dtype=torch.uint8),
        ),
    ]

    monkeypatch.setattr(
        "anime_searcher.indexer.pipeline.iter_torchcodec_frame_batches",
        lambda *args, **kwargs: iter(decoded_batches),
    )

    class DummyEmbedder:
        device = "cuda"
        embedding_dim = EMBEDDING_DIM

        def __init__(self) -> None:
            self.calls = 0

        def preprocess_decoded_batch(self, batch: torch.Tensor) -> torch.Tensor:
            assert batch.dtype == torch.uint8
            return torch.zeros((batch.shape[0], 3, 288, 288), dtype=torch.float32)

        def embed_preprocessed_batch(self, batch: torch.Tensor) -> np.ndarray:
            vectors = _make_vectors(batch.shape[0], self.calls * 8)
            self.calls += 1
            return vectors

    pipeline = ParallelFileIndexer(
        DummyEmbedder(),
        batch_size=2,
        prefetch_batches=2,
        transform_workers=1,
        file_workers=1,
        decode_backend="torchcodec_cuda",
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
    assert result.embeddings.shape == (3, EMBEDDING_DIM)
    assert result.timestamps == [0.0, 1.0, 2.0]


def test_parallel_file_indexer_ffmpeg_cuda_backend_embeds_decoded_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    decoded_batches = [
        DecodedFrameBatch(
            timestamps=[0.0, 1.0],
            data=torch.full((2, 3, 8, 8), 255, dtype=torch.uint8),
        ),
        DecodedFrameBatch(
            timestamps=[2.0],
            data=torch.full((1, 3, 8, 8), 255, dtype=torch.uint8),
        ),
    ]

    monkeypatch.setattr(
        "anime_searcher.indexer.pipeline.iter_ffmpeg_cuda_frame_batches",
        lambda *args, **kwargs: iter(decoded_batches),
    )

    class DummyEmbedder:
        device = "cuda"
        embedding_dim = EMBEDDING_DIM

        def __init__(self) -> None:
            self.calls = 0

        def preprocess_decoded_batch(self, batch: torch.Tensor) -> torch.Tensor:
            assert batch.dtype == torch.uint8
            return torch.zeros((batch.shape[0], 3, 288, 288), dtype=torch.float32)

        def embed_preprocessed_batch(self, batch: torch.Tensor) -> np.ndarray:
            vectors = _make_vectors(batch.shape[0], self.calls * 8)
            self.calls += 1
            return vectors

    pipeline = ParallelFileIndexer(
        DummyEmbedder(),
        batch_size=2,
        prefetch_batches=2,
        transform_workers=1,
        file_workers=1,
        decode_backend="ffmpeg_cuda",
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
    assert result.embeddings.shape == (3, EMBEDDING_DIM)
    assert result.timestamps == [0.0, 1.0, 2.0]
    assert pipeline.decode_fallbacks == 0


def test_parallel_file_indexer_ffmpeg_cuda_falls_back_to_ffmpeg_cpu_on_decode_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")
    frames = [
        (0.0, Image.new("RGB", (2, 2), color=(10, 0, 0))),
        (1.0, Image.new("RGB", (2, 2), color=(20, 0, 0))),
    ]

    monkeypatch.setattr(
        "anime_searcher.indexer.pipeline.iter_ffmpeg_cuda_frame_batches",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            frame_extractor_module.FFmpegCudaDecodeError("nvdec failed")
        ),
    )
    monkeypatch.setattr(
        "anime_searcher.indexer.pipeline.extract_frames",
        lambda *args, **kwargs: iter(frames),
    )

    class DummyEmbedder:
        device = "cuda"
        embedding_dim = EMBEDDING_DIM

        def embed_preprocessed_batch(self, batch: torch.Tensor) -> np.ndarray:
            return _make_vectors(batch.shape[0], 0)

    pipeline = ParallelFileIndexer(
        DummyEmbedder(),
        batch_size=2,
        prefetch_batches=2,
        transform_workers=1,
        file_workers=1,
        decode_backend="ffmpeg_cuda",
    )
    outputs = list(
        pipeline.run(
            [
                IndexingJob(
                    video_path=video_path,
                    series="Demo",
                    episode="episode",
                    fps=1.0,
                )
            ]
        )
    )
    pipeline.close()

    assert isinstance(outputs[1], IndexedFileResult)
    assert outputs[1].timestamps == [0.0, 1.0]
    assert pipeline.decode_fallbacks == 1


def test_parallel_file_indexer_skips_unreadable_file_instead_of_aborting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "broken.mp4"
    video_path.write_bytes(b"broken")

    monkeypatch.setattr(
        "anime_searcher.indexer.pipeline.extract_frames",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            frame_extractor_module.UnreadableVideoError("invalid media stream")
        ),
    )

    class DummyEmbedder:
        device = "cpu"
        embedding_dim = EMBEDDING_DIM

        def embed_preprocessed_batch(self, batch: torch.Tensor) -> np.ndarray:
            return _make_vectors(batch.shape[0], 0)

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
                    episode="broken",
                    fps=1.0,
                )
            ]
        )
    )
    pipeline.close()

    assert isinstance(outputs[0], FileStartedEvent)
    assert isinstance(outputs[1], SkippedFileResult)
    assert outputs[1].job.video_path == video_path
    assert "invalid media stream" in outputs[1].error_message


def test_run_benchmark_reports_decode_backend_and_precision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")
    profile = MediaBinaryProfile("system", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")

    class DummyEmbedder:
        device = "cuda"
        resolved_precision = "fp16"

    class DummyPipeline:
        def __init__(self, *_args, **kwargs) -> None:
            self.file_workers = kwargs["file_workers"]

        def run(self, jobs: list[IndexingJob]):
            yield IndexedFileResult(
                job=jobs[0],
                timestamps=[0.0, 1.0],
                embeddings=_make_vectors(2, 0),
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(benchmark_module, "ParallelFileIndexer", DummyPipeline)

    result = benchmark_module._run_benchmark(
        embedder=DummyEmbedder(),
        jobs=[IndexingJob(video_path=video_path, series="Demo", episode="episode", fps=1.0)],
        batch_size=2,
        prefetch_batches=2,
        transform_workers=1,
        file_workers=1,
        media_profile=profile,
        decode_backend="torchcodec_cuda",
    )

    assert result.decode_backend == "torchcodec_cuda"
    assert result.precision == "fp16"


def test_iter_ffmpeg_cuda_frame_batches_uses_nvdec_command_and_returns_tensors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")
    profile = MediaBinaryProfile("system", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")
    frame_one = bytes(range(12))
    frame_two = bytes(range(12, 24))
    captured_cmd: list[str] | None = None

    class DummyProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(frame_one + frame_two)
            self.stderr = io.BytesIO()
            self.returncode = 0

        def wait(self) -> int:
            return self.returncode

    def fake_popen(cmd, **kwargs):
        nonlocal captured_cmd
        captured_cmd = cmd
        return DummyProcess()

    monkeypatch.setattr(frame_extractor_module, "get_video_resolution", lambda *_args, **_kwargs: (2, 2))
    monkeypatch.setattr(frame_extractor_module, "get_video_codec_name", lambda *_args, **_kwargs: "hevc")
    monkeypatch.setattr(frame_extractor_module.subprocess, "Popen", fake_popen)

    batches = list(
        frame_extractor_module.iter_ffmpeg_cuda_frame_batches(
            video_path,
            1.0,
            batch_size=2,
            media_profile=profile,
        )
    )

    assert captured_cmd is not None
    assert "-hwaccel" in captured_cmd
    assert "cuda" in captured_cmd
    assert "-hwaccel_output_format" in captured_cmd
    assert "-c:v" in captured_cmd
    assert "hevc_cuvid" in captured_cmd
    assert any("scale_cuda=format=nv12,hwdownload,format=nv12,format=rgb24" in arg for arg in captured_cmd)
    assert len(batches) == 1
    assert batches[0].timestamps == [0.0, 1.0]
    assert tuple(batches[0].data.shape) == (2, 3, 2, 2)
    assert batches[0].data.dtype == torch.uint8


def test_resolve_decode_backend_auto_prefers_ffmpeg_cuda_before_torchcodec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    monkeypatch.setattr(frame_extractor_module, "ffmpeg_cuda_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(frame_extractor_module, "torchcodec_cuda_available", lambda *_args, **_kwargs: True)

    assert frame_extractor_module.resolve_decode_backend("auto", "cuda", sample_video=video_path) == "ffmpeg_cuda"


def test_resolve_decode_backend_auto_falls_back_when_torchcodec_cuda_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    monkeypatch.setattr(frame_extractor_module, "ffmpeg_cuda_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(frame_extractor_module, "torchcodec_available", lambda: True)
    monkeypatch.setattr(frame_extractor_module, "torchcodec_cuda_available", lambda *_args, **_kwargs: False)

    assert frame_extractor_module.resolve_decode_backend("auto", "cuda", sample_video=video_path) == "ffmpeg_cpu"
    with pytest.raises(RuntimeError, match="CUDA video decode is unavailable"):
        frame_extractor_module.resolve_decode_backend(
            "torchcodec_cuda",
            "cuda",
            sample_video=video_path,
        )


def test_resolve_decode_backend_explicit_ffmpeg_cuda_errors_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    monkeypatch.setattr(frame_extractor_module, "ffmpeg_cuda_available", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="FFmpeg CUDA decode is unavailable"):
        frame_extractor_module.resolve_decode_backend(
            "ffmpeg_cuda",
            "cuda",
            sample_video=video_path,
        )


def test_select_ffmpeg_cuda_media_profile_prefers_system_binary_when_managed_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")
    managed = MediaBinaryProfile("managed", "/managed/ffmpeg", "/managed/ffprobe")
    system = MediaBinaryProfile("system", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")

    monkeypatch.setattr(frame_extractor_module, "_explicit_media_profile", lambda: None)
    monkeypatch.setattr(frame_extractor_module, "_current_media_profile", lambda: managed)
    monkeypatch.setattr(frame_extractor_module, "_system_media_profile", lambda: system)
    monkeypatch.setattr(frame_extractor_module, "get_video_codec_name", lambda *_args, **_kwargs: "hevc")
    monkeypatch.setattr(
        frame_extractor_module,
        "_probe_ffmpeg_cuda",
        lambda _sample, _fps, ffmpeg_binary, _decoder: ffmpeg_binary == system.ffmpeg_binary,
    )

    profile = frame_extractor_module.select_ffmpeg_cuda_media_profile(video_path, 1.0)

    assert profile == system
    assert frame_extractor_module.ffmpeg_cuda_available(video_path) is True


def test_resolve_decode_backend_rejects_unknown_value(tmp_path: Path) -> None:
    video_path = tmp_path / "episode.mp4"
    video_path.write_bytes(b"video")

    with pytest.raises(RuntimeError, match="Unsupported decode backend"):
        frame_extractor_module.resolve_decode_backend("nope", "cuda", sample_video=video_path)  # type: ignore[arg-type]


def test_benchmark_main_compares_backend_and_precision_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library_root = tmp_path / "library"
    library_path = library_root / "anime"
    series_dir = library_path / "Demo"
    series_dir.mkdir(parents=True)
    video_path = series_dir / "episode.mp4"
    video_path.write_bytes(b"video")
    model_path = _dummy_model(tmp_path)
    cpu_profile = MediaBinaryProfile("managed", "/managed/ffmpeg", "/managed/ffprobe")
    cuda_profile = MediaBinaryProfile("system", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")

    class DummyEmbedder:
        def __init__(self, _model_path: Path, *, precision: str = "auto") -> None:
            self.device = "cuda"
            self.resolved_precision = "fp16" if precision == "fp16" else "fp32"

        def warmup(self) -> None:
            return None

    seen_runs: list[tuple[str, str, int, str]] = []

    def fake_run_benchmark(*, embedder, file_workers: int, decode_backend: str, media_profile, **_kwargs):
        seen_runs.append((decode_backend, embedder.resolved_precision, file_workers, media_profile.name))
        return benchmark_module.BenchmarkResult(
            mode="parallel" if file_workers > 1 else "serial",
            elapsed_seconds=1.0,
            frames=10,
            files=1,
            frames_per_second=10.0,
            file_workers=file_workers,
            device=embedder.device,
            ffmpeg_profile=media_profile.name,
            decode_backend=decode_backend,
            precision=embedder.resolved_precision,
            decode_fallbacks=0,
        )

    monkeypatch.setattr(benchmark_module, "SSCDEmbedder", DummyEmbedder)
    monkeypatch.setattr(benchmark_module, "select_indexing_media_profile", lambda *_args, **_kwargs: cpu_profile)
    monkeypatch.setattr(
        benchmark_module,
        "select_ffmpeg_cuda_media_profile",
        lambda *_args, **_kwargs: cuda_profile,
    )
    monkeypatch.setattr(benchmark_module, "_benchmark_decode_backends", lambda **_kwargs: ["ffmpeg_cpu", "ffmpeg_cuda"])
    monkeypatch.setattr(benchmark_module, "_benchmark_precisions", lambda **_kwargs: ["fp32", "fp16"])
    monkeypatch.setattr(
        benchmark_module,
        "_default_file_workers",
        lambda _device, _transform_workers, fast, decode_backend: 2 if fast and decode_backend == "ffmpeg_cpu" else 1,
    )
    monkeypatch.setattr(benchmark_module, "_run_benchmark", fake_run_benchmark)

    result = RUNNER.invoke(
        benchmark_module.app,
        [
            str(library_root),
            "--type",
            "anime",
            "--model",
            str(model_path),
            "--compare-backends",
            "--compare-precisions",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["compare_backends"] is True
    assert payload["compare_precisions"] is True
    assert len(payload["results"]) == 6
    assert seen_runs == [
        ("ffmpeg_cpu", "fp32", 1, "managed"),
        ("ffmpeg_cpu", "fp32", 2, "managed"),
        ("ffmpeg_cuda", "fp32", 1, "system"),
        ("ffmpeg_cpu", "fp16", 1, "managed"),
        ("ffmpeg_cpu", "fp16", 2, "managed"),
        ("ffmpeg_cuda", "fp16", 1, "system"),
    ]


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


def test_create_pipeline_prefers_torchcodec_cuda_and_single_gpu_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.pt"
    model_path.write_bytes(b"model")
    sample_video = tmp_path / "episode.mp4"
    sample_video.write_bytes(b"video")

    profile = MediaBinaryProfile("test", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")

    class DummyEmbedder:
        def __init__(
            self,
            received_model_path: Path,
            *,
            device: str | None = None,
            precision: str = "auto",
        ) -> None:
            self.device = "cuda"
            self.model_path = received_model_path
            self.precision = precision
            self.resolved_precision = "fp32"
            self.warmed = False

        def warmup(self) -> None:
            self.warmed = True

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
            decode_backend: str,
        ) -> None:
            assert embedder.warmed is True
            self.embedder = embedder
            self.batch_size = batch_size
            self.prefetch_batches = prefetch_batches
            self.transform_workers = transform_workers
            self.file_workers = file_workers
            self.media_profile = media_profile
            self.decode_backend = decode_backend

    monkeypatch.setattr(cli_module, "SSCDEmbedder", DummyEmbedder)
    monkeypatch.setattr(cli_module, "resolve_decode_backend", lambda *_args, **_kwargs: "torchcodec_cuda")
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
        decode_backend="auto",
        precision="auto",
    )

    assert embedder.model_path == model_path
    assert embedder.precision == "auto"
    assert embedder.resolved_precision == "fp32"
    assert pipeline.decode_backend == "torchcodec_cuda"
    assert pipeline.file_workers == 1
    assert selected_profile.name == "torchcodec_cuda"


def test_create_pipeline_prefers_ffmpeg_cuda_and_single_gpu_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.pt"
    model_path.write_bytes(b"model")
    sample_video = tmp_path / "episode.mp4"
    sample_video.write_bytes(b"video")

    class DummyEmbedder:
        def __init__(
            self,
            received_model_path: Path,
            *,
            device: str | None = None,
            precision: str = "auto",
        ) -> None:
            self.device = "cuda"
            self.model_path = received_model_path
            self.precision = precision
            self.resolved_precision = "fp16"
            self.warmed = False

        def warmup(self) -> None:
            self.warmed = True

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
            decode_backend: str,
        ) -> None:
            assert embedder.warmed is True
            self.file_workers = file_workers
            self.decode_backend = decode_backend
            self.media_profile = media_profile

    monkeypatch.setattr(cli_module, "SSCDEmbedder", DummyEmbedder)
    monkeypatch.setattr(cli_module, "resolve_decode_backend", lambda *_args, **_kwargs: "ffmpeg_cuda")
    monkeypatch.setattr(cli_module, "select_indexing_media_profile", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not benchmark ffmpeg profiles for ffmpeg_cuda")))
    profile = MediaBinaryProfile("system", "/usr/bin/ffmpeg", "/usr/bin/ffprobe")
    monkeypatch.setattr(cli_module, "select_ffmpeg_cuda_media_profile", lambda *_args, **_kwargs: profile)
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
        decode_backend="auto",
        precision="auto",
    )

    assert embedder.model_path == model_path
    assert pipeline.decode_backend == "ffmpeg_cuda"
    assert pipeline.file_workers == 1
    assert selected_profile == profile


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


def test_embedder_auto_precision_uses_fp32_on_cuda(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = _dummy_model(tmp_path)

    class DummyModel:
        def __init__(self) -> None:
            self.half_calls = 0
            self.to_calls: list[str] = []

        def to(self, device: str):
            self.to_calls.append(device)
            return self

        def eval(self):
            return self

        def half(self):
            self.half_calls += 1
            return self

    monkeypatch.setattr(embedder_module.torch.jit, "load", lambda *_args, **_kwargs: DummyModel())

    embedder = embedder_module.SSCDEmbedder(model_path, device="cuda", precision="auto")

    assert embedder.resolved_precision == "fp32"
    assert embedder.model_dtype == torch.float32
    assert embedder.model.half_calls == 0


def test_embedder_rejects_unknown_precision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path = _dummy_model(tmp_path)

    class DummyModel:
        def to(self, _device: str):
            return self

        def eval(self):
            return self

    monkeypatch.setattr(embedder_module.torch.jit, "load", lambda *_args, **_kwargs: DummyModel())

    with pytest.raises(ValueError, match="Unsupported precision"):
        embedder_module.SSCDEmbedder(model_path, device="cpu", precision="bf16")  # type: ignore[arg-type]


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


def test_index_progress_json_reports_unreadable_file_and_errors_when_all_files_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_root = tmp_path / "library"
    series_dir = library_root / "anime" / "Demo"
    series_dir.mkdir(parents=True)
    (series_dir / "ep1.mp4").write_bytes(b"ep1")
    model_path = _dummy_model(tmp_path)

    class DummyEmbedder:
        device = "cpu"
        resolved_precision = "fp32"

    class FakePipeline:
        file_workers = 1
        decode_backend = "ffmpeg_cpu"
        decode_fallbacks = 0

        def close(self) -> None:
            return None

        def run(self, jobs: list[IndexingJob]):
            for job in jobs:
                yield FileStartedEvent(job=job)
                yield SkippedFileResult(
                    job=job,
                    error_message=(
                        f"Unable to probe video stream for '{job.video_path.name}' via /usr/bin/ffprobe: "
                        "Invalid data found when processing input"
                    ),
                )

    monkeypatch.setattr(
        cli_module,
        "_create_pipeline",
        lambda *args, **kwargs: (
            DummyEmbedder(),
            FakePipeline(),
            MediaBinaryProfile("test", "/usr/bin/ffmpeg", "/usr/bin/ffprobe"),
        ),
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
    assert [payload["event"] for payload in payloads] == [
        "start",
        "file_started",
        "file_skipped",
        "error",
    ]
    assert payloads[2]["current_file"] == "Demo/ep1.mp4"
    assert "Invalid data found when processing input" in payloads[2]["error"]
    assert "No files were indexed." in payloads[3]["error"]
    assert "ep1.mp4" in payloads[3]["error"]
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


def test_save_merges_series_added_from_stale_snapshots(tmp_path: Path) -> None:
    library_path = tmp_path / "library" / "anime"
    existing_dir = library_path / "Existing"
    existing_dir.mkdir(parents=True)
    existing_file = existing_dir / "ep0.mp4"
    existing_file.write_bytes(b"seed")
    seed_manager = IndexManager(library_path)
    seed_manager.load_or_create(2.0)
    seed_manager.set_series_fps("Existing", 2.0)
    seed_manager.add_file_embeddings(_make_vectors(1, 0), "Existing", "ep0", existing_file, [0.0])
    seed_manager.update_file_state(existing_file, 1)
    seed_manager.save()

    first_manager = IndexManager(library_path)
    first_manager.load_or_create(2.0)
    second_manager = IndexManager(library_path)
    second_manager.load_or_create(2.0)

    alpha_dir = library_path / "Alpha"
    alpha_dir.mkdir()
    alpha_file = alpha_dir / "ep1.mp4"
    alpha_file.write_bytes(b"alpha")
    first_manager.set_series_fps("Alpha", 2.0)
    first_manager.add_file_embeddings(_make_vectors(1, 10), "Alpha", "ep1", alpha_file, [0.0])
    first_manager.update_file_state(alpha_file, 1)

    beta_dir = library_path / "Beta"
    beta_dir.mkdir()
    beta_file = beta_dir / "ep1.mp4"
    beta_file.write_bytes(b"beta")
    second_manager.set_series_fps("Beta", 2.0)
    second_manager.add_file_embeddings(_make_vectors(1, 20), "Beta", "ep1", beta_file, [0.0])
    second_manager.update_file_state(beta_file, 1)

    first_manager.save()
    second_manager.save()

    merged_manager = IndexManager(library_path)
    merged_manager.load_or_create()

    assert set(merged_manager.get_series_list()) == {"Alpha", "Beta", "Existing"}
    assert set(merged_manager.state) == {
        "Alpha/ep1.mp4",
        "Beta/ep1.mp4",
        "Existing/ep0.mp4",
    }


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
async def test_backend_index_anime_passes_decode_backend_and_precision_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    searcher_path = tmp_path / "searcher"
    source_folder = tmp_path / "incoming"
    source_folder.mkdir(parents=True)
    source_path = source_folder / "ep1.mkv"
    source_path.write_bytes(b"source")
    prepared_path = library_path / "Demo" / "ep1.mp4"
    captured_cmd: list[str] | None = None

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
        classmethod(lambda cls, anime_name, library_type=None: None),
    )

    async def fake_prepare_single_source_for_library(*, source_path: Path, dest_dir: Path):
        prepared_path.parent.mkdir(parents=True, exist_ok=True)
        prepared_path.write_bytes(b"prepared")
        return prepared_path, "Copying", True

    async def fake_stream_searcher_command(**kwargs):
        nonlocal captured_cmd
        captured_cmd = kwargs["cmd"]
        yield IndexProgress(status="indexing", message="indexing", progress=0.6)

    async def fake_ensure_episode_manifest(*, force_refresh: bool = False, library_type=None):
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
        async for progress in AnimeLibraryService.index_anime(
            source_folder=source_folder,
            anime_name="Demo",
            require_gpu=False,
            library_type="anime",
            decode_backend="torchcodec_cuda",
            precision="fp16",
        )
    ]

    assert progress_events[-1].status == "complete"
    assert captured_cmd is not None
    assert "--decode-backend" in captured_cmd
    assert "torchcodec_cuda" in captured_cmd
    assert "--precision" in captured_cmd
    assert "fp16" in captured_cmd


def test_anime_matcher_init_searcher_uses_explicit_fp32_precision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library_path = tmp_path / "library"
    library_path.mkdir()
    model_path = _dummy_model(tmp_path)
    captured: dict[str, object] = {}

    class DummyIndexManager:
        def __init__(self, received_library_path: Path) -> None:
            captured["library_path"] = received_library_path

        def load_or_create(self) -> None:
            captured["load_or_create_called"] = True

        def get_series_list(self) -> list[str]:
            return ["Demo"]

    class DummyEmbedder:
        def __init__(
            self,
            received_model_path: Path,
            *,
            device: str | None = None,
            precision: str = "auto",
        ) -> None:
            captured["model_path"] = received_model_path
            captured["precision"] = precision
            self.device = device or "cpu"
            self.precision = precision
            self.resolved_precision = precision

    class DummyQueryProcessor:
        def __init__(self, index_manager, embedder) -> None:
            captured["query_processor_index_manager"] = index_manager
            captured["query_processor_embedder"] = embedder

    monkeypatch.setattr(AnimeMatcherService, "_index_manager", None)
    monkeypatch.setattr(AnimeMatcherService, "_embedder", None)
    monkeypatch.setattr(AnimeMatcherService, "_query_processor", None)
    monkeypatch.setattr(AnimeMatcherService, "_loaded_library_path", None)
    monkeypatch.setattr(AnimeMatcherService, "_loaded_library_type", None)
    monkeypatch.setattr(AnimeMatcherService, "_stale_series", defaultdict(set))
    monkeypatch.setattr(embedder_module, "SSCDEmbedder", DummyEmbedder)
    monkeypatch.setattr(index_manager_module, "IndexManager", DummyIndexManager)
    monkeypatch.setattr(query_module, "QueryProcessor", DummyQueryProcessor)
    monkeypatch.setattr("app.services.anime_matcher.settings.sscd_model_path", model_path)

    assert AnimeMatcherService._init_searcher(library_path, "anime", "Demo") is True
    assert captured["load_or_create_called"] is True
    assert captured["model_path"] == model_path
    assert captured["precision"] == "fp32"


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


def test_checked_in_project_library_excludes_removed_faulty_series() -> None:
    library_path = REPO_ROOT / "modules" / "anime_searcher" / "library" / "anime"
    if not library_path.exists():
        library_path = REPO_ROOT.parent.parent / "modules" / "anime_searcher" / "library" / "anime"
    shard_dir = library_path / ".index" / "series"

    present_series_dirs = {path.name for path in library_path.iterdir() if path.is_dir() and not path.name.startswith(".")}
    present_shards = {path.name for path in shard_dir.iterdir() if path.is_dir()}

    assert present_series_dirs.isdisjoint(FAULTY_PROJECT_SERIES)
    assert present_shards.isdisjoint(set(FAULTY_PROJECT_SERIES.values()))
