"""Job lifecycle contract for the per-scene extensive zoom search service."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.library_types import LibraryType
from app.models import AlternativeMatch, MatchList, Scene, SceneList, SceneMatch
from app.models.project import Project
from app.services.anime_library import AnimeLibraryService
from app.services.anime_matcher import AnimeMatcherService
from app.services.event_hub import event_hub
from app.services.project_service import ProjectService
from app.services.zoom_rematch import ZoomRematchService, ZoomSearchOutcome
from app.services.zoom_search_service import ZoomSearchService


def _scene(i: int, a: float, b: float) -> Scene:
    return Scene(index=i, start_time=a, end_time=b)


def _match(i: int, episode: str, a: float, b: float) -> SceneMatch:
    return SceneMatch(
        scene_index=i,
        episode=episode,
        start_time=a,
        end_time=b,
        confidence=0.9,
        speed_ratio=1.0,
    )


def _outcome(
    old: SceneMatch,
    new: SceneMatch | None,
    changed: bool,
    alternatives: tuple[AlternativeMatch, ...] = (),
) -> ZoomSearchOutcome:
    return ZoomSearchOutcome(
        changed=changed,
        old_match=old,
        new_match=new,
        best_score=0.8,
        current_score=0.5,
        hypotheses_scored=3,
        deadline_hit=False,
        detail="test",
        alternatives=alternatives,
    )


class _Env:
    """Monkeypatched project world shared by the tests."""

    def __init__(self, monkeypatch, tmp_path) -> None:
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x")
        self.project = Project(
            id="proj-1",
            tiktok_url="https://example.com/v",
            library_type=LibraryType.ANIME,
            video_path=str(video),
            series_id=None,
            anime_name="Anime",
        )
        self.scenes = SceneList(scenes=[_scene(0, 0.0, 2.0), _scene(1, 2.0, 5.0)])
        self.matches = MatchList(
            matches=[_match(0, "EP01", 10.0, 12.0), _match(1, "EP01", 20.0, 23.0)]
        )
        self.saved: list[MatchList] = []

        monkeypatch.setattr(
            ProjectService, "load", staticmethod(lambda pid: self.project)
        )
        monkeypatch.setattr(
            ProjectService, "load_scenes", staticmethod(lambda pid: self.scenes)
        )
        monkeypatch.setattr(
            ProjectService, "load_matches", staticmethod(lambda pid: self.matches)
        )
        monkeypatch.setattr(
            ProjectService,
            "save_matches",
            staticmethod(lambda pid, matches: self.saved.append(matches)),
        )
        monkeypatch.setattr(
            AnimeLibraryService,
            "get_library_path",
            classmethod(lambda cls, lt: tmp_path),
        )
        monkeypatch.setattr(
            AnimeMatcherService,
            "_init_searcher",
            classmethod(lambda cls, path, lt, name=None: True),
        )
        from app.services import fast_matching

        monkeypatch.setattr(fast_matching, "decode_enabled", lambda: False)


async def _wait_terminal(service: ZoomSearchService, job_id: str) -> None:
    for _ in range(200):
        job = service._jobs[job_id]
        if job.status in {"complete", "error", "cancelled"}:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"job never terminal: {service._jobs[job_id]}")


@pytest.mark.asyncio
async def test_lifecycle_applies_changed_match(monkeypatch, tmp_path) -> None:
    env = _Env(monkeypatch, tmp_path)
    new = _match(1, "EP03", 400.0, 403.0)
    monkeypatch.setattr(
        ZoomRematchService,
        "search_scene_sync",
        classmethod(
            lambda cls, *a, existing_match, **k: _outcome(existing_match, new, True)
        ),
    )
    service = ZoomSearchService()

    # Job state fans out through the shared event hub (coalescing per job:
    # an unread update is replaced by the next one), so drain right after
    # enqueue for the guaranteed "queued" frame and again at the end for
    # the terminal one. "running" may be coalesced away and is not asserted.
    sub = event_hub.subscribe()
    try:
        job = await service.enqueue("proj-1", 1)
        events: list[dict] = [f["data"] for f in sub.drain()]
        await _wait_terminal(service, job.id)
        events.extend(f["data"] for f in sub.drain())
    finally:
        event_hub.unsubscribe(sub)

    assert job.status == "complete"
    assert job.changed is True
    assert job.applied is True
    assert job.new_match and job.new_match["episode"] == "EP03"
    assert env.saved, "matches.json must be written"
    applied = next(
        m for m in env.saved[-1].matches if m.scene_index == 1
    )
    assert applied.episode == "EP03"

    assert all(e["id"] == job.id for e in events)
    statuses = [e["status"] for e in events]
    assert statuses[0] == "queued"
    assert statuses[-1] == "complete"


@pytest.mark.asyncio
async def test_unchanged_outcome_saves_nothing(monkeypatch, tmp_path) -> None:
    env = _Env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ZoomRematchService,
        "search_scene_sync",
        classmethod(
            lambda cls, *a, existing_match, **k: _outcome(existing_match, None, False)
        ),
    )
    service = ZoomSearchService()
    job = await service.enqueue("proj-1", 0)
    await _wait_terminal(service, job.id)
    assert job.status == "complete"
    assert job.changed is False and job.applied is False
    assert not env.saved


@pytest.mark.asyncio
async def test_unchanged_outcome_persists_zoom_candidates_for_manual_modal(
    monkeypatch, tmp_path
) -> None:
    env = _Env(monkeypatch, tmp_path)
    candidate = AlternativeMatch(
        episode="EP03",
        start_time=400.0,
        end_time=403.0,
        confidence=0.9,
        speed_ratio=1.0,
        vote_count=4,
        algorithm="zoom_search_registered",
    )

    def search(cls, *args, existing_match, context_matches, **kwargs):
        assert context_matches is env.matches
        return _outcome(existing_match, None, False, (candidate,))

    monkeypatch.setattr(
        ZoomRematchService, "search_scene_sync", classmethod(search)
    )
    service = ZoomSearchService()
    job = await service.enqueue("proj-1", 1)
    await _wait_terminal(service, job.id)

    assert job.status == "complete"
    assert job.changed is False and job.applied is False
    assert job.candidates_added == 1
    assert job.result_match is not None
    assert job.result_match["alternatives"][0]["episode"] == "EP03"
    assert env.saved


@pytest.mark.asyncio
async def test_enqueue_dedupes_live_jobs(monkeypatch, tmp_path) -> None:
    _Env(monkeypatch, tmp_path)
    release = threading.Event()
    monkeypatch.setattr(
        ZoomRematchService,
        "search_scene_sync",
        classmethod(
            lambda cls, *a, existing_match, **k: (
                release.wait(5),
                _outcome(existing_match, None, False),
            )[1]
        ),
    )
    service = ZoomSearchService()
    first = await service.enqueue("proj-1", 1)
    second = await service.enqueue("proj-1", 1)
    assert first.id == second.id
    other_scene = await service.enqueue("proj-1", 0)
    assert other_scene.id != first.id
    release.set()
    await _wait_terminal(service, first.id)
    await _wait_terminal(service, other_scene.id)


@pytest.mark.asyncio
async def test_invalidate_project_cancels_live_jobs(monkeypatch, tmp_path) -> None:
    _Env(monkeypatch, tmp_path)
    release = threading.Event()
    monkeypatch.setattr(
        ZoomRematchService,
        "search_scene_sync",
        classmethod(
            lambda cls, *a, existing_match, cancel_event, **k: (
                release.wait(5),
                _outcome(existing_match, None, False),
            )[1]
        ),
    )
    service = ZoomSearchService()
    job = await service.enqueue("proj-1", 1)
    # Let it reach the running state before invalidating.
    for _ in range(100):
        if job.status == "running":
            break
        await asyncio.sleep(0.02)

    service.invalidate_project("proj-1", reason="recompute")
    release.set()
    await _wait_terminal(service, job.id)
    assert job.status == "cancelled"
    assert job.acknowledged is True
    assert service._cancel_events.get(job.id) is None or service._cancel_events[
        job.id
    ].is_set()


@pytest.mark.asyncio
async def test_mid_run_edit_downgrades_to_alternative(monkeypatch, tmp_path) -> None:
    env = _Env(monkeypatch, tmp_path)
    new = _match(1, "EP03", 400.0, 403.0)
    edited = MatchList(
        matches=[_match(0, "EP01", 10.0, 12.0), _match(1, "EP07", 99.0, 102.0)]
    )
    loads = {"count": 0}

    def load_matches(pid):
        loads["count"] += 1
        # First load feeds the search; the re-load before apply sees the
        # owner's concurrent manual edit.
        return env.matches if loads["count"] == 1 else edited

    monkeypatch.setattr(ProjectService, "load_matches", staticmethod(load_matches))
    monkeypatch.setattr(
        ZoomRematchService,
        "search_scene_sync",
        classmethod(
            lambda cls, *a, existing_match, **k: _outcome(existing_match, new, True)
        ),
    )
    service = ZoomSearchService()
    job = await service.enqueue("proj-1", 1)
    await _wait_terminal(service, job.id)

    assert job.status == "complete"
    assert job.applied is False
    assert "alternative" in job.message
    assert env.saved
    kept = next(m for m in env.saved[-1].matches if m.scene_index == 1)
    assert kept.episode == "EP07", "the manual edit must not be overwritten"
    assert kept.alternatives[0].episode == "EP03"
    assert kept.alternatives[0].algorithm == "zoom_search"


@pytest.mark.asyncio
async def test_scene_layout_change_cancels_apply(monkeypatch, tmp_path) -> None:
    env = _Env(monkeypatch, tmp_path)
    new = _match(1, "EP03", 400.0, 403.0)
    reshaped = SceneList(scenes=[_scene(0, 0.0, 5.0)])
    loads = {"count": 0}

    def load_scenes(pid):
        loads["count"] += 1
        return env.scenes if loads["count"] == 1 else reshaped

    monkeypatch.setattr(ProjectService, "load_scenes", staticmethod(load_scenes))
    monkeypatch.setattr(
        ZoomRematchService,
        "search_scene_sync",
        classmethod(
            lambda cls, *a, existing_match, **k: _outcome(existing_match, new, True)
        ),
    )
    service = ZoomSearchService()
    job = await service.enqueue("proj-1", 1)
    await _wait_terminal(service, job.id)
    assert job.status == "cancelled"
    assert "layout changed" in job.message
    assert not env.saved


@pytest.mark.asyncio
async def test_ack_and_prune_prefer_dropping_acknowledged(monkeypatch, tmp_path) -> None:
    _Env(monkeypatch, tmp_path)
    service = ZoomSearchService()
    from app.services.zoom_search_service import ZoomSearchJob

    for index in range(service.MAX_TERMINAL_JOBS + 5):
        job = ZoomSearchJob(
            project_id="proj-1",
            scene_index=index,
            status="complete",
            acknowledged=index < 10,
            created_at=float(index),
        )
        service._jobs[job.id] = job
    unseen_ids = {
        job_id for job_id, job in service._jobs.items() if not job.acknowledged
    }
    service._prune_terminal_jobs()
    assert len(service._jobs) == service.MAX_TERMINAL_JOBS
    assert unseen_ids <= set(service._jobs), "unseen alerts must survive pruning"

    victim = next(iter(unseen_ids))
    acked = service.ack(victim)
    assert acked is not None and acked.acknowledged is True
    assert service.ack("missing") is None


@pytest.mark.asyncio
async def test_pure_project_rejected(monkeypatch, tmp_path) -> None:
    env = _Env(monkeypatch, tmp_path)
    env.project.library_type = LibraryType.PURE
    service = ZoomSearchService()
    job = await service.enqueue("proj-1", 0)
    await _wait_terminal(service, job.id)
    assert job.status == "error"
    assert "pure" in (job.error or "").lower()
