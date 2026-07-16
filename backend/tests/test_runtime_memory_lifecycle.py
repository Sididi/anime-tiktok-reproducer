from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest

from app.services.indexation_queue import indexation_queue
from app.services.runtime_memory import memory_snapshot
from app.services.transcriber import TranscriberService, TranscriptionProgress


def test_memory_snapshot_reports_process_and_resource_counts() -> None:
    snapshot = memory_snapshot(test_marker="present")

    assert snapshot["pid"] > 0
    assert snapshot["rss_mib"] >= 0
    assert snapshot["peak_rss_mib"] >= snapshot["rss_mib"]
    assert snapshot["threads"] >= 1
    assert snapshot["test_marker"] == "present"


def test_empty_model_cleanup_does_not_start_torch_compile_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[None] = []
    torch_marker = object()

    monkeypatch.setitem(sys.modules, "torch", torch_marker)
    monkeypatch.setattr(TranscriberService, "_asr_models", {})
    monkeypatch.setattr(TranscriberService, "_align_models", {})
    monkeypatch.setattr(TranscriberService, "_active_transcriptions", 0)
    monkeypatch.setattr(
        TranscriberService,
        "_cleanup_runtime_workers",
        classmethod(lambda cls: cleanup_calls.append(None)),
    )

    TranscriberService.unload_models()

    assert sys.modules["torch"] is torch_marker
    assert cleanup_calls == []


@pytest.mark.asyncio
async def test_transcription_stream_close_always_requests_model_unload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unload_calls: list[None] = []

    async def fake_impl(cls, project_id: str, language: str):
        yield TranscriptionProgress("transcribing", 0.2, project_id)
        yield TranscriptionProgress("complete", 1.0, language)

    @asynccontextmanager
    async def fake_heavy_slot(kind: str):
        assert kind == "transcription_diarization"
        yield

    monkeypatch.setattr(TranscriberService, "_transcribe_impl", classmethod(fake_impl))
    monkeypatch.setattr(
        TranscriberService,
        "unload_models",
        classmethod(lambda cls: unload_calls.append(None)),
    )
    monkeypatch.setattr(indexation_queue, "heavy_slot", fake_heavy_slot)

    stream = TranscriberService.transcribe("project-1", "fr")
    first = await anext(stream)
    assert first.status == "transcribing"
    await stream.aclose()

    assert len(unload_calls) == 1
