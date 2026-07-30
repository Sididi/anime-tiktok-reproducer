"""_get_spacy_model caches by caller-supplied language string; any unknown
code (client-controlled target_language) must share one fallback pipeline, not
load a brand-new multi-MB spaCy model per distinct key."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.processing as processing


@pytest.fixture(autouse=True)
def _isolate_spacy_cache():
    saved = dict(processing._SPACY_MODELS)
    processing._SPACY_MODELS.clear()
    yield
    processing._SPACY_MODELS.clear()
    processing._SPACY_MODELS.update(saved)


def test_unknown_languages_share_one_model(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    def fake_load(name: str, **kwargs) -> object:
        loads.append(name)
        return object()

    monkeypatch.setattr(processing.spacy, "load", fake_load)

    first = processing._get_spacy_model("de")
    second = processing._get_spacy_model("it")
    third = processing._get_spacy_model("pt-BR")

    assert first is second is third
    assert loads == ["en_core_web_sm"]


def test_known_languages_get_their_own_cached_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[str] = []

    def fake_load(name: str, **kwargs) -> object:
        loads.append(name)
        return object()

    monkeypatch.setattr(processing.spacy, "load", fake_load)

    fr = processing._get_spacy_model("fr")
    fr_again = processing._get_spacy_model("fr")
    en = processing._get_spacy_model("en")

    assert fr is fr_again
    assert fr is not en
    assert loads == ["fr_core_news_sm", "en_core_web_sm"]


def test_missing_model_falls_back_to_english_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[str] = []

    def fake_load(name: str, **kwargs) -> object:
        loads.append(name)
        if name != "en_core_web_sm":
            raise OSError("model not installed")
        return object()

    monkeypatch.setattr(processing.spacy, "load", fake_load)

    fr = processing._get_spacy_model("fr")
    es = processing._get_spacy_model("es")

    assert fr is es
    assert loads.count("en_core_web_sm") == 1
