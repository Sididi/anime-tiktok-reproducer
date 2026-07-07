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
