from __future__ import annotations

import pytest

from app.config import settings
from app.services.llm_service import LLMService


def test_provider_returns_gemini_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    from app.services.gemini_service import GeminiService

    assert LLMService._provider() is GeminiService


def test_provider_returns_claude(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    from app.services.claude_service import ClaudeService

    assert LLMService._provider() is ClaudeService


def test_active_model_gemini(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_model", "gemini-test")
    assert LLMService.active_model() == "gemini-test"


def test_active_model_claude(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "anthropic_model", "claude-test")
    assert LLMService.active_model() == "claude-test"


def test_active_light_model_gemini(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_light_model", "gemini-flash-test")
    assert LLMService.active_light_model() == "gemini-flash-test"


def test_active_light_model_claude(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "anthropic_light_model", "claude-haiku-test")
    assert LLMService.active_light_model() == "claude-haiku-test"


def test_provider_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    assert LLMService.provider_name() == "gemini"

    monkeypatch.setattr(settings, "llm_provider", "claude")
    assert LLMService.provider_name() == "claude"


def test_delegation_generate_text(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini")

    from app.services.gemini_service import GeminiService

    called_with: list[tuple] = []

    def fake_generate_text(prompt, *, model=None, max_output_tokens=None):
        called_with.append((prompt, model, max_output_tokens))
        return "hello"

    monkeypatch.setattr(GeminiService, "generate_text", fake_generate_text)

    result = LLMService.generate_text("test prompt", model="m", max_output_tokens=100)

    assert result == "hello"
    assert called_with == [("test prompt", "m", 100)]
