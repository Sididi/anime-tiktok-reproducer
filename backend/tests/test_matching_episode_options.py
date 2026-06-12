from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import matching
from app.api.routes.matching import _dedupe_episode_options
from app.api.routes.matching import _is_confirmed_match
from app.models import MatchList, Project, Scene, SceneList, SceneMatch


def test_episode_options_collapse_extension_variants_to_extensionless_value():
    episodes = _dedupe_episode_options(
        [
            "/library/S-Rank/[Judas] S-Rank Musume - S01E01.mp4",
            "[Judas] S-Rank Musume - S01E01",
            "[Judas] S-Rank Musume - S01E02.mkv",
            "[Judas] S-Rank Musume - S01E02",
        ]
    )

    assert episodes == [
        "[Judas] S-Rank Musume - S01E01",
        "[Judas] S-Rank Musume - S01E02",
    ]


def test_episode_options_ignore_empty_values():
    assert _dedupe_episode_options(["", "  ", "Episode 01.mp4"]) == ["Episode 01"]


def test_confirmed_match_detects_persisted_manual_choice():
    assert _is_confirmed_match(
        SceneMatch(
            scene_index=3,
            episode="Episode 01",
            start_time=12.0,
            end_time=13.0,
            confidence=1.0,
            speed_ratio=1.0,
            confirmed=True,
        )
    )


def test_confirmed_match_ignores_unfilled_no_match_entry():
    assert not _is_confirmed_match(
        SceneMatch(
            scene_index=3,
            episode="",
            start_time=0.0,
            end_time=0.0,
            confidence=0.0,
            speed_ratio=1.0,
            confirmed=False,
        )
    )


@pytest.mark.asyncio
async def test_batch_auto_fill_does_not_overwrite_confirmed_match(monkeypatch):
    saved_matches: list[MatchList] = []
    current_matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="Manual Episode",
                start_time=10.0,
                end_time=11.0,
                confidence=1.0,
                speed_ratio=1.0,
                confirmed=True,
            )
        ]
    )

    monkeypatch.setattr(
        matching.ProjectService,
        "load",
        lambda project_id: Project(id=project_id, library_type="anime"),
    )
    monkeypatch.setattr(
        matching.ProjectService,
        "load_matches",
        lambda project_id: current_matches,
    )
    monkeypatch.setattr(
        matching.ProjectService,
        "load_scenes",
        lambda project_id: SceneList(
            scenes=[Scene(index=0, start_time=0.0, end_time=1.0)]
        ),
    )
    monkeypatch.setattr(
        matching.ProjectService,
        "save_matches",
        lambda project_id, matches: saved_matches.append(matches.model_copy(deep=True)),
    )

    result = await matching.update_matches_batch(
        "project-1",
        matching.BatchUpdateMatchesRequest(
            updates=[
                matching.BatchUpdateMatchItem(
                    scene_index=0,
                    episode="Auto Episode",
                    start_time=20.0,
                    end_time=21.0,
                    confirmed=True,
                )
            ]
        ),
    )

    assert result["matches"][0]["episode"] == "Manual Episode"
    assert result["matches"][0]["start_time"] == 10.0
    assert saved_matches[0].matches[0].episode == "Manual Episode"
