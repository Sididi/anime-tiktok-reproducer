"""Publish Spotlight videos using Snapchat's allowlisted Public Profile API.

Scheduling belongs to the durable server queue: Snap media expires after 24h and
the documented Spotlight API has no native scheduling or deletion operation.
Persist ``on_state`` before acknowledging it, especially the ``submitting`` stage.
An interrupted final POST is deliberately not retried without operator review.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import re
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_API_BASE = "https://businessapi.snapchat.com"
_TOKEN_URL = "https://accounts.snapchat.com/login/oauth2/access_token"
_CHUNK_BYTES = 32 * 1024 * 1024
_MAX_MEDIA_BYTES = 1_000_000_000
_IO_CHUNK_BYTES = 1024 * 1024
StateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class SnapchatPublishResult:
    success: bool
    resource_id: str | None = None
    url: str | None = None
    detail: str | None = None
    uncertain: bool = False
    state: dict[str, Any] | None = None


class _SnapchatError(RuntimeError):
    """A failed request with a message safe to persist in job status."""


async def _emit(callback: StateCallback | None, value: dict[str, Any]) -> None:
    if callback is not None:
        pending = callback(dict(value))
        if inspect.isawaitable(pending):
            await pending


def _upload_url(path: object) -> str:
    """Only send bearer credentials to the documented Snap API origin."""
    if not isinstance(path, str) or not path:
        raise _SnapchatError("Media response is missing its upload path")
    url = urljoin(f"{_API_BASE}/", path)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "businessapi.snapchat.com"
        or not parsed.path.endswith("/multipart-upload")
        or parsed.fragment
    ):
        raise _SnapchatError("Media response contains an invalid upload path")
    return url


async def _validate_video(video_path: Path) -> None:
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise _SnapchatError("The Spotlight video file is missing or empty")
    # AES padding adds between one and sixteen bytes to the uploaded file.
    if video_path.stat().st_size > _MAX_MEDIA_BYTES - 16:
        raise _SnapchatError("Snapchat uploads must be smaller than 1 GB")
    if video_path.suffix.lower() != ".mp4":
        raise _SnapchatError("Snapchat Spotlight requires an MP4 file")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise _SnapchatError("ffprobe is required on the server for Snapchat uploads") from exc
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise _SnapchatError("ffprobe could not read the Spotlight video")
    try:
        probe = json.loads(stdout)
        video = next(s for s in probe.get("streams", []) if s.get("codec_type") == "video")
        media_format = probe.get("format", {})
        duration = float(media_format.get("duration") or video.get("duration") or 0)
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        formats = str(media_format.get("format_name", "")).split(",")
    except (ValueError, TypeError, AttributeError, StopIteration) as exc:
        raise _SnapchatError("Could not determine Spotlight video format and dimensions") from exc
    if "mp4" not in formats:
        raise _SnapchatError("Snapchat Spotlight requires MP4 media, not just an .mp4 extension")
    if not math.isfinite(duration) or not 6 <= duration <= 60:
        raise _SnapchatError("Snapchat Spotlight requires a video between 6 and 60 seconds")
    if width < 540 or height < 960:
        raise _SnapchatError("Snapchat Spotlight requires a resolution of at least 540x960")


async def _encrypt_video(source: Path, destination: Path, key: bytes, iv: bytes) -> None:
    """Stream AES-256-CBC with PKCS7 padding, matching Snap's openssl example."""
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    padder = padding.PKCS7(128).padder()
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        while chunk := input_file.read(_IO_CHUNK_BYTES):
            output_file.write(encryptor.update(padder.update(chunk)))
            await asyncio.sleep(0)
        output_file.write(encryptor.update(padder.finalize()) + encryptor.finalize())


def _media_is_reusable(state: dict[str, Any], profile_id: str) -> bool:
    if (
        state.get("stage") != "media_ready"
        or not state.get("media_id")
        or state.get("profile_id") != profile_id
    ):
        return False
    try:
        expires = datetime.fromisoformat(str(state["expires_at"]))
        return expires.tzinfo is not None and expires > datetime.now(UTC) + timedelta(minutes=5)
    except (KeyError, ValueError):
        return False


