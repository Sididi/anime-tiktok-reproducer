from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest

from app.api.routes import matching
from app.models import MatchList, Project, ProjectPhase, Scene, SceneList, SceneMatch
from app.services.anime_matcher import MatchProgress
from app.services.indexation_queue import indexation_queue


@pytest.mark.asyncio
async def test_unfinished_episode_import_preserves_saved_project_before_matching(monkeypatch):
    project = Project(
        id="recompute", anime_name="Series", series_id="series-1",
        phase=ProjectPhase.MATCH_VALIDATION,
    )
    monkeypatch.setattr(matching.ProjectService, "aload", AsyncMock(return_value=project))
    monkeypatch.setattr(matching.LibraryHydrationService, "ensure_matcher_ready_for_project", AsyncMock(
        side_effect=RuntimeError("12 local episode(s) have not finished indexing"),
    ))
    save_project, save_scenes, save_matches = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(matching.ProjectService, "asave", save_project)
    monkeypatch.setattr(matching.ProjectService, "asave_scenes", save_scenes)
    monkeypatch.setattr(matching.ProjectService, "asave_matches", save_matches)
    with pytest.raises(matching.HTTPException) as error:
        await matching._find_matches_stream(project.id, matching.FindMatchesRequest())
    assert error.value.status_code == 409
    assert "have not finished indexing" in error.value.detail
    assert project.phase == ProjectPhase.MATCH_VALIDATION
    save_project.assert_not_awaited()
    save_scenes.assert_not_awaited()
    save_matches.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded", [True, False])
@pytest.mark.parametrize("matched", [True, False])
async def test_recompute_only_replaces_saved_results_when_matches_exist(monkeypatch, tmp_path, bounded, matched):
    video = tmp_path / "video.mp4"
    video.touch()
    project = Project(id="recompute", anime_name="Series", video_path=str(video), phase=ProjectPhase.MATCH_VALIDATION)
    scenes = SceneList(scenes=[
        Scene(index=0, start_time=0, end_time=2),
        Scene(index=1, start_time=2, end_time=2.2),
        Scene(index=2, start_time=2.2, end_time=5),
    ])
    result = SimpleNamespace(
        scenes=SceneList(scenes=[Scene(index=0, start_time=0, end_time=5)]),
        matches=MatchList(matches=[SceneMatch(
            scene_index=0, episode="Episode 06" if matched else "",
            start_time=10 if matched else 0, end_time=15 if matched else 0,
            confidence=0.9 if matched else 0, speed_ratio=1,
            doubt_reasons=[] if matched else ["no_evidence"],
        )]),
    )
    monkeypatch.setattr(matching.ProjectService, "aload", AsyncMock(return_value=project))
    monkeypatch.setattr(matching.ProjectService, "aload_scenes", AsyncMock(return_value=scenes))
    monkeypatch.setattr(matching.ProjectService, "asave", AsyncMock())
    save_scenes, save_matches = AsyncMock(), AsyncMock()
    monkeypatch.setattr(matching.ProjectService, "asave_scenes", save_scenes)
    monkeypatch.setattr(matching.ProjectService, "asave_matches", save_matches)
    monkeypatch.setattr(matching.AnimeLibraryService, "get_library_path", lambda *a: tmp_path)
    monkeypatch.setattr(matching, "bounded_matcher_enabled", lambda: bounded)
    monkeypatch.setattr(matching.zoom_search_service, "invalidate_project", lambda *a, **kw: None)

    @asynccontextmanager
    async def slot(*args, **kwargs):
        yield

    monkeypatch.setattr(indexation_queue, "matching_lock", slot)
    monkeypatch.setattr(indexation_queue, "heavy_slot", slot)

    async def align(*args, **kwargs):
        yield MatchProgress("complete", 1, "done", 1, 1, result.matches)

    monkeypatch.setattr(matching.SceneAlignerService, "align_scenes_progress", align)
    monkeypatch.setattr(matching.SceneAlignerService, "get_last_result", lambda: result)
    response = await matching.find_matches(project.id, matching.FindMatchesRequest(episodes=["Episode 06", "Episode 08"]))
    events = [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]
    assert project.phase == ProjectPhase.MATCH_VALIDATION
    if matched:
        assert events[-1]["status"] == "complete"
        save_scenes.assert_awaited_once_with(project.id, result.scenes)
        save_matches.assert_awaited_once_with(project.id, result.matches)
    else:
        assert events[-1]["status"] == "error"
        assert "Existing scenes and matches were kept" in events[-1]["error"]
        save_scenes.assert_not_awaited()
        save_matches.assert_not_awaited()
