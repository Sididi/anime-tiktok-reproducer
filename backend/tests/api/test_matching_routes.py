from __future__ import annotations

import json

import pytest

from app.api.routes.matching import (
    BatchUpdateMatchItem,
    BatchUpdateMatchesRequest,
    UpdateMatchRequest,
    merge_with_previous,
    undo_merge,
    update_match,
    update_matches_batch,
)
from app.models import MatchList, Project, Scene, SceneList, SceneMatch
from app.models.project import ProjectPhase
from app.services.anime_matcher import MatchProgress
from app.services.anime_library import AnimeLibraryService
from app.services.scene_merger import SceneMergerService
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


def _scene(index: int, start_time: float, end_time: float) -> Scene:
    return Scene(index=index, start_time=start_time, end_time=end_time)


def _match(
    scene_index: int,
    *,
    episode: str,
    start_time: float,
    end_time: float,
    confidence: float = 0.9,
    speed_ratio: float = 1.0,
    was_no_match: bool = False,
    merged_from: list[int] | None = None,
) -> SceneMatch:
    return SceneMatch(
        scene_index=scene_index,
        episode=episode,
        start_time=start_time,
        end_time=end_time,
        confidence=confidence,
        speed_ratio=speed_ratio,
        was_no_match=was_no_match,
        merged_from=merged_from,
    )


def _install_manual_merge_project_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    scenes: SceneList,
    matches: MatchList,
    project: Project,
):
    project_dir = tmp_path / project.id
    project_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "project": project,
        "scenes": scenes,
        "matches": matches,
    }

    monkeypatch.setattr(ProjectService, "get_project_dir", lambda project_id: project_dir)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: state["project"])
    monkeypatch.setattr(ProjectService, "load_scenes", lambda project_id: state["scenes"])
    monkeypatch.setattr(ProjectService, "load_matches", lambda project_id: state["matches"])
    monkeypatch.setattr(
        ProjectService,
        "save_scenes",
        lambda project_id, new_scenes: state.__setitem__("scenes", new_scenes),
    )
    monkeypatch.setattr(
        ProjectService,
        "save_matches",
        lambda project_id, new_matches: state.__setitem__("matches", new_matches),
    )
    monkeypatch.setattr(
        ProjectService,
        "save",
        lambda updated_project: state.__setitem__("project", updated_project),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type: tmp_path / "library"),
    )

    (tmp_path / "library").mkdir(parents=True, exist_ok=True)
    return state, project_dir


@pytest.mark.asyncio
async def test_merge_with_previous_rematches_only_new_scene_and_skips_pass2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")

    project = Project(
        id="proj-merge",
        library_type="anime",
        anime_name="Demo",
        video_path=str(video_path),
        phase=ProjectPhase.MATCH_VALIDATION,
    )
    scenes = SceneList(
        scenes=[
            _scene(0, 0.0, 1.0),
            _scene(1, 1.0, 2.0),
            _scene(2, 2.0, 3.5),
        ]
    )
    matches = MatchList(
        matches=[
            _match(0, episode="ep-a", start_time=10.0, end_time=11.0),
            _match(1, episode="ep-a", start_time=11.0, end_time=12.0),
            _match(2, episode="ep-b", start_time=20.0, end_time=21.5),
        ]
    )
    state, project_dir = _install_manual_merge_project_state(
        monkeypatch,
        tmp_path,
        scenes=scenes,
        matches=matches,
        project=project,
    )

    async def fake_match_scenes(
        video_path_arg,
        scenes_arg,
        source_path_arg,
        library_type_arg,
        anime_name=None,
        scene_indices_to_match=None,
        existing_matches=None,
        pass_label="",
    ):
        assert video_path_arg == video_path
        assert scenes_arg.scenes == [
            _scene(0, 0.0, 2.0),
            _scene(1, 2.0, 3.5),
        ]
        assert scene_indices_to_match == [0]
        assert existing_matches is not None
        assert existing_matches.matches[0].merged_from == [0, 1]
        assert existing_matches.matches[1].scene_index == 1
        assert existing_matches.matches[1].episode == "ep-b"

        rematched = existing_matches.model_copy(deep=True)
        rematched.matches[0].episode = "ep-rematched"
        rematched.matches[0].start_time = 100.0
        rematched.matches[0].end_time = 102.0
        rematched.matches[0].confidence = 0.97
        rematched.matches[0].speed_ratio = 1.0
        rematched.matches[0].was_no_match = False
        yield MatchProgress(status="starting", message="starting")
        yield MatchProgress(status="complete", matches=rematched)

    monkeypatch.setattr(
        "app.api.routes.matching.AnimeMatcherService.match_scenes",
        fake_match_scenes,
    )
    monkeypatch.setattr(
        "app.api.routes.matching.SceneMergerService.detect_continuous_pairs",
        classmethod(lambda cls, *args, **kwargs: (_ for _ in ()).throw(AssertionError("pass 2 should not run"))),
    )

    result = await merge_with_previous(project.id, 1)

    assert result["scenes"] == [
        {"index": 0, "start_time": 0.0, "end_time": 2.0, "duration": 2.0},
        {"index": 1, "start_time": 2.0, "end_time": 3.5, "duration": 1.5},
    ]
    assert [scene.index for scene in state["scenes"].scenes] == [0, 1]
    assert state["matches"].matches[0].merged_from == [0, 1]
    assert state["matches"].matches[0].episode == "ep-rematched"
    assert state["matches"].matches[1].scene_index == 1
    assert state["matches"].matches[1].episode == "ep-b"
    assert state["project"].phase == ProjectPhase.MATCH_VALIDATION
    assert (project_dir / "pre_merge_backup.json").exists()


