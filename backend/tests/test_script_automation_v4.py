"""Exercise v4 automation through HTTP payloads and persisted audio/metadata."""
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import Project, Transcription
from app.services.project_service import ProjectService
from app.services.script_automation_service import ScriptAutomationService as Automation
from app.services.voice_config_service import VoiceConfigService, VoiceEntry


def test_opting_v3_voice_into_v4_keeps_the_same_segment_layout():
    payload = {"language": "fr", "scenes": [{"scene_index": 0, "text": "Une nouvelle aventure commence. " * 120}]}
    assert Automation.prepare_tts_payload(script_payload=payload, model_id="eleven_v4") == Automation.prepare_tts_payload(
        script_payload=payload, model_id="eleven_v3",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_request_id", [False, True])
async def test_v4_run_context_seed_manifest_and_pcm(tmp_path, monkeypatch, missing_request_id):
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(settings, "script_automate_enabled", True)
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")
    monkeypatch.setattr(settings, "elevenlabs_output_format", "pcm_48000")
    monkeypatch.setattr(ProjectService, "sync_project_pin", lambda project: None)
    monkeypatch.setattr("app.services.script_automation_service.secrets.randbits", lambda bits: 321)
    project = Project(id="v4test", voice_key="test")
    project_dir = tmp_path / project.id
    project_dir.mkdir()
    (project_dir / "project.json").write_text(project.model_dump_json())
    text = "Le héros traverse la forêt et retrouve enfin ses amis au village. " * 50
    source = Transcription(language="fr", scenes=[{
        "scene_index": 0, "text": text, "start_time": 0, "end_time": 90,
    }])
    (project_dir / "transcription.json").write_text(source.model_dump_json())
    voice = VoiceEntry(key="test", display_name="Test", elevenlabs_voice_id="voice",
                       model_id="eleven_v4", voice_settings={"stability": 0.4, "similarity_boost": 0.8},
                       default_tts_speed=1.05)
    monkeypatch.setattr(VoiceConfigService, "get_voice", lambda key: voice)
    payload = {"language": "fr", "scenes": [{"scene_index": 0, "text": text}]}
    prepared = Automation.prepare_tts_payload(script_payload=payload, model_id="eleven_v4")
    chunks = [segment["text"] for segment in prepared["segments"]]
    assert len(chunks) >= 4
    calls = []

    def post(url, **kwargs):
        assert url.endswith("/text-to-dialogue")
        calls.append(kwargs["json"])
        headers = {} if missing_request_id and len(calls) == 2 else {"request-id": f"req-{len(calls)}"}
        return SimpleNamespace(status_code=200, content=b"\x00\x00" * 4800, headers=headers)

    monkeypatch.setattr("app.services.elevenlabs_service.requests.post", post)
    events = [e async for e in Automation.stream_automation(
        project_id=project.id, target_language="fr", voice_key="test",
        existing_script_json=payload, skip_metadata=True, skip_overlay=True,
    )]
    assert events[-1]["event"] == "complete", events[-1]
    assert len(calls) == len(chunks)
    for i, call in enumerate(calls):
        assert call["seed"] == 321
        assert call["language_code"] == "fr"
        assert call["inputs"] == [{"voice_id": "voice", "text": f"{Automation.NARRATION_CONTROL_PREFIX} {chunks[i]}"}]
        assert "next_text" not in call
        if i < len(chunks) - 1:
            assert call["future_text"] == chunks[i + 1][:100]
        else:
            assert "future_text" not in call
    assert "previous_request_ids" not in calls[0] and "previous_text" not in calls[0]
    assert calls[1]["previous_request_ids"] == ["req-1"]
    if missing_request_id:
        assert calls[2]["previous_text"] == chunks[1][-100:]
        assert "previous_request_ids" not in calls[2]
        assert calls[3]["previous_request_ids"] == ["req-3"]
    else:
        for i in range(1, len(calls)):
            assert calls[i]["previous_request_ids"] == [f"req-{n}" for n in range(max(1, i - 2), i + 1)]
    loaded = ProjectService.load(project.id)
    assert loaded.elevenlabs_seed == 321 and loaded.tts_speed == 1.05
    run_dir = project_dir / Automation.RUNS_DIR_NAME / events[-1]["run_id"]
    manifest = json.loads((run_dir / "tts_manifest.json").read_text())
    assert len(manifest) == len(chunks)
    assert manifest[0]["request_id"] == "req-1"
    assert manifest[-1]["synthesis_arguments"]["seed"] == 321
    assert manifest[-1]["synthesis_arguments"]["model_id"] == "eleven_v4"
    with wave.open(str(run_dir / "parts" / "part_1.wav"), "rb") as audio:
        assert audio.getframerate() == 48000 and audio.getnframes() == 4800
    assert (run_dir / "merged.wav").exists()
