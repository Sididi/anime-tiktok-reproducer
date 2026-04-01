from __future__ import annotations

import pytest

from app.api.routes.scenes import DetectScenesRequest, detect_scenes
from app.models import Project, Scene
from app.services.project_service import ProjectService
from app.services.scene_detector import SceneDetectionProgress, SceneDetectorService


def _chunk_text(chunk: str | bytes) -> str:
    return chunk.decode() if isinstance(chunk, bytes) else chunk


@pytest.mark.asyncio
async def test_detect_scenes_persists_before_emitting_complete_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video")

    project = Project(id="proj123", video_path=str(video_path))
    call_order: list[str] = []

    monkeypatch.setattr(ProjectService, "load", lambda project_id: project)
    monkeypatch.setattr(
        ProjectService,
        "save",
        lambda saved_project: call_order.append(f"save:{saved_project.phase.value}"),
    )
    monkeypatch.setattr(
        ProjectService,
        "save_scenes",
        lambda project_id, scenes: call_order.append(
            f"save_scenes:{len(scenes.scenes)}"
        ),
    )

    async def fake_detect(
        cls,
        video_path_arg,
        threshold,
        min_scene_len,
    ):
        yield SceneDetectionProgress("starting", 0.0, "Opening video...")
        yield SceneDetectionProgress(
            "complete",
            1.0,
            "Detected 1 scenes",
            [Scene(index=0, start_time=0.0, end_time=1.0)],
        )

    monkeypatch.setattr(
        SceneDetectorService,
        "detect_scenes",
        classmethod(fake_detect),
    )

    response = await detect_scenes("proj123", DetectScenesRequest())
    body_iter = response.body_iterator

    first_chunk = _chunk_text(await anext(body_iter))
    assert '"status": "starting"' in first_chunk
    assert call_order == ["save:scene_detection"]

    second_chunk = _chunk_text(await anext(body_iter))
    assert '"status": "complete"' in second_chunk
    assert call_order == [
        "save:scene_detection",
        "save_scenes:1",
        "save:scene_validation",
    ]

    with pytest.raises(StopAsyncIteration):
        await anext(body_iter)
