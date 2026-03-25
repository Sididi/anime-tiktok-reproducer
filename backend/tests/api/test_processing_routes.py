from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.routes.processing import get_script_automation_config
from app.config import settings
from app.models import Project
from app.services.elevenlabs_service import ElevenLabsService
from app.services.music_config_service import MusicConfigService
from app.services.project_service import ProjectService
from app.services.voice_config_service import VoiceConfigService
from app.services.gemini_service import GeminiService


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
    monkeypatch.setattr(GeminiService, "is_configured", lambda: True)
    monkeypatch.setattr(ElevenLabsService, "is_configured", lambda: False)

    result = await get_script_automation_config("proj123")

    assert result["script_title_selection_enabled"] is True
    assert "overlay_title_selection_enabled" not in result
