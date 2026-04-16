from __future__ import annotations

import json
import logging
from typing import Any

import requests

from ..config import settings


logger = logging.getLogger(__name__)


# Thinking configuration
THINKING_LEVEL: str = "high"  # For Gemini 3.x series
THINKING_BUDGET_TOKENS: int = 10000  # For Gemini 2.5 series


class GeminiService:
    """Wrapper around Google Gemini API (AI Studio key auth)."""

    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    @classmethod
    def is_configured(cls) -> bool:
        return bool((settings.gemini_api_key or "").strip())

    @staticmethod
    def _get_thinking_config(model: str, enable: bool) -> dict[str, Any] | None:
        """Return appropriate thinking config based on model family.

        - Gemini 3.x series uses thinkingLevel ("low", "medium", "high")
        - Gemini 2.5 series uses thinkingBudget (token count)
        - Older models don't support thinking
        """
        if not enable:
            return None

        model_lower = model.lower()

        # Gemini 3.x series uses thinkingLevel
        if any(x in model_lower for x in ["gemini-3", "3.1", "3.0"]):
            return {"thinkingLevel": THINKING_LEVEL}

        # Gemini 2.5 series uses thinkingBudget
        if "2.5" in model_lower:
            return {"thinkingBudget": THINKING_BUDGET_TOKENS}

        # Older models don't support thinking
        return None

    @classmethod
    def _generate_content(
        cls,
        *,
        prompt: str,
        response_mime_type: str,
        response_json_schema: dict[str, Any] | None = None,
        model: str | None = None,
        temperature: float = 0.35,
        max_output_tokens: int | None = None,
        enable_thinking: bool = False,
    ) -> dict[str, Any]:
        api_key = (settings.gemini_api_key or "").strip()
        if not api_key:
            raise RuntimeError("Gemini API key is missing (ATR_GEMINI_API_KEY)")

        chosen_model = (model or settings.gemini_model).strip()
        if not chosen_model:
            raise RuntimeError("Gemini model is not configured (ATR_GEMINI_MODEL)")

        generation_config: dict[str, Any] = {
            "responseMimeType": response_mime_type,
            "temperature": temperature,
        }
        if response_json_schema is not None:
            generation_config["responseJsonSchema"] = response_json_schema
        if max_output_tokens is not None:
            generation_config["maxOutputTokens"] = int(max_output_tokens)

        # Add thinking config if enabled and supported by the model
        thinking_config = cls._get_thinking_config(chosen_model, enable_thinking)
        if thinking_config is not None:
            generation_config["thinkingConfig"] = thinking_config

        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

        try:
            response = requests.post(
                f"{cls._BASE_URL}/models/{chosen_model}:generateContent",
                params={"key": api_key},
                json=payload,
                timeout=(10, settings.gemini_timeout),
            )
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"Gemini API timeout after {settings.gemini_timeout}s — response too large or model slow"
            )

        if response.status_code >= 400:
            detail = response.text
            try:
                data = response.json()
                detail = data.get("error", {}).get("message", detail)
            except Exception:
                pass
            raise RuntimeError(f"Gemini API error: {detail}")

        return response.json()

    @staticmethod
    def _response_diagnostics(response_payload: dict[str, Any]) -> str:
        details: list[str] = []

        prompt_feedback = response_payload.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            block_reason = prompt_feedback.get("blockReason")
            if isinstance(block_reason, str) and block_reason.strip():
                details.append(f"blockReason={block_reason.strip()}")

            safety_ratings = prompt_feedback.get("safetyRatings")
            if isinstance(safety_ratings, list):
                categories: list[str] = []
                for rating in safety_ratings:
                    if not isinstance(rating, dict):
                        continue
                    category = rating.get("category")
                    if isinstance(category, str) and category.strip():
                        categories.append(category.strip())
                if categories:
                    details.append(
                        "safetyCategories=" + ",".join(dict.fromkeys(categories))
                    )

        candidates = response_payload.get("candidates")
        if isinstance(candidates, list) and candidates:
            finish_reasons: list[str] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                finish_reason = candidate.get("finishReason")
                if isinstance(finish_reason, str) and finish_reason.strip():
                    finish_reasons.append(finish_reason.strip())
            if finish_reasons:
                details.append("finishReasons=" + ",".join(dict.fromkeys(finish_reasons)))

        return "; ".join(details) if details else "no diagnostics"

    @staticmethod
    def _is_schema_retryable_error(message: str, *, has_schema: bool) -> bool:
        if not has_schema:
            return False

        lower = message.lower()
        return (
            "responsejsonschema" in lower
            or "responseschema" in lower
            or "unknown name" in lower
            or "invalid argument" in lower
            or "too many states" in lower
            or "schema produces a constraint" in lower
            or "did not contain candidates" in lower
            or "did not contain textual output" in lower
        )

    @staticmethod
    def _extract_text(response_payload: dict[str, Any]) -> str:
        candidates = response_payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            diagnostics = GeminiService._response_diagnostics(response_payload)
            logger.warning("Gemini response missing candidates (%s)", diagnostics)
            raise RuntimeError(
                "Gemini response did not contain candidates "
                f"({diagnostics})"
            )

        chunks: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text)

        text_output = "\n".join(chunks).strip()
        if not text_output:
            diagnostics = GeminiService._response_diagnostics(response_payload)
            logger.warning("Gemini response missing textual output (%s)", diagnostics)
            raise RuntimeError(
                "Gemini response did not contain textual output "
                f"({diagnostics})"
            )
        return text_output

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

        raise RuntimeError("Unable to parse Gemini JSON response")

    @classmethod
    def generate_text(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        enable_thinking: bool = False,
    ) -> str:
        payload = cls._generate_content(
            prompt=prompt,
            response_mime_type="text/plain",
            model=model,
            max_output_tokens=max_output_tokens,
            enable_thinking=enable_thinking,
        )
        return cls._extract_text(payload)

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
        raise RuntimeError("Gemini JSON response must be a JSON object")

    @classmethod
    def generate_json_value(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
        enable_thinking: bool = False,
    ) -> Any:
        has_schema = response_json_schema is not None
        used_schema = response_json_schema

        try:
            payload = cls._generate_content(
                prompt=prompt,
                response_mime_type="application/json",
                model=model,
                response_json_schema=used_schema,
                enable_thinking=enable_thinking,
            )
        except RuntimeError as exc:
            if not cls._is_schema_retryable_error(str(exc), has_schema=has_schema):
                raise

            payload = cls._generate_content(
                prompt=prompt,
                response_mime_type="application/json",
                model=model,
                response_json_schema=None,
                enable_thinking=enable_thinking,
            )
            used_schema = None

        try:
            raw = cls._extract_text(payload)
        except RuntimeError as exc:
            if not cls._is_schema_retryable_error(str(exc), has_schema=used_schema is not None):
                raise
            payload = cls._generate_content(
                prompt=prompt,
                response_mime_type="application/json",
                model=model,
                response_json_schema=None,
                enable_thinking=enable_thinking,
            )
            raw = cls._extract_text(payload)

        return cls._parse_json_value(raw)

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        if not cls.is_configured():
            return {"status": "skipped", "detail": "Gemini API key not configured"}
        try:
            # Avoid overly strict output caps that can yield non-text candidates
            # on some Gemini model revisions and create false negatives.
            reply = cls.generate_text("Reply with exactly: pong")
            return {
                "status": "ok",
                "detail": f"Gemini API reachable (model={settings.gemini_model})",
                "model": settings.gemini_model,
                "reply": reply[:60],
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc), "model": settings.gemini_model}
