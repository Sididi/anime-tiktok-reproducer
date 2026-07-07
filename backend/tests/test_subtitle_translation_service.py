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


def test_load_cache_corrupt_file_returns_empty(project_dir):
    (project_dir / "subtitle_translations.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert SubtitleTranslationService._load_cache("proj1") == {}


# --- chunk validation ---

def test_validate_chunk_happy_path():
    parsed = [{"i": 1, "t": "deux"}, {"i": 0, "t": "un"}]
    assert SubtitleTranslationService._validate_chunk(parsed, expected_count=2) == [
        "un",
        "deux",
    ]


@pytest.mark.parametrize(
    "parsed",
    [
        {"i": 0, "t": "x"},  # not a list
        [{"i": 0, "t": "un"}],  # wrong count (expected 2)
        [{"i": 0, "t": "un"}, {"i": 0, "t": "deux"}],  # duplicate index
        [{"i": 0, "t": "un"}, {"i": 5, "t": "deux"}],  # out-of-range index
        [{"i": 0, "t": "un"}, {"i": 1, "t": "   "}],  # blank translation
        [{"i": 0, "t": "un"}, {"i": 1}],  # missing text
        [{"i": 0, "t": "un"}, "deux"],  # non-dict item
    ],
)
def test_validate_chunk_rejects_bad_shapes(parsed):
    with pytest.raises(ValueError):
        SubtitleTranslationService._validate_chunk(parsed, expected_count=2)


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
    assert out == ["Bonjour", "Monde"]
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
    assert out2 == ["Bonjour", "Monde"]
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
    assert out == ["Bonjour", "Bonjour", "Bonjour"]
    assert json.loads(calls[0]["prompt"]) == [{"i": 0, "t": "Hello"}]


@pytest.mark.asyncio
async def test_translate_texts_partial_cache_sends_only_misses(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
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
    assert out == ["Bonjour", "Monde"]
    assert json.loads(calls2[0]["prompt"]) == [{"i": 0, "t": "World"}]


@pytest.mark.asyncio
async def test_translate_texts_retries_once_then_succeeds(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(
        monkeypatch,
        [
            [{"i": 0, "t": "   "}],  # invalid → retry
            [{"i": 0, "t": "Bonjour"}],
        ],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    assert out == ["Bonjour"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_translate_texts_fails_after_retry_returns_none_and_no_cache(
    project_dir, translation_entry, monkeypatch
):
    _mock_llm(
        monkeypatch,
        [RuntimeError("boom"), RuntimeError("boom again")],
    )
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language="en", target_language="fr"
    )
    assert out is None
    assert not (project_dir / "subtitle_translations.json").exists()


@pytest.mark.asyncio
async def test_translate_texts_unknown_source_language(
    project_dir, translation_entry, monkeypatch
):
    calls = _mock_llm(monkeypatch, [[{"i": 0, "t": "Bonjour"}]])
    out = await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=["Hello"], source_language=None, target_language="fr"
    )
    assert out == ["Bonjour"]
    assert "detect the source language" in calls[0]["system"]


@pytest.mark.asyncio
async def test_translate_texts_empty_input(project_dir, translation_entry):
    assert await SubtitleTranslationService.translate_texts(
        project_id="proj1", texts=[], source_language="en", target_language="fr"
    ) == []


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
    assert out == ["T0", "T1", "T2"]
    assert len(calls) == 2
