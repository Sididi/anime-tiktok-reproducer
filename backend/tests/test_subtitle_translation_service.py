"""Unit tests for SubtitleTranslationService — OpenRouter is always mocked."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.subtitle_translation_service import SubtitleTranslationService


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.subtitle_translation_service.settings.projects_dir", tmp_path
    )
    (tmp_path / "proj1").mkdir()
    return tmp_path / "proj1"


# --- cache key ---

def test_cache_key_is_content_hash():
    k1 = SubtitleTranslationService._cache_key("en", "fr", "Hello")
    k2 = SubtitleTranslationService._cache_key("en", "fr", "Hello")
    k3 = SubtitleTranslationService._cache_key("en", "fr", "Hello!")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 64  # sha256 hex


def test_cache_key_none_source_normalized_to_und():
    k_none = SubtitleTranslationService._cache_key(None, "fr", "Hello")
    k_und = SubtitleTranslationService._cache_key("und", "fr", "Hello")
    assert k_none == k_und


def test_cache_key_varies_by_languages():
    base = SubtitleTranslationService._cache_key("en", "fr", "Hello")
    assert SubtitleTranslationService._cache_key("ja", "fr", "Hello") != base
    assert SubtitleTranslationService._cache_key("en", "de", "Hello") != base


# --- cache load/save ---

def test_cache_roundtrip(project_dir):
    entries = {
        "abc": {
            "source_text": "Hello",
            "translated_text": "Bonjour",
            "source_language": "en",
            "target_language": "fr",
            "model": "google/gemini-2.5-flash-lite",
            "translated_at": "2026-07-06T12:00:00",
        }
    }
    SubtitleTranslationService._save_cache("proj1", entries)
    cache_file = project_dir / "subtitle_translations.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert SubtitleTranslationService._load_cache("proj1") == entries


def test_load_cache_missing_file_returns_empty(project_dir):
    assert SubtitleTranslationService._load_cache("proj1") == {}


# --- untranslated warning marker ---

def test_record_untranslated_warning_writes_marker(project_dir):
    SubtitleTranslationService.record_untranslated_warning(
        "proj1", failed_count=3, target_language="fr"
    )
    marker = project_dir / "subtitle_translation_warning.json"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["failed_count"] == 3
    assert payload["target_language"] == "fr"
    assert "recorded_at" in payload


def test_record_untranslated_warning_zero_clears_stale_marker(project_dir):
    SubtitleTranslationService.record_untranslated_warning(
        "proj1", failed_count=3, target_language="fr"
    )
    # a later clean run reports zero failures → stale marker removed
    SubtitleTranslationService.record_untranslated_warning(
        "proj1", failed_count=0, target_language="fr"
    )
    assert not (project_dir / "subtitle_translation_warning.json").exists()


def test_record_untranslated_warning_zero_is_noop_without_marker(project_dir):
    SubtitleTranslationService.record_untranslated_warning(
        "proj1", failed_count=0, target_language="fr"
    )
    assert not (project_dir / "subtitle_translation_warning.json").exists()


def test_load_cache_corrupt_file_returns_empty(project_dir):
    (project_dir / "subtitle_translations.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert SubtitleTranslationService._load_cache("proj1") == {}


# --- partial chunk parsing (salvage whatever is valid, drop the rest) ---

def test_parse_partial_chunk_happy_path():
    parsed = [{"i": 1, "t": "deux"}, {"i": 0, "t": "un"}]
    assert SubtitleTranslationService._parse_partial_chunk(
        parsed, expected_count=2
    ) == {0: "un", 1: "deux"}


def test_parse_partial_chunk_salvages_valid_and_drops_invalid():
    parsed = [
        {"i": 0, "t": "un"},          # good
        {"i": 1, "t": "   "},         # blank -> dropped
        {"i": 2},                      # missing text -> dropped
        {"i": 3, "t": "quatre"},      # good
        "nope",                        # non-dict -> dropped
        {"i": 9, "t": "far"},         # out of range -> dropped
        {"i": True, "t": "bool"},     # bool index -> dropped
        {"i": 0, "t": "dup"},         # duplicate index -> first wins
    ]
    assert SubtitleTranslationService._parse_partial_chunk(
        parsed, expected_count=4
    ) == {0: "un", 3: "quatre"}


def test_parse_partial_chunk_non_list_returns_empty():
    assert SubtitleTranslationService._parse_partial_chunk(
        {"i": 0, "t": "x"}, expected_count=2
    ) == {}


def test_parse_partial_chunk_strips_whitespace():
    assert SubtitleTranslationService._parse_partial_chunk(
        [{"i": 0, "t": "  hello  "}], expected_count=1
    ) == {0: "hello"}


# --- translate_texts flow (OpenRouter mocked) ---


def _mock_llm(monkeypatch, responses):
    """Queue raw parsed-JSON responses for generate_json_value_with_entry; records prompts."""
    calls: list[dict] = []

    def fake_call(prompt, *, entry, system=None, max_output_tokens=None):
        calls.append({"prompt": prompt, "entry": entry, "system": system})
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "app.services.subtitle_translation_service."
        "OpenRouterService.generate_json_value_with_entry",
        fake_call,
    )
    return calls


@pytest.fixture
def translation_entry(monkeypatch):
    from app.models.llm_config import LLMPresetEntry

    entry = LLMPresetEntry(
        openrouter_id="google/gemini-2.5-flash-lite", thinking=None
    )
    monkeypatch.setattr(
        "app.services.subtitle_translation_service."
        "LLMConfigService.translation_entry",
        classmethod(lambda cls: entry),
    )
    return entry


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff sleeps without actually waiting."""
    slept: list[float] = []
    monkeypatch.setattr(
        "app.services.subtitle_translation_service.time.sleep", slept.append
    )
    return slept


