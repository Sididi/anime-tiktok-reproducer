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
from datetime import datetime
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
            if not isinstance(index, int) or not 0 <= index < expected_count:
                raise ValueError(f"translation index out of range: {index!r}")
            if out[index] is not None:
                raise ValueError(f"duplicate translation index: {index}")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"empty translation for index {index}")
            out[index] = text.strip()
        return [text for text in out if text is not None]
