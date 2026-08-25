"""ProjectService async twins, per-project edit lock, atomic writes, pre-filter."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import MatchList, Project, Scene, SceneList, SceneMatch
from app.services import atomic_files
from app.services.atomic_files import write_text_atomic
from app.services.project_locks import ProjectLocks
from app.services.project_service import ProjectService


@pytest.fixture
def projects_dir(tmp_path: Path, monkeypatch) -> Path:
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", pdir)
    monkeypatch.setattr(
        "app.services.library_state_db.settings.library_state_db_path",
        tmp_path / "library_state.db",
    )
    from app.services.library_state_db import LibraryStateDb

    LibraryStateDb.initialize()
    ProjectLocks.reset()
    yield pdir
    ProjectLocks.reset()


def _project(project_id: str, **fields) -> Project:
    project = Project(id=project_id, anime_name="Show", **fields)
    ProjectService.get_project_dir(project_id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return project


# ---------------------------------------------------------------- atomic writes


def test_write_text_atomic_replaces_and_leaves_no_temp(tmp_path: Path):
    target = tmp_path / "file.json"
    target.write_text("old")
    write_text_atomic(target, "new")
    assert target.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["file.json"]


def test_write_text_atomic_failure_keeps_old_content_and_cleans_up(tmp_path: Path, monkeypatch):
    target = tmp_path / "file.json"
    target.write_text("old")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_files.os, "replace", boom)
    with pytest.raises(OSError):
        write_text_atomic(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["file.json"]


def test_write_text_atomic_retries_permission_error(tmp_path: Path, monkeypatch):
    target = tmp_path / "file.json"
    calls = {"n": 0}
    real_replace = atomic_files.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("reader holds the file")
        real_replace(src, dst)

    monkeypatch.setattr(atomic_files.os, "replace", flaky)
    monkeypatch.setattr(atomic_files.time, "sleep", lambda _s: None)
    write_text_atomic(target, "new")
    assert target.read_text() == "new"
    assert calls["n"] == 3


def test_project_saves_are_atomic(projects_dir: Path):
    project = _project("p1")
    ProjectService.save_matches(
        "p1",
        MatchList(matches=[SceneMatch(scene_index=0, episode="EP01", start_time=1.0, end_time=2.0, confidence=0.9, speed_ratio=1.0)]),
    )
    ProjectService.save_scenes("p1", SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=1.0)]))
    names = sorted(p.name for p in ProjectService.get_project_dir("p1").iterdir())
    assert names == ["matches.json", "project.json", "scenes.json"]
    assert ProjectService.load("p1").id == project.id


# ------------------------------------------------------------------ async twins


@pytest.mark.asyncio
async def test_async_twins_round_trip(projects_dir: Path):
    _project("p1")
    loaded = await ProjectService.aload("p1")
    assert loaded is not None and loaded.id == "p1"
    loaded.anime_name = "Renamed"
    await ProjectService.asave(loaded)
    assert (await ProjectService.aload("p1")).anime_name == "Renamed"
    assert await ProjectService.aload("missing") is None

    matches = MatchList(
        matches=[SceneMatch(scene_index=0, episode="EP01", start_time=1.0, end_time=2.0, confidence=0.9, speed_ratio=1.0)]
    )
    await ProjectService.asave_matches("p1", matches)
    assert (await ProjectService.aload_matches("p1")).matches[0].episode == "EP01"
    assert await ProjectService.aload_matches("missing") is None

    scenes = SceneList(scenes=[Scene(index=0, start_time=0.0, end_time=1.0)])
    await ProjectService.asave_scenes("p1", scenes)
    assert len((await ProjectService.aload_scenes("p1")).scenes) == 1

    _project("p2")
    assert {p.id for p in await ProjectService.alist_all()} == {"p1", "p2"}


# -------------------------------------------------------------------- edit lock


@pytest.mark.asyncio
async def test_edit_lock_serialises_concurrent_read_modify_write(projects_dir: Path):
    _project("p1", source_paths=[])

    async def append_source(tag: str) -> None:
        async with ProjectService.edit_lock("p1"):
            project = await ProjectService.aload("p1")
            await asyncio.sleep(0.01)  # widen the window between load and save
            project.source_paths = [*project.source_paths, tag]
            await ProjectService.asave(project)

    await asyncio.gather(*(append_source(f"s{i}") for i in range(5)))
    final = await ProjectService.aload("p1")
    assert sorted(final.source_paths) == [f"s{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_edit_lock_without_lock_loses_updates_demonstration(projects_dir: Path):
    # Documents WHY the lock exists: the same interleaving without it drops writes.
    _project("p1", source_paths=[])

    async def append_source(tag: str) -> None:
        project = await ProjectService.aload("p1")
        await asyncio.sleep(0.01)
        project.source_paths = [*project.source_paths, tag]
        await ProjectService.asave(project)

    await asyncio.gather(*(append_source(f"s{i}") for i in range(5)))
    final = await ProjectService.aload("p1")
    assert len(final.source_paths) < 5


@pytest.mark.asyncio
async def test_edit_lock_reentry_from_same_task_raises(projects_dir: Path):
    async with ProjectLocks.hold("p1"):
        assert ProjectLocks.is_held("p1")
        with pytest.raises(RuntimeError, match="re-entered"):
            async with ProjectLocks.hold("p1"):
                pass
    assert not ProjectLocks.is_held("p1")


@pytest.mark.asyncio
async def test_edit_lock_child_task_after_release_is_not_a_false_reentry(projects_dir: Path):
    async def child():
        async with ProjectLocks.hold("p1"):
            return "ok"

    async with ProjectLocks.hold("p1"):
        task = asyncio.create_task(child())
        await asyncio.sleep(0)  # child starts (and blocks on the lock) while we hold it
    assert await task == "ok"


@pytest.mark.asyncio
async def test_edit_lock_is_per_project(projects_dir: Path):
    async with ProjectLocks.hold("p1"):
        async with ProjectLocks.hold("p2"):
            assert ProjectLocks.is_held("p1") and ProjectLocks.is_held("p2")


# ------------------------------------------------------------------- pre-filter


def test_list_with_reschedule_pending_only_returns_pending_projects(projects_dir: Path):
    _project("idle")
    _project("pending", reschedule_pending={"tiktok": {"retries": 1}})
    assert {p.id for p in ProjectService.list_all()} == {"idle", "pending"}
    assert [p.id for p in ProjectService.list_with_reschedule_pending()] == ["pending"]

    # A corrupt project file must not break the minute loop.
    (projects_dir / "broken").mkdir()
    (projects_dir / "broken" / "project.json").write_text("{not json")
    assert [p.id for p in ProjectService.list_with_reschedule_pending()] == ["pending"]
