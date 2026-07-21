"""TikTok publisher via Post for Me (postforme.dev).

Flow (three phases, driven by the scheduler at separate due times):
  1. stage_media_for_tiktok: GET download_url → POST media/create-upload-url
     → PUT binary (as soon as the job exists on the VPS)
  2. create_tiktok_post: POST social-posts with scheduled_at = slot
     (at slot − TIKTOK_SCHEDULE_LEAD_MINUTES; PFM fires server-side at slot)
  3. poll_tiktok_post_result: GET social-post-results (from slot)
publish_to_tiktok composes all three for instant publishing (late jobs).

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
import re
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


def _platform_error_summary(details: Any) -> str | None:
    """Dig the platform-side error out of PFM's result `details` payload.

    PFM's top-level `error` is a generic string ("Failed to post to TikTok");
    the actionable code lives at details.error.response.data.error.{code,message}
    (e.g. "reached_active_user_cap"). Falls back to details.error.message.
    """
    if not isinstance(details, dict):
        return None
    err = details.get("error")
    if not isinstance(err, dict):
        return None
    response = err.get("response")
    if isinstance(response, dict):
        data = response.get("data")
        platform_error = data.get("error") if isinstance(data, dict) else None
        if isinstance(platform_error, dict):
            code = platform_error.get("code")
            message = platform_error.get("message")
            parts = [str(code)] if code else []
            if message and message != code:
                parts.append(str(message))
            status = response.get("status")
            if status:
                parts.append(f"HTTP {status}")
            if parts:
                return ", ".join(parts)
    message = err.get("message")
    return str(message) if message else None


def _result_error_detail(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        base = error.get("message") or error.get("detail")
    else:
        base = error
    platform = _platform_error_summary(result.get("details"))
    if base and platform:
        return f"{base} [{platform}]"[:500]
    if base:
        return str(base)[:500]
    if platform:
        return platform[:500]
    return "post failed without error detail"


_TIKTOK_VIDEO_URL_RE = re.compile(r"/video/\d+")
_TIKTOK_USERNAME_RE = re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)")


def _derive_tiktok_video_url(platform_data: dict[str, Any]) -> str | None:
    """Build the public /video/<id> permalink from PFM's result payload.

    PFM returns platform_data.url as the channel URL and never updates it to
    the video permalink, but embeds the TikTok video id in platform_data.id
    (e.g. "v_pub_url~v2-1.7659653399897655318"). Combine that id with the
    username parsed from the channel URL. Returns None when either cannot be
    parsed with confidence, so the caller falls back to the channel URL.
    """
    url = str(platform_data.get("url") or "")
    if _TIKTOK_VIDEO_URL_RE.search(url):
        return url  # PFM already gave us a permalink
    username_match = _TIKTOK_USERNAME_RE.search(url)
    if not username_match:
        return None
    trailing = str(platform_data.get("id") or "").rsplit(".", 1)[-1]
    if not (trailing.isascii() and trailing.isdigit() and 18 <= len(trailing) <= 19):
        return None
    return f"https://www.tiktok.com/@{username_match.group(1)}/video/{trailing}"


def _live_post_id(state: TikTokPublishState | None) -> str | None:
    """post_id of an existing non-failed post, else None (failed → recreate)."""
    if state and state.post_id and state.stage != "failed":
        return state.post_id
    return None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=None), follow_redirects=True
    )


async def stage_media_for_tiktok(
    *,
    api_key: str,
    download_url: str,
    base_url: str = DEFAULT_BASE_URL,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    temp_dir: Path | None = None,
    progress_callback: TikTokProgressCallback | None = None,
) -> TikTokPublishResult:
    """Phase 1: Drive download → PFM storage upload. Idempotent: no-ops when
    media is already staged or a live post exists. On failure, increments
    media_attempts in the returned state (quiet pre-window retry counter)."""
    state = _coerce_state(publish_state)
    if state and (state.media_url or _live_post_id(state)):
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    async with _client() as client:
        try:
            media_url = await _upload_media(
                client,
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                download_url=download_url,
                temp_dir=temp_dir,
            )
        except httpx.HTTPStatusError as e:
            detail = _stage_detail("upload", _response_detail(e.response))
            state = replace(
                state or TikTokPublishState(),
                media_attempts=(state.media_attempts if state else 0) + 1,
                last_error=detail,
            )
            return TikTokPublishResult(success=False, detail=detail, publish_state=state)
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
            detail = _stage_detail("upload", f"{type(e).__name__}: {e}")
            state = replace(
                state or TikTokPublishState(),
                media_attempts=(state.media_attempts if state else 0) + 1,
                last_error=detail,
            )
            return TikTokPublishResult(success=False, detail=detail, publish_state=state)
    state = replace(
        state or TikTokPublishState(),
        media_url=media_url,
        stage="media_uploaded",
        created_at=_utc_now(),
        last_error=None,
    )
    await _emit_progress(progress_callback, state)
    return TikTokPublishResult(success=True, publish_state=state)


async def create_tiktok_post(
    *,
    api_key: str,
    social_account_id: str,
    caption: str,
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    scheduled_at: datetime | None = None,
    base_url: str = DEFAULT_BASE_URL,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
) -> TikTokPublishResult:
    """Phase 2: create the social post. With scheduled_at, PFM publishes
    server-side at that instant (stage "post_scheduled"); without it the
    publish starts immediately (stage "post_created"). Idempotent on a live
    post_id; requires staged media."""
    state = _coerce_state(publish_state)
    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    if _live_post_id(state):
        return TikTokPublishResult(success=True, publish_state=state)
    media_url = state.media_url if state else None
    if not media_url:
        return TikTokPublishResult(
            success=False,
            detail=_stage_detail("create_post", "no staged media_url"),
            publish_state=state,
        )
    body: dict[str, Any] = {
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
    if scheduled_at is not None:
        body["scheduled_at"] = scheduled_at.astimezone(UTC).isoformat()
    async with _client() as client:
        try:
            create = await client.post(
                f"{base_url.rstrip('/')}/social-posts",
                headers=_headers(api_key),
                json=body,
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
        stage="post_scheduled" if scheduled_at is not None else "post_created",
        last_error=None,
    )
    await _emit_progress(progress_callback, state)
    logger.info(
        "PFM post created social_account_id=%s post_id=%s scheduled_at=%s",
        social_account_id, post_id,
        scheduled_at.isoformat() if scheduled_at else "instant",
    )
    return TikTokPublishResult(success=True, publish_state=state)


async def poll_tiktok_post_result(  # noqa: PLR0911, PLR0912
    *,
    api_key: str,
    social_account_id: str,
    base_url: str = DEFAULT_BASE_URL,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    publish_state: TikTokPublishState | dict[str, Any] | None = None,
    progress_callback: TikTokProgressCallback | None = None,
) -> TikTokPublishResult:
    """Phase 3: poll social-post-results until TikTok reports the outcome."""
    state = _coerce_state(publish_state)
    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    post_id = state.post_id if state else None
    if not post_id:
        return TikTokPublishResult(
            success=False,
            detail=_stage_detail("poll_results", "no post to poll"),
            publish_state=state,
        )
    started = time.monotonic()
    async with _client() as client:
        elapsed = 0.0
        while True:
            try:
                results_resp = await client.get(
                    f"{base_url.rstrip('/')}/social-post-results",
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
                    url = _derive_tiktok_video_url(platform_data) or platform_data.get("url")
                    state = replace(state, stage="published", url=url)
                    await _emit_progress(progress_callback, state)
                    logger.info(
                        "PFM TikTok publish succeeded post_id=%s url=%s "
                        "platform_data=%s elapsed=%.1fs",
                        post_id, url, platform_data, time.monotonic() - started,
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


async def delete_tiktok_post(
    *, api_key: str, post_id: str, base_url: str = DEFAULT_BASE_URL
) -> None:
    """Cancel a scheduled post. 404 (already gone) is treated as success."""
    async with _client() as client:
        response = await client.delete(
            f"{base_url.rstrip('/')}/social-posts/{post_id}",
            headers=_headers(api_key),
        )
        if response.status_code == 404:
            return
        response.raise_for_status()


async def publish_to_tiktok(
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
    """Instant-publish composition of the three phases (stage → create → poll).

    Kept for the late-job path and API compatibility; the scheduler drives the
    phases individually so each gets its own due time."""
    state = _coerce_state(publish_state)
    if state and state.stage == "published":
        return TikTokPublishResult(success=True, url=state.url, publish_state=state)
    staged = await stage_media_for_tiktok(
        api_key=api_key, download_url=download_url, base_url=base_url,
        publish_state=state, temp_dir=temp_dir, progress_callback=progress_callback,
    )
    if not staged.success:
        return staged
    created = await create_tiktok_post(
        api_key=api_key, social_account_id=social_account_id, caption=caption,
        privacy_status=privacy_status, allow_comment=allow_comment,
        allow_duet=allow_duet, allow_stitch=allow_stitch, scheduled_at=None,
        base_url=base_url, publish_state=staged.publish_state,
        progress_callback=progress_callback,
    )
    if not created.success:
        return created
    return await poll_tiktok_post_result(
        api_key=api_key, social_account_id=social_account_id, base_url=base_url,
        poll_interval=poll_interval, poll_timeout=poll_timeout,
        publish_state=created.publish_state, progress_callback=progress_callback,
    )
