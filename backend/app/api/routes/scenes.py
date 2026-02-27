from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import json
from pathlib import Path

from ...config import settings
from ...models import Scene, SceneList, ProjectPhase
from ...services import ProjectService, SceneDetectorService

router = APIRouter(prefix="/projects/{project_id}/scenes", tags=["scenes"])


class SceneResponse(BaseModel):
    index: int
    start_time: float
    end_time: float
    duration: float


class ScenesResponse(BaseModel):
    scenes: list[SceneResponse]


class UpdateScenesRequest(BaseModel):
    scenes: list[Scene]


class SplitSceneRequest(BaseModel):
    timestamp: float


class MergeScenesRequest(BaseModel):
    scene_indices: list[int]


def to_response(scenes: SceneList) -> ScenesResponse:
    return ScenesResponse(
        scenes=[
            SceneResponse(
                index=s.index,
                start_time=s.start_time,
                end_time=s.end_time,
                duration=s.duration,
            )
            for s in scenes.scenes
        ]
    )


@router.get("/config")
async def get_scenes_config(project_id: str):
    """Get scenes validation feature flags."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return {"skip_ui_enabled": settings.scenes_skip_ui_enabled}


@router.get("", response_model=ScenesResponse)
async def get_scenes(project_id: str) -> ScenesResponse:
    """Get all scenes for a project."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    scenes = ProjectService.load_scenes(project_id)
    if not scenes:
        return ScenesResponse(scenes=[])

    return to_response(scenes)


@router.put("", response_model=ScenesResponse)
async def update_scenes(project_id: str, request: UpdateScenesRequest) -> ScenesResponse:
    """Update all scenes (bulk update)."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    scene_list = SceneList(scenes=request.scenes)
    scene_list.renumber()

    if not scene_list.validate_continuity():
        raise HTTPException(status_code=400, detail="Scenes must be continuous with no gaps")

    ProjectService.save_scenes(project_id, scene_list)
    return to_response(scene_list)


@router.post("/{scene_index}/split", response_model=ScenesResponse)
async def split_scene(project_id: str, scene_index: int, request: SplitSceneRequest) -> ScenesResponse:
    """Split a scene at the given timestamp."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    scenes = ProjectService.load_scenes(project_id)
    if not scenes:
        raise HTTPException(status_code=404, detail="No scenes found")

    if scene_index < 0 or scene_index >= len(scenes.scenes):
        raise HTTPException(status_code=400, detail="Invalid scene index")

    scene = scenes.scenes[scene_index]
    split_time = request.timestamp

    if split_time <= scene.start_time or split_time >= scene.end_time:
        raise HTTPException(status_code=400, detail="Split point must be within scene boundaries")

    # Create two scenes from one
    new_scene = Scene(index=scene_index + 1, start_time=split_time, end_time=scene.end_time)
    scene.end_time = split_time
    scenes.scenes.insert(scene_index + 1, new_scene)
    scenes.renumber()

    ProjectService.save_scenes(project_id, scenes)
    return to_response(scenes)


@router.post("/merge", response_model=ScenesResponse)
async def merge_scenes(project_id: str, request: MergeScenesRequest) -> ScenesResponse:
    """Merge adjacent scenes."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    scenes = ProjectService.load_scenes(project_id)
    if not scenes:
        raise HTTPException(status_code=404, detail="No scenes found")

    indices = sorted(request.scene_indices)
    if len(indices) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 scenes to merge")

    # Check indices are adjacent
    for i in range(1, len(indices)):
        if indices[i] != indices[i - 1] + 1:
            raise HTTPException(status_code=400, detail="Scenes must be adjacent")

    # Check bounds
    if indices[0] < 0 or indices[-1] >= len(scenes.scenes):
        raise HTTPException(status_code=400, detail="Invalid scene indices")

    # Merge: extend first scene to end of last, remove others
    first_scene = scenes.scenes[indices[0]]
    last_scene = scenes.scenes[indices[-1]]
    first_scene.end_time = last_scene.end_time

    # Remove merged scenes (except first)
    for i in reversed(indices[1:]):
        scenes.scenes.pop(i)

    scenes.renumber()
    ProjectService.save_scenes(project_id, scenes)
    return to_response(scenes)


class DetectScenesRequest(BaseModel):
    threshold: float = 18.0
    min_scene_len: int = 10


@router.post("/detect")
async def detect_scenes(project_id: str, request: DetectScenesRequest | None = None):
    """Auto-detect scenes using PySceneDetect and stream progress."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if not project.video_path:
        raise HTTPException(status_code=400, detail="No video available")

    video_path = Path(project.video_path)
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    # Update phase
    project.phase = ProjectPhase.SCENE_DETECTION
    ProjectService.save(project)

    threshold = request.threshold if request else 18.0
    min_scene_len = request.min_scene_len if request else 10

    async def stream_progress():
        async for progress in SceneDetectorService.detect_scenes(
            video_path, threshold, min_scene_len
        ):
            yield f"data: {json.dumps(progress.to_dict())}\n\n"

            if progress.status == "complete" and progress.scenes:
                # Save detected scenes
                scene_list = SceneList(scenes=progress.scenes)
                ProjectService.save_scenes(project_id, scene_list)

                # Update phase
                project.phase = ProjectPhase.SCENE_VALIDATION
                ProjectService.save(project)

            elif progress.status == "error":
                project.phase = ProjectPhase.SETUP
                ProjectService.save(project)

    return StreamingResponse(
        stream_progress(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
