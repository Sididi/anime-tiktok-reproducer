from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import Project, SceneTranscription, Transcription
from app.services.elevenlabs_service import ElevenLabsService
from app.services.gemini_service import GeminiService
from app.services.metadata import MetadataService
from app.services.project_service import ProjectService
from app.services.script_automation_service import ScriptAutomationService
from app.services.voice_config_service import VoiceConfigService


def _build_project() -> Project:
    return Project(id="proj123", anime_name="Test Anime")


def _build_transcription() -> Transcription:
    return Transcription(
        language="fr",
        scenes=[
            SceneTranscription(
                scene_index=1,
                text="Bonjour tout le monde.",
                start_time=0.0,
                end_time=2.0,
                is_raw=False,
            )
        ],
    )


async def _collect_events(**kwargs):
    return [event async for event in ScriptAutomationService.stream_automation(**kwargs)]


@pytest.mark.asyncio
async def test_stream_automation_pauses_before_phase_2(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(settings, "script_automate_enabled", True)
    monkeypatch.setattr(settings, "automate_metadata_overlay_enabled", True)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: _build_project())
    monkeypatch.setattr(ProjectService, "load_transcription", lambda project_id: _build_transcription())
    monkeypatch.setattr(
        VoiceConfigService,
        "get_voice",
        lambda voice_key: SimpleNamespace(
            elevenlabs_voice_id="voice-id",
            voice_settings={},
            model_id=None,
        ),
    )
    monkeypatch.setattr(GeminiService, "is_configured", lambda: True)
    monkeypatch.setattr(ElevenLabsService, "is_configured", lambda: True)

    events = await _collect_events(
        project_id="proj123",
        target_language="fr",
        voice_key="voice-a",
        existing_script_json={
            "language": "fr",
            "scenes": [
                {
                    "scene_index": 1,
                    "text": "Script valide et prêt.",
                }
            ],
        },
        pause_after_script=True,
    )

    assert [event["event"] for event in events] == [
        "starting",
        "llm_script",
        "llm_script",
        "script_ready",
    ]
    assert events[-1]["status"] == "paused"
    assert events[-1]["script_json"]["scenes"][0]["text"] == "Script valide et prêt."
    assert "metadata_json" not in events[-1]


@pytest.mark.asyncio
async def test_stream_automation_resume_uses_edited_script_for_tts_metadata_and_overlay(
    monkeypatch,
    tmp_path: Path,
):
    edited_script = {
        "language": "fr",
        "scenes": [
            {
                "scene_index": 1,
                "text": "Texte édité par l'utilisateur.",
            }
        ],
    }
    seen: dict[str, object] = {}

    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(settings, "script_automate_enabled", True)
    monkeypatch.setattr(settings, "automate_metadata_overlay_enabled", True)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: _build_project())
    monkeypatch.setattr(ProjectService, "load_transcription", lambda project_id: _build_transcription())
    monkeypatch.setattr(
        VoiceConfigService,
        "get_voice",
        lambda voice_key: SimpleNamespace(
            elevenlabs_voice_id="voice-id",
            voice_settings={},
            model_id=None,
        ),
    )
    monkeypatch.setattr(GeminiService, "is_configured", lambda: True)
    monkeypatch.setattr(ElevenLabsService, "is_configured", lambda: True)

    def fake_prepare_tts_payload(*, script_payload, target_language=None):
        seen["tts_script"] = script_payload
        return {
            "language": "fr",
            "normalized_full_text": "Texte édité par l'utilisateur.",
            "segments": [
                {
                    "id": 1,
                    "scene_indices": [1],
                    "text": "Texte édité par l'utilisateur.",
                    "character_count": 30,
                }
            ],
        }

    def fake_build_metadata_prompt(*, anime_name, script_payload, target_language="fr", library_type=None):
        seen["metadata_script"] = script_payload
        return "metadata-prompt"

    def fake_generate_json(prompt, *, model=None, response_json_schema=None):
        assert prompt == "metadata-prompt"
        return {
            "facebook": {
                "title": "Titre",
                "description": "Description",
                "tags": ["anime"],
            },
            "instagram": {"caption": "Caption"},
            "youtube": {
                "title": "Titre youtube",
                "description": "Description",
                "tags": ["anime"],
            },
            "tiktok": {"description": "Description"},
        }

    def fake_generate_video_overlay(*, project, script_payload, target_language):
        seen["overlay_script"] = script_payload
        return {
            "title": "HOOK 1",
            "title_hooks": [f"HOOK {idx}" for idx in range(1, 11)],
            "category": "Action • Fantasy",
        }

    def fake_synthesize(**kwargs):
        seen["tts_text"] = kwargs["text"]
        return b"fake-audio"

    def fake_merge_parts(part_paths, output_path):
        seen["part_count"] = len(part_paths)
        output_path.write_bytes(b"merged")

    monkeypatch.setattr(ScriptAutomationService, "prepare_tts_payload", fake_prepare_tts_payload)
    monkeypatch.setattr(MetadataService, "build_prompt_from_script_payload", fake_build_metadata_prompt)
    monkeypatch.setattr(GeminiService, "generate_json", fake_generate_json)
    monkeypatch.setattr(ScriptAutomationService, "generate_video_overlay", fake_generate_video_overlay)
    monkeypatch.setattr(ElevenLabsService, "synthesize", fake_synthesize)
    monkeypatch.setattr(ScriptAutomationService, "_merge_parts_to_wav", fake_merge_parts)

    events = await _collect_events(
        project_id="proj123",
        target_language="fr",
        voice_key="voice-a",
        existing_script_json=edited_script,
        skip_metadata=False,
        skip_tts=False,
        pause_after_script=False,
        skip_overlay=False,
    )

    assert [event["event"] for event in events] == [
        "starting",
        "llm_script",
        "llm_script",
        "tts_segmenting",
        "tts_segmenting",
        "tts_generating",
        "llm_metadata",
        "llm_metadata",
        "generating_overlay",
        "overlay_ready",
        "complete",
    ]
    assert seen["tts_script"] == edited_script
    assert seen["metadata_script"] == edited_script
    assert seen["overlay_script"] == edited_script
    assert seen["tts_text"] == "Texte édité par l'utilisateur."
    assert seen["part_count"] == 1
    assert events[-1]["metadata_json"]["facebook"]["title"] == "Titre"
    assert events[-1]["overlay_json"]["title"] == "HOOK 1"


def test_generate_video_overlay_normalizes_ten_title_hooks(monkeypatch):
    long_title = "UN HOOK TRES LONG QUI DEPASSE LARGEMENT LA LIMITE AUTORISEE POUR LE TEST"

    monkeypatch.setattr(
        GeminiService,
        "generate_json",
        lambda *args, **kwargs: {
            "title_hooks": [f"{long_title} {idx}" for idx in range(10)],
            "category": "Action • Fantasy",
        },
    )

    overlay = ScriptAutomationService.generate_video_overlay(
        project=_build_project(),
        script_payload={
            "language": "fr",
            "scenes": [
                {
                    "scene_index": 1,
                    "text": "Ceci est le résumé du script.",
                }
            ],
        },
        target_language="fr",
    )

    assert overlay["title"] == overlay["title_hooks"][0]
    assert overlay["category"] == "Action • Fantasy"
    assert len(overlay["title_hooks"]) == 10
    assert all(hook for hook in overlay["title_hooks"])
    assert all(len(hook) <= ScriptAutomationService.MAX_OVERLAY_TITLE_CHARS for hook in overlay["title_hooks"])
