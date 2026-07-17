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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from .llm_config_service import LLMConfigService
from .openrouter_service import OpenRouterService

logger = logging.getLogger(__name__)

CACHE_VERSION = 1
MAX_CUES_PER_CALL = 100
# Whole-batch retry rounds (same model each round); a round only re-sends cues that
# are still missing, so a flaky response costs those cues one extra attempt, not all.
MAX_ATTEMPTS = 4
RETRY_BACKOFF_BASE_SEC = 1.0


@dataclass(frozen=True)
class SubtitleTranslationOutcome:
    """Result of a translation request.

    ``texts`` is always aligned 1:1 with the input; cues that could not be translated
    keep their original text. ``failed_count`` is how many input positions stayed
    untranslated — non-zero means the video ships with some source-language cues.
    """

    texts: list[str]
    failed_count: int


class SubtitleTranslationService:
    """Texts in, translated texts out; failures degrade to the original text and are
    recorded in a per-project warning marker so nothing ships silently untranslated."""

    WARNING_FILENAME = "subtitle_translation_warning.json"

    @classmethod
    def _cache_path(cls, project_id: str) -> Path:
        return settings.projects_dir / project_id / "subtitle_translations.json"

    @classmethod
    def _warning_path(cls, project_id: str) -> Path:
        return settings.projects_dir / project_id / cls.WARNING_FILENAME

    @classmethod
    def record_untranslated_warning(
        cls,
        project_id: str,
        *,
        failed_count: int,
        target_language: str | None,
    ) -> None:
        """Record (or clear) a persistent marker that some cues shipped untranslated.

        ``failed_count == 0`` removes any stale marker from a previous run so the marker
        always reflects the latest processing outcome.
        """
        path = cls._warning_path(project_id)
        if failed_count <= 0:
            path.unlink(missing_ok=True)
            return
        logger.warning(
            "Project %s: %d raw-scene subtitle cue(s) shipped untranslated (target %s)",
            project_id,
            failed_count,
            target_language or "und",
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "failed_count": failed_count,
                        "target_language": target_language,
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Failed to write subtitle translation warning (%s): %s", path, exc
            )

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
    def _parse_partial_chunk(parsed: Any, *, expected_count: int) -> dict[int, str]:
        """Salvage every valid ``{"i": idx, "t": text}`` item; silently drop the rest.

        Returns ``{index: translated_text}`` for the items that parsed cleanly. Unlike a
        strict all-or-nothing validator, a malformed or missing entry only costs that one
        cue — the caller retries whichever indices are still missing.
        """
        if not isinstance(parsed, list):
            return {}
        out: dict[int, str] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            index = item.get("i")
            text = item.get("t")
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            if not 0 <= index < expected_count or index in out:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            out[index] = text.strip()
        return out

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
    def _translate_missing(
        cls,
        missing: dict[str, str],
        source_language: str | None,
        target_language: str,
    ) -> dict[str, str]:
        """Translate ``{cache_key: text}``, retrying only still-missing cues.

        Uses the same model on every round (never escalates). Each round re-sends
        whatever cues remain untranslated, in chunks; a chunk error or a dropped index
        only costs those cues another round. Returns ``{cache_key: translated_text}``
        for whatever succeeded — possibly a subset if every round left some behind.
        """
        entry = LLMConfigService.translation_entry()
        system = cls._system_prompt(source_language, target_language)
        translated: dict[str, str] = {}
        pending = dict(missing)  # cache_key -> source text, still needing translation

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not pending:
                break
            if attempt > 1:
                time.sleep(RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 2)))

            pending_keys = list(pending)
            for start in range(0, len(pending_keys), MAX_CUES_PER_CALL):
                chunk_keys = pending_keys[start : start + MAX_CUES_PER_CALL]
                chunk_texts = [pending[key] for key in chunk_keys]
                prompt = json.dumps(
                    [{"i": i, "t": text} for i, text in enumerate(chunk_texts)],
                    ensure_ascii=False,
                )
                try:
                    parsed = OpenRouterService.generate_json_value_with_entry(
                        prompt, entry=entry, system=system
                    )
                except (RuntimeError, ValueError) as exc:
                    logger.warning(
                        "Subtitle translation chunk failed (attempt %d/%d, %d cues): %s",
                        attempt,
                        MAX_ATTEMPTS,
                        len(chunk_texts),
                        exc,
                    )
                    continue
                for local_index, text in cls._parse_partial_chunk(
                    parsed, expected_count=len(chunk_texts)
                ).items():
                    key = chunk_keys[local_index]
                    translated[key] = text
                    pending.pop(key, None)

        return translated

    @classmethod
    def _translate_texts_sync(
        cls,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> SubtitleTranslationOutcome:
        cache = cls._load_cache(project_id)
        keys = [
            cls._cache_key(source_language, target_language, text) for text in texts
        ]

        missing: dict[str, str] = {}  # cache_key -> text (deduped)
        for key, text in zip(keys, texts):
            if key not in cache and key not in missing:
                missing[key] = text

        if missing:
            translated = cls._translate_missing(missing, source_language, target_language)
            if translated:
                model_id = LLMConfigService.translation_entry().openrouter_id
                translated_at = datetime.now(timezone.utc).isoformat()
                for key, translated_text in translated.items():
                    cache[key] = {
                        "source_text": missing[key],
                        "translated_text": translated_text,
                        "source_language": source_language or "und",
                        "target_language": target_language,
                        "model": model_id,
                        "translated_at": translated_at,
                    }
                cls._save_cache(project_id, cache)
            failed = len(missing) - len(translated)
            log = logger.info if failed == 0 else logger.warning
            log(
                "Translated %d/%d raw-scene subtitle cues (%s -> %s)%s",
                len(translated),
                len(missing),
                source_language or "und",
                target_language,
                "" if failed == 0 else f"; {failed} kept untranslated",
            )

        result_texts = [
            cache[key]["translated_text"] if key in cache else original
            for key, original in zip(keys, texts)
        ]
        failed_count = sum(1 for key in keys if key not in cache)
        return SubtitleTranslationOutcome(texts=result_texts, failed_count=failed_count)

    @classmethod
    async def translate_texts(
        cls,
        *,
        project_id: str,
        texts: list[str],
        source_language: str | None,
        target_language: str,
    ) -> SubtitleTranslationOutcome:
        """Translate cue texts. ``texts`` is aligned 1:1 with the input, keeping the
        original where a cue could not be translated; ``failed_count`` reports how many
        stayed untranslated (never raises — failures degrade to originals)."""
        if not texts:
            return SubtitleTranslationOutcome(texts=[], failed_count=0)
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
            return SubtitleTranslationOutcome(
                texts=list(texts), failed_count=len(texts)
            )
