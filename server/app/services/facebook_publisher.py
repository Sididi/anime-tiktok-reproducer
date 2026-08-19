"""Long-range Facebook scheduling (2026-08 upload flows redesign).

Meta's Reels API only accepts scheduled_publish_time within ~29 days of the
request. Targets beyond that window are held on this server and converted at
T - 28 days:

- CREATE hold (facebook_payload without video_id): download the backend's
  prepared video and upload it as a native scheduled post (3-phase Reels API,
  video_state=SCHEDULED) — a slim async port of the backend's
  SocialUploadService._upload_facebook_reel_scheduled happy path.
- RETIME hold (facebook_payload with video_id): the native post already
  exists (parked at a placeholder time inside the window by the backend);
  push its scheduled_publish_time to the real target.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 900.0
_DEFAULT_GRAPH_API_VERSION = "v25.0"


@dataclass
class FacebookHoldResult:
    success: bool
    video_id: str | None = None
    url: str | None = None
    detail: str | None = None


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body[:500] if body else f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}: {str(payload)[:500]}"


def _page_video_url(page_id: str, video_id: str) -> str:
    return f"https://www.facebook.com/{page_id}/videos/{video_id}"


def _graph_base(graph_api_version: str | None) -> str:
    return f"https://graph.facebook.com/{graph_api_version or _DEFAULT_GRAPH_API_VERSION}"


async def retime_facebook_scheduled_post(
    *,
    page_id: str,
    page_access_token: str,
    video_id: str,
    scheduled_at: datetime,
    graph_api_version: str | None = None,
) -> FacebookHoldResult:
    """Push an existing native scheduled post to its real target instant."""
    epoch = str(int(scheduled_at.astimezone(UTC).timestamp()))
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{_graph_base(graph_api_version)}/{video_id}",
                data={
                    "scheduled_publish_time": epoch,
                    "published": "false",
                    "access_token": page_access_token,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            return FacebookHoldResult(
                success=False, detail=f"retime: {_response_detail(e.response)}"
            )
        except httpx.HTTPError as e:
            return FacebookHoldResult(success=False, detail=f"retime: {e}")
    return FacebookHoldResult(
        success=True, video_id=video_id, url=_page_video_url(page_id, video_id)
    )


async def _download_video(client: httpx.AsyncClient, url: str) -> Path:
    fd, tmp = tempfile.mkstemp(prefix="fb-hold-", suffix=".mp4")
    os.close(fd)
    path = Path(tmp)
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                await response.aread()
            response.raise_for_status()
            with path.open("wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
        if path.stat().st_size <= 0:
            raise RuntimeError("downloaded video is empty")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _upload_binary_sync(
    *, upload_url: str, token: str, video_path: Path
) -> tuple[int, str]:
    file_size = video_path.stat().st_size
    with video_path.open("rb") as f:
        response = httpx.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(file_size),
            },
            content=f,
            timeout=httpx.Timeout(_UPLOAD_TIMEOUT_SECONDS, read=_UPLOAD_TIMEOUT_SECONDS),
            follow_redirects=True,
        )
    return response.status_code, response.text


async def create_facebook_scheduled_post(
    *,
    page_id: str,
    page_access_token: str,
    title: str,
    description: str,
    prepared_video_url: str,
    scheduled_at: datetime,
    graph_api_version: str | None = None,
) -> FacebookHoldResult:
    """3-phase Reels upload with video_state=SCHEDULED at the target instant.

    The video was fully prepared (strategy applied + validated) by the backend
    at upload time; this only moves bytes and finishes the session."""
    base = _graph_base(graph_api_version)
    epoch = str(int(scheduled_at.astimezone(UTC).timestamp()))
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
        try:
            video_path = await _download_video(client, prepared_video_url)
        except Exception as e:
            return FacebookHoldResult(success=False, detail=f"download: {e}")
        try:
            # Phase 1: start
            try:
                start = await client.post(
                    f"{base}/{page_id}/video_reels",
                    data={"upload_phase": "start", "access_token": page_access_token},
                )
                start.raise_for_status()
                start_payload: dict[str, Any] = start.json()
            except httpx.HTTPStatusError as e:
                return FacebookHoldResult(
                    success=False, detail=f"start: {_response_detail(e.response)}"
                )
            except (httpx.HTTPError, ValueError) as e:
                return FacebookHoldResult(success=False, detail=f"start: {e}")
            video_id = str(start_payload.get("video_id") or "")
            upload_url = str(start_payload.get("upload_url") or "")
            if not video_id or not upload_url:
                return FacebookHoldResult(
                    success=False, detail=f"start: missing video_id/upload_url: {start_payload}"
                )

            # Phase 2: binary upload
            try:
                status_code, body = await asyncio.to_thread(
                    _upload_binary_sync,
                    upload_url=upload_url,
                    token=page_access_token,
                    video_path=video_path,
                )
            except (httpx.HTTPError, OSError) as e:
                return FacebookHoldResult(success=False, detail=f"upload: {e}")
            if status_code >= 400:
                return FacebookHoldResult(
                    success=False, detail=f"upload: HTTP {status_code}: {body[:300]}"
                )

            # Phase 3: finish with SCHEDULED state at the real target
            try:
                finish = await client.post(
                    f"{base}/{page_id}/video_reels",
                    data={
                        "upload_phase": "finish",
                        "video_id": video_id,
                        "access_token": page_access_token,
                        "video_state": "SCHEDULED",
                        "scheduled_publish_time": epoch,
                        "title": title,
                        "description": description,
                    },
                )
                finish.raise_for_status()
            except httpx.HTTPStatusError as e:
                return FacebookHoldResult(
                    success=False,
                    video_id=video_id,
                    detail=f"finish: {_response_detail(e.response)}",
                )
            except httpx.HTTPError as e:
                return FacebookHoldResult(
                    success=False, video_id=video_id, detail=f"finish: {e}"
                )
        finally:
            video_path.unlink(missing_ok=True)
    logger.info(
        "Facebook hold converted to native scheduled post page=%s video_id=%s at=%s",
        page_id,
        video_id,
        scheduled_at.isoformat(),
    )
    return FacebookHoldResult(
        success=True, video_id=video_id, url=_page_video_url(page_id, video_id)
    )
