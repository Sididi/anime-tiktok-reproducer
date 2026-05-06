"""Pydantic models for the LLM preset catalog (config/llm/config.yaml)."""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, model_validator


class AnthropicThinking(BaseModel):
    """Reasoning shape for Anthropic models — budget in tokens."""

    max_tokens: int = Field(..., gt=0, le=64000)
    model_config = {"extra": "forbid"}


class GeminiThinking(BaseModel):
    """Reasoning shape for Gemini models — effort level."""

    effort: Literal["low", "medium", "high", "xhigh"]
    model_config = {"extra": "forbid"}


ThinkingConfig = Union[AnthropicThinking, GeminiThinking]


class LLMPresetEntry(BaseModel):
    """One model tier inside a preset (big or light)."""

    openrouter_id: str = Field(..., min_length=1)
    thinking: ThinkingConfig | None = None
    model_config = {"extra": "forbid"}


class LLMPreset(BaseModel):
    """A preset bundles a big model + a light model under a label."""

    label: str = Field(..., min_length=1)
    big: LLMPresetEntry
    light: LLMPresetEntry
    model_config = {"extra": "forbid"}


class LLMConfig(BaseModel):
    """Root of config/llm/config.yaml."""

    default: str = Field(..., min_length=1)
    presets: dict[str, LLMPreset]
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _default_must_exist(self) -> "LLMConfig":
        if self.default not in self.presets:
            raise ValueError(
                f"default preset '{self.default}' is not in presets keys: "
                f"{sorted(self.presets.keys())}"
            )
        return self
