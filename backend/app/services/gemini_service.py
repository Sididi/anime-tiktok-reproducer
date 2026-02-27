from __future__ import annotations

import json
from typing import Any

import requests

from ..config import settings


class GeminiService:
    """Wrapper around Google Gemini API (AI Studio key auth)."""

    _BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    @classmethod
    def is_configured(cls) -> bool:
        return bool((settings.gemini_api_key or "").strip())

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
    def _extract_text(response_payload: dict[str, Any]) -> str:
        candidates = response_payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("Gemini response did not contain candidates")

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
            raise RuntimeError("Gemini response did not contain textual output")
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
    def generate_text(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        payload = cls._generate_content(
            prompt=prompt,
            response_mime_type="text/plain",
            model=model,
            max_output_tokens=max_output_tokens,
        )
        return cls._extract_text(payload)

    @classmethod
    def generate_json(
        cls,
        prompt: str,
        *,
        model: str | None = None,
        response_json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            payload = cls._generate_content(
                prompt=prompt,
                response_mime_type="application/json",
                model=model,
                response_json_schema=response_json_schema,
            )
        except RuntimeError as exc:
            # Some model revisions or API surfaces may reject schema fields.
            lower = str(exc).lower()
            schema_not_supported = (
                response_json_schema is not None
                and (
                    "responsejsonschema" in lower
                    or "responseschema" in lower
                    or "unknown name" in lower
                )
            )
            if not schema_not_supported:
                raise
            payload = cls._generate_content(
                prompt=prompt,
                response_mime_type="application/json",
                model=model,
                response_json_schema=None,
            )
        raw = cls._extract_text(payload)
        stripped = cls._strip_json_fence(raw)

        # First attempt: parse full payload as-is.
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
            raise RuntimeError("Gemini JSON response must be a JSON object")
        except json.JSONDecodeError:
            pass

        # Fallback: extract outer-most JSON object from noisy output.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            candidate = stripped[start : end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        raise RuntimeError("Unable to parse Gemini JSON response")

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
