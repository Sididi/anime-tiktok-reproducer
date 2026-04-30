"""Instagram Reels publisher via Meta Graph API.

Implements the canonical container → poll → publish flow:
  POST /{ig_user_id}/media?media_type=REELS&video_url=...&caption=...
  GET  /{container_id}?fields=status_code  (poll until FINISHED)
  POST /{ig_user_id}/media_publish?creation_id=...
  GET  /{media_id}?fields=permalink

Returns InstagramPublishResult — success=True on publish, even if permalink
fetch fails (the post is live regardless).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class InstagramPublishResult:
    success: bool
    permalink: str | None = None
    detail: str | None = None


_DEFAULT_POLL_INTERVAL_SECONDS = 5.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 5 * 60.0  # 5 minutes


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body[:500] if body else response.reason_phrase

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        subcode = error.get("error_subcode")
        parts = [str(message)] if message else []
        if code is not None:
            parts.append(f"code={code}")
        if subcode is not None:
            parts.append(f"subcode={subcode}")
        if parts:
            return " ".join(parts)
    return str(payload)[:500]


def _status_detail(payload: dict[str, Any]) -> str:
    code = payload.get("status_code")
    status = payload.get("status")
    if status:
        return f"container status_code = {code}; status = {status}"
    return f"container status_code = {code}; no status detail returned"


async def publish_to_instagram(
    *,
    ig_user_id: str,
    ig_access_token: str,
    caption: str,
    video_url: str,
    graph_api_version: str = "v25.0",
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
) -> InstagramPublishResult:
    base = f"https://graph.facebook.com/{graph_api_version}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Create container
        try:
            create = await client.post(
                f"{base}/{ig_user_id}/media",
                params={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "access_token": ig_access_token,
                },
            )
            create.raise_for_status()
            container_id = create.json()["id"]
        except httpx.HTTPStatusError as e:
            return InstagramPublishResult(
                success=False,
                detail=f"create container failed: {_response_detail(e.response)}",
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return InstagramPublishResult(
                success=False, detail=f"create container failed: {e}"
            )

        # 2. Poll status
        elapsed = 0.0
        while elapsed < poll_timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            try:
                status_resp = await client.get(
                    f"{base}/{container_id}",
                    params={
                        "fields": "status_code,status",
                        "access_token": ig_access_token,
                    },
                )
                status_resp.raise_for_status()
                status_payload = status_resp.json()
                code = status_payload.get("status_code")
            except httpx.HTTPStatusError as e:
                return InstagramPublishResult(
                    success=False,
                    detail=f"status poll failed: {_response_detail(e.response)}",
                )
            except httpx.HTTPError as e:
                return InstagramPublishResult(
                    success=False, detail=f"status poll failed: {e}"
                )
            except ValueError as e:
                return InstagramPublishResult(
                    success=False, detail=f"status poll failed: invalid JSON: {e}"
                )
            if code == "FINISHED":
                break
            if code == "ERROR":
                return InstagramPublishResult(
                    success=False, detail=_status_detail(status_payload)
                )
        else:
            return InstagramPublishResult(success=False, detail="poll timeout")

        # 3. Publish
        try:
            pub = await client.post(
                f"{base}/{ig_user_id}/media_publish",
                params={
                    "creation_id": container_id,
                    "access_token": ig_access_token,
                },
            )
            pub.raise_for_status()
            media_id = pub.json()["id"]
        except httpx.HTTPStatusError as e:
            return InstagramPublishResult(
                success=False, detail=f"publish failed: {_response_detail(e.response)}"
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return InstagramPublishResult(success=False, detail=f"publish failed: {e}")

        # 4. Fetch permalink (best-effort; not fatal)
        permalink: str | None = None
        try:
            perma = await client.get(
                f"{base}/{media_id}",
                params={"fields": "permalink", "access_token": ig_access_token},
            )
            perma.raise_for_status()
            permalink = perma.json().get("permalink")
        except httpx.HTTPError:
            logger.warning(
                "permalink fetch failed for %s — publish still succeeded", media_id
            )

        return InstagramPublishResult(success=True, permalink=permalink)
