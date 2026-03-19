from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Final

from ..config import BACKEND_ROOT
from ..library_types import LibraryType

logger = logging.getLogger(__name__)

PROMPTS_DIR: Final[Path] = BACKEND_ROOT / "prompts"

# Prompt groups
SCRIPT: Final[str] = "script"
METADATA: Final[str] = "metadata"
OVERLAY: Final[str] = "overlay"

# Language variants
FR: Final[str] = "fr"
MULTI: Final[str] = "multi"
SAME_LANG: Final[str] = "same_lang"


class PromptResolver:
    """Resolves prompt templates from the file system with type-based fallback.

    Resolution chain for ``resolve(group, variant, library_type)``:

    1. ``{type}/{group}_{variant}.md``  – type-specific, variant-specific
    2. ``{type}/{group}.md``            – type-specific, base (catch-all)
    3. ``default/{group}_{variant}.md`` – default, variant-specific
    4. ``default/{group}.md``           – default, base (ultimate fallback)

    First **non-empty** file found wins.  Empty files (0 bytes) are skipped.
    """

    @classmethod
    def resolve(
        cls,
        *,
        prompt_group: str,
        language_variant: str,
        library_type: LibraryType = LibraryType.ANIME,
    ) -> str:
        """Return the prompt template content after fallback resolution."""
        return cls._resolve_cached(
            prompt_group, language_variant, library_type.value
        )

    @classmethod
    @lru_cache(maxsize=64)
    def _resolve_cached(
        cls,
        prompt_group: str,
        language_variant: str,
        library_type_value: str,
    ) -> str:
        candidates = cls._build_candidate_paths(
            prompt_group, language_variant, library_type_value
        )
        for path in candidates:
            if path.is_file():
                content = path.read_text(encoding="utf-8")
                if content.strip():
                    logger.debug("Prompt resolved: %s", path)
                    return content
                logger.debug("Prompt file empty, skipping: %s", path)

        tried = [str(p.relative_to(PROMPTS_DIR)) for p in candidates]
        raise FileNotFoundError(
            f"No prompt template found for group={prompt_group!r}, "
            f"variant={language_variant!r}, type={library_type_value!r}. "
            f"Tried: {tried}"
        )

    @staticmethod
    def _build_candidate_paths(
        prompt_group: str,
        language_variant: str,
        library_type_value: str,
    ) -> list[Path]:
        specific_name = f"{prompt_group}_{language_variant}.md"
        base_name = f"{prompt_group}.md"
        return [
            PROMPTS_DIR / library_type_value / specific_name,
            PROMPTS_DIR / library_type_value / base_name,
            PROMPTS_DIR / "default" / specific_name,
            PROMPTS_DIR / "default" / base_name,
        ]

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the resolution cache (for tests and hot-reload)."""
        cls._resolve_cached.cache_clear()
