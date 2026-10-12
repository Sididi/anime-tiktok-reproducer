from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import matching
from app.api.routes.matching import _dedupe_episode_options
from app.api.routes.matching import _has_persisted_match_choice
from app.models import MatchList, Project, Scene, SceneList, SceneMatch
from app.services.match_playback_service import MatchPlaybackService


@pytest.mark.asyncio
async def test_episode_list_includes_new_files_when_cached_manifest_has_old_hits(monkeypatch, tmp_path):
    series = tmp_path / "Series"
    series.mkdir()
    old = series / "Season 1 Episode 01.mp4"
    old.touch()
    (series / "Season 2 Episode 01.mp4").touch()
    (series / "Season 2 Episode 02.import.tmp.mp4").touch()
    project = Project(id="project-1", anime_name="Series")
    monkeypatch.setattr(matching.ProjectService, "aload", AsyncMock(return_value=project))
    monkeypatch.setattr(matching.AnimeLibraryService, "get_library_path", lambda *a: tmp_path)
    monkeypatch.setattr(matching.AnimeLibraryService, "ensure_episode_manifest", AsyncMock(return_value={"old": True}))
    monkeypatch.setattr(matching.AnimeLibraryService, "list_episode_paths", lambda *a, **kw: [str(old)])
    result = await matching.list_episodes(project.id)
    assert result["episodes"] == ["Season 1 Episode 01", "Season 2 Episode 01"]


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


def test_persisted_match_choice_detects_existing_source_choice():
    assert _has_persisted_match_choice(
        SceneMatch(
            scene_index=3,
            episode="Episode 01",
            start_time=12.0,
            end_time=13.0,
            confidence=1.0,
            speed_ratio=1.0,
            confirmed=False,
        )
    )


def test_persisted_match_choice_ignores_unfilled_no_match_entry():
    assert not _has_persisted_match_choice(
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


def test_batch_auto_fill_does_not_overwrite_existing_match(monkeypatch):
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
                confirmed=False,
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

    result = asyncio.run(
        matching.update_matches_batch(
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
        ),
    )

    assert result["matches"][0]["episode"] == "Manual Episode"
    assert result["matches"][0]["start_time"] == 10.0
    assert saved_matches[0].matches[0].episode == "Manual Episode"


def test_update_match_rejects_end_time_before_start_time():
    with pytest.raises(matching.HTTPException) as exc_info:
        asyncio.run(
            matching.update_match(
                "project-1",
                0,
                matching.UpdateMatchRequest(
                    episode="Episode 01",
                    start_time=20.0,
                    end_time=19.0,
                    confirmed=True,
                ),
            ),
        )

    assert exc_info.value.status_code == 400
    assert "end_time must be greater than start_time" in exc_info.value.detail


def test_update_matches_batch_rejects_equal_start_and_end_time(monkeypatch):
    monkeypatch.setattr(
        matching.ProjectService,
        "load",
        lambda project_id: Project(id=project_id, library_type="anime"),
    )
    monkeypatch.setattr(
        matching.ProjectService,
        "load_matches",
        lambda project_id: MatchList(
            matches=[
                SceneMatch(
                    scene_index=0,
                    episode="",
                    start_time=0.0,
                    end_time=0.0,
                    confidence=0.0,
                    speed_ratio=1.0,
                    confirmed=False,
                )
            ]
        ),
    )
    monkeypatch.setattr(
        matching.ProjectService,
        "load_scenes",
        lambda project_id: SceneList(
            scenes=[Scene(index=0, start_time=0.0, end_time=1.0)]
        ),
    )

    with pytest.raises(matching.HTTPException) as exc_info:
        asyncio.run(
            matching.update_matches_batch(
                "project-1",
                matching.BatchUpdateMatchesRequest(
                    updates=[
                        matching.BatchUpdateMatchItem(
                            scene_index=0,
                            episode="Episode 01",
                            start_time=20.0,
                            end_time=20.0,
                            confirmed=True,
                        )
                    ]
                ),
            ),
        )

    assert exc_info.value.status_code == 400
    assert "end_time must be greater than start_time" in exc_info.value.detail


def test_playback_manifest_rejects_stale_active_fingerprint(monkeypatch, tmp_path):
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
        MatchPlaybackService,
        "_load_active_fingerprint",
        classmethod(lambda cls, project_id: "old-fingerprint"),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_load_manifest_sync",
        classmethod(
            lambda cls, project_id, fingerprint: {
                "ready": True,
                "fingerprint": fingerprint,
                "scenes": [],
            }
        ),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_validate_manifest_sync",
        classmethod(lambda cls, project_id, manifest: True),
    )
    monkeypatch.setattr(
        matching.ProjectService,
        "load",
        lambda project_id: Project(
            id=project_id,
            library_type="anime",
            video_path=str(tmp_path / "tiktok.mp4"),
        ),
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
        "load_matches",
        lambda project_id: current_matches,
    )

    async def resolve_episode_path(project, episode):
        source_path = tmp_path / "source.mp4"
        source_path.write_bytes(b"source")
        return source_path

    monkeypatch.setattr(
        MatchPlaybackService,
        "_resolve_episode_path",
        classmethod(lambda cls, project, episode: resolve_episode_path(project, episode)),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_build_fingerprint",
        classmethod(lambda cls, project, scenes, matches, source_by_episode: "new-fingerprint"),
    )

    manifest = asyncio.run(MatchPlaybackService.get_manifest("project-1"))

    assert manifest["ready"] is False
