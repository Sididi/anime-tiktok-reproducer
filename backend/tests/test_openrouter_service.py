"""Unit tests for OpenRouterService — API calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.llm_config import (
    AnthropicThinking,
    GeminiThinking,
    LLMPresetEntry,
)
from app.services.openrouter_service import OpenRouterService


def _make_chat_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def test_build_reasoning_anthropic():
    entry = LLMPresetEntry(
        openrouter_id="anthropic/x", thinking=AnthropicThinking(max_tokens=4000)
    )
    out = OpenRouterService._build_reasoning(entry)
    assert out == {"max_tokens": 4000, "exclude": True}


def test_build_reasoning_gemini():
    entry = LLMPresetEntry(
        openrouter_id="google/x", thinking=GeminiThinking(effort="high")
    )
    out = OpenRouterService._build_reasoning(entry)
    assert out == {"effort": "high", "exclude": True}


def test_build_reasoning_none():
    entry = LLMPresetEntry(openrouter_id="x/y", thinking=None)
    assert OpenRouterService._build_reasoning(entry) is None


def test_generate_text_uses_preset_big_by_default(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response("hello")

    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )

    fake_preset = MagicMock()
    fake_preset.big = LLMPresetEntry(
        openrouter_id="anthropic/big", thinking=AnthropicThinking(max_tokens=2000)
    )
    fake_preset.light = LLMPresetEntry(openrouter_id="anthropic/light", thinking=None)
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.get_preset",
        classmethod(lambda cls, key: fake_preset),
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "claude"),
    )

    out = OpenRouterService.generate_text("hi", tier="big")
    assert out == "hello"
    call = fake_client.chat.completions.create.call_args
    assert call.kwargs["model"] == "anthropic/big"
    assert call.kwargs["extra_body"]["reasoning"] == {
        "max_tokens": 2000,
        "exclude": True,
    }


def test_generate_text_light_tier_no_reasoning(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response("ok")

    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )

    fake_preset = MagicMock()
    fake_preset.big = LLMPresetEntry(openrouter_id="x/big", thinking=None)
    fake_preset.light = LLMPresetEntry(openrouter_id="x/light", thinking=None)
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.get_preset",
        classmethod(lambda cls, key: fake_preset),
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "x"),
    )

    OpenRouterService.generate_text("hi", tier="light")
    call = fake_client.chat.completions.create.call_args
    assert call.kwargs["model"] == "x/light"
    assert "reasoning" not in call.kwargs.get("extra_body", {})


def test_generate_json_value_strips_fence(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response(
        "```json\n{\"a\": 1}\n```"
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )
    fake_preset = MagicMock()
    fake_preset.big = LLMPresetEntry(openrouter_id="x/big", thinking=None)
    fake_preset.light = LLMPresetEntry(openrouter_id="x/light", thinking=None)
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.get_preset",
        classmethod(lambda cls, key: fake_preset),
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.LLMConfigService.default_preset_key",
        classmethod(lambda cls: "x"),
    )
    out = OpenRouterService.generate_json_value("hi")
    assert out == {"a": 1}


def test_generate_json_value_with_entry_uses_given_model(monkeypatch):
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _make_chat_response(
        '[{"i": 0, "t": "Bonjour"}]'
    )
    monkeypatch.setattr(
        "app.services.openrouter_service.OpenRouterService._get_client",
        classmethod(lambda cls: fake_client),
    )
    entry = LLMPresetEntry(
        openrouter_id="google/gemini-2.5-flash-lite", thinking=None
    )
    parsed = OpenRouterService.generate_json_value_with_entry(
        '[{"i": 0, "t": "Hello"}]',
        entry=entry,
        system="translate",
    )
    assert parsed == [{"i": 0, "t": "Bonjour"}]
    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "google/gemini-2.5-flash-lite"
    assert kwargs["messages"][0] == {"role": "system", "content": "translate"}
