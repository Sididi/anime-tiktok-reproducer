"""Tests for the raw-scene subtitle translation hook in ProcessingService."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.processing import ProcessingService, SrtEntry


def _install_fake_translate(monkeypatch, results):
    """results: maps source_language -> list[str] | None."""
    calls: list[dict] = []

    async def fake_translate(**kwargs):
        calls.append(kwargs)
        return results[kwargs["source_language"]]

    monkeypatch.setattr(
        "app.services.processing.SubtitleTranslationService.translate_texts",
        fake_translate,
    )
    return calls


@pytest.mark.asyncio
async def test_translates_only_non_target_entries(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {"en": ["FR-Hello", "FR-World"]})
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="Hello"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="Déjà bon"), "fr"),
        (SrtEntry(start=2.0, end=3.0, text="World"), "en"),
    ]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["FR-Hello", "Déjà bon", "FR-World"]
    # timing preserved
    assert [(entry.start, entry.end) for entry in out] == [
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
        monkeypatch, {"en": ["FR-en"], None: ["FR-unknown"]}
    )
    pending = [
        (SrtEntry(start=0.0, end=1.0, text="english"), "en"),
        (SrtEntry(start=1.0, end=2.0, text="mystery"), None),
    ]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["FR-en", "FR-unknown"]
    assert {call["source_language"] for call in calls} == {"en", None}


@pytest.mark.asyncio
async def test_failure_keeps_originals(monkeypatch):
    _install_fake_translate(monkeypatch, {"en": None})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Hello"), "en")]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["Hello"]


@pytest.mark.asyncio
async def test_no_target_language_skips_translation(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Hello"), "en")]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language=None, pending_entries=pending
    )
    assert [entry.text for entry in out] == ["Hello"]
    assert calls == []


@pytest.mark.asyncio
async def test_all_entries_already_target_language(monkeypatch):
    calls = _install_fake_translate(monkeypatch, {})
    pending = [(SrtEntry(start=0.0, end=1.0, text="Bonjour"), "fr")]
    out = await ProcessingService._translate_raw_scene_text_entries(
        project_id="proj1", target_language="fr", pending_entries=pending
    )
    assert [entry.text for entry in out] == ["Bonjour"]
    assert calls == []