def test_prepare_manual_merge_with_previous_flattens_existing_merged_from(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    project_id = "proj-provenance"
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ProjectService, "get_project_dir", lambda pid: project_dir)

    original_scenes = SceneList(
        scenes=[
            _scene(0, 0.0, 1.0),
            _scene(1, 1.0, 2.0),
            _scene(2, 2.0, 3.0),
        ]
    )
    original_matches = MatchList(
        matches=[
            _match(0, episode="ep-a", start_time=10.0, end_time=11.0),
            _match(1, episode="ep-a", start_time=11.0, end_time=12.0),
            _match(2, episode="ep-b", start_time=20.0, end_time=21.0),
        ]
    )
    SceneMergerService.save_pre_merge_backup(
        project_id,
        {
            "scenes": [scene.model_dump() for scene in original_scenes.scenes],
            "matches": [match.model_dump() for match in original_matches.matches],
            "chains": [[0, 1]],
        },
    )

    current_scenes = SceneList(
        scenes=[
            _scene(0, 0.0, 2.0),
            _scene(1, 2.0, 3.0),
        ]
    )
    current_matches = MatchList(
        matches=[
            _match(
                0,
                episode="ep-merged",
                start_time=50.0,
                end_time=52.0,
                confidence=0.95,
                merged_from=[0, 1],
            ),
            _match(1, episode="ep-b", start_time=20.0, end_time=21.0),
        ]
    )

    merged_scenes, merged_matches, backup, merged_scene_index = (
        SceneMergerService.prepare_manual_merge_with_previous(
            project_id,
            1,
            current_scenes,
            current_matches,
        )
    )

    assert merged_scene_index == 0
    assert merged_scenes.scenes == [_scene(0, 0.0, 3.0)]
    assert merged_matches.matches[0].merged_from == [0, 1, 2]
    assert backup["matches"][2]["episode"] == "ep-b"


