"""Tests for LLM preset Pydantic models."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.llm_config import (
    AnthropicThinking,
    GeminiThinking,
    LLMPresetEntry,
    LLMPreset,
    LLMConfig,
)


def test_anthropic_thinking_validates_max_tokens_positive():
    AnthropicThinking(max_tokens=4000)
    with pytest.raises(ValueError):
        AnthropicThinking(max_tokens=0)


def test_gemini_thinking_validates_effort_enum():
    GeminiThinking(effort="high")
    with pytest.raises(ValueError):
        GeminiThinking(effort="medium-high")


def test_preset_entry_accepts_either_thinking_shape_or_null():
    LLMPresetEntry(openrouter_id="x/y", thinking=AnthropicThinking(max_tokens=6000))
    LLMPresetEntry(openrouter_id="x/y", thinking=GeminiThinking(effort="high"))
    LLMPresetEntry(openrouter_id="x/y", thinking=None)


def test_llm_config_default_must_exist_in_presets():
    presets = {
        "claude": LLMPreset(
            label="Claude",
            big=LLMPresetEntry(openrouter_id="anthropic/x", thinking=None),
            light=LLMPresetEntry(openrouter_id="anthropic/y", thinking=None),
        )
    }
    LLMConfig(default="claude", presets=presets)
    with pytest.raises(ValueError):
        LLMConfig(default="missing", presets=presets)