class SnapchatPublisher:
    def __init__(self, credentials: dict[str, Any]) -> None:
        self.credentials = dict(credentials)

    def _safe_detail(self, value: object) -> str:
        detail = str(value)
        for key in ("client_secret", "refresh_token", "access_token"):
            secret = self.credentials.get(key)
            if secret:
                detail = detail.replace(str(secret), "[redacted]")
        return detail[:500]

    def _payload(self, response: httpx.Response, *, oauth: bool = False) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise _SnapchatError(
                f"Snapchat returned HTTP {response.status_code} without JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise _SnapchatError("Snapchat returned an invalid JSON response")
        failed = response.is_error or (
            bool(payload.get("error")) if oauth else payload.get("request_status") != "SUCCESS"
        )
        if failed:
            detail = (
                payload.get("display_message")
                or payload.get("error_description")
                or payload.get("debug_message")
                or payload.get("error_code")
                or payload.get("error")
                or "Unsuccessful API response"
            )
            raise _SnapchatError(self._safe_detail(f"HTTP {response.status_code}: {detail}"))
        return payload

    async def _access_token(
        self,
        client: httpx.AsyncClient,
        on_credentials: StateCallback | None,
    ) -> str:
        data = {
            "grant_type": "refresh_token",
            "client_id": str(self.credentials["client_id"]),
            "client_secret": str(self.credentials["client_secret"]),
            "refresh_token": str(self.credentials["refresh_token"]),
        }
        if self.credentials.get("redirect_uri"):
            data["redirect_uri"] = str(self.credentials["redirect_uri"])
        payload = self._payload(await client.post(_TOKEN_URL, data=data), oauth=True)
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise _SnapchatError("Snapchat OAuth response has no access token")
        self.credentials["access_token"] = access_token
        rotated = payload.get("refresh_token")
        if rotated and rotated != self.credentials["refresh_token"]:
            self.credentials["refresh_token"] = str(rotated)
            # Persist immediately, even if media validation or publishing later fails.
            await _emit(on_credentials, self.credentials)
        return access_token

    async def _upload_media(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        video_path: Path,
        profile_base: str,
        profile_id: str,
    ) -> dict[str, Any]:
        key, iv = secrets.token_bytes(32), secrets.token_bytes(16)
        with tempfile.TemporaryDirectory(prefix="snapchat-upload-") as tmp:
            encrypted_path = Path(tmp) / "encrypted.mp4"
            await _encrypt_video(video_path, encrypted_path, key, iv)
            created_at = datetime.now(UTC)
            media = self._payload(
                await client.post(
                    f"{profile_base}/media",
                    headers=headers,
                    json={
                        "type": "VIDEO",
                        "name": video_path.name[:200],
                        "key": base64.b64encode(key).decode("ascii"),
                        "iv": base64.b64encode(iv).decode("ascii"),
                    },
                )
            )
            media_id = str(media.get("media_id") or "")
            if not media_id:
                raise _SnapchatError("Snapchat media response is missing media_id")
            add_url = _upload_url(media.get("add_path"))
            finalize_url = _upload_url(media.get("finalize_path"))
            with encrypted_path.open("rb") as encrypted:
                part_number = 0
                while chunk := encrypted.read(_CHUNK_BYTES):
                    part_number += 1
                    self._payload(
                        await client.post(
                            add_url,
                            headers=headers,
                            data={"action": "ADD", "part_number": str(part_number)},
                            files={
                                "file": (f"part-{part_number}", chunk, "application/octet-stream")
                            },
                        )
                    )
            self._payload(
                await client.post(
                    finalize_url,
                    headers=headers,
                    files={"action": (None, "FINALIZE")},
                )
            )
        return {
            "stage": "media_ready",
            "profile_id": profile_id,
            "media_id": media_id,
            "expires_at": (created_at + timedelta(hours=24)).isoformat(),
        }

    async def publish(
        self,
        video_path: Path,
        description: str = "",
        *,
        state: dict[str, Any] | None = None,
        on_state: StateCallback | None = None,
        on_credentials: StateCallback | None = None,
    ) -> SnapchatPublishResult:
        current = dict(state or {})
        if current.get("stage") == "submitted" and current.get("resource_id"):
            return SnapchatPublishResult(
                True,
                resource_id=str(current["resource_id"]),
                url=current.get("url"),
                detail="Spotlight submitted; Snapchat moderation may still be pending",
                state=current,
            )
        if current.get("stage") == "submitting":
            return SnapchatPublishResult(
                False,
                detail="Spotlight submission outcome is unknown; check Snapchat before retrying",
                uncertain=True,
                state=current,
            )
        missing = [
            key
            for key in ("profile_id", "client_id", "client_secret", "refresh_token")
            if not str(self.credentials.get(key) or "").strip()
        ]
        if missing:
            return SnapchatPublishResult(
                False,
                detail=f"Missing Snapchat credentials: {', '.join(missing)}",
                state=current,
            )
        profile_id = str(self.credentials["profile_id"])
        locale = str(self.credentials.get("locale") or "en_US")
        if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", locale):
            return SnapchatPublishResult(
                False, detail="Snapchat locale must look like en_US", state=current
            )
        profile_base = f"{_API_BASE}/v1/public_profiles/{quote(profile_id, safe='')}"
        stage = "validation"
        try:
            if not _media_is_reusable(current, profile_id):
                await _validate_video(video_path)
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=900)) as client:
                stage = "oauth"
                token = await self._access_token(client, on_credentials)
                headers = {"Authorization": f"Bearer {token}"}
                if not _media_is_reusable(current, profile_id):
                    stage = "upload"
                    current = await self._upload_media(
                        client,
                        headers,
                        video_path,
                        profile_base,
                        profile_id,
                    )
                    await _emit(on_state, current)

                stage = "submit"
                current = {**current, "stage": "submitting"}
                # A process crash or timeout after this point must never automatically
                # repeat the POST: Snap documents no idempotency key for Spotlight.
                await _emit(on_state, current)
                response = await client.post(
                    f"{profile_base}/spotlights",
                    headers=headers,
                    json={
                        "media_id": current["media_id"],
                        "description": description[:160],
                        "locale": locale,
                        "skip_save_to_profile": False,
                    },
                )
                try:
                    payload = self._payload(response)
                except _SnapchatError:
                    try:
                        rejected = (
                            response.status_code < 500
                            and response.status_code != 408
                            and response.json().get("request_status") == "ERROR"
                        )
                    except (ValueError, AttributeError):
                        rejected = False
                    if rejected:
                        # Snap definitively rejected this request, so a later
                        # explicit retry may safely reuse the uploaded media.
                        current = {**current, "stage": "media_ready"}
                        await _emit(on_state, current)
                    raise
                resource_id = str(payload.get("spotlight_id") or "")
                if not resource_id:
                    raise _SnapchatError("Snapchat submission response is missing spotlight_id")
                current = {
                    **current,
                    "stage": "submitted",
                    "resource_id": resource_id,
                    "url": f"https://www.snapchat.com/spotlight/{quote(resource_id, safe='')}",
                }
                await _emit(on_state, current)
                return SnapchatPublishResult(
                    True,
                    resource_id=resource_id,
                    url=current["url"],
                    detail="Spotlight submitted; Snapchat moderation may still be pending",
                    state=current,
                )
        except (_SnapchatError, httpx.HTTPError, OSError, TimeoutError) as exc:
            uncertain = current.get("stage") == "submitting"
            detail = (
                self._safe_detail(exc) if isinstance(exc, _SnapchatError) else type(exc).__name__
            )
            if uncertain:
                detail += "; check Snapchat before retrying to avoid a duplicate Spotlight"
            return SnapchatPublishResult(
                False,
                detail=f"{stage}: {detail}",
                uncertain=uncertain,
                state=current,
            )
