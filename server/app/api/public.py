"""Public avatar serving. No auth."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/avatars")


@router.get("/{filename}")
async def get_avatar(filename: str, request: Request) -> FileResponse:
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(400, "Invalid filename")
    avatars_dir: Path = request.app.state.settings.avatars_dir
    try:
        resolved = (avatars_dir / filename).resolve()
        avatars_root = avatars_dir.resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "Invalid path") from None
    if avatars_root not in resolved.parents:
        raise HTTPException(400, "Invalid path")
    if not resolved.is_file():
        raise HTTPException(404, "Avatar not found")
    mime, _ = mimetypes.guess_type(resolved.name)
    return FileResponse(
        resolved,
        media_type=mime or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
