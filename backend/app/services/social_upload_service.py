from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time
from typing import Callable, Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..config import settings
from ..models import VideoMetadataPayload
from ..utils.meta_graph import extract_graph_error as _extract_graph_error
from .meta_token_service import MetaTokenService


@dataclass
class PlatformUploadResult:
    platform: str
    status: str  # uploaded | skipped | failed
    url: str | None = None
    resource_id: str | None = None
    detail: str | None = None
    quota_exceeded: bool = False


def _extract_http_error_detail(exc: HttpError) -> str:
    """Return a readable message from Google API HttpError."""
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        error = payload.get("error", {})
        message = error.get("message")
        reasons = [
            str(item.get("reason", ""))
            for item in error.get("errors", [])
            if isinstance(item, dict) and item.get("reason")
        ]
        parts: list[str] = []
        if message:
            parts.append(str(message))
        if reasons:
            parts.append(f"reasons={','.join(reasons)}")
        if parts:
            return " | ".join(parts)
    except Exception:
        pass
    return str(exc)


def _is_youtube_auth_error(exc: HttpError) -> bool:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        error = payload.get("error", {})
        code = int(error.get("code", 0) or 0)
        reasons = {
            str(item.get("reason", "")).lower()
            for item in error.get("errors", [])
            if isinstance(item, dict)
        }
        auth_reasons = {
            "unauthorized",
            "autherror",
            "insufficientpermissions",
            "forbidden",
            "youtubeaccountnotlinked",
            "youtubesignuprequired",
        }
        if code in {401, 403}:
            return True
        if any(reason in auth_reasons for reason in reasons):
            return True
    except Exception:
        pass
    return "unauthorized" in _extract_http_error_detail(exc).lower()


def _is_youtube_quota_error(exc: HttpError) -> bool:
    """Detect daily quota exhaustion from YouTube API errors."""
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        reasons = [
            str(item.get("reason", "")).lower()
            for item in payload.get("error", {}).get("errors", [])
            if isinstance(item, dict)
        ]
        quota_reasons = {
            "quotaexceeded",
            "dailylimitexceeded",
            "dailylimitexceededunreg",
            "userratelimitexceeded",
            "ratelimitexceeded",
        }
        if any(reason in quota_reasons for reason in reasons):
            return True
    except Exception:
        pass

    detail = _extract_http_error_detail(exc).lower()
    quota_markers = (
        "quotaexceeded",
        "dailylimitexceeded",
        "exceeded your quota",
    )
    return any(marker in detail for marker in quota_markers)


