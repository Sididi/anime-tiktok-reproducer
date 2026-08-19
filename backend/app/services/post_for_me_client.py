"""Post for Me (postforme.dev) client — backend-side TikTok publishing.

Since 2026-08 the backend owns TikTok scheduling: the PFM post is created at
upload time with its final `scheduled_at` (PFM accepts arbitrary future
instants; verified up to 400 days), instead of relaying a job to the VPS
scheduler. The /server TikTok dispatch path is kept commented for reference
(server/app/services/reminder_scheduler.py).

Sync port of the primitives in server/app/services/post_for_me_publisher.py
(the reference implementation). The only secret is ATR_PFM_API_KEY (.env).

Post lifecycle stages mirror the server's TikTokPublishState.stage:
  media_uploaded → post_scheduled (scheduled_at set) | post_created (instant)
  → published | failed
A post at stage post_scheduled can be updated (PUT) or cancelled (DELETE);
post_created/published posts are immutable (TikTok is processing/has posted).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_UPLOAD_TIMEOUT_SECONDS = 900.0
_DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_POST_FOR_ME_TIKTOK_PLATFORMS = frozenset(("tiktok", "tiktok_business"))


class PostForMeError(RuntimeError):
    """A PFM API call failed. `detail` carries the trimmed response payload."""

    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class PostForMeNotConfiguredError(PostForMeError):
    def __init__(self) -> None:
        super().__init__("ATR_PFM_API_KEY is not configured")


@dataclass
class PfmPublishOutcome:
    """Terminal result of a PFM post, from /social-post-results."""

    success: bool
    url: str | None = None
    detail: str | None = None


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
    (e.g. "v_pub_url~v2-1.7659653399897655318"). Returns None when either
    cannot be parsed with confidence (caller falls back to the channel URL).
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


def build_post_body(
    *,
    social_account_id: str,
    media_url: str,
    caption: str,
    post_for_me_platform: str = "tiktok",
    privacy_status: str = "public",
    allow_comment: bool = True,
    allow_duet: bool = True,
    allow_stitch: bool = True,
    scheduled_at: datetime | None = None,
) -> dict[str, Any]:
    """CreateSocialPostDto body. scheduled_at omitted ⇒ instant publish."""
    if post_for_me_platform not in _POST_FOR_ME_TIKTOK_PLATFORMS:
        supported = ", ".join(sorted(_POST_FOR_ME_TIKTOK_PLATFORMS))
        raise ValueError(
            f"unsupported Post for Me TikTok platform {post_for_me_platform!r}; "
            f"expected one of: {supported}"
        )
    body: dict[str, Any] = {
        "caption": caption,
        "social_accounts": [social_account_id],
        "media": [{"url": media_url}],
        "platform_configurations": {
            post_for_me_platform: {
                "privacy_status": privacy_status,
                "allow_comment": allow_comment,
                "allow_duet": allow_duet,
                "allow_stitch": allow_stitch,
            }
        },
    }
    if scheduled_at is not None:
        body["scheduled_at"] = scheduled_at.astimezone(UTC).isoformat()
    return body


class PostForMeClient:
    """Thin sync wrapper over the PFM REST API. All methods raise
    PostForMeError on failure (PostForMeNotConfiguredError without a key)."""

    @classmethod
    def _base_url(cls) -> str:
        return (settings.pfm_base_url or "https://api.postforme.dev/v1").rstrip("/")

    @classmethod
    def _headers(cls) -> dict[str, str]:
        api_key = settings.pfm_api_key
        if not api_key:
            raise PostForMeNotConfiguredError()
        return {"Authorization": f"Bearer {api_key}"}

    @classmethod
    def _client(cls) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=True)

    # ------------------------------------------------------------------ media

    @classmethod
    def stage_media(cls, video_path: Path) -> str:
        """Upload the local file to PFM storage. Returns the media_url."""
        headers = cls._headers()
        with cls._client() as client:
            try:
                create = client.post(
                    f"{cls._base_url()}/media/create-upload-url",
                    headers=headers,
                    json={},
                )
                create.raise_for_status()
                payload = _unwrap(create.json())
                upload_url = str(payload["upload_url"])
                media_url = str(payload["media_url"])
            except httpx.HTTPStatusError as e:
                raise PostForMeError(
                    f"create-upload-url: {_response_detail(e.response)}",
                    e.response.status_code,
                ) from e
            except (httpx.HTTPError, KeyError, ValueError) as e:
                raise PostForMeError(f"create-upload-url: {type(e).__name__}: {e}") from e
        with video_path.open("rb") as f:
            response = httpx.put(
                upload_url,
                content=f,
                headers={"Content-Type": "video/mp4"},
                timeout=httpx.Timeout(_UPLOAD_TIMEOUT_SECONDS, read=_UPLOAD_TIMEOUT_SECONDS),
                follow_redirects=True,
            )
        if response.status_code >= 400:
            raise PostForMeError(
                f"media upload: HTTP {response.status_code}: {response.text[:300]}",
                response.status_code,
            )
        return media_url

    # ------------------------------------------------------------------ posts

    @classmethod
    def create_post(cls, body: dict[str, Any]) -> str:
        """POST /social-posts. Returns the post id."""
        headers = cls._headers()
        with cls._client() as client:
            try:
                create = client.post(
                    f"{cls._base_url()}/social-posts", headers=headers, json=body
                )
                create.raise_for_status()
                post_id = str(_unwrap(create.json())["id"])
            except httpx.HTTPStatusError as e:
                raise PostForMeError(
                    f"create_post: {_response_detail(e.response)}",
                    e.response.status_code,
                ) from e
            except (httpx.HTTPError, KeyError, ValueError) as e:
                raise PostForMeError(f"create_post: {type(e).__name__}: {e}") from e
        logger.info(
            "PFM post created post_id=%s scheduled_at=%s",
            post_id,
            body.get("scheduled_at", "instant"),
        )
        return post_id

    @classmethod
    def update_post(cls, post_id: str, body: dict[str, Any]) -> None:
        """PUT /social-posts/{id} — full-body update (used for reschedules).

        PFM's update semantics for scheduled posts are best-effort: callers
        must be ready to fall back to delete_post + create_post.
        """
        headers = cls._headers()
        with cls._client() as client:
            try:
                response = client.put(
                    f"{cls._base_url()}/social-posts/{post_id}",
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise PostForMeError(
                    f"update_post: {_response_detail(e.response)}",
                    e.response.status_code,
                ) from e
            except httpx.HTTPError as e:
                raise PostForMeError(f"update_post: {type(e).__name__}: {e}") from e
        logger.info(
            "PFM post updated post_id=%s scheduled_at=%s",
            post_id,
            body.get("scheduled_at", "instant"),
        )

    @classmethod
    def delete_post(cls, post_id: str) -> None:
        """DELETE /social-posts/{id}. 404 (already gone) is treated as success."""
        headers = cls._headers()
        with cls._client() as client:
            try:
                response = client.delete(
                    f"{cls._base_url()}/social-posts/{post_id}", headers=headers
                )
                if response.status_code == 404:
                    return
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise PostForMeError(
                    f"delete_post: {_response_detail(e.response)}",
                    e.response.status_code,
                ) from e
            except httpx.HTTPError as e:
                raise PostForMeError(f"delete_post: {type(e).__name__}: {e}") from e
        logger.info("PFM post deleted post_id=%s", post_id)

    # ---------------------------------------------------------------- results

    @classmethod
    def fetch_outcome(
        cls, post_id: str, social_account_id: str | None = None
    ) -> PfmPublishOutcome | None:
        """Single-shot GET /social-post-results. None while still pending."""
        headers = cls._headers()
        with cls._client() as client:
            try:
                response = client.get(
                    f"{cls._base_url()}/social-post-results",
                    headers=headers,
                    params={"post_id": post_id},
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as e:
                raise PostForMeError(
                    f"fetch_results: {_response_detail(e.response)}",
                    e.response.status_code,
                ) from e
            except (httpx.HTTPError, ValueError) as e:
                raise PostForMeError(f"fetch_results: {type(e).__name__}: {e}") from e
        results = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            return None
        result = next(
            (
                r
                for r in results
                if isinstance(r, dict)
                and social_account_id is not None
                and r.get("social_account_id") == social_account_id
            ),
            results[0],
        )
        if not isinstance(result, dict):
            return None
        if result.get("success"):
            platform_data = result.get("platform_data") or {}
            url = _derive_tiktok_video_url(platform_data) or platform_data.get("url")
            return PfmPublishOutcome(success=True, url=url)
        return PfmPublishOutcome(success=False, detail=_result_error_detail(result))

    @classmethod
    def poll_outcome(
        cls,
        post_id: str,
        social_account_id: str | None = None,
        *,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        poll_timeout: float = 10 * 60.0,
    ) -> PfmPublishOutcome:
        """Bounded blocking poll (urgent-immediate publishes). Timeout is not
        terminal for the post — PFM keeps processing; the status sync service
        resumes polling later."""
        started = time.monotonic()
        while True:
            outcome = cls.fetch_outcome(post_id, social_account_id)
            if outcome is not None:
                return outcome
            if time.monotonic() - started >= poll_timeout:
                return PfmPublishOutcome(
                    success=False,
                    detail=(
                        f"poll_results: timeout after {int(poll_timeout)}s; "
                        f"post_id={post_id}; resumable=true"
                    ),
                )
            time.sleep(poll_interval)
