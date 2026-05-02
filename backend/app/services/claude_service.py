from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ..config import settings


logger = logging.getLogger(__name__)


# Extended thinking configuration
THINKING_BUDGET_TOKENS: int = 10000
THINKING_MAX_TOKENS: int = 24000


def _is_adaptive_thinking_model(model: str) -> bool:
    """Models that require adaptive thinking and reject sampling parameters.

    Opus 4.7 removed ``thinking.type=enabled`` / ``budget_tokens`` and the
    ``temperature`` / ``top_p`` / ``top_k`` sampling fields. They must be
    replaced with ``thinking.type=adaptive`` and (optionally)
    ``output_config.effort``. Sonnet 4.6 and Opus 4.6 still accept the
    older shape, so we only adapt for the Opus 4.7+ family.
    """
    normalized = (model or "").strip().lower()
    return "opus-4-7" in normalized


class ClaudeService:
    """Wrapper around Anthropic Claude API (anthropic SDK)."""

    @classmethod
    def is_configured(cls) -> bool:
        return bool((settings.anthropic_api_key or "").strip())

    @classmethod
    def _get_client(cls) -> anthropic.Anthropic:
        api_key = (settings.anthropic_api_key or "").strip()
        if not api_key:
            raise RuntimeError("Anthropic API key is missing (ATR_ANTHROPIC_API_KEY)")
        return anthropic.Anthropic(api_key=api_key, timeout=settings.anthropic_timeout)

    @classmethod
    def _extract_text(cls, response: anthropic.types.Message) -> str:
        chunks: list[str] = []
        for block in response.content:
            if block.type == "text":
                chunks.append(block.text)
        text = "\n".join(chunks).strip()
        if not text:
            raise RuntimeError(
                f"Claude response did not contain text output "
                f"(stop_reason={response.stop_reason})"
            )
        return text

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        trimmed = raw.strip()
        if not trimmed.startswith("```"):
            return trimmed

        lines = trimmed.splitlines()
        if len(lines) <= 2:
            return trimmed

        if lines[0].startswith("```"):
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

        raise RuntimeError("Unable to parse Claude JSON response")

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        enable_thinking: bool = False,
    ) -> str:
        client = cls._get_client()
        chosen_model = (model or settings.anthropic_model).strip()
        if not chosen_model:
            raise RuntimeError("Anthropic model is not configured (ATR_ANTHROPIC_MODEL)")

        create_kwargs: dict[str, Any] = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": prompt}],
        }

        adaptive = _is_adaptive_thinking_model(chosen_model)

        if enable_thinking:
            create_kwargs["max_tokens"] = max_output_tokens or THINKING_MAX_TOKENS
            if adaptive:
                # Opus 4.7+: adaptive thinking only; sampling params disallowed.
                create_kwargs["thinking"] = {"type": "adaptive"}
            else:
                # Sonnet 4.6 / Opus 4.6: extended thinking with explicit budget.
                create_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": THINKING_BUDGET_TOKENS,
                }
        else:
            create_kwargs["max_tokens"] = max_output_tokens or 16000
            if not adaptive:
                create_kwargs["temperature"] = 0.35

        try:
            response = client.messages.create(**create_kwargs)
        except anthropic.APITimeoutError:
            raise RuntimeError(
                f"Claude API timeout after {settings.anthropic_timeout}s"
            )

        return cls._extract_text(response)

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
        enable_thinking: bool = False,
    ) -> Any:
        """Generate a JSON value from Claude.

        The ``response_json_schema`` parameter is accepted for API parity with
        GeminiService but is **not** forwarded to Claude's ``output_config``
        because the Anthropic API rejects many common JSON-Schema properties
        (``minItems`` > 1, ``maxItems``, ``maxLength``, …).  Instead we rely
        on a system prompt that forces pure-JSON output and on the user prompt
        which already specifies the exact structure and constraints.

        When ``enable_thinking`` is True, extended thinking mode is enabled
        with a budget of THINKING_BUDGET_TOKENS. This helps with complex
        multi-constraint reasoning tasks like script generation.
        """
        client = cls._get_client()
        chosen_model = (model or settings.anthropic_model).strip()
        if not chosen_model:
            raise RuntimeError("Anthropic model is not configured (ATR_ANTHROPIC_MODEL)")

        create_kwargs: dict[str, Any] = {
            "model": chosen_model,
            "system": (
                "You must respond with valid JSON only. "
                "No markdown fences, no explanation."
            ),
            "messages": [{"role": "user", "content": prompt}],
        }

        adaptive = _is_adaptive_thinking_model(chosen_model)

        if enable_thinking:
            create_kwargs["max_tokens"] = THINKING_MAX_TOKENS
            if adaptive:
                # Opus 4.7+: adaptive thinking only; sampling params disallowed.
                create_kwargs["thinking"] = {"type": "adaptive"}
            else:
                # Sonnet 4.6 / Opus 4.6: extended thinking with explicit budget.
                create_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": THINKING_BUDGET_TOKENS,
                }
        else:
            create_kwargs["max_tokens"] = 16000
            if not adaptive:
                create_kwargs["temperature"] = 0.35

        try:
            response = client.messages.create(**create_kwargs)
        except anthropic.APITimeoutError:
            raise RuntimeError(
                f"Claude API timeout after {settings.anthropic_timeout}s"
            )

        raw = cls._extract_text(response)
        return cls._parse_json_value(raw)

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
        enable_thinking: bool = False,
    ) -> dict[str, Any]:
        parsed = cls.generate_json_value(
            prompt,
            model=model,
            response_json_schema=response_json_schema,
            enable_thinking=enable_thinking,
        )
        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError("Claude JSON response must be a JSON object")

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        if not cls.is_configured():
            return {"status": "skipped", "detail": "Anthropic API key not configured"}
        try:
            reply = cls.generate_text("Reply with exactly: pong")
            return {
                "status": "ok",
                "detail": f"Claude API reachable (model={settings.anthropic_model})",
                "model": settings.anthropic_model,
                "reply": reply[:60],
            }
        except Exception as exc:
            return {
                "status": "error",
                "detail": str(exc),
                "model": settings.anthropic_model,
            }
