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
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
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


_DEFAULT_POLL_INTERVAL_SECONDS = 60.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 15 * 60.0
_RATE_LIMIT_POLL_INTERVAL_SECONDS = 60.0
_MAX_POLL_INTERVAL_SECONDS = 300.0
_DEFAULT_CONTENT_PUBLISHING_QUOTA_TOTAL = 50
_MAX_CAPTION_CHARS = 2200
_MAX_HASHTAGS = 30
_MAX_MENTIONS = 20
_MAX_REEL_BYTES = 300 * 1024 * 1024
_MIN_REEL_DURATION_SECONDS = 3.0
_MAX_REEL_DURATION_SECONDS = 15 * 60.0
_ALLOWED_VIDEO_CODECS = {"h264", "hevc"}
_ALLOWED_AUDIO_CODECS = {"aac"}
_ALLOWED_CONTAINERS = {"mov", "mp4", "m4v", "quicktime"}
_TRANSCODE_TRIGGER_RATIO = 0.95
_TRANSCODE_TARGET_RATIO = 0.92
_TRANSCODE_AUDIO_BITRATE = 192_000
_TRANSCODE_MIN_VIDEO_BITRATE = 500_000
_TRANSCODE_TIMEOUT_SECONDS = 3600


def _stage_detail(stage: str, detail: str) -> str:
    return f"{stage}: {detail}"


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
        fbtrace_id = error.get("fbtrace_id") or payload.get("fbtrace_id")
        error_user_msg = error.get("error_user_msg")
        parts = [str(message)] if message else []
        if error_user_msg and error_user_msg != message:
            parts.append(f"user_msg={error_user_msg}")
        if code is not None:
            parts.append(f"code={code}")
        if subcode is not None:
            parts.append(f"subcode={subcode}")
        if fbtrace_id:
            parts.append(f"fbtrace_id={fbtrace_id}")
        if parts:
            return " ".join(parts)
    return str(payload)[:500]


def _upload_response_detail(response: _UploadResponse) -> str:
    try:
        payload = json.loads(response.body)
    except ValueError:
        body = response.body.strip()
        return body[:500] if body else f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        subcode = error.get("error_subcode")
        fbtrace_id = error.get("fbtrace_id") or payload.get("fbtrace_id")
        parts = [str(message)] if message else [f"HTTP {response.status_code}"]
        if code is not None:
            parts.append(f"code={code}")
        if subcode is not None:
            parts.append(f"subcode={subcode}")
        if fbtrace_id:
            parts.append(f"fbtrace_id={fbtrace_id}")
        return " ".join(parts)
    debug_info = payload.get("debug_info") if isinstance(payload, dict) else None
    if isinstance(debug_info, dict):
        message = debug_info.get("message")
        debug_type = debug_info.get("type")
        parts = [str(message)] if message else [f"HTTP {response.status_code}"]
        if debug_type:
            parts.append(f"type={debug_type}")
        return " ".join(parts)
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


def _validate_caption(caption: str) -> str | None:
    if len(caption) > _MAX_CAPTION_CHARS:
        return f"caption is {len(caption)} chars; max is {_MAX_CAPTION_CHARS}"
    hashtags = re.findall(r"(?<!\w)#[\w]+", caption)
    if len(hashtags) > _MAX_HASHTAGS:
        return f"caption has {len(hashtags)} hashtags; max is {_MAX_HASHTAGS}"
    mentions = re.findall(r"(?<!\w)@[\w.]+", caption)
    if len(mentions) > _MAX_MENTIONS:
        return f"caption has {len(mentions)} mentions; max is {_MAX_MENTIONS}"
    return None


def _content_publishing_quota_detail(payload: dict[str, Any]) -> str | None:
    quota_usage = payload.get("quota_usage")
    quota_total = None
    config = payload.get("config")
    if isinstance(config, dict):
        quota_total = config.get("quota_total")
    if quota_usage is None and isinstance(payload.get("data"), list) and payload["data"]:
        first = payload["data"][0]
        if isinstance(first, dict):
            quota_usage = first.get("quota_usage")
            first_config = first.get("config")
            if isinstance(first_config, dict):
                quota_total = first_config.get("quota_total")
    if quota_usage is None:
        logger.warning(
            "Instagram content_publishing_limit response did not include quota_usage; "
            "continuing without quota enforcement (keys=%s)",
            sorted(str(k) for k in payload),
        )
        return None
    if quota_total is None:
        quota_total = _DEFAULT_CONTENT_PUBLISHING_QUOTA_TOTAL
    with contextlib.suppress(TypeError, ValueError):
        used = int(quota_usage)
        total = int(quota_total)
        if total > 0 and used >= total:
            return f"content publishing quota exhausted ({used}/{total})"
        logger.info("Instagram content publishing quota usage is %d/%d", used, total)
    return None


