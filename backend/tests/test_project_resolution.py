"""Tests for Project resolution helpers (LLM preset, template, speed)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.project import Project


def test_default_fields_are_none():
    p = Project()
    assert p.llm_preset is None
    assert p.template is None
    assert p.min_playback_speed is None
    assert p.elevenlabs_seed is None


def test_elevenlabs_seed_accepts_unsigned_32_bit_range():
    assert Project(elevenlabs_seed=0).elevenlabs_seed == 0
    assert Project(elevenlabs_seed=2**32 - 1).elevenlabs_seed == 2**32 - 1

    with pytest.raises(ValueError):
        Project(elevenlabs_seed=-1)
    with pytest.raises(ValueError):
        Project(elevenlabs_seed=2**32)


def test_speed_validator_accepts_valid_range():
    Project(min_playback_speed=0.5)
    Project(min_playback_speed=1.0)


def test_speed_validator_rejects_invalid():
    with pytest.raises(ValueError):
        Project(min_playback_speed=0.0)
    with pytest.raises(ValueError):
        Project(min_playback_speed=1.5)
    with pytest.raises(ValueError):
        Project(min_playback_speed=-0.1)


def test_resolved_min_playback_speed_uses_project_value(monkeypatch):
    monkeypatch.setattr(
        "app.models.project.settings.min_playback_speed_factor", 0.75
    )
    p = Project(min_playback_speed=0.6)
    assert p.resolved_min_playback_speed() == 0.6


def test_resolved_min_playback_speed_falls_back_to_settings(monkeypatch):
    monkeypatch.setattr(
        "app.models.project.settings.min_playback_speed_factor", 0.75
    )
    p = Project()
    assert p.resolved_min_playback_speed() == 0.75


def test_resolved_llm_preset_uses_project_value(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_config_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "claude"),
    )
    p = Project(llm_preset="gemini")
    assert p.resolved_llm_preset_key() == "gemini"


def test_resolved_llm_preset_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "app.services.llm_config_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "claude"),
    )
    p = Project()
    assert p.resolved_llm_preset_key() == "claude"


def test_resolved_template_key_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(
        "app.services.template_service.TemplateService.default_key",
        classmethod(lambda cls: "classic"),
    )
    p = Project()
    assert p.resolved_template_key() == "classic"


def test_template_optional_defaults_are_resolved(monkeypatch):
    template = SimpleNamespace(
        min_playback_speed=0.5,
        llm_preset="mistral",
        voice_key="nicolas_petit",
        music_key="montagem_batchi_cut_start_15s",
    )
    monkeypatch.setattr(
        "app.services.template_service.TemplateService.get",
        classmethod(lambda cls, key: template),
    )
    project = Project(template="zoomed")
    assert project.resolved_min_playback_speed() == 0.5
    assert project.resolved_llm_preset_key() == "mistral"
    assert project.resolved_voice_key() == "nicolas_petit"
    assert project.resolved_music_key() == "montagem_batchi_cut_start_15s"
