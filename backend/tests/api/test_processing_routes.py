from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from app.api.routes import processing as processing_routes
from app.api.routes.processing import PreviewBuildRequest
from app.api.routes.processing import get_script_automation_config
from app.api.routes.processing import stage_preview_audio
from app.api.routes.processing import build_preview
from app.config import settings
from app.models import Project
from app.services.elevenlabs_service import ElevenLabsService
from app.services.music_config_service import MusicConfigService
from app.services.project_service import ProjectService
from app.services.voice_config_service import VoiceConfigService
from app.services.llm_service import LLMService


@pytest.mark.asyncio
async def test_get_script_automation_config_exposes_script_title_selection_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "script_title_selection_enabled", True)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: Project(id=project_id))
    monkeypatch.setattr(
        VoiceConfigService,
        "get_config",
        lambda: SimpleNamespace(voices={}, default_voice_key=None),
    )
    monkeypatch.setattr(
        MusicConfigService,
        "get_config",
        lambda: SimpleNamespace(musics={}, default_music_key=None),
    )
    monkeypatch.setattr(LLMService, "is_configured", lambda: True)
    monkeypatch.setattr(ElevenLabsService, "is_configured", lambda: False)

    result = await get_script_automation_config("proj123")

    assert result["script_title_selection_enabled"] is True
    assert "overlay_title_selection_enabled" not in result


@pytest.mark.asyncio
async def test_stage_preview_audio_offloads_audio_concat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(ProjectService, "load", lambda project_id: Project(id=project_id))
    monkeypatch.setattr(ProjectService, "get_project_dir", lambda project_id: tmp_path)

    async def fake_write_upload(upload: UploadFile, destination):
        destination.write_bytes(upload.file.read())

    to_thread_calls: list[tuple[object, tuple[object, ...]]] = []

    async def fake_to_thread(fn, *args, **kwargs):
        to_thread_calls.append((fn, args))
        return None

    monkeypatch.setattr(processing_routes, "_write_upload_to_path", fake_write_upload)
    monkeypatch.setattr(processing_routes.asyncio, "to_thread", fake_to_thread)

    result = await stage_preview_audio(
        "proj-1",
        audio=None,
        audio_parts=[
            UploadFile(file=BytesIO(b"part-1"), filename="part-1.mp3"),
            UploadFile(file=BytesIO(b"part-2"), filename="part-2.mp3"),
        ],
    )

    assert result == {"staged": True}
    assert to_thread_calls
    assert to_thread_calls[0][0] is processing_routes._concat_audio_parts_to_wav
    assert to_thread_calls[0][1][-1] == tmp_path / "preview_staged.wav"


@pytest.mark.asyncio
async def test_build_preview_offloads_audio_mix_and_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    (tmp_path / "new_tts.wav").write_bytes(b"wav")
    monkeypatch.setattr(ProjectService, "load", lambda project_id: Project(id=project_id))
    monkeypatch.setattr(ProjectService, "get_project_dir", lambda project_id: tmp_path)

    to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(fn, *args, **kwargs):
        to_thread_calls.append((fn, args, kwargs))
        if fn is processing_routes._build_preview_audio_sync:
            return 12.5
        return None

    monkeypatch.setattr(processing_routes.asyncio, "to_thread", fake_to_thread)

    result = await build_preview(
        "proj-1",
        PreviewBuildRequest(tts_speed=1.0, music_key="demo-music"),
    )

    assert result["duration_seconds"] == 12.5
    assert to_thread_calls
    assert to_thread_calls[0][0] is processing_routes._build_preview_audio_sync
    assert to_thread_calls[0][2]["preview_path"] == tmp_path / "preview.wav"