async def _preflight_instagram_account(
    client: httpx.AsyncClient,
    *,
    base: str,
    ig_user_id: str,
    ig_access_token: str,
) -> None:
    account = await client.get(
        f"{base}/{ig_user_id}",
        params={"fields": "id", "access_token": ig_access_token},
    )
    account.raise_for_status()

    quota = await client.get(
        f"{base}/{ig_user_id}/content_publishing_limit",
        params={"access_token": ig_access_token},
    )
    quota.raise_for_status()
    payload = quota.json()
    if not isinstance(payload, dict):
        raise RuntimeError("content publishing limit response was not an object")
    if detail := _content_publishing_quota_detail(payload):
        raise RuntimeError(detail)


def _float_or_none(value: Any) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        return float(value)
    return None


def _validate_video_streams(payload: dict[str, Any]) -> str | None:  # noqa: PLR0911
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    format_name = str(fmt.get("format_name") or "").lower()
    if format_name and not any(c in format_name for c in _ALLOWED_CONTAINERS):
        return f"container {format_name!r} is not MP4/MOV"

    duration = _float_or_none(fmt.get("duration"))
    if duration is not None and not (
        _MIN_REEL_DURATION_SECONDS <= duration <= _MAX_REEL_DURATION_SECONDS
    ):
        return (
            f"duration {duration:.2f}s outside "
            f"{_MIN_REEL_DURATION_SECONDS:.0f}-{_MAX_REEL_DURATION_SECONDS:.0f}s"
        )

    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    video_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"),
        None,
    )
    if not isinstance(video_stream, dict):
        return "missing video stream"
    video_codec = str(video_stream.get("codec_name") or "").lower()
    if video_codec and video_codec not in _ALLOWED_VIDEO_CODECS:
        return f"video codec {video_codec!r} is not H.264/HEVC"
    width = int(video_stream.get("width") or 0)
    if width > 1920:
        return f"video width {width}px exceeds 1920px"
    frame_rate = _parse_frame_rate(video_stream.get("avg_frame_rate"))
    if frame_rate is None:
        frame_rate = _parse_frame_rate(video_stream.get("r_frame_rate"))
    if frame_rate is not None and not (23 <= frame_rate <= 60):
        return f"frame rate {frame_rate:.2f} FPS outside 23-60 FPS"

    audio_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )
    if isinstance(audio_stream, dict):
        audio_codec = str(audio_stream.get("codec_name") or "").lower()
        if audio_codec and audio_codec not in _ALLOWED_AUDIO_CODECS:
            return f"audio codec {audio_codec!r} is not AAC"
    return None


def _parse_frame_rate(value: Any) -> float | None:
    if not value:
        return None
    text = str(value)
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        with contextlib.suppress(ValueError, ZeroDivisionError):
            den = float(denominator)
            if den:
                return float(numerator) / den
        return None
    return _float_or_none(text)


