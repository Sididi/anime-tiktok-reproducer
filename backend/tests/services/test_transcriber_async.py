from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.config import settings
from app.models import Project, Scene, SceneList
from app.services.project_service import ProjectService
from app.services.transcriber import TranscriberService


@pytest.mark.asyncio
async def test_transcribe_emits_extracting_audio_phase_and_offloads_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    main_thread = threading.current_thread().name
    extraction_threads: list[str] = []

    def fake_extract_audio(media_path: Path, output_wav: Path) -> None:
        extraction_threads.append(threading.current_thread().name)
        output_wav.write_bytes(b"wav")

    def fake_transcribe_sync(cls, audio_path: Path, language: str | None, model_size: str):
        return [], "fr"

    monkeypatch.setattr(
        ProjectService,
        "load",
        lambda project_id: Project(id=project_id, video_path=str(video_path)),
    )
    monkeypatch.setattr(
        ProjectService,
        "load_scenes",
        lambda project_id: SceneList(
            scenes=[Scene(index=0, start_time=0.0, end_time=1.0)]
        ),
    )
    monkeypatch.setattr(ProjectService, "get_project_dir", lambda project_id: tmp_path)
    monkeypatch.setattr(ProjectService, "load_matches", lambda project_id: None)
    monkeypatch.setattr(ProjectService, "save_transcription", lambda project_id, transcription: None)
    monkeypatch.setattr(TranscriberService, "_extract_audio_for_whisper", staticmethod(fake_extract_audio))
    monkeypatch.setattr(TranscriberService, "_transcribe_sync", classmethod(fake_transcribe_sync))
    monkeypatch.setattr(TranscriberService, "unload_models", classmethod(lambda cls: None))
    monkeypatch.setattr(settings, "hf_token", None)

    events = []
    async for progress in TranscriberService.transcribe("project-1"):
        events.append(progress)

    assert any(event.status == "extracting_audio" for event in events)
    assert extraction_threads
    assert all(thread_name != main_thread for thread_name in extraction_threads)
    assert events[-1].status == "complete"


@pytest.mark.asyncio
async def test_transcribe_emits_heartbeat_while_transcription_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "video.mp4"
    wav_path = tmp_path / "audio_16khz.wav"
    video_path.write_bytes(b"video")
    wav_path.write_bytes(b"wav")

    def fake_transcribe_sync(cls, audio_path: Path, language: str | None, model_size: str):
        time.sleep(0.03)
        return [], "fr"

    monkeypatch.setattr(
        ProjectService,
        "load",
        lambda project_id: Project(id=project_id, video_path=str(video_path)),
    )
    monkeypatch.setattr(
        ProjectService,
        "load_scenes",
        lambda project_id: SceneList(
            scenes=[Scene(index=0, start_time=0.0, end_time=1.0)]
        ),
    )
    monkeypatch.setattr(ProjectService, "get_project_dir", lambda project_id: tmp_path)
    monkeypatch.setattr(ProjectService, "load_matches", lambda project_id: None)
    monkeypatch.setattr(ProjectService, "save_transcription", lambda project_id, transcription: None)
    monkeypatch.setattr(TranscriberService, "_transcribe_sync", classmethod(fake_transcribe_sync))
    monkeypatch.setattr(TranscriberService, "unload_models", classmethod(lambda cls: None))
    monkeypatch.setattr(settings, "hf_token", None)
    monkeypatch.setattr(
        "app.services.transcriber.TRANSCRIPTION_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )

    events = []
    async for progress in TranscriberService.transcribe("project-1"):
        events.append(progress)

    transcribing_events = [event for event in events if event.status == "transcribing"]
    assert len(transcribing_events) >= 2
    assert events[-1].status == "complete"
