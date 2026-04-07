from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from ..config import settings


logger = logging.getLogger(__name__)


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
    ) -> str:
        client = cls._get_client()
        chosen_model = (model or settings.anthropic_model).strip()
        if not chosen_model:
            raise RuntimeError("Anthropic model is not configured (ATR_ANTHROPIC_MODEL)")

        try:
            response = client.messages.create(
                model=chosen_model,
                max_tokens=max_output_tokens or 16000,
                temperature=0.35,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APITimeoutError:
            raise RuntimeError(
                f"Claude API timeout after {settings.anthropic_timeout}s"
            )

        return cls._extract_text(response)

    @classmethod
    def _sanitize_schema(cls, schema: dict[str, Any]) -> dict[str, Any]:
        """Deep-clone a JSON schema and clamp constraints unsupported by Claude.

        Claude's ``output_config`` rejects ``minItems`` values > 1 for arrays.
        We clamp those to 1 so the schema is still accepted and structure is
        enforced, while the prompt carries the exact-count requirement.
        """
        import copy

        sanitized = copy.deepcopy(schema)

        def _walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            if node.get("type") == "array" and "minItems" in node:
                if node["minItems"] > 1:
                    node["minItems"] = 1
            for value in node.values():
                if isinstance(value, dict):
                    _walk(value)
                elif isinstance(value, list):
                    for item in value:
                        _walk(item)

        _walk(sanitized)
        return sanitized

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
    ) -> Any:
        client = cls._get_client()
        chosen_model = (model or settings.anthropic_model).strip()
        if not chosen_model:
            raise RuntimeError("Anthropic model is not configured (ATR_ANTHROPIC_MODEL)")

        create_kwargs: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": 16000,
            "temperature": 0.35,
            "messages": [{"role": "user", "content": prompt}],
        }

        has_schema = response_json_schema is not None
        if has_schema:
            create_kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": cls._sanitize_schema(response_json_schema),
                }
            }
        else:
            create_kwargs["system"] = (
                "You must respond with valid JSON only. "
                "No markdown fences, no explanation."
            )

        try:
            response = client.messages.create(**create_kwargs)
        except anthropic.BadRequestError as exc:
            if has_schema and "schema" in str(exc).lower():
                logger.warning(
                    "Claude schema error, retrying without schema: %s", exc
                )
                create_kwargs.pop("output_config", None)
                create_kwargs["system"] = (
                    "You must respond with valid JSON only. "
                    "No markdown fences, no explanation."
                )
                response = client.messages.create(**create_kwargs)
            else:
                raise RuntimeError(f"Claude API error: {exc}") from exc
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
    ) -> dict[str, Any]:
        parsed = cls.generate_json_value(
            prompt,
            model=model,
            response_json_schema=response_json_schema,
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
