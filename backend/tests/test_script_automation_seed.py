"""Tests for persistence of ElevenLabs TTS seeds."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.projects import ProjectResponse
from app.models.project import Project
from app.services.project_service import ProjectService
from app.services.script_automation_service import ScriptAutomationService


def test_generate_and_store_elevenlabs_seed_persists_exact_value(
    tmp_path, monkeypatch
):
    projects_dir = tmp_path / "projects"
    project = Project(id="seedproject1")
    (projects_dir / project.id).mkdir(parents=True)
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects_dir)
    monkeypatch.setattr(
        "app.services.script_automation_service.secrets.randbits",
        lambda bits: 3_456_789 if bits == 32 else None,
    )
    monkeypatch.setattr(ProjectService, "sync_project_pin", lambda _project: None)
    ProjectService.save(project)

    seed = ScriptAutomationService._generate_and_store_elevenlabs_seed(project.id)

    assert seed == 3_456_789
    loaded = ProjectService.load(project.id)
    assert loaded is not None
    assert loaded.elevenlabs_seed == seed
    project_json = json.loads(
        ProjectService.get_project_file(project.id).read_text(encoding="utf-8")
    )
    assert project_json["elevenlabs_seed"] == seed
    assert ProjectResponse.from_project(loaded).elevenlabs_seed == seed


def test_v2_sends_seed_and_keeps_request_stitching():
    kwargs = ScriptAutomationService._tts_seed_and_continuity_kwargs(
        seed=123,
        model_id="eleven_multilingual_v2",
        previous_request_ids=["older", "request-1"],
        previous_text="Before.",
        next_text="After.",
    )

    assert kwargs == {
        "seed": 123,
        "previous_request_ids": ["request-1"],
    }


def test_v3_sends_seed_without_unsupported_request_stitching():
    kwargs = ScriptAutomationService._tts_seed_and_continuity_kwargs(
        seed=456,
        model_id="eleven_v3",
        previous_request_ids=["request-1"],
        previous_text="Before.",
        next_text="After.",
    )

    assert kwargs == {"seed": 456}


def test_v4_sends_three_nearest_previous_requests_and_following_text():
    kwargs = ScriptAutomationService._tts_seed_and_continuity_kwargs(
        seed=0,
        model_id="eleven_v4",
        previous_request_ids=["old", "first", "second", "nearest"],
        previous_text="Before.",
        next_text="After.",
    )
    assert kwargs == {
        "seed": 0,
        "previous_request_ids": ["first", "second", "nearest"],
        "next_text": "After.",
    }


def test_v4_uses_previous_text_when_request_id_is_missing():
    kwargs = ScriptAutomationService._tts_seed_and_continuity_kwargs(
        seed=123,
        model_id="eleven_v4",
        previous_request_ids=[],
        previous_text="Before.",
    )
    assert kwargs == {"seed": 123, "previous_text": "Before."}
