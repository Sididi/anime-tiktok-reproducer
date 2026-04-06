from __future__ import annotations

import logging
from typing import Any

from ..config import settings


logger = logging.getLogger(__name__)


class LLMService:
    """Facade that delegates to the active LLM provider (Gemini or Claude)."""

    @classmethod
    def _provider(cls):
        """Return the active provider service class."""
        if settings.llm_provider == "claude":
            from .claude_service import ClaudeService

            return ClaudeService
        from .gemini_service import GeminiService

        return GeminiService

    @classmethod
    def is_configured(cls) -> bool:
        return cls._provider().is_configured()

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        return cls._provider().generate_text(
            prompt,
            model=model,
            max_output_tokens=max_output_tokens,
        )

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return cls._provider().generate_json(
            prompt,
            model=model,
            response_json_schema=response_json_schema,
        )

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
    ) -> Any:
        return cls._provider().generate_json_value(
            prompt,
            model=model,
            response_json_schema=response_json_schema,
        )

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        return cls._provider().check_api_health()

    @classmethod
    def provider_name(cls) -> str:
        """Return 'gemini' or 'claude'."""
        return settings.llm_provider

    @classmethod
    def active_model(cls) -> str:
        """Return the main model name for the active provider."""
        if settings.llm_provider == "claude":
            return settings.anthropic_model
        return settings.gemini_model

    @classmethod
    def active_light_model(cls) -> str:
        """Return the light model name for the active provider."""
        if settings.llm_provider == "claude":
            return settings.anthropic_light_model
        return settings.gemini_light_model
