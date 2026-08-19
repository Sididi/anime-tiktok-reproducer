"""Immediate (urgent) Instagram Reels publish from the backend.

Slim sync port of the happy path of server/app/services/instagram_publisher.py
(the reference implementation, which keeps the full retry/fallback hardening
for the scheduled VPS flow):

  POST /{ig_user_id}/media  (media_type=REELS, upload_type=resumable)
  POST {upload_uri} with the binary payload (OAuth headers)
  GET  /{container_id}?fields=status_code   poll until FINISHED
  POST /{ig_user_id}/media_publish
  GET  /{media_id}?fields=permalink         best-effort

Fallback: when the rupload path fails and a public video URL exists (the
prepared Drive artifact), a video_url-ingest container is tried once.

Used only by the urgent-immediate flow — normal Instagram scheduling keeps
going through the VPS job (Instagram has no native scheduling API).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("uvicorn.error")

_DEFAULT_POLL_INTERVAL_SECONDS = 10.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 15 * 60.0
_UPLOAD_TIMEOUT_SECONDS = 900.0
_MAX_CAPTION_CHARS = 2200


@dataclass
class InstagramImmediateResult:
    success: bool
    permalink: str | None = None
    detail: str | None = None


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body[:500] if body else f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}: {str(payload)[:500]}"


class InstagramImmediateService:
    @classmethod
    def publish_now(
        cls,
        *,
        ig_user_id: str,
        ig_access_token: str,
        caption: str,
        video_path: Path | None,
        video_url: str | None,
        graph_api_version: str = "v25.0",
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        poll_timeout: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
        share_to_feed: bool = True,
    ) -> InstagramImmediateResult:
        if len(caption) > _MAX_CAPTION_CHARS:
            return InstagramImmediateResult(
                success=False,
                detail=f"caption exceeds {_MAX_CAPTION_CHARS} characters",
            )
        if video_path is None and not video_url:
            return InstagramImmediateResult(
                success=False, detail="no prepared video available"
            )
        base = f"https://graph.facebook.com/{graph_api_version}"
        with httpx.Client(timeout=httpx.Timeout(30.0, read=120.0)) as client:
            container_id, detail = cls._create_and_upload(
                client,
                base=base,
                ig_user_id=ig_user_id,
                ig_access_token=ig_access_token,
                caption=caption,
                share_to_feed=share_to_feed,
                video_path=video_path,
                video_url=video_url,
            )
            if container_id is None:
                return InstagramImmediateResult(success=False, detail=detail)

            status_detail = cls._poll_finished(
                client,
                base=base,
                container_id=container_id,
                ig_access_token=ig_access_token,
                poll_interval=poll_interval,
                poll_timeout=poll_timeout,
            )
            if status_detail is not None:
                return InstagramImmediateResult(success=False, detail=status_detail)

            try:
                pub = client.post(
                    f"{base}/{ig_user_id}/media_publish",
                    data={"creation_id": container_id, "access_token": ig_access_token},
                )
                pub.raise_for_status()
                media_id = str(pub.json()["id"])
            except httpx.HTTPStatusError as e:
                return InstagramImmediateResult(
                    success=False, detail=f"publish: {_response_detail(e.response)}"
                )
            except (httpx.HTTPError, KeyError, ValueError) as e:
                return InstagramImmediateResult(success=False, detail=f"publish: {e}")

            permalink: str | None = None
            try:
                perma = client.get(
                    f"{base}/{media_id}",
                    params={"fields": "permalink", "access_token": ig_access_token},
                )
                perma.raise_for_status()
                permalink = perma.json().get("permalink")
            except httpx.HTTPError:
                logger.warning(
                    "Instagram permalink fetch failed media_id=%s — publish succeeded",
                    media_id,
                )
            logger.info(
                "Instagram immediate publish succeeded ig_user_id=%s media_id=%s",
                ig_user_id,
                media_id,
            )
            return InstagramImmediateResult(success=True, permalink=permalink)

    # ------------------------------------------------------------------ steps

    @classmethod
    def _create_container(
        cls,
        client: httpx.Client,
        *,
        base: str,
        ig_user_id: str,
        ig_access_token: str,
        caption: str,
        share_to_feed: bool,
        upload_method: str,
        video_url: str | None = None,
    ) -> tuple[str, str | None]:
        create_data = {
            "media_type": "REELS",
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": ig_access_token,
        }
        if upload_method == "rupload":
            create_data["upload_type"] = "resumable"
        else:
            create_data["video_url"] = video_url or ""
        create = client.post(f"{base}/{ig_user_id}/media", data=create_data)
        create.raise_for_status()
        payload = create.json()
        container_id = str(payload["id"])
        upload_uri = payload.get("uri")
        if upload_method == "rupload" and not upload_uri:
            raise KeyError("uri")
        return container_id, str(upload_uri) if upload_uri else None

    @classmethod
    def _create_and_upload(
        cls,
        client: httpx.Client,
        *,
        base: str,
        ig_user_id: str,
        ig_access_token: str,
        caption: str,
        share_to_feed: bool,
        video_path: Path | None,
        video_url: str | None,
    ) -> tuple[str | None, str | None]:
        """Returns (container_id, error_detail)."""
        rupload_error: str | None = None
        if video_path is not None and video_path.exists():
            try:
                container_id, upload_uri = cls._create_container(
                    client,
                    base=base,
                    ig_user_id=ig_user_id,
                    ig_access_token=ig_access_token,
                    caption=caption,
                    share_to_feed=share_to_feed,
                    upload_method="rupload",
                )
                file_size = video_path.stat().st_size
                with video_path.open("rb") as f:
                    response = httpx.post(
                        str(upload_uri),
                        headers={
                            "Authorization": f"OAuth {ig_access_token}",
                            "offset": "0",
                            "file_size": str(file_size),
                            "Content-Length": str(file_size),
                        },
                        content=f,
                        timeout=httpx.Timeout(
                            _UPLOAD_TIMEOUT_SECONDS, read=_UPLOAD_TIMEOUT_SECONDS
                        ),
                        follow_redirects=True,
                    )
                if response.status_code < 400 and '"success":true' in response.text.replace(
                    " ", ""
                ):
                    return container_id, None
                rupload_error = f"rupload: HTTP {response.status_code}: {response.text[:300]}"
            except httpx.HTTPStatusError as e:
                rupload_error = f"rupload container: {_response_detail(e.response)}"
            except (httpx.HTTPError, KeyError, ValueError, OSError) as e:
                rupload_error = f"rupload: {type(e).__name__}: {e}"
            logger.warning(
                "Instagram immediate rupload failed (%s); trying video_url ingest",
                rupload_error,
            )

        if video_url:
            try:
                container_id, _ = cls._create_container(
                    client,
                    base=base,
                    ig_user_id=ig_user_id,
                    ig_access_token=ig_access_token,
                    caption=caption,
                    share_to_feed=share_to_feed,
                    upload_method="video_url",
                    video_url=video_url,
                )
                return container_id, None
            except httpx.HTTPStatusError as e:
                detail = f"video_url container: {_response_detail(e.response)}"
            except (httpx.HTTPError, KeyError, ValueError) as e:
                detail = f"video_url container: {type(e).__name__}: {e}"
            if rupload_error:
                detail = f"{rupload_error}; {detail}"
            return None, detail
        return None, rupload_error or "no video source"

    @classmethod
    def _poll_finished(
        cls,
        client: httpx.Client,
        *,
        base: str,
        container_id: str,
        ig_access_token: str,
        poll_interval: float,
        poll_timeout: float,
    ) -> str | None:
        """None on FINISHED; error detail otherwise."""
        started = time.monotonic()
        last_code = "UNKNOWN"
        while True:
            try:
                status_resp = client.get(
                    f"{base}/{container_id}",
                    params={
                        "fields": "status_code,status",
                        "access_token": ig_access_token,
                    },
                )
                status_resp.raise_for_status()
                payload = status_resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in {429, 500, 502, 503, 504}:
                    payload = {}
                else:
                    return f"status_poll: {_response_detail(e.response)}"
            except (httpx.HTTPError, ValueError) as e:
                return f"status_poll: {type(e).__name__}: {e}"
            code = str(payload.get("status_code") or "").upper()
            if code:
                last_code = code
            if code == "FINISHED":
                return None
            if code in ("ERROR", "EXPIRED"):
                status = payload.get("status") or payload.get("error_message")
                return f"status_poll: container {code}" + (
                    f" — {status}" if status else ""
                )
            if time.monotonic() - started >= poll_timeout:
                return (
                    f"status_poll: timeout after {int(poll_timeout)}s "
                    f"(last_status={last_code})"
                )
            time.sleep(poll_interval)
