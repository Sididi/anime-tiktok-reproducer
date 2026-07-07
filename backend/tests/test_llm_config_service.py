"""Tests for LLMConfigService YAML loading."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.llm_config_service import LLMConfigService


VALID_YAML = """\
default: claude
presets:
  claude:
    label: "Claude"
    big:
      openrouter_id: anthropic/claude-opus-4.7
      thinking:
        max_tokens: 6000
    light:
      openrouter_id: anthropic/claude-haiku-4.5
      thinking: null
  gemini:
    label: "Gemini"
    big:
      openrouter_id: google/gemini-3-pro-preview
      thinking:
        effort: high
    light:
      openrouter_id: google/gemini-2.5-flash
      thinking: null
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_loads_valid_config(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    cfg = LLMConfigService.get_config(force_reload=True)
    assert cfg.default == "claude"
    assert "claude" in cfg.presets
    assert cfg.presets["claude"].big.openrouter_id == "anthropic/claude-opus-4.7"


def test_default_preset_resolves(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    assert LLMConfigService.default_preset_key() == "claude"
    preset = LLMConfigService.get_preset("gemini")
    assert preset.big.openrouter_id == "google/gemini-3-pro-preview"


def test_unknown_preset_key_raises(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    with pytest.raises(ValueError):
        LLMConfigService.get_preset("nope")


def test_invalid_yaml_raises(tmp_path, monkeypatch):
    path = _write(tmp_path, "default: claude\npresets: not-a-mapping\n")
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    with pytest.raises(ValueError):
        LLMConfigService.get_config(force_reload=True)


def test_default_must_exist_in_presets(tmp_path, monkeypatch):
    body = VALID_YAML.replace("default: claude", "default: missing")
    path = _write(tmp_path, body)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    with pytest.raises(ValueError):
        LLMConfigService.get_config(force_reload=True)


TRANSLATION_YAML = VALID_YAML + """\
translation:
  openrouter_id: google/gemini-2.5-flash-lite
  thinking: null
"""


def test_translation_entry_from_config(tmp_path, monkeypatch):
    path = _write(tmp_path, TRANSLATION_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    entry = LLMConfigService.translation_entry()
    assert entry.openrouter_id == "google/gemini-2.5-flash-lite"
    assert entry.thinking is None


def test_translation_entry_falls_back_to_default_light(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID_YAML)
    monkeypatch.setattr(
        "app.services.llm_config_service.settings.llm_config_path", path
    )
    LLMConfigService.get_config(force_reload=True)
    entry = LLMConfigService.translation_entry()
    # default preset is "claude"; its light model is haiku
    assert entry.openrouter_id == "anthropic/claude-haiku-4.5"
