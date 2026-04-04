from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.services.source_chunk_streaming_service import SourceChunkStreamingService


def _reset_descriptor_state() -> None:
    SourceChunkStreamingService._descriptor_cache.clear()
    SourceChunkStreamingService._descriptor_inflight.clear()


@pytest.mark.asyncio
async def test_get_descriptor_keeps_mp4_hevc_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source_path = tmp_path / "episode.mp4"
    source_path.write_bytes(b"hevc")

    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_stream_sync",
        staticmethod(
            lambda _: {"codec_name": "hevc", "pix_fmt": "yuv420p10le"}
        ),
    )
    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_duration_sync",
        staticmethod(lambda _: 123.0),
    )
    _reset_descriptor_state()

    descriptor = await SourceChunkStreamingService.get_descriptor(source_path)

    assert descriptor["mode"] == "passthrough"
    assert descriptor["codec"] == "hevc"
    assert descriptor["pix_fmt"] == "yuv420p10le"


@pytest.mark.asyncio
async def test_get_descriptor_marks_non_mp4_sources_as_chunked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    source_path = tmp_path / "episode.mkv"
    source_path.write_bytes(b"vp9")

    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_stream_sync",
        staticmethod(lambda _: {"codec_name": "vp9", "pix_fmt": "yuv420p"}),
    )
    monkeypatch.setattr(
        SourceChunkStreamingService,
        "_probe_duration_sync",
        staticmethod(lambda _: 88.0),
    )
    _reset_descriptor_state()

    descriptor = await SourceChunkStreamingService.get_descriptor(source_path)

    assert descriptor["mode"] == "chunked"
    assert descriptor["codec"] == "vp9"
