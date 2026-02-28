from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import subprocess
import tempfile
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

logger = logging.getLogger("uvicorn.error")


@dataclass
class PlatformUploadResult:
    platform: str
    status: str  # uploaded | skipped | failed
    url: str | None = None
    resource_id: str | None = None
    detail: str | None = None
    quota_exceeded: bool = False


@dataclass
class FacebookMediaProbe:
    duration_seconds: float | None
    has_audio: bool


@dataclass
class FacebookVideoPreparation:
    status: str  # ready | skip | error
    video_path: Path | None = None
    detail: str | None = None
    transcoded: bool = False
    original_duration_seconds: float | None = None
    speed_factor: float | None = None


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
    _FACEBOOK_MAX_DURATION_SECONDS = 90.0
    _FACEBOOK_MAX_SPEED_FACTOR = 1.40
    _FACEBOOK_MAX_ACCEL_PERCENT = 40.0

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
    def _is_instagram_container_field_error(cls, response: requests.Response) -> bool:
        err = cls._graph_error_object(response)
        message = str(err.get("message") or "").lower()
        code = int(err.get("code") or 0) if err.get("code") is not None else 0
        subcode = int(err.get("error_subcode") or 0) if err.get("error_subcode") is not None else 0
        if code != 100:
            return False
        markers = (
            "nonexisting field",
            "invalid parameter",
            "not a valid parameter",
            "paramètre non valide",
        )
        return subcode == 2207065 or any(marker in message for marker in markers)

    @classmethod
    def _is_instagram_resumable_upload_indeterminate(cls, response: requests.Response) -> bool:
        try:
            payload = response.json()
        except Exception:
            return False
        debug_info = payload.get("debug_info")
        if not isinstance(debug_info, dict):
            return False
        debug_type = str(debug_info.get("type") or "").strip().lower()
        debug_message = str(debug_info.get("message") or "").strip().lower()
        return debug_type == "processingfailederror" and "request processing failed" in debug_message

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
        credentials: Credentials | None = None,
        scheduled_at: datetime | None = None,
        category_id: str | None = None,
        channel_id: str | None = None,
    ) -> PlatformUploadResult:
        try:
            creds = credentials if credentials is not None else cls._google_credentials()
            if credentials is not None:
                from google.auth.transport.requests import Request as _Request
                creds.refresh(_Request())
            youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
            if channel_id is not None:
                expected_channel_id = channel_id or None
            else:
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

            effective_category = category_id or settings.youtube_category_id

            if scheduled_at:
                privacy_status = "private"
                publish_at = scheduled_at.isoformat()
            else:
                privacy_status = "public"
                publish_at = None

            status_body: dict[str, Any] = {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": False,
            }
            if publish_at:
                status_body["publishAt"] = publish_at

            upload_request = youtube.videos().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": metadata.youtube.title,
                        "description": metadata.youtube.description,
                        "tags": metadata.youtube.tags,
                        "categoryId": effective_category,
                        "defaultLanguage": youtube_language,
                        "defaultAudioLanguage": youtube_language,
                    },
                    "status": status_body,
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
                        "name": youtube_language,
                        "isDraft": False,
                    }
                },
                media_body=MediaFileUpload(str(subtitle_path), mimetype="application/octet-stream"),
            ).execute()

            detail_parts = []
            if scheduled_at:
                detail_parts.append(f"Scheduled for {scheduled_at.isoformat()}")

            return PlatformUploadResult(
                platform="youtube",
                status="uploaded",
                url=f"https://youtu.be/{video_id}",
                resource_id=video_id,
                detail="; ".join(detail_parts) if detail_parts else None,
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
    def _probe_facebook_media(
        cls,
        *,
        video_path: Path,
    ) -> tuple[FacebookMediaProbe | None, str | None]:
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError:
            return None, "ffprobe is not available on the server."
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            detail = stderr[:300] if stderr else str(exc)
            return None, f"ffprobe failed: {detail}"
        except Exception as exc:
            return None, f"ffprobe failed: {exc}"

        try:
            payload = json.loads(probe.stdout or "{}")
        except Exception:
            return None, "ffprobe returned invalid JSON."

        duration_seconds: float | None = None
        fmt = payload.get("format")
        if isinstance(fmt, dict):
            try:
                duration_seconds = float(fmt.get("duration")) if fmt.get("duration") is not None else None
            except Exception:
                duration_seconds = None

        has_audio = False
        streams = payload.get("streams")
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                codec_type = str(stream.get("codec_type") or "").strip().lower()
                if codec_type == "audio":
                    has_audio = True
                    break

        return FacebookMediaProbe(duration_seconds=duration_seconds, has_audio=has_audio), None

    @classmethod
    def _transcode_facebook_video_to_limit(
        cls,
        *,
        input_path: Path,
        output_path: Path,
        speed_factor: float,
        has_audio: bool,
    ) -> str | None:
        speed_value = f"{speed_factor:.6f}"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-filter:v",
            f"setpts=PTS/{speed_value}",
        ]
        if has_audio:
            cmd.extend(
                [
                    "-map",
                    "0:a:0?",
                    "-filter:a",
                    f"atempo={speed_value}",
                ]
            )
        else:
            cmd.extend(["-an"])

        cmd.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if has_audio:
            cmd.extend(
                [
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                ]
            )
        cmd.extend(
            [
                "-movflags",
                "+faststart",
                "-t",
                f"{int(cls._FACEBOOK_MAX_DURATION_SECONDS)}",
                str(output_path),
            ]
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return "ffmpeg is not available on the server."
        except Exception as exc:
            return f"ffmpeg transcoding failed: {exc}"

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
            return f"ffmpeg transcoding failed: {detail[:300]}"
        if not output_path.exists() or output_path.stat().st_size <= 0:
            return "ffmpeg transcoding produced an empty output file."
        return None

    @classmethod
    def _cut_facebook_video(
        cls,
        *,
        input_path: Path,
        output_path: Path,
    ) -> str | None:
        """Hard-cut a video at the Facebook max duration (stream copy, no re-encode)."""
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-t",
            f"{int(cls._FACEBOOK_MAX_DURATION_SECONDS)}",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return "ffmpeg is not available on the server."
        except Exception as exc:
            return f"ffmpeg cut failed: {exc}"

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
            return f"ffmpeg cut failed: {detail[:300]}"
        if not output_path.exists() or output_path.stat().st_size <= 0:
            return "ffmpeg cut produced an empty output file."
        return None

    @classmethod
    def _prepare_facebook_video_for_upload(
        cls,
        *,
        source_video_path: Path,
        work_dir: Path,
    ) -> FacebookVideoPreparation:
        probe, probe_error = cls._probe_facebook_media(video_path=source_video_path)
        if probe_error:
            return FacebookVideoPreparation(
                status="error",
                detail=f"Facebook video preparation failed: {probe_error}",
            )
        if probe is None or probe.duration_seconds is None or probe.duration_seconds <= 0:
            return FacebookVideoPreparation(
                status="error",
                detail=(
                    "Facebook video preparation failed: "
                    "unable to detect a valid video duration via ffprobe."
                ),
            )

        duration_seconds = probe.duration_seconds
        if duration_seconds <= cls._FACEBOOK_MAX_DURATION_SECONDS + 0.01:
            return FacebookVideoPreparation(
                status="ready",
                video_path=source_video_path,
                transcoded=False,
                original_duration_seconds=duration_seconds,
                speed_factor=1.0,
            )

        speed_factor = duration_seconds / cls._FACEBOOK_MAX_DURATION_SECONDS
        accel_percent = (speed_factor - 1.0) * 100.0
        if speed_factor - cls._FACEBOOK_MAX_SPEED_FACTOR > 1e-6:
            return FacebookVideoPreparation(
                status="skip",
                detail=(
                    "Refus volontaire: vidéo trop longue "
                    f"({duration_seconds:.2f}s). "
                    f"Accélération requise +{accel_percent:.1f}% "
                    f"(>{cls._FACEBOOK_MAX_ACCEL_PERCENT:.0f}% max)."
                ),
                original_duration_seconds=duration_seconds,
                speed_factor=speed_factor,
            )

        work_dir.mkdir(parents=True, exist_ok=True)
        output_path = work_dir / f"{source_video_path.stem}.facebook_90s.mp4"
        transcode_error = cls._transcode_facebook_video_to_limit(
            input_path=source_video_path,
            output_path=output_path,
            speed_factor=speed_factor,
            has_audio=probe.has_audio,
        )
        if transcode_error:
            return FacebookVideoPreparation(
                status="error",
                detail=f"Facebook video preparation failed: {transcode_error}",
                original_duration_seconds=duration_seconds,
                speed_factor=speed_factor,
            )

        output_probe, output_probe_error = cls._probe_facebook_media(video_path=output_path)
        if output_probe_error:
            return FacebookVideoPreparation(
                status="error",
                detail=f"Facebook video preparation failed: {output_probe_error}",
                original_duration_seconds=duration_seconds,
                speed_factor=speed_factor,
            )
        output_duration = output_probe.duration_seconds if output_probe else None
        if output_duration is None or output_duration <= 0:
            return FacebookVideoPreparation(
                status="error",
                detail=(
                    "Facebook video preparation failed: "
                    "unable to detect transcoded video duration."
                ),
                original_duration_seconds=duration_seconds,
                speed_factor=speed_factor,
            )
        if output_duration > cls._FACEBOOK_MAX_DURATION_SECONDS + 0.2:
            return FacebookVideoPreparation(
                status="error",
                detail=(
                    "Facebook video preparation failed: "
                    f"transcoded video is still too long ({output_duration:.2f}s)."
                ),
                original_duration_seconds=duration_seconds,
                speed_factor=speed_factor,
            )

        return FacebookVideoPreparation(
            status="ready",
            video_path=output_path,
            transcoded=True,
            original_duration_seconds=duration_seconds,
            speed_factor=speed_factor,
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
        page_id: str | None = None,
        page_access_token: str | None = None,
        scheduled_at: datetime | None = None,
        facebook_strategy: str | None = None,
        facebook_prep_dir: Path | None = None,
    ) -> PlatformUploadResult:
        # Handle explicit user strategy choice
        strategy = (facebook_strategy or "auto").strip().lower()
        if strategy == "skip":
            return PlatformUploadResult(
                platform="facebook",
                status="skipped",
                detail="Refus volontaire par l'utilisateur: vidéo trop longue pour Facebook.",
            )

        # Use explicit per-account credentials if provided, else fall back to global
        if page_id and page_access_token:
            token = page_access_token
        else:
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

        # Scheduling: use Reels API 3-phase upload with video_state=SCHEDULED
        if scheduled_at:
            return cls._upload_facebook_reel_scheduled(
                video_path=video_path,
                subtitle_path=subtitle_path,
                subtitle_locale=subtitle_locale,
                metadata=metadata,
                page_id=page_id,
                token=token,
                scheduled_at=scheduled_at,
                facebook_strategy=strategy,
                facebook_prep_dir=facebook_prep_dir,
            )

        # Immediate publish: use standard /videos endpoint
        base = cls._graph_base()

        try:
            with tempfile.TemporaryDirectory(prefix="atr-fb-upload-") as prep_dir:
                # Apply user strategy (cut / sped_up / auto)
                if strategy == "cut":
                    cut_output = Path(prep_dir) / f"{video_path.stem}.facebook_cut.mp4"
                    cut_error = cls._cut_facebook_video(
                        input_path=video_path,
                        output_path=cut_output,
                    )
                    if cut_error:
                        return PlatformUploadResult(
                            platform="facebook",
                            status="failed",
                            detail=f"Facebook cut failed: {cut_error}",
                        )
                    prepared_video_path = cut_output
                    allow_drive_url_fast_path = False
                elif strategy == "sped_up":
                    # Try to reuse pre-cached sped up file from facebook-check
                    cached = (
                        facebook_prep_dir / "sped_up.mp4"
                        if facebook_prep_dir and (facebook_prep_dir / "sped_up.mp4").exists()
                        else None
                    )
                    if cached:
                        prepared_video_path = cached
                    else:
                        # Fallback: transcode on the fly
                        prep = cls._prepare_facebook_video_for_upload(
                            source_video_path=video_path,
                            work_dir=Path(prep_dir),
                        )
                        if prep.status != "ready" or prep.video_path is None:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=prep.detail or "Facebook sped-up transcoding failed.",
                            )
                        prepared_video_path = prep.video_path
                    allow_drive_url_fast_path = False
                else:
                    # "auto" — original behaviour
                    prep = cls._prepare_facebook_video_for_upload(
                        source_video_path=video_path,
                        work_dir=Path(prep_dir),
                    )
                    if prep.status == "skip":
                        return PlatformUploadResult(
                            platform="facebook",
                            status="skipped",
                            detail=prep.detail,
                        )
                    if prep.status == "error" or prep.video_path is None:
                        return PlatformUploadResult(
                            platform="facebook",
                            status="failed",
                            detail=prep.detail or "Facebook video preparation failed.",
                        )
                    prepared_video_path = prep.video_path
                    allow_drive_url_fast_path = bool(video_url and not prep.transcoded)

                with requests.Session() as session:
                    source_mode = "local"
                    video_id: str | None = None

                    # Fast path: let Meta ingest directly from the public Drive URL.
                    if allow_drive_url_fast_path and video_url:
                        url_resp = cls._request_with_retries(
                            lambda: session.post(
                                f"{base}/{page_id}/videos",
                                data={
                                    "title": metadata.facebook.title,
                                    "description": metadata.facebook.description,
                                    "published": "true",
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
                            with prepared_video_path.open("rb") as source:
                                return session.post(
                                    f"{base}/{page_id}/videos",
                                    data={
                                        "title": metadata.facebook.title,
                                        "description": metadata.facebook.description,
                                        "published": "true",
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
        ig_user_id: str | None = None,
        ig_access_token: str | None = None,
    ) -> PlatformUploadResult:
        # Use explicit per-account credentials if provided, else fall back to global
        if ig_user_id and ig_access_token:
            token = ig_access_token
        else:
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

        # Instagram API does not support scheduled_publish_time for Reels
        # and does not reliably ingest via URL; always use resumable local upload.

        try:
            base = cls._graph_base()
            with requests.Session() as session:
                container_id = cls._create_instagram_container_resumable(
                    session=session,
                    base=base,
                    ig_user_id=ig_user_id,
                    token=token,
                    caption=metadata.instagram.caption,
                    video_path=video_path,
                )
                media_id, permalink = cls._publish_instagram_container(
                    session=session,
                    base=base,
                    ig_user_id=ig_user_id,
                    token=token,
                    container_id=container_id,
                )

            return PlatformUploadResult(
                platform="instagram",
                status="uploaded",
                url=permalink,
                resource_id=media_id,
            )
        except Exception as exc:
            return PlatformUploadResult(
                platform="instagram",
                status="failed",
                detail=str(exc),
            )

    @classmethod
    def _upload_facebook_reel_scheduled(
        cls,
        *,
        video_path: Path,
        subtitle_path: Path,
        subtitle_locale: str,
        metadata: VideoMetadataPayload,
        page_id: str,
        token: str,
        scheduled_at: datetime,
        facebook_strategy: str = "auto",
        facebook_prep_dir: Path | None = None,
    ) -> PlatformUploadResult:
        """Schedule a Facebook Reel via 3-phase Reels API (video_state=SCHEDULED)."""
        base = cls._graph_base()
        max_flow_attempts = 2

        try:
            if not video_path.exists():
                return PlatformUploadResult(
                    platform="facebook",
                    status="failed",
                    detail=f"Facebook reel source video does not exist: {video_path}",
                )
            scheduled_utc = (
                scheduled_at.replace(tzinfo=timezone.utc)
                if scheduled_at.tzinfo is None
                else scheduled_at.astimezone(timezone.utc)
            )
            now_utc = datetime.now(timezone.utc)
            min_schedule = now_utc + timedelta(minutes=10)
            max_schedule = now_utc + timedelta(days=29)
            if scheduled_utc <= min_schedule:
                return PlatformUploadResult(
                    platform="facebook",
                    status="failed",
                    detail=(
                        "Facebook scheduled_publish_time must be more than 10 minutes in the future "
                        f"(scheduled={scheduled_utc.isoformat()}, now={now_utc.isoformat()})."
                    ),
                )
            if scheduled_utc > max_schedule:
                return PlatformUploadResult(
                    platform="facebook",
                    status="failed",
                    detail=(
                        "Facebook scheduled_publish_time must be within 29 days "
                        f"(scheduled={scheduled_utc.isoformat()}, now={now_utc.isoformat()})."
                    ),
                )
            scheduled_epoch = str(int(scheduled_utc.timestamp()))

            with tempfile.TemporaryDirectory(prefix="atr-fb-reel-") as prep_dir:
                # Apply user strategy (cut / sped_up / auto)
                if facebook_strategy == "cut":
                    cut_output = Path(prep_dir) / f"{video_path.stem}.facebook_cut.mp4"
                    cut_error = cls._cut_facebook_video(
                        input_path=video_path,
                        output_path=cut_output,
                    )
                    if cut_error:
                        return PlatformUploadResult(
                            platform="facebook",
                            status="failed",
                            detail=f"Facebook cut failed: {cut_error}",
                        )
                    prepared_video_path = cut_output
                elif facebook_strategy == "sped_up":
                    cached = (
                        facebook_prep_dir / "sped_up.mp4"
                        if facebook_prep_dir and (facebook_prep_dir / "sped_up.mp4").exists()
                        else None
                    )
                    if cached:
                        prepared_video_path = cached
                    else:
                        prep = cls._prepare_facebook_video_for_upload(
                            source_video_path=video_path,
                            work_dir=Path(prep_dir),
                        )
                        if prep.status != "ready" or prep.video_path is None:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=prep.detail or "Facebook sped-up transcoding failed.",
                            )
                        prepared_video_path = prep.video_path
                else:
                    # "auto" — original behaviour
                    prep = cls._prepare_facebook_video_for_upload(
                        source_video_path=video_path,
                        work_dir=Path(prep_dir),
                    )
                    if prep.status == "skip":
                        return PlatformUploadResult(
                            platform="facebook",
                            status="skipped",
                            detail=prep.detail,
                        )
                    if prep.status == "error" or prep.video_path is None:
                        return PlatformUploadResult(
                            platform="facebook",
                            status="failed",
                            detail=prep.detail or "Facebook video preparation failed.",
                        )
                    prepared_video_path = prep.video_path

                file_size = prepared_video_path.stat().st_size
                if file_size <= 0:
                    return PlatformUploadResult(
                        platform="facebook",
                        status="failed",
                        detail="Facebook reel source video is empty (0 bytes).",
                    )
                media_validation_error = cls._validate_facebook_reel_media(video_path=prepared_video_path)
                if media_validation_error:
                    return PlatformUploadResult(
                        platform="facebook",
                        status="failed",
                        detail=media_validation_error,
                    )

                last_retryable_finish_error: str | None = None
                for flow_attempt in range(1, max_flow_attempts + 1):
                    with requests.Session() as session:
                        # Phase 1: Start — initialize upload session
                        start_resp = cls._request_with_retries(
                            lambda: session.post(
                                f"{base}/{page_id}/video_reels",
                                data={
                                    "upload_phase": "start",
                                    "access_token": token,
                                },
                                timeout=60,
                            ),
                            max_attempts=3,
                        )
                        if start_resp.status_code >= 400:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel start phase failed: {_extract_graph_error(start_resp)}",
                            )
                        start_payload = start_resp.json()
                        video_id = start_payload.get("video_id")
                        upload_url = start_payload.get("upload_url")
                        if not video_id:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel start phase returned no video_id: {start_payload}",
                            )
                        if not upload_url:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel start phase returned no upload_url: {start_payload}",
                            )

                        # Phase 2: Upload binary to rupload endpoint using the exact upload_url from START.
                        upload_resp = cls._upload_facebook_reel_binary(
                            session=session,
                            upload_url=str(upload_url),
                            token=token,
                            video_path=prepared_video_path,
                            file_size=file_size,
                            offset=0,
                            max_attempts=2,
                        )
                        if upload_resp.status_code >= 400:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel upload phase failed ({upload_resp.status_code}): {upload_resp.text[:300]}",
                            )
                        upload_problem = cls._facebook_reel_upload_response_problem(upload_resp)
                        if upload_problem:
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel upload phase returned an invalid payload: {upload_problem}",
                            )

                        # Facebook docs recommend checking /{video-id}?fields=status and resuming interrupted uploads.
                        upload_ready, upload_ready_detail = cls._ensure_facebook_reel_upload_ready_for_finish(
                            session=session,
                            base=base,
                            video_id=str(video_id),
                            upload_url=str(upload_url),
                            token=token,
                            video_path=prepared_video_path,
                            file_size=file_size,
                        )
                        if not upload_ready:
                            if flow_attempt < max_flow_attempts:
                                last_retryable_finish_error = upload_ready_detail
                                logger.warning(
                                    "Facebook reel upload not ready for finish on attempt %s/%s (video_id=%s): %s. "
                                    "Retrying with a fresh START session.",
                                    flow_attempt,
                                    max_flow_attempts,
                                    video_id,
                                    upload_ready_detail,
                                )
                                cls._cleanup_failed_facebook_video(
                                    session=session,
                                    base=base,
                                    token=token,
                                    video_id=str(video_id),
                                )
                                time.sleep(min(30, 5 * flow_attempt))
                                continue
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel upload did not reach a finishable state: {upload_ready_detail}",
                            )

                        # Phase 3: Finish with video_state=SCHEDULED
                        finish_resp = cls._request_with_retries(
                            lambda: session.post(
                                f"{base}/{page_id}/video_reels",
                                data={
                                    "upload_phase": "finish",
                                    "video_id": video_id,
                                    "access_token": token,
                                    "video_state": "SCHEDULED",
                                    "scheduled_publish_time": scheduled_epoch,
                                    "title": metadata.facebook.title,
                                    "description": metadata.facebook.description,
                                },
                                timeout=120,
                            ),
                            max_attempts=3,
                        )
                        if finish_resp.status_code >= 400:
                            finish_error = _extract_graph_error(finish_resp)
                            status_payload = cls._get_facebook_video_status_payload(
                                session=session,
                                base=base,
                                video_id=str(video_id),
                                token=token,
                            )
                            if status_payload:
                                finish_error = (
                                    f"{finish_error} | "
                                    f"video_status={cls._summarize_facebook_video_status(status_payload)}"
                                )
                            if (
                                flow_attempt < max_flow_attempts
                                and cls._is_facebook_reel_finish_retryable_error(finish_resp)
                            ):
                                last_retryable_finish_error = finish_error
                                logger.warning(
                                    "Facebook reel finish transient failure on attempt %s/%s (video_id=%s): %s. Retrying full flow.",
                                    flow_attempt,
                                    max_flow_attempts,
                                    video_id,
                                    finish_error,
                                )
                                cls._cleanup_failed_facebook_video(
                                    session=session,
                                    base=base,
                                    token=token,
                                    video_id=str(video_id),
                                )
                                time.sleep(min(30, 5 * flow_attempt))
                                continue
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel finish phase failed: {finish_error}",
                            )

                        finish_payload = finish_resp.json()
                        if not cls._is_facebook_reel_finish_payload_valid(
                            finish_payload=finish_payload,
                            expected_video_id=str(video_id),
                        ):
                            return PlatformUploadResult(
                                platform="facebook",
                                status="failed",
                                detail=f"Reel finish phase returned ambiguous payload: {finish_payload}",
                            )

                        # Upload captions (non-fatal for the reel itself)
                        cap_resp = cls._upload_facebook_caption_with_wait(
                            session=session,
                            base=base,
                            video_id=str(video_id),
                            token=token,
                            subtitle_path=subtitle_path,
                            subtitle_locale=subtitle_locale,
                        )
                        caption_detail = ""
                        if cap_resp.status_code >= 400:
                            logger.warning(
                                "Facebook scheduled captions failed for video_id=%s status=%s error=%s",
                                video_id,
                                cap_resp.status_code,
                                _extract_graph_error(cap_resp),
                            )
                            caption_detail = f"; captions failed: {_extract_graph_error(cap_resp)}"

                        return PlatformUploadResult(
                            platform="facebook",
                            status="uploaded",
                            url=f"https://www.facebook.com/reel/{video_id}",
                            resource_id=str(video_id),
                            detail=f"Reel scheduled for {scheduled_at.isoformat()}{caption_detail}",
                        )

                retry_detail = (
                    f"Reel finish phase failed after retries: {last_retryable_finish_error}"
                    if last_retryable_finish_error
                    else "Reel finish phase failed after retries"
                )
                return PlatformUploadResult(
                    platform="facebook",
                    status="failed",
                    detail=retry_detail,
                )
        except Exception as exc:
            return PlatformUploadResult(
                platform="facebook",
                status="failed",
                detail=str(exc),
            )

    @classmethod
    def _upload_facebook_reel_binary(
        cls,
        *,
        session: requests.Session,
        upload_url: str,
        token: str,
        video_path: Path,
        file_size: int,
        offset: int,
        max_attempts: int = 2,
    ) -> requests.Response:
        offset = max(0, min(offset, file_size))
        remaining = max(0, file_size - offset)

        def _upload_binary() -> requests.Response:
            with video_path.open("rb") as f:
                if offset:
                    f.seek(offset)
                return session.post(
                    upload_url,
                    headers={
                        "Authorization": f"OAuth {token}",
                        "offset": str(offset),
                        "file_size": str(file_size),
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(remaining),
                    },
                    data=f,
                    timeout=1800,
                )

        return cls._request_with_retries(_upload_binary, max_attempts=max_attempts)

    @classmethod
    def _cleanup_failed_facebook_video(
        cls,
        *,
        session: requests.Session,
        base: str,
        token: str,
        video_id: str,
    ) -> None:
        try:
            session.delete(
                f"{base}/{video_id}",
                params={"access_token": token},
                timeout=30,
            )
        except Exception:
            pass

    @classmethod
    def _validate_facebook_reel_media(cls, *, video_path: Path) -> str | None:
        """
        Best-effort validation against documented Reels media constraints.
        If ffprobe is unavailable, validation is skipped.
        """
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,r_frame_rate",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError:
            logger.warning("ffprobe is not available; skipping Facebook reel media validation.")
            return None
        except Exception as exc:
            logger.warning("ffprobe media validation failed for %s: %s", video_path, exc)
            return None

        try:
            payload = json.loads(probe.stdout or "{}")
        except Exception:
            return None

        streams = payload.get("streams")
        fmt = payload.get("format")
        if not isinstance(streams, list) or not streams:
            return None
        first_stream = streams[0] if isinstance(streams[0], dict) else {}
        if not isinstance(first_stream, dict):
            return None

        try:
            width = int(first_stream.get("width") or 0)
        except Exception:
            width = 0
        try:
            height = int(first_stream.get("height") or 0)
        except Exception:
            height = 0

        duration_seconds = None
        if isinstance(fmt, dict):
            try:
                duration_seconds = float(fmt.get("duration")) if fmt.get("duration") is not None else None
            except Exception:
                duration_seconds = None

        fps = None
        rate_raw = str(first_stream.get("r_frame_rate") or "").strip()
        if rate_raw and "/" in rate_raw:
            num_raw, den_raw = rate_raw.split("/", 1)
            try:
                num = float(num_raw)
                den = float(den_raw)
                if den != 0:
                    fps = num / den
            except Exception:
                fps = None

        issues: list[str] = []
        # Based on Meta Reels Publishing API docs:
        # min resolution 540x960, frame rate 24-60 fps, duration 3-90s.
        if width and height:
            if min(width, height) < 540 or max(width, height) < 960:
                issues.append(f"resolution {width}x{height} is below the minimum 540x960")
        if fps is not None and (fps < 24 or fps > 60):
            issues.append(f"frame rate {fps:.2f}fps is outside the supported 24-60fps range")
        if duration_seconds is not None and (duration_seconds < 3 or duration_seconds > 90):
            issues.append(f"duration {duration_seconds:.2f}s is outside the supported 3-90s range")

        if issues:
            return (
                "Facebook reel media validation failed: "
                + "; ".join(issues)
                + "."
            )
        return None

    @classmethod
    def _facebook_reel_upload_response_problem(cls, response: requests.Response) -> str | None:
        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            if payload.get("error"):
                return str(payload.get("error"))[:300]
            if "success" in payload and not cls._coerce_bool(payload.get("success")):
                return str(payload)[:300]
            return None

        text = (response.text or "").strip()
        if not text:
            return None
        lowered = text.lower()
        if "error" in lowered or "fail" in lowered:
            return text[:300]
        return None

    @classmethod
    def _get_facebook_video_status_payload(
        cls,
        *,
        session: requests.Session,
        base: str,
        video_id: str,
        token: str,
    ) -> dict[str, Any] | None:
        status_resp = cls._request_with_retries(
            lambda: session.get(
                f"{base}/{video_id}",
                params={
                    "fields": "status",
                    "access_token": token,
                },
                timeout=60,
            ),
            max_attempts=3,
        )
        if status_resp.status_code >= 400:
            return None
        try:
            payload = status_resp.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    @classmethod
    def _summarize_facebook_video_status(cls, status_payload: dict[str, Any]) -> str:
        status = status_payload.get("status")
        if not isinstance(status, dict):
            return "unavailable"

        uploading_phase = status.get("uploading_phase")
        processing_phase = status.get("processing_phase")
        publishing_phase = status.get("publishing_phase")
        bits: list[str] = []

        video_status = str(status.get("video_status") or "").strip()
        if video_status:
            bits.append(f"video={video_status}")

        if isinstance(uploading_phase, dict):
            up_status = str(uploading_phase.get("status") or "").strip()
            if up_status:
                bits.append(f"uploading={up_status}")
            bytes_transferred = (
                uploading_phase.get("bytes_transfered")
                if uploading_phase.get("bytes_transfered") is not None
                else uploading_phase.get("bytes_transferred")
            )
            try:
                bytes_value = int(bytes_transferred) if bytes_transferred is not None else None
            except Exception:
                bytes_value = None
            if bytes_value is not None:
                bits.append(f"bytes={bytes_value}")

        if isinstance(processing_phase, dict):
            proc_status = str(processing_phase.get("status") or "").strip()
            if proc_status:
                bits.append(f"processing={proc_status}")
            proc_error = processing_phase.get("error")
            if isinstance(proc_error, dict):
                message = str(proc_error.get("message") or "").strip()
                if message:
                    bits.append(f"processing_error={message}")

        if isinstance(publishing_phase, dict):
            pub_status = str(publishing_phase.get("status") or "").strip()
            if pub_status:
                bits.append(f"publishing={pub_status}")

        return ", ".join(bits) if bits else "unavailable"

    @classmethod
    def _ensure_facebook_reel_upload_ready_for_finish(
        cls,
        *,
        session: requests.Session,
        base: str,
        video_id: str,
        upload_url: str,
        token: str,
        video_path: Path,
        file_size: int,
    ) -> tuple[bool, str]:
        resume_attempted = False
        for attempt in range(1, 6):
            status_payload = cls._get_facebook_video_status_payload(
                session=session,
                base=base,
                video_id=video_id,
                token=token,
            )
            if not status_payload:
                return False, "unable to fetch video status from Graph"

            status = status_payload.get("status")
            if not isinstance(status, dict):
                return False, "video status payload is missing status object"

            uploading_phase = status.get("uploading_phase")
            processing_phase = status.get("processing_phase")
            uploading_status = ""
            bytes_transferred_value = 0
            if isinstance(uploading_phase, dict):
                uploading_status = str(uploading_phase.get("status") or "").strip().lower()
                bytes_transferred_raw = (
                    uploading_phase.get("bytes_transfered")
                    if uploading_phase.get("bytes_transfered") is not None
                    else uploading_phase.get("bytes_transferred")
                )
                try:
                    bytes_transferred_value = int(bytes_transferred_raw or 0)
                except Exception:
                    bytes_transferred_value = 0

            processing_error = None
            if isinstance(processing_phase, dict):
                err = processing_phase.get("error")
                if isinstance(err, dict):
                    candidate = str(err.get("message") or "").strip()
                    processing_error = candidate or None

            status_summary = cls._summarize_facebook_video_status(status_payload)

            if processing_error:
                return False, f"processing error: {processing_error} ({status_summary})"

            if uploading_status == "complete":
                return True, status_summary

            # Meta docs: resume upload by setting offset to upload_phase.bytes_transfered.
            if (
                not resume_attempted
                and 0 < bytes_transferred_value < file_size
                and uploading_status in {"in_progress", "not_started", ""}
            ):
                resume_resp = cls._upload_facebook_reel_binary(
                    session=session,
                    upload_url=upload_url,
                    token=token,
                    video_path=video_path,
                    file_size=file_size,
                    offset=bytes_transferred_value,
                    max_attempts=2,
                )
                resume_attempted = True
                if resume_resp.status_code >= 400:
                    return (
                        False,
                        "resume upload failed "
                        f"({resume_resp.status_code}): {resume_resp.text[:300]} ({status_summary})",
                    )
            if attempt < 5:
                time.sleep(min(20, attempt * 2))

        final_status_payload = cls._get_facebook_video_status_payload(
            session=session,
            base=base,
            video_id=video_id,
            token=token,
        )
        final_summary = (
            cls._summarize_facebook_video_status(final_status_payload)
            if final_status_payload
            else "unavailable"
        )
        return False, f"uploading phase did not reach complete before finish ({final_summary})"

    @classmethod
    def _coerce_bool(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return False

    @classmethod
    def _is_facebook_reel_finish_payload_valid(
        cls,
        *,
        finish_payload: dict[str, Any],
        expected_video_id: str,
    ) -> bool:
        success = cls._coerce_bool(finish_payload.get("success"))
        returned_video_id = finish_payload.get("video_id") or finish_payload.get("id")
        returned_post_id = finish_payload.get("post_id")
        if returned_video_id is not None and str(returned_video_id) != expected_video_id:
            return False
        if success:
            return True
        if returned_video_id is not None and str(returned_video_id) == expected_video_id:
            return True
        if returned_post_id:
            return True
        return False

    @classmethod
    def _is_facebook_reel_finish_retryable_error(cls, response: requests.Response) -> bool:
        err = cls._graph_error_object(response)
        message = str(err.get("message") or "").lower()
        try:
            code = int(err.get("code") or 0)
        except Exception:
            code = 0
        try:
            subcode = int(err.get("error_subcode") or 0)
        except Exception:
            subcode = 0

        # Observed intermittent Meta finish failure for valid files.
        if code == 6000 and subcode == 1363130:
            return True
        if code == 6000 and "problem uploading your video file" in message:
            return True
        if code == 6000 and "video upload is missing" in message:
            return True
        return False

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
                        files={
                            # Meta expects captions_file as text/plain or application/octet-stream.
                            "captions_file": (subtitle_path.name, captions_file, "text/plain")
                        },
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
        extra_params: dict[str, str] | None = None,
    ) -> str:
        container_data = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        }
        if extra_params:
            container_data.update(extra_params)
        container_resp = cls._request_with_retries(
            lambda: session.post(
                f"{base}/{ig_user_id}/media",
                data=container_data,
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

        if upload_resp.status_code >= 400 and not cls._is_instagram_resumable_upload_indeterminate(upload_resp):
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
        extra_params: dict[str, str] | None = None,
    ) -> str:
        container_data = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        }
        if extra_params:
            container_data.update(extra_params)
        container_resp = cls._request_with_retries(
            lambda: session.post(
                f"{base}/{ig_user_id}/media",
                data=container_data,
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
    def _publish_instagram_container(
        cls,
        *,
        session: requests.Session,
        base: str,
        ig_user_id: str,
        token: str,
        container_id: str,
    ) -> tuple[str, str]:
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
            raise RuntimeError(f"media_publish failed: {_extract_graph_error(publish_resp)}")

        media_id = str(publish_resp.json().get("id") or "").strip()
        if not media_id:
            raise RuntimeError(f"media_publish returned no media id: {publish_resp.text}")

        permalink: str | None = None
        media_info = cls._request_with_retries(
            lambda: session.get(
                f"{base}/{media_id}",
                params={"fields": "permalink", "access_token": token},
                timeout=60,
            ),
            max_attempts=2,
        )
        if media_info.status_code < 400:
            permalink = str(media_info.json().get("permalink") or "").strip() or None
        if not permalink:
            permalink = f"https://www.instagram.com/p/{media_id}/"
        return media_id, permalink

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
            payload = cls._get_instagram_container_status_payload(
                session=session,
                base=base,
                container_id=container_id,
                token=token,
            )
            status_code = str(payload.get("status_code") or "").upper()
            status_text = str(payload.get("status") or "")
            error_message = str(payload.get("error_message") or payload.get("message") or "").strip()
            video_status = payload.get("video_status")
            effective_status = status_code or status_text.upper()
            if effective_status == "FINISHED":
                return
            if effective_status in {"ERROR", "EXPIRED"}:
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

                detail = error_message or status_text or status_code or "Unknown container error"
                if phase_bits:
                    detail = f"{detail} ({', '.join(phase_bits)})"
                raise RuntimeError(f"Instagram container failed: {detail}")
            if effective_status:
                last_status = effective_status

            time.sleep(interval_seconds)

        suffix = f" (last status: {last_status})" if last_status else ""
        raise TimeoutError(
            f"Instagram container did not reach FINISHED within {timeout_seconds}s{suffix}"
        )

    @classmethod
    def _get_instagram_container_status_payload(
        cls,
        *,
        session: requests.Session,
        base: str,
        container_id: str,
        token: str,
    ) -> dict[str, Any]:
        field_candidates = (
            "status_code,status,error_message,video_status",
            "status_code,status,video_status",
            "status_code,status",
            "status_code",
        )
        last_error_detail = ""

        for fields in field_candidates:
            status_resp = cls._request_with_retries(
                lambda: session.get(
                    f"{base}/{container_id}",
                    params={
                        "fields": fields,
                        "access_token": token,
                    },
                    timeout=60,
                ),
                max_attempts=3,
            )
            if status_resp.status_code < 400:
                payload = status_resp.json()
                if isinstance(payload, dict):
                    return payload
                return {}

            detail = _extract_graph_error(status_resp)
            if cls._is_instagram_container_field_error(status_resp):
                last_error_detail = detail
                continue
            raise RuntimeError(f"Instagram container status failed: {detail}")

        suffix = f" after field fallback ({last_error_detail})" if last_error_detail else ""
        raise RuntimeError(f"Instagram container status failed{suffix}")
