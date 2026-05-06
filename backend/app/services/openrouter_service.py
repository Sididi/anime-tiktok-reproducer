"""Single LLM provider — calls every model through OpenRouter via the
openai-compatible chat completions API."""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

from openai import OpenAI, APITimeoutError

from ..config import settings
from ..models.llm_config import (
    AnthropicThinking,
    GeminiThinking,
    LLMPresetEntry,
)
from .llm_config_service import LLMConfigService


logger = logging.getLogger(__name__)

Tier = Literal["big", "light"]


class OpenRouterService:
    """Wrapper over OpenRouter's OpenAI-compatible API."""

    _client: OpenAI | None = None

    @classmethod
    def _get_client(cls) -> OpenAI:
        if cls._client is not None:
            return cls._client
        api_key = (settings.openrouter_api_key or "").strip()
        if not api_key:
            raise RuntimeError(
                "OpenRouter API key is missing (ATR_OPENROUTER_API_KEY)"
            )
        cls._client = OpenAI(
            api_key=api_key,
            base_url=settings.openrouter_base_url,
            timeout=settings.openrouter_timeout,
        )
        return cls._client

    @classmethod
    def is_configured(cls) -> bool:
        return bool((settings.openrouter_api_key or "").strip())

    @classmethod
    def _resolve_entry(cls, *, preset_key: str | None, tier: Tier) -> LLMPresetEntry:
        key = preset_key or LLMConfigService.default_preset_key()
        preset = LLMConfigService.get_preset(key)
        return preset.big if tier == "big" else preset.light

    @classmethod
    def _build_reasoning(cls, entry: LLMPresetEntry) -> dict[str, Any] | None:
        if entry.thinking is None:
            return None
        if isinstance(entry.thinking, AnthropicThinking):
            return {"max_tokens": entry.thinking.max_tokens, "exclude": True}
        if isinstance(entry.thinking, GeminiThinking):
            return {"effort": entry.thinking.effort, "exclude": True}
        raise RuntimeError(f"Unknown thinking shape: {entry.thinking!r}")

    @classmethod
    def _chat(
        cls,
        prompt: str,
        *,
        entry: LLMPresetEntry,
        system: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        client = cls._get_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": entry.openrouter_id,
            "messages": messages,
        }
        if max_output_tokens:
            kwargs["max_tokens"] = max_output_tokens

        extra_body: dict[str, Any] = {}
        reasoning = cls._build_reasoning(entry)
        if reasoning is not None:
            extra_body["reasoning"] = reasoning
        if extra_body:
            kwargs["extra_body"] = extra_body

        try:
            response = client.chat.completions.create(**kwargs)
        except APITimeoutError as exc:
            raise RuntimeError(
                f"OpenRouter timeout after {settings.openrouter_timeout}s "
                f"(model={entry.openrouter_id})"
            ) from exc

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if not text:
            raise RuntimeError(
                f"OpenRouter response was empty (model={entry.openrouter_id})"
            )
        return text

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        trimmed = raw.strip()
        if not trimmed.startswith("```"):
            return trimmed
        lines = trimmed.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @classmethod
    def _parse_json_value(cls, raw: str) -> Any:
        stripped = cls._strip_json_fence(raw)
        decoder = json.JSONDecoder()
        try:
            parsed, end = decoder.raw_decode(stripped)
            if not stripped[end:].strip():
                return parsed
        except json.JSONDecodeError:
            pass
        for idx, char in enumerate(stripped):
            if char not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[idx:])
                return parsed
            except json.JSONDecodeError:
                continue
        raise RuntimeError("Unable to parse OpenRouter JSON response")

    # --- public API (matches LLMService facade) ---

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
        max_output_tokens: int | None = None,
    ) -> str:
        entry = cls._resolve_entry(preset_key=preset_key, tier=tier)
        return cls._chat(prompt, entry=entry, max_output_tokens=max_output_tokens)

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> Any:
        entry = cls._resolve_entry(preset_key=preset_key, tier=tier)
        raw = cls._chat(
            prompt,
            entry=entry,
            system="You must respond with valid JSON only. No markdown fences, no explanation.",
        )
        return cls._parse_json_value(raw)

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        preset_key: str | None = None,
        tier: Tier = "big",
    ) -> dict[str, Any]:
        parsed = cls.generate_json_value(prompt, preset_key=preset_key, tier=tier)
        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("OpenRouter JSON response must be a JSON object")

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        if not cls.is_configured():
            return {"status": "skipped", "detail": "OpenRouter API key not configured"}
        try:
            preset_key = LLMConfigService.default_preset_key()
            preset = LLMConfigService.get_preset(preset_key)
            reply = cls.generate_text("Reply with exactly: pong", tier="light")
            return {
                "status": "ok",
                "detail": f"OpenRouter reachable (preset={preset_key})",
                "model": preset.light.openrouter_id,
                "reply": reply[:60],
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
