# backend/app/api/routes/lan_transfer.py
"""LAN transfer endpoints for the Premiere Pro CEP panel (spec:
docs/superpowers/specs/2026-07-05-lan-transfer-design.md)."""
from __future__ import annotations

import asyncio
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response

from ...config import settings
from ...services.lan_transfer_service import LanTransferService
from ...services.project_service import ProjectService

logger = logging.getLogger(__name__)

API_VERSION = LanTransferService.API_VERSION


def require_lan_token(x_atr_lan_token: str | None = Header(default=None)) -> None:
    expected = settings.lan_transfer_token
    if not expected:
        raise HTTPException(status_code=503, detail="LAN transfer not configured")
    if not x_atr_lan_token or not hmac.compare_digest(x_atr_lan_token, expected):
        raise HTTPException(status_code=401, detail="Invalid LAN token")


router = APIRouter(prefix="/lan", tags=["lan-transfer"], dependencies=[Depends(require_lan_token)])


@router.get("/ping")
async def ping():
    return {"ok": True, "api_version": API_VERSION}


def _load_project_or_404(project_id: str):
    project = ProjectService.load(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/manifest")
async def get_manifest(project_id: str):
    project = await asyncio.to_thread(_load_project_or_404, project_id)
    try:
        return await asyncio.to_thread(LanTransferService.build_manifest_payload, project)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/projects/{project_id}/files/{relative_path:path}")
async def download_manifest_file(project_id: str, relative_path: str):
    project = await asyncio.to_thread(_load_project_or_404, project_id)
    entry = await asyncio.to_thread(LanTransferService.resolve_entry, project, relative_path)
    if entry is None:
        raise HTTPException(status_code=404, detail="File not in project manifest")
    if entry.source_path is not None:
        return FileResponse(path=entry.source_path, filename=entry.source_path.name)
    return Response(content=entry.inline_content or b"", media_type=entry.mime_type or "application/octet-stream")


@router.post("/projects/{project_id}/outputs/{filename}")
async def upload_output(project_id: str, filename: str, request: Request, background_tasks: BackgroundTasks):
    await asyncio.to_thread(_load_project_or_404, project_id)
    if not LanTransferService.is_allowed_output_filename(filename):
        raise HTTPException(status_code=422, detail="Filename not allowed")
    destination = await LanTransferService.receive_output_stream(project_id, filename, request.stream())
    size = destination.stat().st_size
    logger.info("LAN output received: project=%s file=%s bytes=%d", project_id, filename, size)
    background_tasks.add_task(LanTransferService.relay_output_to_drive, project_id, destination)
    return {"ok": True, "filename": filename, "size": size}
