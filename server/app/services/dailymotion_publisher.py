"""Dailymotion API v2 publishing with a recoverable, private staging step.

The caller owns the publication clock. API v2 does not document a future
publication field, so a due job uploads privately and then changes visibility.
Creating and submitting states MUST be persisted before their HTTP mutations.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

API_BASE = "https://api.dailymotion.com/v2"
TOKEN_URL = "https://oauth2.dailymotion.com/v2/token"
StateCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class DailymotionPublishResult:
    success: bool
    resource_id: str | None = None
    url: str | None = None
    detail: str | None = None
    uncertain: bool = False
    state: dict[str, Any] = field(default_factory=dict)


class DailymotionError(RuntimeError):
    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.status_code = status_code


async def _emit(callback: StateCallback | None, values: dict[str, Any]) -> None:
    if callback is not None:
        result = callback(dict(values))
        if inspect.isawaitable(result):
            await result


def _upload_url(value: Any) -> str:
    """Only send the video and Bearer token to an official HTTPS upload host."""
    parsed = urlsplit(str(value or ""))
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host.startswith("upload-")
        or not host.endswith(".dailymotion.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise DailymotionError("Dailymotion returned an invalid upload URL")
    return str(value)


def _object(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise DailymotionError(f"Dailymotion {operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise DailymotionError(f"Dailymotion {operation} returned an invalid object")
    return value


class DailymotionPublisher:
    def __init__(self, credentials: dict[str, Any]):
        self.credentials = dict(credentials)
        self._token: str | None = None
        self._expires_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=120.0, follow_redirects=False)

    def _check(self, response: httpx.Response, operation: str) -> None:
        if response.is_success:
            return
        # Avoid logging response bodies, request URLs or headers: OAuth errors
        # and upload URLs can contain credentials and signed capabilities.
        code = ""
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                candidate = str(error.get("code") or "")
                if re.fullmatch(r"[A-Z_0-9]{1,80}", candidate):
                    code = f" ({candidate})"
        except ValueError:
            pass
        raise DailymotionError(
            f"Dailymotion {operation}: HTTP {response.status_code}{code}",
            response.status_code,
        )

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and self._expires_at > time.monotonic():
            return self._token
        for key in ("client_id", "client_secret"):
            if not str(self.credentials.get(key) or "").strip():
                raise DailymotionError(f"Dailymotion {key} is not configured")
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "scope": "video.manage video.read",
            },
        )
        self._check(response, "authentication")
        payload = _object(response, "authentication")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise DailymotionError("Dailymotion authentication returned no access token")
        try:
            expires_in = float(payload.get("expires_in", 1800))
        except (TypeError, ValueError) as exc:
            raise DailymotionError("Dailymotion authentication returned invalid expiry") from exc
        self._token = token
        self._expires_at = time.monotonic() + max(0.0, expires_in - 60)
        return token

    async def _request(
        self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        # A rejected 401 is safe to retry; transport errors and server errors on
        # mutations are deliberately left to persisted-state reconciliation.
        for attempt in range(2):
            token = await self._access_token(client)
            response = await client.request(
                method,
                f"{API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            if response.status_code != 401 or attempt:
                return response
            self._token = None
        raise AssertionError("unreachable")

    async def _prepare_video(
        self,
        client: httpx.AsyncClient,
        video_path: Path,
        description: str,
    ) -> tuple[str, dict[str, Any]]:
        profile_id = str(self.credentials.get("profile_id") or "").strip()
        if not profile_id:
            raise DailymotionError("Dailymotion profile_id is not configured")
        title = str(self.credentials.get("title") or description.split("\n", 1)[0]).strip()
        if not title:
            raise DailymotionError("Dailymotion requires a non-empty video title")
        is_for_kids = self.credentials.get("is_for_kids", False)
        if not isinstance(is_for_kids, bool):
            raise DailymotionError("Dailymotion is_for_kids must be true or false")
        path = Path(video_path)
        if not path.is_file() or path.stat().st_size == 0:
            raise DailymotionError("Dailymotion video file is missing or empty")
        session = await self._request(client, "POST", "/files/upload_sessions")
        self._check(session, "create upload session")
        upload_url = _upload_url(_object(session, "upload session").get("upload_url"))
        token = await self._access_token(client)
        with path.open("rb") as video_file:
            uploaded = await client.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (path.name, video_file, "video/mp4")},
                timeout=900.0,
            )
        self._check(uploaded, "upload file")
        file_url = _object(uploaded, "file upload").get("url")
        if not isinstance(file_url, str) or not file_url.startswith("https://"):
            raise DailymotionError("Dailymotion upload returned no HTTPS file URL")
        body: dict[str, Any] = {
            "title": title[:255],
            "category": str(self.credentials.get("category") or "shortfilms"),
            "visibility": "private",
            "is_for_kids": is_for_kids,
            "source": {"file_url": file_url},
        }
        if description.strip():
            body["description"] = description.strip()[:3000]
        return profile_id, body

    async def publish(
        self,
        video_path: Path,
        description: str = "",
        *,
        state: dict[str, Any] | None = None,
        on_state: StateCallback | None = None,
        on_credentials: StateCallback | None = None,
    ) -> DailymotionPublishResult:
        # client_credentials issues new access tokens, not rotating refresh
        # tokens. No token needs to be written to the caller's credential store.
        current = dict(state or {})

        def outcome(success: bool, detail: str | None = None, *, uncertain: bool = False):
            return DailymotionPublishResult(
                success=success,
                resource_id=str(current.get("resource_id") or "") or None,
                url=current.get("url"),
                detail=detail,
                uncertain=uncertain,
                state=dict(current),
            )

        if current.get("stage") == "submitted" and current.get("resource_id"):
            return outcome(True)
        if current.get("stage") == "creating" and not current.get("resource_id"):
            return outcome(
                False,
                "Private video creation was interrupted; reconcile in Dailymotion Studio "
                "before retrying to avoid creating a duplicate.",
                uncertain=True,
            )
        mutation: str | None = None
        try:
            async with self._client() as client:
                resource_id = str(current.get("resource_id") or "")
                if resource_id:
                    path = f"/videos/{quote(resource_id, safe='')}"
                    response = await self._request(
                        client,
                        "GET",
                        path,
                        params={"fields": "video_id,visibility,video_url"},
                    )
                    self._check(response, "reconciliation")
                    existing = _object(response, "reconciliation")
                    if existing.get("visibility") == "public":
                        current.update(
                            stage="submitted",
                            url=existing.get("video_url")
                            or f"https://www.dailymotion.com/video/{resource_id}",
                        )
                        await _emit(on_state, current)
                        return outcome(True)
                    if existing.get("visibility") != "private":
                        current["stage"] = "submitting"
                        raise DailymotionError(
                            "Existing Dailymotion video is not private; inspect its "
                            "visibility in Studio before retrying."
                        )
                    current["stage"] = "private"
                else:
                    profile_id, body = await self._prepare_video(client, video_path, description)
                    current["stage"] = "creating"
                    await _emit(on_state, current)
                    mutation = "create"
                    created = await self._request(
                        client,
                        "POST",
                        f"/profiles/{quote(profile_id, safe='')}/videos",
                        json=body,
                    )
                    self._check(created, "create private video")
                    data = _object(created, "create private video")
                    resource_id = str(data.get("video_id") or data.get("id") or "")
                    if not resource_id:
                        raise DailymotionError("Dailymotion created video without returning its ID")
                    current.update(
                        stage="private",
                        resource_id=resource_id,
                        url=f"https://www.dailymotion.com/video/{resource_id}",
                    )
                    mutation = None
                    await _emit(on_state, current)
                    path = f"/videos/{quote(resource_id, safe='')}"
                current["stage"] = "submitting"
                await _emit(on_state, current)
                mutation = "publish"
                published = await self._request(
                    client,
                    "PATCH",
                    path,
                    json={"visibility": "public"},
                )
                self._check(published, "publish")
                current["stage"] = "submitted"
                mutation = None
                await _emit(on_state, current)
                return outcome(True)
        except DailymotionError as exc:
            # Explicit 4xx rejection establishes that the mutation did not
            # succeed; 5xx or malformed success replies leave its result unknown.
            if mutation and exc.status_code is not None and 400 <= exc.status_code < 500:
                current["stage"] = "private" if current.get("resource_id") else "pending"
                await _emit(on_state, current)
                mutation = None
            return outcome(
                False,
                str(exc),
                uncertain=bool(mutation) or current.get("stage") == "submitting",
            )
        except (httpx.HTTPError, OSError) as exc:
            return outcome(
                False,
                f"Dailymotion request failed ({type(exc).__name__})",
                uncertain=bool(mutation) or current.get("stage") == "submitting",
            )

    async def delete(self, resource_id: str) -> None:
        """Remove a staged or published video; already missing is successful."""
        if not resource_id:
            raise DailymotionError("Dailymotion resource_id is required for deletion")
        async with self._client() as client:
            response = await self._request(
                client,
                "DELETE",
                f"/videos/{quote(resource_id, safe='')}",
            )
            if response.status_code != 404:
                self._check(response, "delete")
