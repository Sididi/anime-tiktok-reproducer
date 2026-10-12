"""One archive gate for every project API, including media and scheduling."""

from fastapi import HTTPException, Request

from ..services.project_service import ArchivedProjectError, ProjectService


async def require_active_project(request: Request) -> None:
    project_id = request.path_params.get("project_id")
    if not project_id:
        return
    # The manager's local metadata row is the only project-scoped archive API.
    if request.method == "GET" and request.url.path == f"/api/project-manager/projects/{project_id}/row":
        return
    project = await ProjectService.aload(project_id)
    if project is not None and project.archived_at:
        raise HTTPException(status_code=410, detail={
            "code": "project_archived",
            "message": str(ArchivedProjectError()),
            "project_id": project_id,
        })
