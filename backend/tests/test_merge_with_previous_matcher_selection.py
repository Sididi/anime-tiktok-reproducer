"""The manual merge/rematch route must follow ATR_MATCHER_V2.

`/matches/merge-with-previous` re-matches exactly one merged scene. It used to
always call `AnimeMatcherService.match_scenes`, so a project matched by the
bounded hierarchical matcher had one scene re-matched by an algorithm that
produced none of its neighbours. This pins the routing:

  - flag unset/0 -> HierarchicalMatcherService.rematch_scene_sync
  - ATR_MATCHER_V2=1 -> AnimeMatcherService.match_scenes (unchanged)

and, in both cases, that the route restores the merge provenance it owns.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import matching
from app.models import (
    MatchList,
    Project,
    ProjectPhase,
    Scene,
    SceneList,
    SceneMatch,
)


def _match(index: int, episode: str, merged=None) -> SceneMatch:
    return SceneMatch(
        scene_index=index,
        episode=episode,
        start_time=10.0 + index,
        end_time=11.0 + index,
        confidence=0.9,
        speed_ratio=1.0,
        merged_from=merged,
    )


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub every collaborator the route touches except the matchers."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"0")
    library = tmp_path / "library"
    library.mkdir()

    project = Project(
        id="p1",
        name="p1",
        phase=ProjectPhase.MATCH_VALIDATION,
        library_type="anime",
        anime_name="series",
        video_path=str(video),
    )
    scenes = SceneList(
        scenes=[
            Scene(index=0, start_time=0.0, end_time=2.0),
            Scene(index=1, start_time=2.0, end_time=5.0),
        ]
    )
    matches = MatchList(matches=[_match(0, "episode-1"), _match(1, "episode-2")])
    merged_scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=5.0)])
    merged_matches = MatchList(matches=[_match(0, "episode-1", merged=[0, 1])])

    monkeypatch.setattr(matching.ProjectService, "load", staticmethod(lambda pid: project))
    monkeypatch.setattr(matching.ProjectService, "load_scenes", staticmethod(lambda pid: scenes))
    monkeypatch.setattr(matching.ProjectService, "load_matches", staticmethod(lambda pid: matches))
    saved: dict[str, object] = {}
    monkeypatch.setattr(
        matching.ProjectService, "save_scenes",
        staticmethod(lambda pid, value: saved.__setitem__("scenes", value)),
    )
    monkeypatch.setattr(
        matching.ProjectService, "save_matches",
        staticmethod(lambda pid, value: saved.__setitem__("matches", value)),
    )
    monkeypatch.setattr(matching.ProjectService, "save", staticmethod(lambda value: None))
    monkeypatch.setattr(
        matching.AnimeLibraryService, "get_library_path",
        staticmethod(lambda library_type: library),
    )
    monkeypatch.setattr(
        matching.SceneMergerService, "prepare_manual_merge_with_previous",
        staticmethod(lambda *a, **k: (merged_scenes, merged_matches, {}, 0)),
    )
    monkeypatch.setattr(
        matching.SceneMergerService, "save_pre_merge_backup",
        staticmethod(lambda pid, backup: None),
    )
    return saved


def test_bounded_matcher_handles_the_manual_rematch(wired, monkeypatch):
    from app.services.hierarchical_matcher import HierarchicalMatcherService

    monkeypatch.delenv("ATR_MATCHER_V2", raising=False)
    monkeypatch.setattr("app.config.settings.matcher_v2", False)

    calls: list[dict] = []

    def fake_rematch(cls, video_path, scenes, library_type, anime_name=None, **kwargs):
        calls.append(kwargs)
        return MatchList(matches=[_match(0, "bounded-result")])

    monkeypatch.setattr(
        HierarchicalMatcherService, "rematch_scene_sync", classmethod(fake_rematch)
    )
    monkeypatch.setattr(
        matching.AnimeMatcherService, "_init_searcher",
        classmethod(lambda cls, *a, **k: True),
    )

    async def fail_match_scenes(*a, **k):
        raise AssertionError("the old matcher must not run on the bounded path")
        yield  # pragma: no cover

    monkeypatch.setattr(matching.AnimeMatcherService, "match_scenes", fail_match_scenes)

    result = asyncio.run(matching.merge_with_previous("p1", 1))

    assert calls and calls[0]["scene_index"] == 0
    assert result["matches"][0]["episode"] == "bounded-result"
    # merge provenance is the route's own fact, restored after either matcher
    assert result["matches"][0]["merged_from"] == [0, 1]

    # The absorbed-into fragment's pre-merge match is handed over as a prior:
    # scene 0 spans 0.0-2.0 and was matched to episode-1 at 10.0-11.0.
    prior = calls[0]["prior"]
    assert prior is not None
    assert prior.episode == "episode-1"
    assert (prior.q_start, prior.q_end) == (0.0, 2.0)
    assert (prior.source_start, prior.source_end) == (10.0, 11.0)


def test_no_prior_when_the_previous_fragment_was_unmatched(wired, monkeypatch):
    from app.services.hierarchical_matcher import HierarchicalMatcherService

    monkeypatch.delenv("ATR_MATCHER_V2", raising=False)
    monkeypatch.setattr("app.config.settings.matcher_v2", False)

    # Wipe the previous fragment's match: there is nothing to continue from.
    stale = matching.ProjectService.load_matches("p1")
    stale.matches[0].episode = ""
    stale.matches[0].start_time = 0.0
    stale.matches[0].end_time = 0.0

    calls: list[dict] = []
    monkeypatch.setattr(
        HierarchicalMatcherService,
        "rematch_scene_sync",
        classmethod(
            lambda cls, *a, **k: (
                calls.append(k),
                MatchList(matches=[_match(0, "bounded-result")]),
            )[1]
        ),
    )
    monkeypatch.setattr(
        matching.AnimeMatcherService, "_init_searcher",
        classmethod(lambda cls, *a, **k: True),
    )

    asyncio.run(matching.merge_with_previous("p1", 1))

    assert calls and calls[0]["prior"] is None


def test_flag_keeps_the_old_matcher_for_the_manual_rematch(wired, monkeypatch):
    from app.services.hierarchical_matcher import HierarchicalMatcherService

    monkeypatch.setenv("ATR_MATCHER_V2", "1")

    def fail_rematch(cls, *a, **k):
        raise AssertionError("the bounded matcher must not run behind the flag")

    monkeypatch.setattr(
        HierarchicalMatcherService, "rematch_scene_sync", classmethod(fail_rematch)
    )

    from app.services.anime_matcher import MatchProgress

    async def fake_match_scenes(*a, **k):
        yield MatchProgress(
            "complete", 1.0, "", 1, 1, MatchList(matches=[_match(0, "legacy-result")])
        )

    monkeypatch.setattr(matching.AnimeMatcherService, "match_scenes", fake_match_scenes)

    result = asyncio.run(matching.merge_with_previous("p1", 1))

    assert result["matches"][0]["episode"] == "legacy-result"
    assert result["matches"][0]["merged_from"] == [0, 1]
