"""Instagram Reels publisher via Meta Graph API.

Implements the resumable upload -> poll -> publish flow:
  GET  video_url and save the MP4 locally
  POST /{ig_user_id}/media?media_type=REELS&upload_type=resumable&caption=...
  POST {upload_uri} with the binary payload
  GET  /{container_id}?fields=status_code  (poll until FINISHED)
  POST /{ig_user_id}/media_publish?creation_id=...
  GET  /{media_id}?fields=permalink

Returns InstagramPublishResult. success=True means media_publish succeeded, even
if the best-effort permalink lookup fails.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class InstagramPublishResult:
    success: bool
    permalink: str | None = None
    detail: str | None = None


@dataclass
class _UploadResponse:
    status_code: int
    body: str


class _RetryableStatusPollError(RuntimeError):
    pass


_DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 5 * 60.0  # 5 minutes
_RATE_LIMIT_POLL_INTERVAL_SECONDS = 60.0
_MAX_POLL_INTERVAL_SECONDS = 300.0


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


def _upload_response_detail(response: _UploadResponse) -> str:
    try:
        payload = json.loads(response.body)
    except ValueError:
        body = response.body.strip()
        return body[:500] if body else f"HTTP {response.status_code}"
    return str(payload)[:500]


def _status_detail(payload: dict[str, Any]) -> str:
    code = payload.get("status_code")
    error_message = str(
        payload.get("error_message") or payload.get("message") or ""
    ).strip()
    status = str(payload.get("status") or "").strip()
    video_status = payload.get("video_status")
    phase_bits: list[str] = []
    if isinstance(video_status, dict):
        uploading_phase = video_status.get("uploading_phase")
        processing_phase = video_status.get("processing_phase")
        if isinstance(uploading_phase, dict):
            up_status = str(uploading_phase.get("status") or "").strip()
            bytes_transferred = uploading_phase.get("bytes_transferred")
            if up_status:
                phase_bits.append(f"uploading_phase={up_status}")
            if bytes_transferred is not None:
                phase_bits.append(f"bytes_transferred={bytes_transferred}")
        if isinstance(processing_phase, dict):
            proc_status = str(processing_phase.get("status") or "").strip()
            if proc_status:
                phase_bits.append(f"processing_phase={proc_status}")

    detail = error_message or status
    if detail:
        result = f"container status_code = {code}; status = {detail}"
    else:
        result = f"container status_code = {code}; no status detail returned"
    if phase_bits:
        result = f"{result} ({', '.join(phase_bits)})"
    return result


def _is_container_field_error(response: httpx.Response) -> bool:
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    code = int(error.get("code") or 0) if error.get("code") is not None else 0
    subcode = (
        int(error.get("error_subcode") or 0)
        if error.get("error_subcode") is not None
        else 0
    )
    message = str(error.get("message") or "").lower()
    markers = (
        "nonexisting field",
        "invalid parameter",
        "not a valid parameter",
        "paramètre non valide",
    )
    return code == 100 and (subcode == 2207065 or any(m in message for m in markers))


def _is_retryable_graph_response(response: httpx.Response) -> bool:
    if response.status_code in {429, 500, 502, 503, 504}:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    code = int(error.get("code") or 0) if error.get("code") is not None else 0
    message = str(error.get("message") or "").lower()
    return code in {4, 17, 32, 613} or "request limit" in message


def _is_resumable_upload_indeterminate_body(body: str) -> bool:
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    debug_info = payload.get("debug_info") if isinstance(payload, dict) else None
    if not isinstance(debug_info, dict):
        return False
    debug_type = str(debug_info.get("type") or "").strip().lower()
    debug_message = str(debug_info.get("message") or "").strip().lower()
    return (
        debug_type == "processingfailederror"
        and "request processing failed" in debug_message
    )


def _upload_headers(ig_access_token: str, file_size: int) -> dict[str, str]:
    return {
        "Authorization": f"OAuth {ig_access_token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Length": str(file_size),
        "X-Entity-Length": str(file_size),
        "Content-Type": "application/octet-stream",
    }


def _upload_resumable_binary_sync(
    *,
    upload_uri: str,
    ig_access_token: str,
    video_path: Path,
    timeout_seconds: float,
) -> _UploadResponse:
    file_size = video_path.stat().st_size
    request = urllib.request.Request(
        upload_uri,
        data=video_path.read_bytes(),
        method="POST",
        headers=_upload_headers(ig_access_token, file_size),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", "replace")
            return _UploadResponse(status_code=response.status, body=body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return _UploadResponse(status_code=e.code, body=body)


async def _upload_resumable_binary(
    *,
    upload_uri: str,
    ig_access_token: str,
    video_path: Path,
    timeout_seconds: float = 900.0,
) -> _UploadResponse:
    return await asyncio.to_thread(
        _upload_resumable_binary_sync,
        upload_uri=upload_uri,
        ig_access_token=ig_access_token,
        video_path=video_path,
        timeout_seconds=timeout_seconds,
    )


async def _download_video(client: httpx.AsyncClient, video_url: str) -> Path:
    fd, tmp = tempfile.mkstemp(prefix="ig-reel-", suffix=".mp4")
    os.close(fd)
    path = Path(tmp)
    try:
        async with client.stream("GET", video_url) as response:
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


async def _get_status_payload(
    client: httpx.AsyncClient,
    *,
    base: str,
    container_id: str,
    ig_access_token: str,
) -> dict[str, Any]:
    field_candidates = (
        "status_code,status",
        "status_code,status,video_status",
        "status_code,status,error_message,video_status",
        "status_code",
    )
    last_error_detail = ""
    for fields in field_candidates:
        status_resp = await client.get(
            f"{base}/{container_id}",
            params={"fields": fields, "access_token": ig_access_token},
        )
        try:
            status_resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if _is_retryable_graph_response(e.response):
                raise _RetryableStatusPollError(_response_detail(e.response)) from e
            if _is_container_field_error(e.response):
                last_error_detail = _response_detail(e.response)
                continue
            raise
        payload = status_resp.json()
        return payload if isinstance(payload, dict) else {}
    suffix = f" after field fallback ({last_error_detail})" if last_error_detail else ""
    raise RuntimeError(f"container status failed{suffix}")


def _effective_status(payload: dict[str, Any]) -> str:
    code = str(payload.get("status_code") or "").upper()
    if code:
        return code
    return str(payload.get("status") or "").upper()


def _next_retry_poll_interval(
    current_interval: float,
    *,
    elapsed: float,
    poll_timeout: float,
) -> float:
    remaining = poll_timeout - elapsed
    if remaining <= 0:
        return current_interval
    target = min(
        max(_RATE_LIMIT_POLL_INTERVAL_SECONDS, current_interval * 2),
        _MAX_POLL_INTERVAL_SECONDS,
    )
    return max(0.0, min(target, remaining))


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
    timeout = httpx.Timeout(30.0, read=None)
    video_path: Path | None = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            video_path = await _download_video(client, video_url)
        except httpx.HTTPStatusError as e:
            return InstagramPublishResult(
                success=False,
                detail=f"download video failed: {_response_detail(e.response)}",
            )
        except (httpx.HTTPError, RuntimeError) as e:
            return InstagramPublishResult(
                success=False, detail=f"download video failed: {e}"
            )

        try:
            create = await client.post(
                f"{base}/{ig_user_id}/media",
                data={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": caption,
                    "share_to_feed": "true",
                    "access_token": ig_access_token,
                },
            )
            create.raise_for_status()
            create_payload = create.json()
            container_id = create_payload["id"]
            upload_uri = create_payload["uri"]
        except httpx.HTTPStatusError as e:
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False,
                detail=f"create container failed: {_response_detail(e.response)}",
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False, detail=f"create container failed: {e}"
            )

        try:
            upload = await _upload_resumable_binary(
                upload_uri=str(upload_uri),
                ig_access_token=ig_access_token,
                video_path=video_path,
            )
            if upload.status_code >= 400:
                if not _is_resumable_upload_indeterminate_body(upload.body):
                    video_path.unlink(missing_ok=True)
                    return InstagramPublishResult(
                        success=False,
                        detail=(
                            "resumable upload failed: "
                            f"{_upload_response_detail(upload)}"
                        ),
                    )
                logger.info(
                    "Instagram resumable upload returned indeterminate processing "
                    "response for container %s; polling container status",
                    container_id,
                )
        except (OSError, TimeoutError) as e:
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False, detail=f"resumable upload failed: {e}"
            )
        finally:
            if video_path is not None:
                video_path.unlink(missing_ok=True)
                video_path = None

        elapsed = 0.0
        next_poll_interval = poll_interval
        while elapsed < poll_timeout:
            await asyncio.sleep(next_poll_interval)
            elapsed += next_poll_interval
            try:
                status_payload = await _get_status_payload(
                    client,
                    base=base,
                    container_id=container_id,
                    ig_access_token=ig_access_token,
                )
                code = _effective_status(status_payload)
            except httpx.HTTPStatusError as e:
                return InstagramPublishResult(
                    success=False,
                    detail=f"status poll failed: {_response_detail(e.response)}",
                )
            except _RetryableStatusPollError as e:
                logger.info("Instagram status poll retryable error: %s", e)
                next_poll_interval = _next_retry_poll_interval(
                    next_poll_interval,
                    elapsed=elapsed,
                    poll_timeout=poll_timeout,
                )
                continue
            except httpx.HTTPError as e:
                return InstagramPublishResult(
                    success=False, detail=f"status poll failed: {e}"
                )
            except (RuntimeError, ValueError) as e:
                return InstagramPublishResult(
                    success=False, detail=f"status poll failed: {e}"
                )
            if code == "FINISHED":
                break
            if code in {"ERROR", "EXPIRED"}:
                return InstagramPublishResult(
                    success=False, detail=_status_detail(status_payload)
                )
            next_poll_interval = poll_interval
        else:
            return InstagramPublishResult(success=False, detail="poll timeout")

        try:
            pub = await client.post(
                f"{base}/{ig_user_id}/media_publish",
                data={
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
                "permalink fetch failed for %s - publish still succeeded", media_id
            )

        return InstagramPublishResult(success=True, permalink=permalink)
