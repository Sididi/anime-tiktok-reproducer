"""TikTok publisher via Post for Me (postforme.dev).

Flow:
  GET  download_url                      -> save the MP4 locally
  POST {base}/media/create-upload-url    -> {upload_url, media_url}
  PUT  upload_url                        -> binary upload (signed, no auth)
  POST {base}/social-posts               -> immediate publish (no scheduled_at)
  GET  {base}/social-post-results?post_id=... (poll until a result exists)

Managed credentials: Post for Me's audited TikTok app publishes on our behalf.
The only secret is the project API key (server .env, never persisted in jobs).

Double-post guard: TikTokPublishState persists the PFM post id the moment the
post is created. Retries with a live post id poll its results instead of
creating a new post; a new post is only created when the previous one has a
definitive failed result (stage == "failed"), reusing the uploaded media_url.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.models.job import TikTokPublishState

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.postforme.dev/v1"
_DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 30 * 60.0
_UPLOAD_TIMEOUT_SECONDS = 900.0

TikTokProgressCallback = Callable[[TikTokPublishState], Awaitable[None] | None]


@dataclass
class TikTokPublishResult:
    success: bool
    url: str | None = None
    detail: str | None = None
    publish_state: TikTokPublishState | None = None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _stage_detail(stage: str, detail: str) -> str:
    return f"{stage}: {detail}"


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body[:500] if body else f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}: {str(payload)[:500]}"


def _unwrap(payload: Any) -> dict[str, Any]:
    """PFM object endpoints return either the object or {'data': object}."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _coerce_state(
    state: TikTokPublishState | dict[str, Any] | None,
) -> TikTokPublishState | None:
    if isinstance(state, TikTokPublishState):
        return state
    return TikTokPublishState.from_dict(state)


async def _emit_progress(
    callback: TikTokProgressCallback | None, state: TikTokPublishState
) -> None:
    if callback is None:
        return
    result = callback(state)
    if result is not None:
        await result


async def _download_video(
    client: httpx.AsyncClient, url: str, temp_dir: Path | None
) -> Path:
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="tt-pfm-", suffix=".mp4",
        dir=str(temp_dir) if temp_dir is not None else None,
    )
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


def _put_file_sync(
    *, upload_url: str, video_path: Path, timeout_seconds: float
) -> tuple[int, str]:
    """Blocking binary PUT to the signed URL. Returns (status_code, body)."""
    with video_path.open("rb") as f:
        response = httpx.put(
            upload_url,
            content=f,
            headers={"Content-Type": "video/mp4"},
            timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds),
            follow_redirects=True,
        )
    return response.status_code, response.text


async def _upload_media(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    download_url: str,
    temp_dir: Path | None,
) -> str:
    """Download the video and push it to PFM storage. Returns media_url."""
    video_path = await _download_video(client, download_url, temp_dir)
    try:
        create = await client.post(
            f"{base_url}/media/create-upload-url",
            headers=_headers(api_key),
            json={},
        )
        create.raise_for_status()
        payload = _unwrap(create.json())
        upload_url = str(payload["upload_url"])
        media_url = str(payload["media_url"])
        status_code, body = await asyncio.to_thread(
            _put_file_sync,
            upload_url=upload_url,
            video_path=video_path,
            timeout_seconds=_UPLOAD_TIMEOUT_SECONDS,
        )
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}: {body[:300]}")
        return media_url
    finally:
        video_path.unlink(missing_ok=True)