@pytest.mark.asyncio
async def test_manual_merge_then_undo_restores_original_scenes_and_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")

    project = Project(
        id="proj-undo",
        library_type="anime",
        anime_name="Demo",
        video_path=str(video_path),
        phase=ProjectPhase.MATCH_VALIDATION,
    )
    original_scenes = SceneList(
        scenes=[
            _scene(0, 0.0, 1.0),
            _scene(1, 1.0, 2.0),
            _scene(2, 2.0, 3.0),
        ]
    )
    original_matches = MatchList(
        matches=[
            _match(0, episode="ep-a", start_time=10.0, end_time=11.0),
            _match(1, episode="ep-a", start_time=11.0, end_time=12.0),
            _match(2, episode="ep-b", start_time=20.0, end_time=21.0),
        ]
    )
    state, _ = _install_manual_merge_project_state(
        monkeypatch,
        tmp_path,
        scenes=original_scenes,
        matches=original_matches,
        project=project,
    )

    async def fake_match_scenes(*args, scene_indices_to_match=None, existing_matches=None, **kwargs):
        assert scene_indices_to_match == [0]
        rematched = existing_matches.model_copy(deep=True)
        rematched.matches[0].episode = "ep-rematched"
        rematched.matches[0].start_time = 100.0
        rematched.matches[0].end_time = 102.0
        rematched.matches[0].confidence = 0.99
        rematched.matches[0].was_no_match = False
        yield MatchProgress(status="complete", matches=rematched)

    monkeypatch.setattr(
        "app.api.routes.matching.AnimeMatcherService.match_scenes",
        fake_match_scenes,
    )

    await merge_with_previous(project.id, 1)
    restored = await undo_merge(project.id, 0)

    assert [scene["index"] for scene in restored["scenes"]] == [0, 1, 2]
    assert [scene["duration"] for scene in restored["scenes"]] == [1.0, 1.0, 1.0]
    assert [match["episode"] for match in restored["matches"]] == [
        "ep-a",
        "ep-a",
        "ep-b",
    ]
    assert [scene.index for scene in state["scenes"].scenes] == [0, 1, 2]
    assert [match.scene_index for match in state["matches"].matches] == [0, 1, 2]


@pytest.mark.asyncio
async def test_manual_merge_refreshes_backup_for_individual_scene_before_undo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")

    project = Project(
        id="proj-backup-refresh",
        library_type="anime",
        anime_name="Demo",
        video_path=str(video_path),
        phase=ProjectPhase.MATCH_VALIDATION,
    )
    current_scenes = SceneList(
        scenes=[
            _scene(0, 0.0, 2.0),
            _scene(1, 2.0, 3.0),
        ]
    )
    current_matches = MatchList(
        matches=[
            _match(
                0,
                episode="ep-merged",
                start_time=40.0,
                end_time=42.0,
                confidence=0.95,
                merged_from=[0, 1],
            ),
            _match(1, episode="ep-updated", start_time=60.0, end_time=61.0),
        ]
    )
    state, project_dir = _install_manual_merge_project_state(
        monkeypatch,
        tmp_path,
        scenes=current_scenes,
        matches=current_matches,
        project=project,
    )

    stale_backup = {
        "scenes": [
            _scene(0, 0.0, 1.0).model_dump(),
            _scene(1, 1.0, 2.0).model_dump(),
            _scene(2, 2.0, 3.0).model_dump(),
        ],
        "matches": [
            _match(0, episode="ep-a", start_time=10.0, end_time=11.0).model_dump(),
            _match(1, episode="ep-a", start_time=11.0, end_time=12.0).model_dump(),
            _match(2, episode="ep-stale", start_time=20.0, end_time=21.0).model_dump(),
        ],
        "chains": [[0, 1]],
    }
    (project_dir / "pre_merge_backup.json").write_text(json.dumps(stale_backup, indent=2))

    async def fake_match_scenes(*args, scene_indices_to_match=None, existing_matches=None, **kwargs):
        assert scene_indices_to_match == [0]
        rematched = existing_matches.model_copy(deep=True)
        rematched.matches[0].episode = "ep-rematched"
        rematched.matches[0].start_time = 100.0
        rematched.matches[0].end_time = 103.0
        rematched.matches[0].confidence = 0.99
        rematched.matches[0].was_no_match = False
        yield MatchProgress(status="complete", matches=rematched)

    monkeypatch.setattr(
        "app.api.routes.matching.AnimeMatcherService.match_scenes",
        fake_match_scenes,
    )

    await merge_with_previous(project.id, 1)
    restored = await undo_merge(project.id, 0)

    assert [match["episode"] for match in restored["matches"]] == [
        "ep-a",
        "ep-a",
        "ep-updated",
    ]
    saved_backup = json.loads((project_dir / "pre_merge_backup.json").read_text())
    assert saved_backup["matches"][2]["episode"] == "ep-updated"
