from pathlib import Path

from app.api.routes.raw_scenes import _merge_invalidated_scenes, _persist_detection_after_validation
from app.models.raw_scene import RawSceneCandidate, RawSceneDetectionResult
from app.models.transcription import SceneTranscription
from app.services import ProjectService


def _write_detection_file(project_dir: Path, detection: RawSceneDetectionResult) -> None:
    (project_dir / "raw_scene_detection.json").write_text(
        detection.model_dump_json(indent=2)
    )


def test_persist_detection_removes_invalidated_candidates_and_reindexes(monkeypatch, tmp_path):
    project_id = "p1"
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True)

    monkeypatch.setattr(ProjectService, "get_project_dir", lambda _project_id: project_dir)

    detection = RawSceneDetectionResult(
        has_raw_scenes=True,
        candidates=[
            RawSceneCandidate(
                scene_index=1,
                start_time=10.0,
                end_time=20.0,
                confidence=0.9,
                reason="non_tts_speaker",
            ),
            RawSceneCandidate(
                scene_index=2,
                start_time=20.0,
                end_time=30.0,
                confidence=0.8,
                reason="non_tts_speaker",
            ),
        ],
    )
    _write_detection_file(project_dir, detection)

    # Scene 1 was invalidated and merged away; remaining scenes were reindexed.
    updated_scenes = [
        SceneTranscription(
            scene_index=0,
            text="tts",
            words=[],
            start_time=0.0,
            end_time=20.0,
            is_raw=False,
        ),
        SceneTranscription(
            scene_index=1,
            text="",
            words=[],
            start_time=20.0,
            end_time=30.0,
            is_raw=True,
        ),
    ]

    _persist_detection_after_validation(
        project_id=project_id,
        invalidated_raw_indices={1},
        updated_scenes=updated_scenes,
    )

    persisted = RawSceneDetectionResult.model_validate_json(
        (project_dir / "raw_scene_detection.json").read_text()
    )

    assert persisted.has_raw_scenes is True
    assert len(persisted.candidates) == 1
    assert persisted.candidates[0].start_time == 20.0
    assert persisted.candidates[0].end_time == 30.0
    assert persisted.candidates[0].scene_index == 1


def test_persist_detection_sets_has_raw_false_when_all_invalidated(monkeypatch, tmp_path):
    project_id = "p2"
    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True)

    monkeypatch.setattr(ProjectService, "get_project_dir", lambda _project_id: project_dir)

    detection = RawSceneDetectionResult(
        has_raw_scenes=True,
        candidates=[
            RawSceneCandidate(
                scene_index=0,
                start_time=0.0,
                end_time=5.0,
                confidence=1.0,
                reason="empty_no_tts",
            )
        ],
    )
    _write_detection_file(project_dir, detection)

    _persist_detection_after_validation(
        project_id=project_id,
        invalidated_raw_indices={0},
        updated_scenes=[],
    )

    persisted = RawSceneDetectionResult.model_validate_json(
        (project_dir / "raw_scene_detection.json").read_text()
    )

    assert persisted.has_raw_scenes is False
    assert persisted.candidates == []


def test_merge_invalidated_scenes_keeps_user_text_scene_and_only_merges_empty():
    scenes = [
        SceneTranscription(
            scene_index=0,
            text="Hello",
            words=[],
            start_time=0.0,
            end_time=1.0,
            is_raw=False,
        ),
        SceneTranscription(
            scene_index=1,
            text="",
            words=[],
            start_time=1.0,
            end_time=2.0,
            is_raw=False,
        ),
        SceneTranscription(
            scene_index=2,
            text="world",
            words=[],
            start_time=2.0,
            end_time=3.0,
            is_raw=False,
        ),
    ]

    merged = _merge_invalidated_scenes(scenes)

    assert len(merged) == 2
    assert merged[0].scene_index == 0
    assert merged[0].start_time == 0.0
    assert merged[0].end_time == 2.0
    assert merged[0].text == "Hello"

    assert merged[1].scene_index == 1
    assert merged[1].start_time == 2.0
    assert merged[1].end_time == 3.0
    assert merged[1].text == "world"