async def _probe_duration_seconds(video_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        result = await asyncio.to_thread(run)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _float_or_none(result.stdout.strip())


async def _maybe_transcode_for_size(video_path: Path) -> Path:  # noqa: PLR0911
    """Re-encode the video below the Reels size cap when it's too large.

    Returns the original path when no transcode is needed, ffmpeg/ffprobe is
    unavailable, or the transcode fails — letting downstream validation surface
    the size error as before.
    """
    file_size = video_path.stat().st_size
    if file_size <= _MAX_REEL_BYTES * _TRANSCODE_TRIGGER_RATIO:
        return video_path

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        logger.warning(
            "Instagram video is %d bytes (>cap) but ffmpeg is unavailable; "
            "skipping pre-upload transcode",
            file_size,
        )
        return video_path

    duration = await _probe_duration_seconds(video_path)
    if duration is None or duration <= 0:
        logger.warning(
            "Instagram video is %d bytes (>cap) but duration probe failed; "
            "skipping pre-upload transcode",
            file_size,
        )
        return video_path

    target_total_bits = int(_MAX_REEL_BYTES * _TRANSCODE_TARGET_RATIO * 8)
    video_bitrate = max(
        int(target_total_bits / duration) - _TRANSCODE_AUDIO_BITRATE,
        _TRANSCODE_MIN_VIDEO_BITRATE,
    )

    fd, out_str = tempfile.mkstemp(prefix="ig-reel-tc-", suffix=".mp4")
    os.close(fd)
    out_path = Path(out_str)
    pass_log_dir = Path(tempfile.mkdtemp(prefix="ig-reel-pass-"))
    pass_log_prefix = pass_log_dir / "ffmpeg2pass"

    def run_pass(pass_num: int) -> subprocess.CompletedProcess[str]:
        common = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-c:v",
            "libx264",
            "-b:v",
            str(video_bitrate),
            "-preset",
            "medium",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-passlogfile",
            str(pass_log_prefix),
            "-pass",
            str(pass_num),
        ]
        if pass_num == 1:
            cmd = [*common, "-an", "-f", "mp4", os.devnull]
        else:
            cmd = [
                *common,
                "-c:a",
                "aac",
                "-b:a",
                str(_TRANSCODE_AUDIO_BITRATE),
                str(out_path),
            ]
        return subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_TRANSCODE_TIMEOUT_SECONDS,
        )

    logger.info(
        "Instagram pre-upload transcode start: %d bytes over %.2fs, "
        "target video bitrate %d bps",
        file_size,
        duration,
        video_bitrate,
    )
    try:
        for pass_num in (1, 2):
            try:
                result = await asyncio.to_thread(run_pass, pass_num)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning(
                    "Instagram pre-upload transcode pass %d errored: %s",
                    pass_num,
                    e,
                )
                out_path.unlink(missing_ok=True)
                return video_path
            if result.returncode != 0:
                logger.warning(
                    "Instagram pre-upload transcode pass %d failed: %s",
                    pass_num,
                    (result.stderr or "").strip()[:300],
                )
                out_path.unlink(missing_ok=True)
                return video_path

        new_size = out_path.stat().st_size if out_path.exists() else 0
        if new_size <= 0:
            logger.warning("Instagram pre-upload transcode produced empty file")
            out_path.unlink(missing_ok=True)
            return video_path

        logger.info(
            "Instagram pre-upload transcode complete: %d bytes -> %d bytes",
            file_size,
            new_size,
        )
        video_path.unlink(missing_ok=True)
        return out_path
    finally:
        shutil.rmtree(pass_log_dir, ignore_errors=True)


async def _validate_video(video_path: Path) -> str | None:  # noqa: PLR0911
    file_size = video_path.stat().st_size
    if file_size <= 0:
        return "downloaded video is empty"
    if file_size > _MAX_REEL_BYTES:
        return f"file size {file_size} bytes exceeds {_MAX_REEL_BYTES} bytes"

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.warning("ffprobe unavailable; Instagram video validation is limited")
        return None

    def run_probe() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(video_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        probe = await asyncio.to_thread(run_probe)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("ffprobe failed; Instagram video validation is limited: %s", e)
        return None
    if probe.returncode != 0:
        stderr = probe.stderr.strip()
        return f"ffprobe failed: {stderr[:300] if stderr else probe.returncode}"
    try:
        payload = json.loads(probe.stdout)
    except ValueError as e:
        return f"ffprobe returned invalid JSON: {e}"
    if not isinstance(payload, dict):
        return "ffprobe response was not an object"
    return _validate_video_streams(payload)


def _upload_resumable_binary_sync(
    *,
    upload_uri: str,
    ig_access_token: str,
    video_path: Path,
    timeout_seconds: float,
) -> _UploadResponse:
    file_size = video_path.stat().st_size
    try:
        with video_path.open("rb") as f:
            response = httpx.post(
                upload_uri,
                headers=_upload_headers(ig_access_token, file_size),
                content=f,
                timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds),
                follow_redirects=True,
            )
        return _UploadResponse(status_code=response.status_code, body=response.text)
    except httpx.HTTPError as e:
        raise OSError(str(e)) from e


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
        "status_code,status,error_message",
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


