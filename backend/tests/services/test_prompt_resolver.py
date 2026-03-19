"""Tests for PromptResolver fallback chain, caching, and edge cases."""

from __future__ import annotations

import pytest

from app.library_types import LibraryType
from app.services.prompt_resolver import PROMPTS_DIR, PromptResolver


@pytest.fixture(autouse=True)
def _clean_cache():
    """Clear PromptResolver cache before and after each test."""
    PromptResolver.clear_cache()
    yield
    PromptResolver.clear_cache()


@pytest.fixture()
def prompts_dir(tmp_path, monkeypatch):
    """Point PROMPTS_DIR to a temp directory for isolation."""
    import app.services.prompt_resolver as mod

    monkeypatch.setattr(mod, "PROMPTS_DIR", tmp_path)
    return tmp_path


# ---------- Fallback chain ----------


def test_resolves_type_specific_variant(prompts_dir):
    """Level 1: {type}/script_fr.md is preferred over default."""
    (prompts_dir / "simpsons").mkdir()
    (prompts_dir / "simpsons" / "script_fr.md").write_text("simpsons script fr")
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("default script fr")

    result = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.SIMPSONS,
    )
    assert result == "simpsons script fr"


def test_falls_back_to_type_specific_base(prompts_dir):
    """Level 2: {type}/script.md when {type}/script_fr.md is absent."""
    (prompts_dir / "simpsons").mkdir()
    (prompts_dir / "simpsons" / "script.md").write_text("simpsons script base")
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("default script fr")

    result = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.SIMPSONS,
    )
    assert result == "simpsons script base"


def test_falls_back_to_default_variant(prompts_dir):
    """Level 3: default/script_fr.md when no type-specific files exist."""
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("default script fr")

    result = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.SIMPSONS,
    )
    assert result == "default script fr"


def test_falls_back_to_default_base(prompts_dir):
    """Level 4: default/script.md as ultimate fallback."""
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script.md").write_text("default script base")

    result = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.SIMPSONS,
    )
    assert result == "default script base"


def test_file_not_found_when_nothing_exists(prompts_dir):
    """FileNotFoundError with descriptive message when no file matches."""
    (prompts_dir / "default").mkdir()

    with pytest.raises(FileNotFoundError, match="No prompt template found"):
        PromptResolver.resolve(
            prompt_group="script",
            language_variant="fr",
            library_type=LibraryType.ANIME,
        )


# ---------- Empty file handling ----------


def test_skips_empty_override_file(prompts_dir):
    """An empty type-specific file is skipped; falls through to default."""
    (prompts_dir / "anime").mkdir()
    (prompts_dir / "anime" / "script_fr.md").write_text("")
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("default content")

    result = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.ANIME,
    )
    assert result == "default content"


def test_skips_whitespace_only_file(prompts_dir):
    """A file with only whitespace is treated as empty."""
    (prompts_dir / "anime").mkdir()
    (prompts_dir / "anime" / "script_fr.md").write_text("   \n  \n  ")
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("default content")

    result = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.ANIME,
    )
    assert result == "default content"


# ---------- Caching ----------


def test_cache_returns_same_content(prompts_dir):
    """Repeated calls return cached content without re-reading."""
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("cached content")

    result1 = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.ANIME,
    )
    # Modify file on disk — should NOT affect cached result
    (prompts_dir / "default" / "script_fr.md").write_text("modified content")

    result2 = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.ANIME,
    )
    assert result1 == result2 == "cached content"


def test_clear_cache_forces_reread(prompts_dir):
    """After clear_cache, the resolver re-reads from disk."""
    (prompts_dir / "default").mkdir()
    (prompts_dir / "default" / "script_fr.md").write_text("original")

    result1 = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.ANIME,
    )
    (prompts_dir / "default" / "script_fr.md").write_text("updated")
    PromptResolver.clear_cache()

    result2 = PromptResolver.resolve(
        prompt_group="script",
        language_variant="fr",
        library_type=LibraryType.ANIME,
    )
    assert result1 == "original"
    assert result2 == "updated"


# ---------- All library types resolve through default ----------


@pytest.mark.parametrize("library_type", list(LibraryType))
def test_all_library_types_resolve_via_default(prompts_dir, library_type):
    """Every LibraryType resolves when only default/ exists."""
    (prompts_dir / "default").mkdir(exist_ok=True)
    (prompts_dir / "default" / "metadata_multi.md").write_text("metadata multi")

    result = PromptResolver.resolve(
        prompt_group="metadata",
        language_variant="multi",
        library_type=library_type,
    )
    assert result == "metadata multi"
