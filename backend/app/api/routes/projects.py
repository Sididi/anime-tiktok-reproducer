from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from ...library_types import DEFAULT_LIBRARY_TYPE, LibraryType
from ...models import Project, ProjectPhase
from ...services import LibraryHydrationService, ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    tiktok_url: str | None = None
    source_path: str | None = None
    anime_name: str | None = None
    series_id: str | None = None
    library_type: LibraryType = DEFAULT_LIBRARY_TYPE


class UpdateProjectRequest(BaseModel):
    anime_name: str | None = None
    series_id: str | None = None
    library_type: LibraryType | None = None


class ProjectResponse(BaseModel):
    id: str
    tiktok_url: str | None
    source_paths: list[str]
    phase: ProjectPhase
    created_at: str
    updated_at: str
    video_path: str | None
    video_duration: float | None
    video_fps: float | None
    anime_name: str | None
    series_id: str | None
    library_type: LibraryType
    output_language: str | None
    drive_folder_id: str | None
    drive_folder_url: str | None
    generation_discord_message_id: str | None
    final_upload_discord_message_id: str | None
    upload_completed_at: str | None
    upload_last_result: dict[str, Any] | None

    @classmethod
    def from_project(cls, project: Project) -> "ProjectResponse":
        return cls(
            id=project.id,
            tiktok_url=project.tiktok_url,
            source_paths=project.source_paths,
            phase=project.phase,
            created_at=project.created_at.isoformat(),
            updated_at=project.updated_at.isoformat(),
            video_path=project.video_path,
            video_duration=project.video_duration,
            video_fps=project.video_fps,
            anime_name=project.anime_name,
            series_id=project.series_id,
            library_type=project.library_type,
            output_language=project.output_language,
            drive_folder_id=project.drive_folder_id,
            drive_folder_url=project.drive_folder_url,
            generation_discord_message_id=project.generation_discord_message_id,
            final_upload_discord_message_id=project.final_upload_discord_message_id,
            upload_completed_at=project.upload_completed_at.isoformat() if project.upload_completed_at else None,
            upload_last_result=project.upload_last_result,
        )


@router.post("", response_model=ProjectResponse)
async def create_project(request: CreateProjectRequest) -> ProjectResponse:
    """Create a new project."""
    project = ProjectService.create(
        tiktok_url=request.tiktok_url,
        source_path=request.source_path,
        anime_name=request.anime_name,
        series_id=request.series_id,
        library_type=request.library_type,
    )
    return ProjectResponse.from_project(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects() -> list[ProjectResponse]:
    """List all projects."""
    projects = ProjectService.list_all()
    return [ProjectResponse.from_project(p) for p in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str) -> ProjectResponse:
    """Get a project by ID."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse.from_project(project)


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> dict:
    """Delete a project."""
    if not ProjectService.delete(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"status": "deleted"}


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, request: UpdateProjectRequest) -> ProjectResponse:
    """Update a project's settings."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if request.anime_name is not None:
        project.anime_name = request.anime_name
    if request.series_id is not None:
        project.series_id = request.series_id
    if request.library_type is not None:
        project.library_type = request.library_type

    ProjectService.save(project)
    ProjectService.sync_project_pin(project)
    return ProjectResponse.from_project(project)


@router.post("/{project_id}/library/activate")
async def activate_project_library(project_id: str) -> dict[str, Any]:
    """Activate the selected series locally before matching."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.series_id:
        raise HTTPException(status_code=400, detail="Project does not have a selected series_id")

    try:
        return await LibraryHydrationService.activate_project_series(
            project_id=project.id,
            library_type=project.library_type,
            series_id=project.series_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{project_id}/library/activation")
async def get_project_library_activation(project_id: str) -> dict[str, Any]:
    """Get current activation/hydration state for the project's selected series."""
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.series_id:
        raise HTTPException(status_code=400, detail="Project does not have a selected series_id")

    return await LibraryHydrationService.get_activation_state(
        library_type=project.library_type,
        series_id=project.series_id,
    )