def _result_error_detail(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail")
        if message:
            return str(message)[:500]
    if error:
        return str(error)[:500]
    return "post failed without error detail"


async def publish_to_tiktok(  # noqa: PLR0911, PLR0912, PLR0915
    *,
    api_key: str,
    social_account_id: str,
    caption: str,
    download_url: str,
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    base_url: str = DEFAULT_BASE_URL,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
    temp_dir: Path | None = None,
) -> TikTokPublishResult:
    base = base_url.rstrip("/")
    state = _coerce_state(publish_state)
    started = time.monotonic()

    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None), follow_redirects=True
    ) as client:
        # ---- Ensure media is uploaded (reuse persisted media_url on retry) ----
        media_url = state.media_url if state else None
        post_id = state.post_id if state and state.stage != "failed" else None

        if post_id is None and media_url is None:
            try:
                media_url = await _upload_media(
                    client,
                    base_url=base,
                    api_key=api_key,
                    download_url=download_url,
                    temp_dir=temp_dir,
                )
            except httpx.HTTPStatusError as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("upload", _response_detail(e.response)),
                    publish_state=state,
                )
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("upload", f"{type(e).__name__}: {e}"),
                    publish_state=state,
                )
            state = TikTokPublishState(
                media_url=media_url,
                stage="media_uploaded",
                created_at=_utc_now(),
            )
            await _emit_progress(progress_callback, state)

        # ---- Ensure the post exists ----
        if post_id is None:
            body = {
                "caption": caption,
                "social_accounts": [social_account_id],
                "media": [{"url": media_url}],
                "platform_configurations": {
                    "tiktok": {
                        "privacy_status": privacy_status,
                        "allow_comment": allow_comment,
                        "allow_duet": allow_duet,
                        "allow_stitch": allow_stitch,
                    }
                },
            }
            try:
                create = await client.post(
                    f"{base}/social-posts", headers=_headers(api_key), json=body
                )
                create.raise_for_status()
                post_id = str(_unwrap(create.json())["id"])
            except httpx.HTTPStatusError as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("create_post", _response_detail(e.response)),
                    publish_state=state,
                )
            except (httpx.HTTPError, KeyError, ValueError) as e:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("create_post", f"{type(e).__name__}: {e}"),
                    publish_state=state,
                )
            state = replace(
                state or TikTokPublishState(),
                post_id=post_id,
                stage="post_created",
                last_error=None,
            )
            await _emit_progress(progress_callback, state)
            logger.info(
                "PFM post created social_account_id=%s post_id=%s", social_account_id, post_id
            )

        # ---- Poll results ----
        elapsed = 0.0
        while True:
            try:
                results_resp = await client.get(
                    f"{base}/social-post-results",
                    headers=_headers(api_key),
                    params={"post_id": post_id},
                )
                results_resp.raise_for_status()
                payload = results_resp.json()
            except httpx.HTTPError as e:
                detail = (
                    _response_detail(e.response)
                    if isinstance(e, httpx.HTTPStatusError)
                    else f"{type(e).__name__}: {e}"
                )
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("poll_results", detail),
                    publish_state=state,
                )
            results = payload.get("data") if isinstance(payload, dict) else None
            state = replace(state, last_polled_at=_utc_now())
            if isinstance(results, list) and results:
                result = next(
                    (
                        r for r in results
                        if isinstance(r, dict)
                        and r.get("social_account_id") == social_account_id
                    ),
                    results[0],
                )
                if result.get("success"):
                    platform_data = result.get("platform_data") or {}
                    url = platform_data.get("url")
                    state = replace(state, stage="published", url=url)
                    await _emit_progress(progress_callback, state)
                    logger.info(
                        "PFM TikTok publish succeeded post_id=%s url=%s elapsed=%.1fs",
                        post_id, url, time.monotonic() - started,
                    )
                    return TikTokPublishResult(
                        success=True, url=url, publish_state=state
                    )
                detail = _result_error_detail(result)
                state = replace(state, stage="failed", last_error=detail)
                await _emit_progress(progress_callback, state)
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail("result", detail),
                    publish_state=state,
                )
            await _emit_progress(progress_callback, state)
            if elapsed >= poll_timeout:
                return TikTokPublishResult(
                    success=False,
                    detail=_stage_detail(
                        "poll_results",
                        f"timeout after {int(poll_timeout)}s; "
                        f"post_id={post_id}; resumable=true",
                    ),
                    publish_state=state,
                )
            await asyncio.sleep(poll_interval)
            elapsed += max(poll_interval, 0.001)
