from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.facecam_cover_service import FacecamCoverService as Covers
from app.services.facecam_service import FacecamService
from app.services.project_service import ProjectService
from test_facecam import draft, source


TITLE = ("Tensei shita Daiseijo wa, Seijo de Aru Koto wo Hitakakusu "
         "(A Tale of the Great Saint, The Reincarnated Great Saint Hides That She's a Saint)")
MEDIA = {
    "id": 187402,
    "title": {"romaji": "Tensei shita Dai Seijo wa, Seijo de Aru Koto wo Hita Kakusu",
              "english": "A Tale of the Secret Saint", "native": None},
    "synonyms": ["Daiseijo"],
}


def mock_catalog(monkeypatch, responses):
    import app.services.facecam_cover_service as module
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json):
            search = json["variables"]["search"]
            calls.append(search)
            media = responses.get(search, [])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"data": {"Page": {"media": media}}})
    monkeypatch.setattr(module.httpx, "Client", Client)
    return calls


@pytest.mark.parametrize("primary_candidates", [[], [{"id": 99, "title": {"romaji": "Unrelated anime"}}]])
def test_spacing_variants_are_found_and_auto_selected_by_full_identity(source, monkeypatch, primary_candidates):
    project, _ = draft(source)
    project.anime_name = TITLE
    ProjectService.save(project)
    calls = mock_catalog(monkeypatch, {TITLE: primary_candidates, "Daiseijo": [MEDIA]})
    selected = []
    monkeypatch.setattr(Covers, "select", lambda pid, mid: selected.append(mid))
    monkeypatch.setattr(Covers, "render", lambda pid: "cover.png")

    assert Covers.ensure(project.id) == "cover.png"
    assert selected == [MEDIA["id"]]
    assert "Daiseijo" in calls
    assert len(calls) <= len(Covers.title_queries(TITLE)) + Covers.MAX_DISCOVERY_QUERIES
    assert len(set(calls)) == len(calls)


def test_discovery_terms_cannot_match_a_different_anime_or_season(source, monkeypatch):
    project, _ = draft(source)
    project.anime_name = TITLE
    ProjectService.save(project)
    wrong = {"id": 2, "title": {"romaji": MEDIA["title"]["romaji"] + " 2nd Season"},
             "synonyms": ["Daiseijo", "Hitakakusu"]}
    calls = mock_catalog(monkeypatch, {"Daiseijo": [wrong]})
    monkeypatch.setattr(Covers, "select", lambda *args: pytest.fail("Do not select from a partial title"))
    with pytest.raises(ValueError, match="Aucune correspondance certaine"):
        Covers.ensure(project.id)
    assert "Daiseijo" in calls
    assert FacecamService.load(project.id).cover is None


def test_discovery_keeps_all_candidates_and_refuses_ambiguous_identity(source, monkeypatch):
    project, _ = draft(source)
    project.anime_name = TITLE
    ProjectService.save(project)
    calls = mock_catalog(monkeypatch, {"Daiseijo": [MEDIA], "Tensei": [{**MEDIA, "id": 2}]})
    monkeypatch.setattr(Covers, "select", lambda *args: pytest.fail("Do not select the first search result"))
    with pytest.raises(ValueError, match="Plusieurs animes"):
        Covers.ensure(project.id)
    assert "Daiseijo" in calls and "Tensei" in calls


def test_exact_primary_match_avoids_extra_queries_and_deduplicates(source, monkeypatch):
    project, _ = draft(source)
    project.anime_name = TITLE
    ProjectService.save(project)
    calls = mock_catalog(monkeypatch, {query: [MEDIA] for query in Covers.title_queries(TITLE)})
    assert Covers.search(project.id) == [MEDIA]
    assert calls == Covers.title_queries(TITLE)


@pytest.mark.parametrize("payload", [
    {"errors": [{"message": "Rate limited"}], "data": None},
    {"errors": [{"message": "Partial failure"}], "data": {"Page": {"media": [MEDIA]}}},
    {"data": {}}, {"data": {"Page": {"media": None}}},
    {"data": {"Page": {"media": [None]}}},
])
def test_invalid_or_partial_catalog_response_is_not_treated_as_no_match(source, monkeypatch, payload):
    import app.services.facecam_cover_service as module
    project, _ = draft(source)
    calls = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(Covers, "select", lambda *args: pytest.fail("Incomplete catalog result"))
    with pytest.raises(ValueError, match="AniList"):
        Covers.ensure(project.id)
    assert len(calls) == 1
    assert FacecamService.load(project.id).cover is None