class SocialUploadService:
    """Uploads to YouTube/Facebook/Instagram with metadata payloads."""
    _RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}
    _MAX_REQUEST_ATTEMPTS = 4
    _RETRY_BASE_DELAY_SECONDS = 1.0
    _SUPPORTED_YOUTUBE_LANGUAGES = {"fr", "en", "es"}

    @classmethod
    def _graph_base(cls) -> str:
        return f"https://graph.facebook.com/{settings.meta_graph_api_version}"

    @classmethod
    def _sleep_backoff(cls, attempt: int) -> None:
        time.sleep(cls._RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))

    @classmethod
    def _request_with_retries(
        cls,
        request_fn: Callable[[], requests.Response],
        *,
        max_attempts: int | None = None,
    ) -> requests.Response:
        attempts = max_attempts or cls._MAX_REQUEST_ATTEMPTS
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = request_fn()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= attempts:
                    raise
                cls._sleep_backoff(attempt)
                continue

            if response.status_code in cls._RETRY_STATUS_CODES and attempt < attempts:
                cls._sleep_backoff(attempt)
                continue
            return response

        if last_exc:
            raise last_exc
        raise RuntimeError("Request failed after retries")

    @classmethod
    def _graph_error_object(cls, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
            err = payload.get("error")
            if isinstance(err, dict):
                return err
        except Exception:
            pass
        return {}

    @classmethod
    def _is_page_token_required_error(cls, response: requests.Response) -> bool:
        err = cls._graph_error_object(response)
        code = err.get("code")
        message = str(err.get("message") or "").lower()
        if code == 210:
            return True
        return "page access token is required" in message

    @classmethod
    def is_youtube_configured(cls) -> bool:
        return bool(
            settings.youtube_google_client_id
            and settings.youtube_google_client_secret
            and settings.youtube_google_refresh_token
        )

    @classmethod
    def _google_credentials(cls) -> Credentials:
        if not cls.is_youtube_configured():
            raise RuntimeError(
                "YouTube OAuth is not configured. Set ATR_GOOGLE_CLIENT_ID, "
                "ATR_GOOGLE_CLIENT_SECRET, ATR_GOOGLE_YOUTUBE_REFRESH_TOKEN "
                "(or fallback ATR_GOOGLE_REFRESH_TOKEN)."
            )
        creds = Credentials(
            token=None,
            refresh_token=settings.youtube_google_refresh_token,
            token_uri=settings.youtube_google_token_uri,
            client_id=settings.youtube_google_client_id,
            client_secret=settings.youtube_google_client_secret,
            scopes=[
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.force-ssl",
            ],
        )
        creds.refresh(Request())
        return creds

    @classmethod
    def youtube_credentials(cls) -> Credentials:
        return cls._google_credentials()

    @classmethod
    def _list_mine_youtube_channels(cls, youtube) -> list[dict[str, Any]]:
        channels: list[dict[str, Any]] = []
        request = youtube.channels().list(part="id,snippet", mine=True, maxResults=50)
        while request is not None:
            response = request.execute()
            batch = response.get("items", [])
            if isinstance(batch, list):
                channels.extend(item for item in batch if isinstance(item, dict))
            request = youtube.channels().list_next(request, response)
        return channels

    @classmethod
    def _expected_youtube_channel_id(cls) -> str | None:
        value = (settings.youtube_channel_id or "").strip()
        return value or None

    @classmethod
    def _validate_youtube_target_channel(cls, youtube) -> tuple[str | None, str | None]:
        channels = cls._list_mine_youtube_channels(youtube)
        if not channels:
            return (
                None,
                "Authenticated but no YouTube channel returned for current account.",
            )

        expected = cls._expected_youtube_channel_id()
        if not expected:
            return None, None

        channel_ids = {str(item.get("id") or "") for item in channels}
        if expected in channel_ids:
            return expected, None

        available = ", ".join(
            f"{item.get('id')} ({item.get('snippet', {}).get('title', 'unknown')})"
            for item in channels[:10]
        ) or "none"
        return (
            None,
            f"Configured ATR_YOUTUBE_CHANNEL_ID={expected} is not available for this token. "
            f"Available channels: {available}",
        )

    @classmethod
    def _uploaded_video_channel_id(cls, youtube, video_id: str) -> str | None:
        response = youtube.videos().list(part="snippet", id=video_id, maxResults=1).execute()
        items = response.get("items", [])
        if not isinstance(items, list) or not items:
            return None
        first = items[0] if isinstance(items[0], dict) else {}
        snippet = first.get("snippet") if isinstance(first, dict) else {}
        if isinstance(snippet, dict) and snippet.get("channelId"):
            return str(snippet["channelId"])
        return None

    @classmethod
    def _youtube_language_code(cls, *, target_language: str | None, subtitle_locale: str | None) -> str:
        if target_language:
            lang = target_language.split("_")[0].lower()
            if lang in cls._SUPPORTED_YOUTUBE_LANGUAGES:
                return lang
        if subtitle_locale:
            lang = subtitle_locale.split("_")[0].lower()
            if lang in cls._SUPPORTED_YOUTUBE_LANGUAGES:
                return lang
        return "fr"

    @classmethod
    def upload_youtube(
        cls,
        *,
        video_path: Path,
        subtitle_path: Path,
        subtitle_locale: str,
        target_language: str | None,
        metadata: VideoMetadataPayload,
    ) -> PlatformUploadResult:
        try:
            youtube = build("youtube", "v3", credentials=cls._google_credentials(), cache_discovery=False)
            expected_channel_id, channel_error = cls._validate_youtube_target_channel(youtube)
            if channel_error:
                return PlatformUploadResult(
                    platform="youtube",
                    status="failed",
                    detail=channel_error,
                )
            youtube_language = cls._youtube_language_code(
                target_language=target_language,
                subtitle_locale=subtitle_locale,
            )

            upload_request = youtube.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": metadata.youtube.title,
                        "description": metadata.youtube.description,
                        "tags": metadata.youtube.tags,
                        "categoryId": settings.youtube_category_id,
                        "defaultLanguage": youtube_language,
                        "defaultAudioLanguage": youtube_language,
                    },
                    "status": {
                        "privacyStatus": "private",
                        "selfDeclaredMadeForKids": False,
                        "containsSyntheticMedia": False,
                    },
                },
                media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
            )
            response = upload_request.execute()
            video_id = response["id"]
            if expected_channel_id:
                actual_channel_id = cls._uploaded_video_channel_id(youtube, video_id)
                if actual_channel_id != expected_channel_id:
                    try:
                        youtube.videos().delete(id=video_id).execute()
                    except Exception:
                        pass
                    return PlatformUploadResult(
                        platform="youtube",
                        status="failed",
                        detail=(
                            "YouTube upload was created under an unexpected channel and has been deleted. "
                            f"Expected={expected_channel_id}, actual={actual_channel_id or 'unknown'}"
                        ),
                    )

            youtube.captions().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "language": youtube_language,
                        "isDraft": False,
                    }
                },
                media_body=MediaFileUpload(str(subtitle_path), mimetype="application/octet-stream"),
            ).execute()

            return PlatformUploadResult(
                platform="youtube",
                status="uploaded",
                url=f"https://youtu.be/{video_id}",
                resource_id=video_id,
            )
        except HttpError as exc:
            quota_exceeded = _is_youtube_quota_error(exc)
            detail = _extract_http_error_detail(exc)
            if _is_youtube_auth_error(exc):
                detail = (
                    f"{detail} | Hint: the Google token must include "
                    "youtube.upload + youtube.force-ssl scopes and belong to an account "
                    "with an active YouTube channel."
                )
            return PlatformUploadResult(
                platform="youtube",
                status="failed",
                detail=detail,
                quota_exceeded=quota_exceeded,
            )
        except Exception as exc:
            return PlatformUploadResult(
                platform="youtube",
                status="failed",
                detail=str(exc),
            )

    @classmethod
    def upload_facebook(
        cls,
        *,
        video_path: Path,
        subtitle_path: Path,
        subtitle_locale: str,
        metadata: VideoMetadataPayload,
        video_url: str | None = None,
    ) -> PlatformUploadResult:
        try:
            creds = MetaTokenService.get_upload_credentials()
        except Exception as exc:
            return PlatformUploadResult(
                platform="facebook",
                status="skipped",
                detail=f"Facebook token resolution failed: {exc}",
            )

        page_id = creds.page_id
        token = creds.facebook_page_access_token
        if not page_id or not token:
            return PlatformUploadResult(
                platform="facebook",
                status="skipped",
                detail="Facebook API credentials are not configured",
            )

        base = cls._graph_base()
        try:
            with requests.Session() as session:
                source_mode = "local"
                video_id: str | None = None

                # Fast path: let Meta ingest directly from the public Drive URL.
                if video_url:
                    url_resp = cls._request_with_retries(
                        lambda: session.post(
                            f"{base}/{page_id}/videos",
                            data={
                                "title": metadata.facebook.title,
                                "description": metadata.facebook.description,
                                "published": "false",
                                "access_token": token,
                                "file_url": video_url,
                            },
                            timeout=120,
                        ),
                        max_attempts=3,
                    )
                    if url_resp.status_code < 400:
                        payload = url_resp.json()
                        candidate = payload.get("id")
                        if candidate:
                            video_id = str(candidate)
                            source_mode = "drive_url"

                # Fallback path: upload bytes from local file.
                if not video_id:
                    def _upload_video_once() -> requests.Response:
                        with video_path.open("rb") as source:
                            return session.post(
                                f"{base}/{page_id}/videos",
                                data={
                                    "title": metadata.facebook.title,
                                    "description": metadata.facebook.description,
                                    "published": "false",
                                    "access_token": token,
                                },
                                files={"source": source},
                                timeout=1200,
                            )

                    resp = cls._request_with_retries(_upload_video_once, max_attempts=3)
                    if resp.status_code >= 400:
                        detail = _extract_graph_error(resp)
                        if cls._is_page_token_required_error(resp):
                            detail = (
                                f"{detail} "
                                "(configured token is not page-scoped for video publishing; "
                                "provide a real page access token or allow derivation from page fields)."
                            )
                        return PlatformUploadResult(
                            platform="facebook",
                            status="failed",
                            detail=f"Video upload failed: {detail}",
                        )
                    payload = resp.json()
                    candidate = payload.get("id")
                    if not candidate:
                        raise RuntimeError(f"Unexpected Facebook response: {payload}")
                    video_id = str(candidate)

                # Caption upload (required for platform success in this workflow).
                cap_resp = cls._upload_facebook_caption_with_wait(
                    session=session,
                    base=base,
                    video_id=video_id,
                    token=token,
                    subtitle_path=subtitle_path,
                    subtitle_locale=subtitle_locale,
                )

            if cap_resp.status_code >= 400:
                # If subtitle upload is unavailable, this platform is skipped by policy.
                try:
                    with requests.Session() as session:
                        session.delete(
                            f"{base}/{video_id}",
                            params={"access_token": token},
                            timeout=20,
                        )
                except Exception:
                    pass
                return PlatformUploadResult(
                    platform="facebook",
                    status="skipped",
                    detail=f"Subtitle upload unsupported or rejected: {_extract_graph_error(cap_resp)}",
                )

            return PlatformUploadResult(
                platform="facebook",
                status="uploaded",
                url=f"https://www.facebook.com/{video_id}",
                resource_id=video_id,
                detail="Uploaded via Drive URL ingestion" if source_mode == "drive_url" else None,
            )
        except Exception as exc:
            return PlatformUploadResult(
                platform="facebook",
                status="failed",
                detail=str(exc),
            )

    @classmethod
    def upload_instagram(
        cls,
        *,
        video_path: Path,
        metadata: VideoMetadataPayload,
        preferred_video_url: str | None = None,
    ) -> PlatformUploadResult:
        try:
            creds = MetaTokenService.get_upload_credentials()
        except Exception as exc:
            return PlatformUploadResult(
                platform="instagram",
                status="skipped",
                detail=f"Instagram token resolution failed: {exc}",
            )

        ig_user_id = creds.instagram_business_account_id
        token = creds.instagram_access_token
        if not ig_user_id or not token:
            return PlatformUploadResult(
                platform="instagram",
                status="skipped",
                detail="Instagram API credentials are not configured",
            )

        try:
            base = cls._graph_base()
            with requests.Session() as session:
                ingestion_mode = "resumable_local"
                container_id: str | None = None

                if preferred_video_url:
                    try:
                        container_id = cls._create_instagram_container_url(
                            session=session,
                            base=base,
                            ig_user_id=ig_user_id,
                            token=token,
                            caption=metadata.instagram.caption,
                            video_url=preferred_video_url,
                        )
                        ingestion_mode = "drive_url"
                    except Exception:
                        container_id = None

                if not container_id:
                    container_id = cls._create_instagram_container_resumable(
                        session=session,
                        base=base,
                        ig_user_id=ig_user_id,
                        token=token,
                        caption=metadata.instagram.caption,
                        video_path=video_path,
                    )

                cls._poll_instagram_container_ready(
                    session=session,
                    base=base,
                    container_id=container_id,
                    token=token,
                )

                publish_resp = cls._request_with_retries(
                    lambda: session.post(
                        f"{base}/{ig_user_id}/media_publish",
                        data={
                            "creation_id": container_id,
                            "access_token": token,
                        },
                        timeout=120,
                    ),
                    max_attempts=3,
                )
                if publish_resp.status_code >= 400:
                    return PlatformUploadResult(
                        platform="instagram",
                        status="failed",
                        detail=f"media_publish failed: {_extract_graph_error(publish_resp)}",
                    )
                media_id = str(publish_resp.json().get("id") or "")
                if not media_id:
                    return PlatformUploadResult(
                        platform="instagram",
                        status="failed",
                        detail=f"media_publish returned no media id: {publish_resp.text}",
                    )

                permalink = None
                media_info = cls._request_with_retries(
                    lambda: session.get(
                        f"{base}/{media_id}",
                        params={"fields": "permalink", "access_token": token},
                        timeout=60,
                    ),
                    max_attempts=2,
                )
                if media_info.status_code < 400:
                    permalink = media_info.json().get("permalink")
                if not permalink:
                    permalink = f"https://www.instagram.com/p/{media_id}/"

            return PlatformUploadResult(
                platform="instagram",
                status="uploaded",
                url=permalink,
                resource_id=media_id,
                detail=(
                    "Published without sidecar subtitle (not supported by Instagram Graph API); "
                    f"ingestion mode={ingestion_mode}"
                ),
            )
        except Exception as exc:
            return PlatformUploadResult(
                platform="instagram",
                status="failed",
                detail=str(exc),
            )

    @classmethod
    def _upload_facebook_caption_with_wait(
        cls,
        *,
        session: requests.Session,
        base: str,
        video_id: str,
        token: str,
        subtitle_path: Path,
        subtitle_locale: str,
    ) -> requests.Response:
        attempts = 6
        last_response: requests.Response | None = None
        for attempt in range(1, attempts + 1):
            def _upload_caption_once() -> requests.Response:
                with subtitle_path.open("rb") as captions_file:
                    return session.post(
                        f"{base}/{video_id}/captions",
                        data={
                            "access_token": token,
                            "locale": subtitle_locale,
                        },
                        files={"captions_file": captions_file},
                        timeout=120,
                    )

            cap_resp = cls._request_with_retries(_upload_caption_once, max_attempts=3)
            last_response = cap_resp
            if cap_resp.status_code < 400:
                return cap_resp
            if attempt >= attempts or not cls._is_facebook_caption_retryable(cap_resp):
                return cap_resp
            time.sleep(min(30, attempt * 5))

        if last_response is None:
            raise RuntimeError("Facebook caption upload failed before a response was received")
        return last_response

    @classmethod
    def _is_facebook_caption_retryable(cls, response: requests.Response) -> bool:
        if response.status_code in cls._RETRY_STATUS_CODES:
            return True
        message = _extract_graph_error(response).lower()
        markers = (
            "processing",
            "transcod",
            "not ready",
            "please wait",
            "try again",
            "temporarily unavailable",
        )
        return any(marker in message for marker in markers)

    @classmethod
    def _create_instagram_container_resumable(
        cls,
        *,
        session: requests.Session,
        base: str,
        ig_user_id: str,
        token: str,
        caption: str,
        video_path: Path,
    ) -> str:
        container_resp = cls._request_with_retries(
            lambda: session.post(
                f"{base}/{ig_user_id}/media",
                data={
                    "media_type": "REELS",
                    "upload_type": "resumable",
                    "caption": caption,
                    "share_to_feed": "true",
                    "access_token": token,
                },
                timeout=120,
            ),
            max_attempts=3,
        )
        if container_resp.status_code >= 400:
            raise RuntimeError(
                f"Instagram container creation failed: {_extract_graph_error(container_resp)}"
            )

        payload = container_resp.json()
        container_id = payload.get("id")
        upload_uri = payload.get("uri")
        if not container_id:
            raise RuntimeError(f"Instagram container creation returned no id: {payload}")
        if not upload_uri:
            raise RuntimeError(f"Instagram resumable container returned no upload URI: {payload}")

        file_size = video_path.stat().st_size
        def _upload_once() -> requests.Response:
            with video_path.open("rb") as source:
                return session.post(
                    str(upload_uri),
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset": "0",
                        "file_size": str(file_size),
                        "Content-Type": "application/octet-stream",
                    },
                    data=source,
                    timeout=1800,
                )

        upload_resp = cls._request_with_retries(_upload_once, max_attempts=2)

        if upload_resp.status_code >= 400:
            raise RuntimeError(
                f"Instagram resumable upload failed: {_extract_graph_error(upload_resp)}"
            )

        return str(container_id)

    @classmethod
    def _create_instagram_container_url(
        cls,
        *,
        session: requests.Session,
        base: str,
        ig_user_id: str,
        token: str,
        caption: str,
        video_url: str,
    ) -> str:
        container_resp = cls._request_with_retries(
            lambda: session.post(
                f"{base}/{ig_user_id}/media",
                data={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": caption,
                    "share_to_feed": "true",
                    "access_token": token,
                },
                timeout=120,
            ),
            max_attempts=3,
        )
        if container_resp.status_code >= 400:
            raise RuntimeError(
                f"Instagram video_url container creation failed: {_extract_graph_error(container_resp)}"
            )
        payload = container_resp.json()
        container_id = payload.get("id")
        if not container_id:
            raise RuntimeError(f"Instagram video_url container returned no id: {payload}")
        return str(container_id)

    @classmethod
    def _poll_instagram_container_ready(
        cls,
        *,
        session: requests.Session,
        base: str,
        container_id: str,
        token: str,
    ) -> None:
        timeout_seconds = max(settings.instagram_publish_timeout_seconds, 30)
        interval_seconds = max(settings.instagram_publish_poll_interval_seconds, 2)
        deadline = time.monotonic() + timeout_seconds
        last_status = ""

        while time.monotonic() < deadline:
            status_resp = cls._request_with_retries(
                lambda: session.get(
                    f"{base}/{container_id}",
                    params={
                        # "error_message" is not available on all IG container node types.
                        "fields": "status_code,status",
                        "access_token": token,
                    },
                    timeout=60,
                ),
                max_attempts=3,
            )
            if status_resp.status_code >= 400:
                raise RuntimeError(
                    f"Instagram container status failed: {_extract_graph_error(status_resp)}"
                )

            payload = status_resp.json()
            status_code = str(payload.get("status_code") or "").upper()
            status_text = str(payload.get("status") or "")
            error_message = str(payload.get("error_message") or payload.get("message") or "").strip()
            effective_status = status_code or status_text.upper()
            if effective_status == "FINISHED":
                return
            if effective_status in {"ERROR", "EXPIRED"}:
                detail = error_message or status_text or status_code or "Unknown container error"
                raise RuntimeError(f"Instagram container failed: {detail}")
            if effective_status:
                last_status = effective_status

            time.sleep(interval_seconds)

        suffix = f" (last status: {last_status})" if last_status else ""
        raise TimeoutError(
            f"Instagram container did not reach FINISHED within {timeout_seconds}s{suffix}"
        )