async def publish_to_instagram(  # noqa: PLR0911, PLR0912, PLR0915
    *,
    ig_user_id: str,
    ig_access_token: str,
    caption: str,
    video_url: str,
    graph_api_version: str = "v25.0",
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
    share_to_feed: bool = True,
    thumb_offset: int | None = None,
) -> InstagramPublishResult:
    base = f"https://graph.facebook.com/{graph_api_version}"
    timeout = httpx.Timeout(30.0, read=None)
    video_path: Path | None = None
    started = time.monotonic()

    if detail := _validate_caption(caption):
        return InstagramPublishResult(success=False, detail=_stage_detail("preflight", detail))

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        try:
            await _preflight_instagram_account(
                client,
                base=base,
                ig_user_id=ig_user_id,
                ig_access_token=ig_access_token,
            )
        except httpx.HTTPStatusError as e:
            return InstagramPublishResult(
                success=False,
                detail=_stage_detail("preflight", _response_detail(e.response)),
            )
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            return InstagramPublishResult(
                success=False, detail=_stage_detail("preflight", str(e))
            )

        try:
            video_path = await _download_video(client, video_url)
        except httpx.HTTPStatusError as e:
            return InstagramPublishResult(
                success=False,
                detail=_stage_detail("download", _response_detail(e.response)),
            )
        except (httpx.HTTPError, RuntimeError) as e:
            return InstagramPublishResult(
                success=False, detail=_stage_detail("download", str(e))
            )

        video_path = await _maybe_transcode_for_size(video_path)

        if detail := await _validate_video(video_path):
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False, detail=_stage_detail("validate", detail)
            )

        try:
            create_data = {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false",
                "access_token": ig_access_token,
            }
            if thumb_offset is not None:
                create_data["thumb_offset"] = str(thumb_offset)
            create = await client.post(
                f"{base}/{ig_user_id}/media",
                data=create_data,
            )
            create.raise_for_status()
            create_payload = create.json()
            container_id = create_payload["id"]
            upload_uri = create_payload["uri"]
            logger.info(
                "Instagram container created ig_user_id=%s graph_api_version=%s container_id=%s",
                ig_user_id,
                graph_api_version,
                container_id,
            )
        except httpx.HTTPStatusError as e:
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False,
                detail=_stage_detail("create_container", _response_detail(e.response)),
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False, detail=_stage_detail("create_container", str(e))
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
                        detail=_stage_detail("rupload", _upload_response_detail(upload)),
                    )
                logger.info(
                    "Instagram resumable upload returned indeterminate processing "
                    "response for container %s; polling container status",
                    container_id,
                )
            else:
                logger.info(
                    "Instagram rupload succeeded ig_user_id=%s container_id=%s status_code=%s",
                    ig_user_id,
                    container_id,
                    upload.status_code,
                )
        except (OSError, TimeoutError) as e:
            video_path.unlink(missing_ok=True)
            return InstagramPublishResult(
                success=False, detail=_stage_detail("rupload", str(e))
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
                    detail=_stage_detail("status_poll", _response_detail(e.response)),
                )
            except _RetryableStatusPollError as e:
                logger.info(
                    "Instagram status poll retryable error ig_user_id=%s container_id=%s: %s",
                    ig_user_id,
                    container_id,
                    e,
                )
                next_poll_interval = _next_retry_poll_interval(
                    next_poll_interval,
                    elapsed=elapsed,
                    poll_timeout=poll_timeout,
                )
                continue
            except httpx.HTTPError as e:
                return InstagramPublishResult(
                    success=False, detail=_stage_detail("status_poll", str(e))
                )
            except (RuntimeError, ValueError) as e:
                return InstagramPublishResult(
                    success=False, detail=_stage_detail("status_poll", str(e))
                )
            if code == "FINISHED":
                logger.info(
                    "Instagram container finished ig_user_id=%s container_id=%s elapsed=%.1fs",
                    ig_user_id,
                    container_id,
                    time.monotonic() - started,
                )
                break
            if code == "PUBLISHED":
                logger.info(
                    "Instagram container already published ig_user_id=%s container_id=%s",
                    ig_user_id,
                    container_id,
                )
                return InstagramPublishResult(success=True)
            if code in {"ERROR", "EXPIRED"}:
                return InstagramPublishResult(
                    success=False,
                    detail=_stage_detail("status_poll", _status_detail(status_payload)),
                )
            next_poll_interval = poll_interval
        else:
            return InstagramPublishResult(
                success=False, detail=_stage_detail("status_poll", "poll timeout")
            )

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
            logger.info(
                "Instagram publish succeeded ig_user_id=%s container_id=%s "
                "media_id=%s elapsed=%.1fs",
                ig_user_id,
                container_id,
                media_id,
                time.monotonic() - started,
            )
        except httpx.HTTPStatusError as e:
            return InstagramPublishResult(
                success=False, detail=_stage_detail("publish", _response_detail(e.response))
            )
        except (httpx.HTTPError, KeyError, ValueError) as e:
            return InstagramPublishResult(
                success=False, detail=_stage_detail("publish", str(e))
            )

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
                "Instagram permalink fetch failed media_id=%s - publish still succeeded",
                media_id,
            )

        return InstagramPublishResult(success=True, permalink=permalink)
