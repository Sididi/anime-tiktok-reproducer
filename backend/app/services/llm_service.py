"""Facade over OpenRouterService. Provides a `tier` parameter so call sites
declare whether they need the heavyweight reasoning model (big) or the
fast/cheap one (light)."""
from __future__ import annotations

import logging
from typing import Any, Literal

from .llm_config_service import LLMConfigService
from .openrouter_service import OpenRouterService


logger = logging.getLogger(__name__)

Tier = Literal["big", "light"]


class LLMService:
    """Single entry point for LLM calls. Delegates to OpenRouter."""

    @classmethod
    def is_configured(cls) -> bool:
        return OpenRouterService.is_configured()

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
        max_output_tokens: int | None = None,
    ) -> str:
        return OpenRouterService.generate_text(
            prompt,
            preset_key=preset_key,
            tier=tier,
            max_output_tokens=max_output_tokens,
        )

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> dict[str, Any]:
        return OpenRouterService.generate_json(
            prompt, preset_key=preset_key, tier=tier
        )

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> Any:
        return OpenRouterService.generate_json_value(
            prompt, preset_key=preset_key, tier=tier
        )

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        return OpenRouterService.check_api_health()

    @classmethod
    def preset_key(cls, *, preset_key: str | None = None) -> str:
        return preset_key or LLMConfigService.default_preset_key()

    @classmethod
    def active_model(cls, *, preset_key: str | None = None) -> str:
        key = cls.preset_key(preset_key=preset_key)
        return LLMConfigService.get_preset(key).big.openrouter_id

    @classmethod
    def active_light_model(cls, *, preset_key: str | None = None) -> str:
        key = cls.preset_key(preset_key=preset_key)
        return LLMConfigService.get_preset(key).light.openrouter_id
