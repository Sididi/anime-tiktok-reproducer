"""Tests for persisting the voice a TTS automation run generates with.

The upload validation in /script/restructured recomputes the expected TTS
segment count from the *project's* resolved voice (its model decides the
segmentation target). If an automation run generates parts with a voice that
is never persisted onto the project, the counts can diverge (e.g. 8 v2 parts
vs 10 expected v3 segments), so the run must record its voice on the project.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.project import Project
from app.services.project_service import ProjectService
from app.services.script_automation_service import ScriptAutomationService


def _setup_project(tmp_path, monkeypatch, **project_kwargs) -> Project:
    projects_dir = tmp_path / "projects"
    project = Project(id="voicepersist1", **project_kwargs)
    (projects_dir / project.id).mkdir(parents=True)
    monkeypatch.setattr(
        "app.services.project_service.settings.projects_dir", projects_dir
    )
    monkeypatch.setattr(ProjectService, "sync_project_pin", lambda _project: None)
    ProjectService.save(project)
    return project


def test_persist_run_voice_key_stores_voice_on_project(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, monkeypatch, voice_key=None)

    ScriptAutomationService._persist_run_voice_key(project.id, "nicolas_petit")

    loaded = ProjectService.load(project.id)
    assert loaded is not None
    assert loaded.voice_key == "nicolas_petit"
    project_json = json.loads(
        ProjectService.get_project_file(project.id).read_text(encoding="utf-8")
    )
    assert project_json["voice_key"] == "nicolas_petit"


def test_persist_run_voice_key_overwrites_previous_choice(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, monkeypatch, voice_key="sebastien")

    ScriptAutomationService._persist_run_voice_key(project.id, "maxime")

    loaded = ProjectService.load(project.id)
    assert loaded is not None
    assert loaded.voice_key == "maxime"


def test_persist_run_voice_key_missing_project_raises(tmp_path, monkeypatch):
    _setup_project(tmp_path, monkeypatch)

    try:
        ScriptAutomationService._persist_run_voice_key("does-not-exist", "maxime")
    except RuntimeError as exc:
        assert "Project not found" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for unknown project")


def _voice(default_tts_speed=None):
    from app.services.voice_config_service import VoiceEntry

    return VoiceEntry(
        key="maxime",
        display_name="Maxime",
        elevenlabs_voice_id="voice-id",
        voice_settings={},
        default_tts_speed=default_tts_speed,
    )


def test_voice_default_tts_speed_applied_after_generation(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, monkeypatch, tts_speed=1.3)

    applied = ScriptAutomationService._persist_voice_default_tts_speed(
        project.id, _voice(default_tts_speed=1.1)
    )

    assert applied == 1.1
    loaded = ProjectService.load(project.id)
    assert loaded is not None
    assert loaded.tts_speed == 1.1


def test_voice_without_default_tts_speed_resets_to_neutral(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, monkeypatch, tts_speed=1.3)

    applied = ScriptAutomationService._persist_voice_default_tts_speed(
        project.id, _voice(default_tts_speed=None)
    )

    assert applied == 1.0
    loaded = ProjectService.load(project.id)
    assert loaded is not None
    assert loaded.tts_speed == 1.0
