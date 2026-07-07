"""Translates raw-scene subtitle cues to the project language.

Per-project JSON cache keyed by content hash: unchanged cues always hit,
changed cues miss — no scene-change detection needed. LLM calls go through
OpenRouter using the dedicated translation model entry.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from .llm_config_service import LLMConfigService
from .openrouter_service import OpenRouterService

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
MAX_CUES_PER_CALL = 100


class SubtitleTranslationService:
    """Texts in, translated texts out; ``None`` on failure so callers keep originals."""

    @classmethod
    def _cache_path(cls, project_id: str) -> Path:
        return settings.projects_dir / project_id / "subtitle_translations.json"

    @classmethod
    def _cache_key(
        cls,
        source_language: str | None,
        target_language: str,
        text: str,
    ) -> str:
        source = source_language or "und"
        return hashlib.sha256(
            f"{source}|{target_language}|{text}".encode("utf-8")
        ).hexdigest()

    @classmethod
    def _load_cache(cls, project_id: str) -> dict[str, dict[str, Any]]:
        path = cls._cache_path(project_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Subtitle translation cache unreadable (%s): %s", path, exc)
            return {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            logger.warning("Subtitle translation cache malformed (%s); ignoring", path)
            return {}
        return entries

    @classmethod
    def _save_cache(cls, project_id: str, entries: dict[str, dict[str, Any]]) -> None:
        path = cls._cache_path(project_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"version": CACHE_VERSION, "entries": entries},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to write subtitle translation cache (%s): %s", path, exc)

    @staticmethod
    def _validate_chunk(parsed: Any, *, expected_count: int) -> list[str]:
        """Enforce the [{"i": idx, "t": text}] contract; raise ValueError otherwise."""
        if not isinstance(parsed, list) or len(parsed) != expected_count:
            raise ValueError(
                f"expected a JSON array of {expected_count} items, got {type(parsed).__name__}"
            )
        out: list[str | None] = [None] * expected_count
        for item in parsed:
            if not isinstance(item, dict):
                raise ValueError("translation item is not an object")
            index = item.get("i")
            text = item.get("t")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected_count:
                raise ValueError(f"translation index out of range: {index!r}")
            if out[index] is not None:
                raise ValueError(f"duplicate translation index: {index}")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"empty translation for index {index}")
            out[index] = text.strip()
        return [text for text in out if text is not None]

    @staticmethod
    def _system_prompt(source_language: str | None, target_language: str) -> str:
        source_clause = (
            f"from '{source_language}' "
            if source_language
            else "(detect the source language) "
        )
        return (
            "You are a professional subtitle translator for anime dialogue. "
            f"Translate each cue {source_clause}to '{target_language}'. "
            "Preserve tone, character names, and honorifics. "
            "Keep each translation concise enough to read as an on-screen subtitle. "
            'Respond with a JSON array of objects {"i": <same index as input>, '
            '"t": <translated text>} covering every input cue, in any order. '
            "No markdown fences, no explanations."
        )

    @classmethod
    def _translate_chunk(
        cls,
        chunk: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str]:
        entry = LLMConfigService.translation_entry()
        system = cls._system_prompt(source_language, target_language)
        prompt = json.dumps(
            [{"i": index, "t": text} for index, text in enumerate(chunk)],
            ensure_ascii=False,
        )
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                parsed = OpenRouterService.generate_json_value_with_entry(
                    prompt,
                    entry=entry,
                    system=system,
                )
                return cls._validate_chunk(parsed, expected_count=len(chunk))
            except (RuntimeError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "Subtitle translation chunk failed (attempt %d, %d cues): %s",
                    _attempt + 1,
                    len(chunk),
                    exc,
                )
        raise RuntimeError(f"subtitle translation failed after retry: {last_error}")

    @classmethod
    def _translate_texts_sync(
        cls,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str]:
        cache = cls._load_cache(project_id)
        keys = [
            cls._cache_key(source_language, target_language, text) for text in texts
        ]

        missing_keys: list[str] = []
        missing_texts: list[str] = []
        seen: set[str] = set()
        for key, text in zip(keys, texts):
            if key in cache or key in seen:
                continue
            seen.add(key)
            missing_keys.append(key)
            missing_texts.append(text)

        if missing_texts:
            translated: list[str] = []
            for start in range(0, len(missing_texts), MAX_CUES_PER_CALL):
                chunk = missing_texts[start : start + MAX_CUES_PER_CALL]
                translated.extend(
                    cls._translate_chunk(chunk, source_language, target_language)
                )
            model_id = LLMConfigService.translation_entry().openrouter_id
            translated_at = datetime.now(timezone.utc).isoformat()
            for key, source_text, translated_text in zip(
                missing_keys, missing_texts, translated
            ):
                cache[key] = {
                    "source_text": source_text,
                    "translated_text": translated_text,
                    "source_language": source_language or "und",
                    "target_language": target_language,
                    "model": model_id,
                    "translated_at": translated_at,
                }
            cls._save_cache(project_id, cache)
            logger.info(
                "Translated %d raw-scene subtitle cues (%s -> %s) with %s",
                len(missing_texts),
                source_language or "und",
                target_language,
                model_id,
            )

        return [cache[key]["translated_text"] for key in keys]

    @classmethod
    async def translate_texts(
        cls,
        *,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> list[str] | None:
        """Translate cue texts, aligned 1:1 with the input; ``None`` on failure."""
        if not texts:
            return []
        try:
            return await asyncio.to_thread(
                cls._translate_texts_sync,
                project_id,
                list(texts),
                source_language,
                target_language,
            )
        except Exception as exc:
            logger.warning(
                "Raw-scene subtitle translation unavailable (%s -> %s), keeping originals: %s",
                source_language or "und",
                target_language,
                exc,
            )
            return None
