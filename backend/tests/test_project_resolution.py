"""Tests for Project resolution helpers (LLM preset, template, speed)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.project import Project


def test_default_fields_are_none():
    p = Project()
    assert p.llm_preset is None
    assert p.template is None
    assert p.min_playback_speed is None


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
