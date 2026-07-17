"""Tests for the raw-scene subtitle translation hook in ProcessingService."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.processing import ProcessingService, SrtEntry
from app.services.subtitle_translation_service import SubtitleTranslationOutcome


def _install_fake_translate(monkeypatch, results):
    """results: maps source_language -> SubtitleTranslationOutcome."""
    calls: list[dict] = []

    async def fake_translate(**kwargs):
        calls.append(kwargs)
        return results[kwargs["source_language"]]

    monkeypatch.setattr(
        "app.services.processing.SubtitleTranslationService.translate_texts",
        fake_translate,
    )
    return calls


def _ok(texts):
    return SubtitleTranslationOutcome(texts=texts, failed_count=0)


@pytest.mark.asyncio
async def test_translates_only_non_target_entries(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {"en": _ok(["FR-Hello", "FR-World"])})
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="Hello"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="Déjà bon"), "fr"),
        (SrtEntry(start=2.0, end=3.0, text="World"), "en"),
    ]
    entries, failed = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in entries] == ["FR-Hello", "Déjà bon", "FR-World"]
    assert failed == 0
    # timing preserved
    assert [(entry.start, entry.end) for entry in entries] == [
        (0.0, 1.0),
        (1.0, 2.0),
        (2.0, 3.0),
    ]
    assert calls == [
        {
            "project_id": "proj1",
            "texts": ["Hello", "World"],
            "source_language": "en",
            "target_language": "fr",
        }
    ]


@pytest.mark.asyncio
async def test_groups_by_source_language(monkeypatch):
    calls = _install_fake_translate(
        monkeypatch, {"en": _ok(["FR-en"]), None: _ok(["FR-unknown"])}
    )
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="english"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="mystery"), None),
    ]
    entries, failed = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in entries] == ["FR-en", "FR-unknown"]
    assert failed == 0
    assert {call["source_language"] for call in calls} == {"en", None}


@pytest.mark.asyncio
async def test_failure_keeps_originals_and_reports_count(monkeypatch):
    # partial failure: one cue translated, one kept as original
    _install_fake_translate(
        monkeypatch,
        {"en": SubtitleTranslationOutcome(texts=["FR-Hi", "World"], failed_count=1)},
    )
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="Hi"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="World"), "en"),
    ]
    entries, failed = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in entries] == ["FR-Hi", "World"]
    assert failed == 1


@pytest.mark.asyncio
async def test_failed_counts_sum_across_groups(monkeypatch):
    _install_fake_translate(
        monkeypatch,
        {
            "en": SubtitleTranslationOutcome(texts=["English"], failed_count=1),
            "ja": SubtitleTranslationOutcome(texts=["Japanese"], failed_count=1),
        },
    )
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="English"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="Japanese"), "ja"),
    ]
    _entries, failed = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert failed == 2


@pytest.mark.asyncio
async def test_no_target_language_skips_translation(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Hello"), "en")]
    entries, failed = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language=None, pending_entries=pending
    )
    assert [entry.text for entry in entries] == ["Hello"]
    assert failed == 0
    assert calls == []


@pytest.mark.asyncio
async def test_all_entries_already_target_language(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Bonjour"), "fr")]
    entries, failed = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in entries] == ["Bonjour"]
    assert failed == 0
    assert calls == []
