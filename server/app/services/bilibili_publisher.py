"""Publish to bilibili.com's approved Open Platform (ARC_BASE).

This is not an adapter for bilibili.tv. Scheduling belongs to the VPS: only
the final submission creates a public-facing archive, which then enters
Bilibili's review queue. Never repeat a submission whose outcome is unknown.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_BASE = "https://member.bilibili.com/arcopen/fn/archive"
_UPLOAD_URL = "https://openupos.bilivideo.com/video/v2/part/upload"
_TOKEN_URL = "https://api.bilibili.com/x/account-oauth2/v1/token"
_IDENTITY_URL = "https://member.bilibili.com/arcopen/fn/user/account/info"
_CHUNK_BYTES = 10 * 1024 * 1024
_ACCEPTED = "Bilibili submission accepted; platform review determines visibility"
StateCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class BilibiliPublishResult:
    success: bool
    resource_id: str | None = None
    url: str | None = None
    detail: str | None = None
    uncertain: bool = False
    state: dict[str, Any] = field(default_factory=dict)


class _APIError(Exception):
    """A provider-declared rejection, safe to distinguish from a lost reply."""


def _response_data(response: httpx.Response) -> dict[str, Any]:
    # Do not stringify HTTP exceptions: token-authenticated upload URLs contain
    # credentials in their query string, including in httpx exception messages.
    if response.status_code >= 400:
        raise _APIError(f"Bilibili HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or "code" not in payload:
        raise ValueError("Bilibili returned an invalid API response")
    if not isinstance(payload["code"], int) or isinstance(payload["code"], bool):
        raise ValueError("Bilibili returned an invalid API status code")
    if payload["code"] != 0:
        code = str(payload["code"])
        hint = ""
        if code in {"127001", "122007"}:
            hint = "; renew the account's OAuth authorization"
        elif code in {"127005", "127006", "127007", "127011", "127304", "127305"}:
            hint = "; check developer/application approval and ARC_BASE authorization"
        raise _APIError(f"Bilibili API error {code}{hint}")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("Bilibili returned invalid response data")
    return data


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class BilibiliPublisher:
    def __init__(self, credentials: dict[str, Any]) -> None:
        self.credentials = dict(credentials)

    def _headers(self, body: bytes = b"") -> dict[str, str]:
        signed = {
            "x-bili-accesskeyid": str(self.credentials["client_id"]),
            "x-bili-content-md5": hashlib.md5(body).hexdigest(),
            "x-bili-signature-method": "HMAC-SHA256",
            "x-bili-signature-nonce": str(uuid.uuid4()),
            "x-bili-signature-version": "2.0",
            "x-bili-timestamp": str(int(time.time())),
        }
        canonical = "\n".join(f"{key}:{value}" for key, value in sorted(signed.items()))
        signature = hmac.new(
            str(self.credentials["client_secret"]).encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        return {
            **signed,
            "Authorization": signature,
            "Access-Token": str(self.credentials["access_token"]),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _post(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            if payload is not None else b""
        )
        return await client.post(
            f"{_BASE}/{endpoint}", content=body, headers=self._headers(body), params=params
        )

    def _submission(self, description: str) -> dict[str, Any]:
        for key in ("client_id", "client_secret", "access_token", "openid"):
            if not str(self.credentials.get(key) or "").strip():
                raise ValueError(f"Bilibili {key} is required for API mode")
        # The official token response calls an absolute UTC timestamp
        # 'expires_in'. Configuration stores it unambiguously as expires_at.
        expires_at = self.credentials.get("expires_at")
        if not expires_at or float(expires_at) <= time.time() + 300:
            raise ValueError(
                "Bilibili OAuth authorization expires soon or expires_at is missing; "
                "reauthorize and update access_token and expires_at before publishing"
            )
        title = str(self.credentials.get("title") or "").strip()
        if not title or len(title) > 80:
            raise ValueError("Bilibili requires a title of 1-80 characters")
        category_id = int(self.credentials.get("category_id") or 0)
        if category_id <= 0:
            raise ValueError("Bilibili category_id must be selected from the account's categories")
        raw_tags = self.credentials.get("tags")
        if isinstance(raw_tags, list):
            raw_tags = ",".join(
                str(tag).strip().lstrip("#") for tag in raw_tags if str(tag).strip()
            )
        tags = str(raw_tags or "").strip()
        if not tags:
            raise ValueError("Bilibili requires at least one tag")
        copyright_mode = int(self.credentials.get("copyright") or 0)
        if copyright_mode not in {1, 2}:
            raise ValueError("Bilibili copyright must be 1 (original) or 2 (repost)")
        source = str(self.credentials.get("source") or "").strip()
        if copyright_mode == 2 and not source:
            raise ValueError("Bilibili reposts require a source URL")
        payload: dict[str, Any] = {
            "title": title,
            "tid": category_id,
            "tag": tags,
            "desc": description,
            "copyright": copyright_mode,
        }
        if source:
            payload["source"] = source
        return payload

    async def _upload_media(
        self,
        client: httpx.AsyncClient,
        video_path: Path,
        progress: dict[str, Any],
        save: Callable[..., Awaitable[None]],
    ) -> None:
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise ValueError("Bilibili video file is missing or empty")
        digest = await asyncio.to_thread(_file_digest, video_path)
        if progress.get("source_sha256") not in (None, digest):
            raise ValueError("Bilibili upload state belongs to different video content")
        # A lost merge acknowledgement is safe to recover by uploading under a
        # fresh token: no archive exists before final submission. Do not rewrite
        # chunks on the old token, which may already have been merged.
        merge_unknown = progress.get("merge_started") and not progress.get("upload_completed")
        if not progress.get("upload_token") or merge_unknown:
            data = _response_data(await self._post(
                client, "video/init", payload={"name": video_path.name}
            ))
            upload_token = str(data.get("upload_token") or "")
            if not upload_token:
                raise ValueError("Bilibili did not return an upload token")
            await save(
                upload_token=upload_token, source_sha256=digest, uploaded_parts=0,
                merge_started=False, upload_completed=False,
            )
        if progress.get("upload_completed"):
            return

        upload_token = progress["upload_token"]
        # The chunk endpoint authenticates by upload_token, and must receive
        # raw bytes (multipart corrupts the uploaded video).
        uploaded_parts = int(progress.get("uploaded_parts") or 0)
        with video_path.open("rb") as stream:
            stream.seek(uploaded_parts * _CHUNK_BYTES)
            part_number = uploaded_parts + 1
            while chunk := await asyncio.to_thread(stream.read, _CHUNK_BYTES):
                response = await client.post(
                    _UPLOAD_URL,
                    params={"upload_token": upload_token, "part_number": part_number},
                    content=chunk,
                    headers={"Content-Type": "application/octet-stream"},
                )
                _response_data(response)
                await save(uploaded_parts=part_number)
                part_number += 1
        await save(merge_started=True)
        _response_data(await self._post(
            client, "video/complete", params={"upload_token": upload_token}
        ))
        await save(upload_completed=True)

    async def publish(
        self,
        video_path: Path,
        description: str = "",
        *,
        state: dict[str, Any] | None = None,
        on_state: StateCallback | None = None,
        on_credentials: StateCallback | None = None,
    ) -> BilibiliPublishResult:
        """Upload chunks then submit once, with durable checkpoints.

        on_state must complete its durable write before returning. OAuth token
        rotation is deliberately not performed: the currently verified official
        examples only establish the authorization-code exchange contract.
        """
        progress = dict(state or {})

        def result(success: bool, detail: str, *, uncertain: bool = False):
            resource_id = progress.get("resource_id")
            return BilibiliPublishResult(
                success=success,
                resource_id=resource_id,
                url=f"https://www.bilibili.com/video/{resource_id}" if resource_id else None,
                detail=detail,
                uncertain=uncertain,
                state=dict(progress),
            )

        async def save(**values: Any) -> None:
            checkpoint = {**progress, **values}
            if on_state is not None:
                await on_state(dict(checkpoint))
            progress.update(values)

        if progress.get("resource_id"):
            return result(True, _ACCEPTED)
        if progress.get("submission_started"):
            return result(
                False,
                "Bilibili submission outcome is unknown; check Creator Studio before retrying",
                uncertain=True,
            )

        try:
            submission = self._submission(description)
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as client:
                identity = _response_data(await client.get(_IDENTITY_URL, headers=self._headers()))
                if identity.get("openid") != self.credentials["openid"]:
                    raise ValueError("Bilibili OAuth token belongs to a different creator account")
                await self._upload_media(client, video_path, progress, save)

                # This marker must survive a worker crash *before* the final
                # request, otherwise a restart can publish a duplicate archive.
                await save(submission_started=True)
                response = await self._post(
                    client, "add-by-utoken", payload=submission,
                    params={"upload_token": progress["upload_token"]},
                )
                if response.status_code >= 500 or response.status_code == 408:
                    raise ValueError("Bilibili submission response was inconclusive")
                try:
                    data = _response_data(response)
                except _APIError:
                    await save(submission_started=False)
                    raise
                resource_id = str(data.get("resource_id") or "")
                if not resource_id:
                    raise ValueError("Bilibili submission returned no resource ID")
                await save(resource_id=resource_id)
            return result(True, _ACCEPTED)
        except (_APIError, ValueError, TypeError, OSError) as exc:
            return result(False, str(exc), uncertain=bool(progress.get("submission_started")))
        except httpx.HTTPError:
            return result(
                False, "Bilibili request failed; network error",
                uncertain=bool(progress.get("submission_started")),
            )

    @staticmethod
    async def exchange_authorization_code(
        *, client_id: str, client_secret: str, code: str,
    ) -> dict[str, Any]:
        """Exchange a code from the registered, state-validated OAuth callback.

        Returns credentials to store securely, never logged. expires_in in this
        provider's response is an absolute Unix timestamp, not a TTL.
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            data = _response_data(await client.post(_TOKEN_URL, data={
                "client_id": client_id, "client_secret": client_secret,
                "grant_type": "authorization_code", "code": code,
            }))
            if not data.get("access_token") or not data.get("expires_in"):
                raise ValueError("Bilibili authorization returned incomplete token data")
            publisher = BilibiliPublisher({
                "client_id": client_id, "client_secret": client_secret,
                "access_token": data["access_token"],
            })
            identity = _response_data(await client.get(
                _IDENTITY_URL, headers=publisher._headers()
            ))
            if not identity.get("openid"):
                raise ValueError("Bilibili authorization returned no creator OpenID")
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": data["expires_in"],
            "openid": identity["openid"],
        }
