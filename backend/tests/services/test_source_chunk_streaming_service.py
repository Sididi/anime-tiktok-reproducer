import asyncio
from pathlib import Path
import sys
import time

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.services.source_chunk_streaming_service import SourceChunkStreamingService


def _reset_descriptor_state() -> None:
    SourceChunkStreamingService._descriptor_cache.clear()
    SourceChunkStreamingService._descriptor_inflight.clear()


@pytest.mark.asyncio
async def test_get_descriptor_deduplicates_inflight_and_reuses_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source_path = tmp_path / "episode.mp4"
    source_path.write_bytes(b"descriptor-cache")
    call_counts = {"stream": 0, "duration": 0}

    def fake_probe_stream(video_path: Path) -> dict | None:
        call_counts["stream"] += 1
        time.sleep(0.05)
        return {"codec_name": "h264", "pix_fmt": "yuv420p"}

    def fake_probe_duration(video_path: Path) -> float | None:
        call_counts["duration"] += 1
        time.sleep(0.05)
        return 42.0

    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_stream_sync",
        staticmethod(fake_probe_stream),
    )
    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_duration_sync",
        staticmethod(fake_probe_duration),
    )
    _reset_descriptor_state()

    first, second, third = await asyncio.gather(
        SourceChunkStreamingService.get_descriptor(source_path),
        SourceChunkStreamingService.get_descriptor(source_path),
        SourceChunkStreamingService.get_descriptor(source_path),
    )
    cached = await SourceChunkStreamingService.get_descriptor(source_path)

    assert call_counts == {"stream": 1, "duration": 1}
    assert first == second == third == cached
    assert cached["mode"] == "passthrough"
    assert cached["duration"] == 42.0


@pytest.mark.asyncio
async def test_get_descriptor_invalidates_cache_when_source_stat_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source_path = tmp_path / "episode.mp4"
    source_path.write_bytes(b"a")
    call_counts = {"stream": 0, "duration": 0}

    def fake_probe_stream(video_path: Path) -> dict | None:
        call_counts["stream"] += 1
        return {"codec_name": "h264", "pix_fmt": "yuv420p"}

    def fake_probe_duration(video_path: Path) -> float | None:
        call_counts["duration"] += 1
        return float(video_path.stat().st_size)

    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_stream_sync",
        staticmethod(fake_probe_stream),
    )
    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_duration_sync",
        staticmethod(fake_probe_duration),
    )
    _reset_descriptor_state()

    first = await SourceChunkStreamingService.get_descriptor(source_path)
    source_path.write_bytes(b"cache-invalidated")
    second = await SourceChunkStreamingService.get_descriptor(source_path)

    assert call_counts == {"stream": 2, "duration": 2}
    assert first["duration"] == 1.0
    assert second["duration"] == float(len(b"cache-invalidated"))
