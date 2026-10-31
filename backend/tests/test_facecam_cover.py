from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

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


def png(color=(200, 40, 40), size=(320, 180)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_discovery_terms_cannot_match_a_different_anime_or_season(source, monkeypatch):
    project, _ = draft(source)
    project.anime_name = TITLE
    ProjectService.save(project)
    wrong = {"id": 2, "title": {"romaji": MEDIA["title"]["romaji"] + " 2nd Season"},
             "synonyms": ["Daiseijo", "Hitakakusu"]}
    calls = mock_catalog(monkeypatch, {"Daiseijo": [wrong]})
    judged = []
    monkeypatch.setattr(Covers, "judge", lambda project, results: judged.append(results))
    monkeypatch.setattr(Covers, "select", lambda *args: pytest.fail("Do not select from a partial title"))
    monkeypatch.setattr(Covers, "frame_cover", lambda pid: png())
    Covers.ensure(project.id)
    assert "Daiseijo" in calls and judged == [[wrong]]
    assert FacecamService.load(project.id).cover["frame"] is True


def test_discovery_keeps_all_candidates_and_ranks_ambiguous_identity(source, monkeypatch):
    project, _ = draft(source)
    project.anime_name = TITLE
    ProjectService.save(project)
    calls = mock_catalog(monkeypatch, {"Daiseijo": [{**MEDIA, "episodes": 24, "popularity": 900}],
                                       "Tensei": [{**MEDIA, "id": 2, "episodes": 12, "popularity": 10}]})
    monkeypatch.setattr(Covers, "episode_count", lambda project: 12)
    selected = []
    monkeypatch.setattr(Covers, "select", lambda pid, mid: selected.append(mid))
    monkeypatch.setattr(Covers, "render", lambda pid: "cover.png")
    Covers.ensure(project.id)
    assert "Daiseijo" in calls and "Tensei" in calls
    assert selected == [2]  # The library's episode count beats popularity.
    monkeypatch.setattr(Covers, "episode_count", lambda project: None)
    selected.clear()
    Covers.ensure(project.id)
    assert selected == [MEDIA["id"]]


def test_ranking_prefers_main_title_then_episode_count_then_literal_spelling():
    def media(id, romaji, episodes, synonyms=(), format="TV", popularity=0):
        return {"id": id, "title": {"romaji": romaji}, "synonyms": list(synonyms), "episodes": episodes,
                "format": format, "popularity": popularity}
    clover, petit = media(1, "Black Clover", 170), media(2, "Petit Clover Advance", 15, ["Black Clover"], "SPECIAL", 99)
    assert [m["id"] for m in Covers.rank("Black Clover", [petit, clover], 15)] == [1, 2]
    season1, season2 = media(3, "Hataraku Saibou", 13), media(4, "Hataraku Saibou!!", 8, popularity=99)
    assert Covers.rank("Cells at Work! (Hataraku Saibou)", [season2, season1], 13)[0]["id"] == 3
    assert Covers.rank("Cells at Work! (Hataraku Saibou)", [season2, season1], None)[0]["id"] == 3
    yamada, ore = media(7, "Yamada-kun to Lv999 no Koi wo Suru", 13), media(8, "Ore Monogatari!!", 24, popularity=99)
    ore["title"]["english"] = "My Love Story!!"
    assert Covers.rank("Yamada-kun to Lv999 no Koi wo Suru (My Love Story)", [ore, yamada], 24)[0]["id"] == 7
    ova, tv = media(9, "Ushio to Tora", 10, format="OVA"), media(10, "Ushio to Tora (TV)", 26, ["Ushio and Tora"])
    ova["title"]["english"], tv["title"]["english"] = "Ushio and Tora", "Ushio and Tora (TV)"
    assert Covers.rank("Ushio and Tora", [ova, tv], 26)[0]["id"] == 10
    season2 = media(11, "Kekkon Yubiwa Monogatari II", 13, ["Kekkon Yubiwa Monogatari 2nd Season"])
    assert Covers.exact_matches("Kekkon Yubiwa Monogatari (Tales of Wedding Rings)", [season2]) == []
    assert Covers.normalize_title("Title II") != Covers.normalize_title("Title III")
    ova, tv = media(5, "Bucchigiri", 4, format="OVA"), media(6, "BUCCHIGIRI?!", 12)
    assert Covers.rank("Bucchigiri", [ova, tv], 12)[0]["id"] == 6


def test_subtitle_halves_are_discovery_queries_only():
    assert Covers.discovery_queries("Chiisana Eiyuu - Modest Heroes")[:2] == ["Chiisana Eiyuu", "Modest Heroes"]
    film = {"id": 1, "title": {"romaji": "Chiisana Eiyuu"}}
    assert Covers.exact_matches("Chiisana Eiyuu - Modest Heroes", [film]) == []


@pytest.mark.parametrize("library, catalog", [
    ("Tatsuki Fujimoto 17_26", "Tatsuki Fujimoto 17-26"),
    ("Kageki Shojo!!", "Kageki Shoujo!!"),
    ("Kono Kaisha ni Suki na Hito ga Imasu(I Have a Crush at Work)", "I Have a Crush at Work"),
    ("Yamada-kun to Lv999 no Koi wo Suru (My Love Story)", "Yamada-kun to Lv999 no Koi o Suru"),
    ("Taisho Otome Fairy Tale S01 1080p WEBRip AAC x265-EMBER", "Taisho Otome Fairy Tale"),
    ("Pokémon", "Pokemon"),
])
def test_library_spelling_variants_are_the_same_identity(library, catalog):
    media = {"id": 1, "title": {"romaji": catalog, "english": None, "native": None}, "synonyms": []}
    assert Covers.exact_matches(library, [media]) == [media]


def test_normalization_keeps_japanese_voiced_marks_and_strips_release_tags():
    assert Covers.normalize_title("ガンダム") != Covers.normalize_title("カンタム")
    assert Covers.title_queries("Taisho Otome Fairy Tale S01 1080p WEBRip AAC x265-EMBER") == ["Taisho Otome Fairy Tale"]
    assert Covers.exact_matches("Kill Ao", [{"id": 1, "title": {"romaji": "Kill Ao 2nd Season"}}]) == []


def test_unknown_spelling_is_resolved_by_constrained_judge(source, monkeypatch):
    from app.services.llm_service import LLMService
    project, _ = draft(source)
    candidates = [{"id": 5, "title": {"romaji": "Naruto Shippuuden"}}, {"id": 6, "title": {"romaji": "NARUTO"}}]
    prompts = []
    monkeypatch.setattr(LLMService, "is_configured", classmethod(lambda cls: True))
    def answer(prompt, **kwargs):
        prompts.append((prompt, kwargs["response_schema"]))
        return {"anilist_id": 6}
    monkeypatch.setattr(LLMService, "generate_json", answer)
    project = FacecamService.project(project.id)
    assert Covers.judge(project, candidates)["id"] == 6
    assert prompts[0][1]["properties"]["anilist_id"]["enum"] == [5, 6, 0]
    assert "Naruto" in prompts[0][0] and "episode.mp4" in prompts[0][0]
    monkeypatch.setattr(LLMService, "generate_json", lambda prompt, **kwargs: {"anilist_id": 5})
    assert Covers.judge(project, candidates)["id"] == 5
    stranger = {"id": 9, "title": {"romaji": "Oni Chichi", "english": "Demon Father"}}
    monkeypatch.setattr(LLMService, "generate_json", lambda prompt, **kwargs: {"anilist_id": 9})
    assert Covers.judge(project, [stranger]) is None  # No word in common with "Naruto".
    monkeypatch.setattr(LLMService, "generate_json", lambda prompt, **kwargs: {"anilist_id": 0})
    assert Covers.judge(project, candidates) is None
    monkeypatch.setattr(LLMService, "generate_json", lambda prompt, **kwargs: pytest.fail("Adult guesses are excluded"))
    assert Covers.judge(project, [{**candidates[1], "isAdult": True}]) is None
    monkeypatch.setattr(LLMService, "generate_json", lambda prompt, **kwargs: (_ for _ in ()).throw(RuntimeError()))
    assert Covers.judge(project, candidates) is None


def test_identity_is_shared_by_every_project_of_the_series(source, monkeypatch):
    first, _ = draft(source)
    second = FacecamService.create(source.id)
    for project in (first, second):
        project.series_id = "series-1"
        ProjectService.save(project)
    Covers.store(first.id, png(size=(300, 450)), {"anilist_id": 42, "source_url": "https://anilist.co/anime/42"})
    selected = []
    monkeypatch.setattr(Covers, "search", lambda *args: pytest.fail("A known series needs no search"))
    monkeypatch.setattr(Covers, "select", lambda pid, mid: selected.append((pid, mid)))
    monkeypatch.setattr(Covers, "render", lambda pid: "cover.png")
    Covers.ensure(second.id)
    assert selected == [(second.id, 42)]
    assert list(Covers.identities()) == ["anime:series-1"]

    Covers.store(first.id, png((10, 200, 10), (300, 450)), {"manual": True})
    monkeypatch.setattr(Covers, "select", lambda *args: pytest.fail("A shared upload wins over AniList"))
    Covers.ensure(second.id)
    shared = FacecamService.load(second.id).cover
    assert shared["manual"] is True and Image.open(shared["path"]).getpixel((5, 5))[1] > 190


def test_covers_chosen_before_the_registry_are_reused(source, monkeypatch):
    first, _ = draft(source)
    second = FacecamService.create(source.id)
    for project in (first, second):
        project.series_id = "series-1"
        ProjectService.save(project)
    Covers.store(first.id, png(size=(300, 450)), {"anilist_id": 42}, remember=False)
    selected = []
    monkeypatch.setattr(Covers, "select", lambda pid, mid: selected.append(mid))
    monkeypatch.setattr(Covers, "render", lambda pid: "cover.png")
    Covers.ensure(second.id)
    assert selected == [42]


def test_frame_fallback_never_blocks_and_is_retried_later(source, monkeypatch):
    project, _ = draft(source)
    monkeypatch.setattr(Covers, "search", lambda *args: (_ for _ in ()).throw(ValueError("AniList down")))
    frames = []
    monkeypatch.setattr(Covers, "frame_cover", lambda pid: frames.append(pid) or png())
    assert Covers.ensure(project.id).name == "anime_reveal.png"
    assert FacecamService.load(project.id).cover["frame"] is True and frames == [project.id]
    assert Covers.identities() == {}
    Covers.ensure(project.id)
    assert frames == [project.id]  # The existing frame is kept while AniList still fails.
    monkeypatch.setattr(Covers, "search", lambda *args: [{"id": 7, "title": {"romaji": "Naruto"}}])
    selected = []
    monkeypatch.setattr(Covers, "select", lambda pid, mid: selected.append(mid))
    Covers.ensure(project.id)
    assert selected == [7]


def test_frame_cover_uses_the_most_detailed_matched_frame(source, monkeypatch):
    import app.services.facecam_cover_service as module
    from app.services.anime_library import AnimeLibraryService
    project, _ = draft(source)
    monkeypatch.setattr(AnimeLibraryService, "resolve_episode_path", lambda *args, **kwargs: Path("/anime/episode.mp4"))
    flat, detailed = png((0, 0, 0)), io.BytesIO()
    noisy = Image.new("RGB", (64, 36))
    noisy.putdata([((x * 37) % 256, (x * 91) % 256, 0) for x in range(64 * 36)])
    noisy.save(detailed, "PNG")
    outputs = iter([flat, detailed.getvalue(), flat])
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=next(outputs)))
    assert Covers.frame_cover(project.id) == detailed.getvalue()