@pytest.mark.asyncio
async def test_translate_texts_miss_then_hit(project_dir, translation_entry, monkeypatch):
    calls = _mock_llm(
        monkeypatch, [[{"i": 0, "t": "Bonjour"}, {"i": 1, "t": "Monde"}]]
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out.texts == ["Bonjour", "Monde"]
    assert out.failed_count == 0
    assert len(calls) == 1
    sent = json.loads(calls[0]["prompt"])
    assert sent == [{"i": 0, "t": "Hello"}, {"i": 1, "t": "World"}]
    assert "en" in calls[0]["system"] and "fr" in calls[0]["system"]

    # second run: pure cache hit, zero LLM calls
    out2 = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out2.texts == ["Bonjour", "Monde"]
    assert out2.failed_count == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_translate_texts_dedupes_identical_cues(project_dir, translation_entry, monkeypatch):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "Hello", "Hello"],
        source_language="en",
        target_language="fr",
    )
    assert out.texts == ["Bonjour", "Bonjour", "Bonjour"]
    assert json.loads(calls[0]["prompt"]) == [{"i": 0, "t": "Hello"}]


@pytest.mark.asyncio
async def test_translate_texts_partial_cache_sends_only_misses(
    project_dir, translation_entry, monkeypatch
):
    _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    calls2 = _mock_llm(monkeypatch, [[{"i": 0, "t": "Monde"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out.texts == ["Bonjour", "Monde"]
    assert json.loads(calls2[0]["prompt"]) == [{"i": 0, "t": "World"}]


@pytest.mark.asyncio
async def test_translate_texts_partial_response_retries_only_missing(
    project_dir, translation_entry, no_sleep, monkeypatch
):
    # First call drops index 1; only the still-missing cue is re-sent.
    calls = _mock_llm(
        monkeypatch,
        [
            [{"i": 0, "t": "Bonjour"}],  # index 1 missing
            [{"i": 0, "t": "Monde"}],  # retry of the one remaining cue
        ],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out.texts == ["Bonjour", "Monde"]
    assert out.failed_count == 0
    assert len(calls) == 2
    assert json.loads(calls[1]["prompt"]) == [{"i": 0, "t": "World"}]
    assert no_sleep  # a backoff sleep happened between rounds


@pytest.mark.asyncio
async def test_translate_texts_retries_with_backoff_after_chunk_error(
    project_dir, translation_entry, no_sleep, monkeypatch
):
    calls = _mock_llm(
        monkeypatch,
        [
            RuntimeError("boom"),  # whole chunk fails on attempt 1
            [{"i": 0, "t": "Bonjour"}],  # attempt 2 succeeds
        ],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    assert out.texts == ["Bonjour"]
    assert out.failed_count == 0
    assert len(calls) == 2
    assert len(no_sleep) == 1 and no_sleep[0] > 0


@pytest.mark.asyncio
async def test_translate_texts_gives_up_after_max_attempts_keeps_originals(
    project_dir, translation_entry, no_sleep, monkeypatch
):
    from app.services import subtitle_translation_service as mod

    _mock_llm(monkeypatch, [RuntimeError("boom")] * mod.MAX_ATTEMPTS)
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    assert out.texts == ["Hello"]  # original kept
    assert out.failed_count == 1
    # nothing translated → no cache written
    assert not (project_dir / "subtitle_translations.json").exists()
    # backoff between each retry round
    assert len(no_sleep) == mod.MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_translate_texts_permanent_partial_keeps_failed_originals(
    project_dir, translation_entry, no_sleep, monkeypatch
):
    from app.services import subtitle_translation_service as mod

    # First round translates only "Hello"; every retry of the leftover cue comes back
    # empty, so "World" is never translated.
    _mock_llm(
        monkeypatch,
        [[{"i": 0, "t": "Bonjour"}]] + [[]] * (mod.MAX_ATTEMPTS - 1),
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["Hello", "World"],
        source_language="en",
        target_language="fr",
    )
    assert out.texts == ["Bonjour", "World"]  # World kept untranslated
    assert out.failed_count == 1
    # the successful cue is cached; the failed one is not (so a later run retries it)
    cache = SubtitleTranslationService._load_cache("proj1")
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_translate_texts_unknown_source_language(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language=None, target_language="fr"
    )
    assert out.texts == ["Bonjour"]
    assert "detect the source language" in calls[0]["system"]


@pytest.mark.asyncio
async def test_translate_texts_empty_input(project_dir, translation_entry):
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=[], source_language="en", target_language="fr"
    )
    assert out.texts == [] and out.failed_count == 0


@pytest.mark.asyncio
async def test_translate_texts_chunks_large_batches(
    project_dir, translation_entry, monkeypatch
):
    monkeypatch.setattr(
        "app.services.subtitle_translation_service.MAX_CUES_PER_CALL", 2
    )
    calls = _mock_llm(
        monkeypatch,
        [
            [{"i": 0, "t": "T0"}, {"i": 1, "t": "T1"}],
            [{"i": 0, "t": "T2"}],
        ],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1",
        texts=["a", "b", "c"],
        source_language="en",
        target_language="fr",
    )
    assert out.texts == ["T0", "T1", "T2"]
    assert out.failed_count == 0
    assert len(calls) == 2
