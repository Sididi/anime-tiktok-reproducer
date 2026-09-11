from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes import matching
from app.models import MatchList, Project, SceneMatch
from app.services.deferred_download import DeferredDownloadService


@pytest.fixture
def hydration_route(monkeypatch, tmp_path):
    project = Project(id="project-1", anime_name="Series", series_id="series-1")
    matches = MatchList(matches=[SceneMatch(
        scene_index=0, episode="Episode 01", start_time=0, end_time=1,
        confidence=1, speed_ratio=1,
    )])
    monkeypatch.setattr(matching.ProjectService, "aload", AsyncMock(return_value=project))
    monkeypatch.setattr(matching.ProjectService, "aload_matches", AsyncMock(return_value=matches))
    monkeypatch.setattr(matching.AnimeLibraryService, "get_library_path", lambda *a: tmp_path)
    activate = AsyncMock()
    hydrate = AsyncMock()
    monkeypatch.setattr(matching.LibraryHydrationService, "ensure_matcher_ready_for_project", activate)
    monkeypatch.setattr(matching.LibraryHydrationService, "hydrate_series", hydrate)

    def no_torrents(*args, **kwargs):
        raise AssertionError("Automatic torrent recovery must not run")

    monkeypatch.setattr(DeferredDownloadService, "recover_missing_episodes", no_torrents)
    return project, matches, activate, hydrate


async def _events():
    response = await matching.deferred_download("project-1")
    return [json.loads(chunk.removeprefix("data: ")) async for chunk in response.body_iterator]


@pytest.mark.asyncio
async def test_storage_box_hydration_completes_without_torrent_recovery(hydration_route):
    _, _, activate, hydrate = hydration_route
    events = await _events()
    assert events[-1]["status"] == "complete"
    activate.assert_awaited_once()
    hydrate.assert_awaited_once()
    assert hydrate.call_args.kwargs["episode_keys"] == ["Episode 01"]
    assert hydrate.call_args.kwargs["full_series"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["hydrate_index", "hydrate_episode"])
async def test_storage_box_failure_is_reported_without_fallback(hydration_route, stage):
    _, _, activate, hydrate = hydration_route
    target = activate if stage == "hydrate_index" else hydrate
    target.side_effect = RuntimeError("Storage Box unavailable")
    events = await _events()
    assert events[-1] == {
        "status": "error", "phase": stage,
        "error": "Storage Box unavailable", "message": "Storage Box unavailable",
    }
    assert all(event["status"] not in ("warning", "complete") for event in events)


@pytest.mark.asyncio
async def test_empty_matches_do_not_hydrate_entire_series(hydration_route):
    _, matches, activate, hydrate = hydration_route
    matches.matches.clear()
    events = await _events()
    assert events[-1]["status"] == "complete"
    activate.assert_not_awaited()
    hydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_local_sources_request_storage_box_series(hydration_route):
    project, _, activate, hydrate = hydration_route
    project.series_id = None
    events = await _events()
    assert events[-1]["status"] == "error"
    assert "select the Storage Box series" in events[-1]["error"]
    activate.assert_not_awaited()
    hydrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_local_sources_need_no_storage_box(hydration_route, tmp_path):
    project, _, _, hydrate = hydration_route
    project.series_id = None
    source = tmp_path / "Series" / "Episode 01.mp4"
    source.parent.mkdir()
    source.write_bytes(b"local video")
    events = await _events()
    assert events[-1]["status"] == "complete"
    hydrate.assert_not_awaited()