def test_rate_limited_catalog_is_retried(source, monkeypatch):
    import app.services.facecam_cover_service as module
    sleeps, responses = [], iter([
        SimpleNamespace(status_code=429, headers={"Retry-After": "3"}),
        SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None,
                        json=lambda: {"data": {"Page": {"media": [MEDIA]}}}),
    ])
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return next(responses)
    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(Covers, "title_queries", staticmethod(lambda title: ["only"]))
    assert Covers.search_title("only") == [MEDIA] and sleeps == [3.0]


def test_card_uses_catalog_spelling_of_the_same_title(source, monkeypatch):
    import app.services.facecam_cover_service as module
    project, _ = draft(source)
    project.anime_name = "Tatsuki Fujimoto 17_26"
    ProjectService.save(project)
    media = {"id": 198188, "title": {"romaji": "Fujimoto Tatsuki 17-26", "english": "Tatsuki Fujimoto 17-26"},
             "synonyms": [], "coverImage": {"extraLarge": "https://img"}, "siteUrl": "https://anilist.co/anime/198188"}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {"Media": media}})
        def get(self, url):
            return SimpleNamespace(raise_for_status=lambda: None, content=png(size=(300, 450)))
    monkeypatch.setattr(module.httpx, "Client", Client)
    cover = Covers.select(project.id, 198188).cover
    assert cover["title"] == "Tatsuki Fujimoto 17-26" and cover["anilist_id"] == 198188
    assert Covers.identities()[Covers.identity_key(FacecamService.project(project.id))]["anilist_id"] == 198188
    image = Image.open(Covers.render(project.id))
    assert image.size == (1080, 1920)


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
    monkeypatch.setattr(Covers, "frame_cover", lambda pid: png())
    Covers.ensure(project.id)
    assert len(calls) == 1
    assert FacecamService.load(project.id).cover["frame"] is True


