from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import require_internal_token

router = APIRouter(prefix="/api/internal/facecam", dependencies=[Depends(require_internal_token)])
DriveId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=200)]


class Registration(BaseModel):
    anime_title: str = Field(max_length=400)
    folder_id: DriveId
    zip_id: DriveId
    zip_name: str = Field(max_length=400)


@router.get("/health")
async def health(request: Request):
    try:
        return await request.app.state.facecam.preflight()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@router.put("/tasks/{project_id}")
async def register(project_id: str, payload: Registration, request: Request):
    try:
        return await request.app.state.facecam.register(project_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/tasks/{project_id}")
async def get_task(project_id: str, request: Request):
    try:
        return request.app.state.facecam.get(project_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, "Tâche Facecam introuvable.") from exc


@router.post("/tasks/{project_id}/reopen")
async def reopen(project_id: str, request: Request):
    try:
        return await request.app.state.facecam.reopen(project_id)
    except KeyError as exc:
        raise HTTPException(404, "Tâche Facecam introuvable.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
