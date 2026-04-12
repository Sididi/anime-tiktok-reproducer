from __future__ import annotations

import pytest

from app.api.routes.matching import (
    BatchUpdateMatchItem,
    BatchUpdateMatchesRequest,
    UpdateMatchRequest,
    update_match,
    update_matches_batch,
)
from app.models import MatchList, Project, Scene, SceneList, SceneMatch
from app.services.anime_library import AnimeLibraryService
from app.services.project_service import ProjectService


@pytest.mark.asyncio
async def test_update_match_canonicalizes_absolute_episode_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    canonical_path = (
        tmp_path
        / "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.04."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit].mp4"
    )
    canonical_path.write_bytes(b"video")

    existing_matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=21,
                episode="legacy-no-match",
                start_time=0.0,
                end_time=1.0,
                confidence=0.7,
                speed_ratio=1.0,
                confirmed=False,
                was_no_match=True,
                merged_from=[99, 100],
            )
        ]
    )
    saved: dict[str, MatchList] = {}

    monkeypatch.setattr(
        ProjectService,
        "load",
        lambda project_id: Project(id=project_id, library_type="anime"),
    )
    monkeypatch.setattr(ProjectService, "load_matches", lambda project_id: existing_matches)
    monkeypatch.setattr(
        ProjectService,
        "load_scenes",
        lambda project_id: SceneList(
            scenes=[Scene(index=21, start_time=0.0, end_time=2.0)]
        ),
    )
    monkeypatch.setattr(
        ProjectService,
        "save_matches",
        lambda project_id, matches: saved.setdefault("matches", matches),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(lambda cls, episode, **_kwargs: canonical_path),
    )

    result = await update_match(
        "proj-1",
        21,
        UpdateMatchRequest(
            episode=str(canonical_path),
            start_time=10.0,
            end_time=11.0,
            confirmed=True,
        ),
    )

    persisted = saved["matches"].matches[0]
    assert (
        persisted.episode
        == "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.04."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit]"
    )
    assert persisted.start_time == pytest.approx(10.0)
    assert persisted.end_time == pytest.approx(11.0)
    assert persisted.confirmed is True
    assert persisted.was_no_match is True
    assert persisted.merged_from == [99, 100]
    assert persisted.speed_ratio == pytest.approx(2.0)
    assert result["match"]["episode"] == persisted.episode


@pytest.mark.asyncio
async def test_update_matches_batch_canonicalizes_episode_refs_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    canonical_path = (
        tmp_path
        / "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.01."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit].mp4"
    )
    canonical_path.write_bytes(b"video")

    existing_matches = MatchList(
        matches=[
            SceneMatch(
                scene_index=21,
                episode="legacy-path",
                start_time=0.0,
                end_time=1.0,
                confidence=0.9,
                speed_ratio=1.0,
                confirmed=False,
                was_no_match=True,
            ),
            SceneMatch(
                scene_index=37,
                episode="legacy-merged",
                start_time=2.0,
                end_time=3.0,
                confidence=0.6,
                speed_ratio=1.0,
                confirmed=False,
                was_no_match=True,
                merged_from=[41, 42, 43, 44],
            ),
        ]
    )
    saved: dict[str, MatchList] = {}

    monkeypatch.setattr(
        ProjectService,
        "load",
        lambda project_id: Project(id=project_id, library_type="anime"),
    )
    monkeypatch.setattr(ProjectService, "load_matches", lambda project_id: existing_matches)
    monkeypatch.setattr(
        ProjectService,
        "load_scenes",
        lambda project_id: SceneList(
            scenes=[
                Scene(index=21, start_time=0.0, end_time=2.0),
                Scene(index=37, start_time=0.0, end_time=7.5),
            ]
        ),
    )
    monkeypatch.setattr(
        ProjectService,
        "save_matches",
        lambda project_id, matches: saved.setdefault("matches", matches),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "resolve_episode_path",
        classmethod(
            lambda cls, episode, **_kwargs: canonical_path
            if episode == str(canonical_path)
            else None
        ),
    )

    result = await update_matches_batch(
        "proj-1",
        BatchUpdateMatchesRequest(
            updates=[
                BatchUpdateMatchItem(
                    scene_index=21,
                    episode=str(canonical_path),
                    start_time=10.0,
                    end_time=11.0,
                    confirmed=True,
                ),
                BatchUpdateMatchItem(
                    scene_index=37,
                    episode="[Anime Time] Anohana - The Flower We Saw That Day Movie",
                    start_time=20.0,
                    end_time=25.0,
                    confirmed=True,
                ),
            ]
        ),
    )

    persisted_by_scene = {
        match.scene_index: match for match in saved["matches"].matches
    }
    assert (
        persisted_by_scene[21].episode
        == "[bonkai77].Anohana.The.Flower.We.Saw.That.Day.Episode.01."
        "[BD.1080p.Dual.Audio.x265.HEVC.10bit]"
    )
    assert (
        persisted_by_scene[37].episode
        == "[Anime Time] Anohana - The Flower We Saw That Day Movie"
    )
    assert persisted_by_scene[21].was_no_match is True
    assert persisted_by_scene[37].was_no_match is True
    assert persisted_by_scene[37].merged_from == [41, 42, 43, 44]
    assert persisted_by_scene[21].speed_ratio == pytest.approx(2.0)
    assert persisted_by_scene[37].speed_ratio == pytest.approx(1.5)
    assert result["matches"][0]["episode"] == persisted_by_scene[21].episode
    assert result["matches"][1]["episode"] == persisted_by_scene[37].episode
