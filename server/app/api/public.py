"""Public asset serving. No auth."""
from __future__ import annotations

import contextlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.services.instagram_prepared_media import (
    prepared_media_path,
    validate_prepared_media_id,
    validate_prepared_media_token,
)

router = APIRouter(prefix="/api")


@router.get("/avatars/{filename}")
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


@router.api_route(
    "/instagram/prepared/{project_id}/{token}.mp4",
    methods=["GET", "HEAD"],
)
async def get_prepared_instagram_video(
    project_id: str,
    token: str,
    request: Request,
) -> FileResponse:
    try:
        validate_prepared_media_id(project_id, label="project_id")
        validate_prepared_media_token(token)
    except ValueError:
        raise HTTPException(404, "Prepared media not found") from None

    root = request.app.state.settings.data_dir / "instagram-prepared"
    path = prepared_media_path(root, project_id, token)
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError):
        raise HTTPException(404, "Prepared media not found") from None
    if root_resolved not in resolved.parents or not resolved.is_file():
        raise HTTPException(404, "Prepared media not found")

    return FileResponse(
        resolved,
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.api_route("/videos/{project_id}", methods=["GET", "HEAD"])
async def get_job_video(project_id: str, request: Request) -> Response:
    """Proxy a job video through this server for Meta Graph URL ingestion.

    Instagram's `video_url` ingestion is less tolerant of Google Drive download
    URLs than browsers are. Serving the already-public Drive asset from the VPS
    gives Meta a stable URL on our domain and lets us forward range requests.
    """
    store = request.app.state.job_store
    job = await store.get(project_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)
    headers = {}
    if range_header := request.headers.get("range"):
        headers["Range"] = range_header

    try:
        upstream = await client.send(
            client.build_request(request.method, job.drive_video_url, headers=headers),
            stream=request.method != "HEAD",
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"Video upstream fetch failed: {exc}") from exc

    if upstream.status_code >= 400:
        body = ""
        if request.method != "HEAD":
            with contextlib.suppress(httpx.HTTPError):
                body = (await upstream.aread()).decode("utf-8", errors="replace").strip()
        host = urlparse(job.drive_video_url).netloc or "upstream"
        await upstream.aclose()
        await client.aclose()
        suffix = f": {body[:300]}" if body else ""
        raise HTTPException(
            502,
            f"Video upstream {host} returned HTTP {upstream.status_code}{suffix}",
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower()
        in {
            "accept-ranges",
            "content-length",
            "content-range",
            "etag",
            "last-modified",
        }
    }
    response_headers["Cache-Control"] = "public, max-age=3600"
    media_type = upstream.headers.get("content-type") or "video/mp4"

    if request.method == "HEAD":
        await upstream.aclose()
        await client.aclose()
        return Response(
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=media_type,
        )

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=media_type,
    )