def test_small_covers_are_upscaled_once_to_fill_the_card(source, monkeypatch):
    import app.services.facecam_cover_upscale as cover_upscale
    project, _ = draft(source)
    calls = []
    monkeypatch.setattr(cover_upscale, "upscale",
                        lambda image: calls.append(image.size) or image.resize((image.width * 4, image.height * 4)))
    Covers.store(project.id, png(size=(460, 690)), {"anilist_id": 1})
    Covers.render(project.id)
    assert calls == [(460, 690)]  # Cached after the first render.
    card = Image.open(FacecamService.directory(project.id) / "anime_reveal.png")
    assert card.getpixel((70, 800)) != (14, 17, 24) and card.getpixel((1005, 800)) != (14, 17, 24)

    Covers.store(project.id, png(size=(1920, 1080)), {"frame": True})
    assert calls == [(460, 690)]  # Large frames are only reduced.
    assert len(list(FacecamService.directory(project.id).glob("cover_hd-*.jpg"))) == 1

    monkeypatch.setattr(cover_upscale, "upscale", lambda image: (_ for _ in ()).throw(RuntimeError("no torch")))
    Covers.store(project.id, png(size=(200, 300)), {"manual": True})
    assert Image.open(Covers.render(project.id)).getpixel((70, 800)) != (14, 17, 24)


def test_real_anime_upscaler_runs_on_cpu(monkeypatch):
    import app.services.facecam_cover_upscale as cover_upscale
    real = Path(__file__).resolve().parents[1] / "data/models/realesrgan/RealESRGAN_x4plus_anime_6B.pth"
    if not real.is_file():
        pytest.skip("Real-ESRGAN weights are downloaded on first use")
    monkeypatch.setattr(cover_upscale, "weights_path", lambda: real)
    assert cover_upscale.upscale(Image.new("RGB", (24, 36), (200, 40, 40))).size == (96, 144)
