from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...models import Project, ProjectPhase
from ...services import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    tiktok_url: str | None = None
    source_path: str | None = None
    anime_name: str | None = None


class UpdateProjectRequest(BaseModel):
    anime_name: str | None = None


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
        )


@router.post("", response_model=ProjectResponse)
async def create_project(request: CreateProjectRequest) -> ProjectResponse:
    """Create a new project."""
    project = ProjectService.create(
        tiktok_url=request.tiktok_url,
        source_path=request.source_path,
        anime_name=request.anime_name,
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

    ProjectService.save(project)
    return ProjectResponse.from_project(project)
