from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
import logging
import mimetypes
from pathlib import Path
import random
import socket
import ssl
from threading import Lock, local
import time
from typing import Callable, Iterable, Any, TypeVar

import httplib2
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, MediaIoBaseDownload

from ..config import settings


FOLDER_MIME = "application/vnd.google-apps.folder"
FORCE_BINARY_UPLOAD_SUFFIXES = {".sqpreset", ".prfpset", ".mogrt"}
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}
RETRYABLE_403_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "sharingRateLimitExceeded",
}
logger = logging.getLogger("uvicorn.error")
_RequestResultT = TypeVar("_RequestResultT")
_GOOGLE_AUTH_HTTP_TIMEOUT_SECONDS = 10
# httplib2 keeps one persistent socket per client; Google (or any NAT on the
# way) drops it after a few idle minutes. Reusing that dead socket surfaces as
# a broken pipe on write or an unexpected TLS EOF on read - never as an
# HttpError - so those exceptions mean "reconnect and try again", not "failed".
TRANSPORT_ERROR_TYPES = (
    ssl.SSLError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    http.client.HTTPException,
    httplib2.ServerNotFoundError,
)


class DriveVideoMetadataLookupError(RuntimeError):
    """Drive could not answer a final-video metadata request."""


class _BoundedGoogleAuthRequest(Request):
    """Keep OAuth token refreshes from outliving an interactive request."""

    def __call__(
        self,
        url,
        method="GET",
        body=None,
        headers=None,
        timeout=120,
        **kwargs,
    ):
        try:
            bounded_timeout = min(
                float(timeout), float(_GOOGLE_AUTH_HTTP_TIMEOUT_SECONDS)
            )
        except (TypeError, ValueError):
            bounded_timeout = float(_GOOGLE_AUTH_HTTP_TIMEOUT_SECONDS)
        return super().__call__(
            url=url,
            method=method,
            body=body,
            headers=headers,
            timeout=bounded_timeout,
            **kwargs,
        )


def _escape_query_value(s: str) -> str:
    """Escape a value for use in Drive API query strings."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveService:
    """Google Drive utilities for project-level folder and file management."""
    ARCHIVE_FOLDER_NAME = "Archive Projets"
    ARCHIVE_ROOT_FILES = {"import_project.jsx", "tts_edited.wav"}
    ARCHIVE_FULL_FOLDERS = {"subtitles", "raw_scene_subtitles", "assets"}
    ARCHIVE_SOURCE_FILES = {"title_overlay.png", "category_overlay.png"}
    _lock = Lock()
    _credentials_cache: Credentials | None = None
    _client_local = local()
    _video_duration_cache_lock = Lock()
    _video_duration_cache: dict[str, tuple[float, float]] = {}

    # The normal Drive client deliberately keeps Google's generous socket
    # timeout for large transfers. Preflight metadata requests need a separate,
    # bounded client so a broken route to Google cannot strand the UI.
    _VIDEO_METADATA_HTTP_TIMEOUT_SECONDS = 10
    _VIDEO_METADATA_MAX_ATTEMPTS = 2
    _VIDEO_DURATION_CACHE_TTL_SECONDS = 60.0
    # Top-level MP4 boxes before moov are few (ftyp, free, mdat); the bound
    # just keeps a malformed header from turning into an endless range walk.
    _VIDEO_HEADER_MAX_BOXES = 16

    _SMALL_FILE_BYTES = 8 * 1024 * 1024

    @classmethod
    def is_configured(cls) -> bool:
        return bool(
            settings.drive_google_client_id
            and settings.drive_google_client_secret
            and settings.drive_google_refresh_token
            and settings.google_drive_parent_folder_id
        )

    @classmethod
    def _credentials(cls) -> Credentials:
        if not cls.is_configured():
            raise RuntimeError("Google Drive is not configured")

        with cls._lock:
            cached = cls._credentials_cache
            if cached is None or (
                cached.refresh_token != settings.drive_google_refresh_token
                or cached.client_id != settings.drive_google_client_id
                or cached.client_secret != settings.drive_google_client_secret
                or cached.token_uri != settings.drive_google_token_uri
            ):
                cached = Credentials(
                    token=None,
                    refresh_token=settings.drive_google_refresh_token,
                    token_uri=settings.drive_google_token_uri,
                    client_id=settings.drive_google_client_id,
                    client_secret=settings.drive_google_client_secret,
                    scopes=[
                        "https://www.googleapis.com/auth/drive",
                    ],
                )
                cls._credentials_cache = cached

            now = datetime.now(timezone.utc)
            expiry = cached.expiry
            if expiry is None:
                refresh_soon = True
            else:
                expiry_utc = (
                    expiry.replace(tzinfo=timezone.utc)
                    if expiry.tzinfo is None
                    else expiry.astimezone(timezone.utc)
                )
                refresh_soon = expiry_utc <= now + timedelta(minutes=5)
            if cached.token is None or refresh_soon:
                cached.refresh(_BoundedGoogleAuthRequest())
            return cached

    @classmethod
    def credentials(cls) -> Credentials:
        """Return refreshed Google credentials for integrations checks/calls."""
        return cls._credentials()

    @classmethod
    def rclone_token_json(cls) -> str:
        """OAuth token payload for rclone's drive backend (RCLONE_DRIVE_TOKEN).

        Ships a freshly-refreshed access token so rclone starts hot, plus the
        refresh token so it can self-refresh in memory if a transfer outlives
        the access token (~1h).
        """
        creds = cls._credentials()
        expiry = creds.expiry
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return json.dumps(
            {
                "access_token": creds.token,
                "token_type": "Bearer",
                "refresh_token": creds.refresh_token,
                "expiry": (
                    expiry.isoformat()
                    if expiry is not None
                    else datetime.now(timezone.utc).isoformat()
                ),
            }
        )

    @classmethod
    def client(cls):
        """Return a thread-local Drive API client bound to refreshed credentials."""
        creds = cls._credentials()
        cached_client = getattr(cls._client_local, "client", None)
        cached_creds_ref = getattr(cls._client_local, "creds_ref", None)

        # googleapiclient uses httplib2 under the hood; sharing one service object
        # across threads can crash the interpreter in concurrent network calls.
        if cached_client is None or cached_creds_ref is not creds:
            cached_client = build("drive", "v3", credentials=creds, cache_discovery=False)
            cls._client_local.client = cached_client
            cls._client_local.creds_ref = creds
        return cached_client

    @classmethod
    def reset_client(cls) -> None:
        """Drop the current thread-local Drive client so the next request rebuilds it."""
        cached_client = getattr(cls._client_local, "client", None)
        http = getattr(cached_client, "_http", None)
        if http is not None and hasattr(http, "close"):
            try:
                http.close()
            except Exception:
                pass
        for attr in ("client", "creds_ref"):
            if hasattr(cls._client_local, attr):
                delattr(cls._client_local, attr)

    @classmethod
    def _video_metadata_client(cls):
        """Return a thread-local Drive client with a short socket timeout."""
        creds = cls._credentials()
        cached_client = getattr(cls._client_local, "video_metadata_client", None)
        cached_creds_ref = getattr(
            cls._client_local, "video_metadata_creds_ref", None
        )
        if cached_client is None or cached_creds_ref is not creds:
            authorized_http = AuthorizedHttp(
                creds,
                http=httplib2.Http(timeout=cls._VIDEO_METADATA_HTTP_TIMEOUT_SECONDS),
            )
            cached_client = build(
                "drive",
                "v3",
                http=authorized_http,
                cache_discovery=False,
            )
            cls._client_local.video_metadata_client = cached_client
            cls._client_local.video_metadata_creds_ref = creds
        return cached_client

    @classmethod
    def _reset_video_metadata_client(cls) -> None:
        cached_client = getattr(cls._client_local, "video_metadata_client", None)
        http = getattr(cached_client, "_http", None)
        if http is not None and hasattr(http, "close"):
            try:
                http.close()
            except Exception:
                pass
        for attr in ("video_metadata_client", "video_metadata_creds_ref"):
            if hasattr(cls._client_local, attr):
                delattr(cls._client_local, attr)

    @classmethod
    def _client(cls):
        return cls.client()

    @staticmethod
    def _http_error_status_code(exc: Exception) -> int | None:
        if not isinstance(exc, HttpError):
            return None
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _http_error_reason_codes(exc: Exception) -> set[str]:
        if not isinstance(exc, HttpError):
            return set()
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            raw = content.decode("utf-8", errors="ignore")
        else:
            raw = str(content or "")
        if not raw:
            return set()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        error = payload.get("error", {})
        reasons: set[str] = set()
        if isinstance(error, dict):
            errors = error.get("errors", [])
            if isinstance(errors, list):
                for item in errors:
                    if isinstance(item, dict):
                        reason = item.get("reason")
                        if isinstance(reason, str) and reason:
                            reasons.add(reason)
            reason = error.get("status")
            if isinstance(reason, str) and reason:
                reasons.add(reason)
        return reasons

    @staticmethod
    def _is_transport_error(exc: Exception) -> bool:
        """True when the request died on the wire, before Drive answered."""
        return isinstance(exc, TRANSPORT_ERROR_TYPES)

    @classmethod
    def _recycle_connection(cls, drive=None) -> None:
        """Drop the dead socket so the next request dials a fresh one.

        ``httplib2`` reconnects lazily, so closing the pooled connection of the
        very client that failed is what makes a retry meaningful - the caller
        may hold its own ``drive`` handle that ``reset_client`` cannot reach.
        """
        http_client = getattr(drive, "_http", None)
        if http_client is not None and hasattr(http_client, "close"):
            try:
                http_client.close()
            except Exception:
                pass
        cls.reset_client()

    @classmethod
    def _is_retryable_http_error(cls, exc: Exception) -> bool:
        status_code = cls._http_error_status_code(exc)
        if status_code in TRANSIENT_HTTP_STATUS_CODES:
            return True
        if status_code == 403 and cls._http_error_reason_codes(exc) & RETRYABLE_403_REASONS:
            return True
        return False

    @classmethod
    def _execute_with_retries(
        cls,
        request_fn: Callable[[], _RequestResultT],
        *,
        max_attempts: int = 5,
        operation: str = "drive_request",
        retry_transport_errors: bool = False,
        drive=None,
    ) -> _RequestResultT:
        """Run a Drive request, retrying transient failures.

        ``retry_transport_errors`` additionally retries connection-level
        failures (stale keep-alive socket, TLS EOF) after recycling the
        connection. Only opt in for idempotent requests: a transport error can
        also mean the call reached Drive and only the response was lost.
        """
        attempt = 1
        while True:
            try:
                result = request_fn()
                if attempt > 1:
                    logger.info(
                        "Drive request succeeded after retries: operation=%s attempts=%d",
                        operation,
                        attempt,
                    )
                return result
            except Exception as exc:
                status_code = cls._http_error_status_code(exc)
                transport_failure = retry_transport_errors and cls._is_transport_error(exc)
                should_retry = transport_failure or cls._is_retryable_http_error(exc)
                if not should_retry or attempt >= max_attempts:
                    raise
                if transport_failure:
                    cls._recycle_connection(drive)
                    logger.warning(
                        "Drive connection lost; reconnecting and retrying request: "
                        "operation=%s attempt=%d/%d error=%s",
                        operation,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    time.sleep(min(2.0, 0.25 * attempt))
                    attempt += 1
                    continue
                backoff_seconds = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
                logger.warning(
                    "Transient Drive error; retrying request: operation=%s status=%s reasons=%s attempt=%d/%d backoff_seconds=%.2f",
                    operation,
                    status_code,
                    sorted(cls._http_error_reason_codes(exc)),
                    attempt,
                    max_attempts,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                attempt += 1

    @classmethod
    def _query_files(
        cls,
        q: str,
        fields: str = "files(id,name,mimeType,webViewLink)",
        *,
        drive=None,
    ) -> list[dict[str, Any]]:
        drive = drive or cls._client()
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            def _list(token: str | None = page_token) -> dict[str, Any]:
                return drive.files().list(
                    q=q,
                    fields=f"nextPageToken,{fields}",
                    pageSize=1000,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()

            response = cls._execute_with_retries(
                _list,
                operation="drive_query_files",
                retry_transport_errors=True,
                drive=drive,
            )
            items.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return items

    @classmethod
    def find_project_folder_by_name(cls, folder_name: str, *, drive=None) -> dict[str, Any] | None:
        if not cls.is_configured():
            return None

        parent = settings.google_drive_parent_folder_id
        if parent is None:
            raise RuntimeError("Google Drive parent folder not configured")
        q = (
            f"mimeType='{FOLDER_MIME}' and trashed=false and "
            f"name='{_escape_query_value(folder_name)}' and '{_escape_query_value(parent)}' in parents"
        )
        results = cls._query_files(q, drive=drive)
        return results[0] if results else None

    @classmethod
    def list_project_folders_under_parent(cls, *, drive=None) -> dict[str, dict[str, Any]]:
        """Return project folders (keyed by folder name) under configured parent folder."""
        if not cls.is_configured():
            return {}
        parent = settings.google_drive_parent_folder_id
        if parent is None:
            raise RuntimeError("Google Drive parent folder not configured")
        q = (
            f"mimeType='{FOLDER_MIME}' and trashed=false and "
            f"'{_escape_query_value(parent)}' in parents"
        )
        folders = cls._query_files(q, drive=drive)
        by_name: dict[str, dict[str, Any]] = {}
        for folder in folders:
            name = str(folder.get("name") or "")
            if not name:
                continue
            # Keep first match deterministically if duplicates exist.
            by_name.setdefault(name, folder)
        return by_name

    @classmethod
    def ensure_project_folder(
        cls,
        folder_name: str,
        existing_folder_id: str | None = None,
        *,
        drive=None,
    ) -> tuple[str, str]:
        drive = drive or cls._client()

        if existing_folder_id:
            try:
                existing = drive.files().get(
                    fileId=existing_folder_id,
                    fields="id,webViewLink",
                    supportsAllDrives=True,
                ).execute()
                return existing["id"], existing.get("webViewLink", "")
            except Exception:
                pass

        existing = cls.find_project_folder_by_name(folder_name, drive=drive)
        if existing:
            return existing["id"], existing.get("webViewLink", "")

        parent = settings.google_drive_parent_folder_id
        if parent is None:
            raise RuntimeError("Google Drive parent folder not configured")
        metadata = {"name": folder_name, "mimeType": FOLDER_MIME, "parents": [parent]}
        created = drive.files().create(
            body=metadata,
            fields="id,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return created["id"], created.get("webViewLink", "")

    @classmethod
    def _ensure_child_folder(
        cls,
        folder_name: str,
        parent_id: str,
        *,
        drive=None,
    ) -> dict[str, Any]:
        drive = drive or cls._client()
        q = (
            f"mimeType='{FOLDER_MIME}' and trashed=false and "
            f"name='{_escape_query_value(folder_name)}' and "
            f"'{_escape_query_value(parent_id)}' in parents"
        )
        existing = cls._query_files(q, drive=drive)
        if existing:
            return existing[0]
        return drive.files().create(
            body={"name": folder_name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id,name,mimeType,webViewLink",
            supportsAllDrives=True,
        ).execute()

    @classmethod
    def _copy_archive_tree(
        cls,
        source_folder_id: str,
        destination_folder_id: str,
        *,
        drive,
        allowed_names: set[str] | None = None,
    ) -> int:
        copied = 0
        for item in cls.list_children(source_folder_id, drive=drive):
            name = str(item.get("name") or "")
            if allowed_names is not None and name not in allowed_names:
                continue
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            if item.get("mimeType") == FOLDER_MIME:
                destination = cls._ensure_child_folder(
                    name, destination_folder_id, drive=drive
                )
                copied += cls._copy_archive_tree(
                    item_id, destination["id"], drive=drive
                )
                continue

            def _copy() -> dict[str, Any]:
                return drive.files().copy(
                    fileId=item_id,
                    body={"name": name, "parents": [destination_folder_id]},
                    fields="id",
                    supportsAllDrives=True,
                ).execute()

            cls._execute_with_retries(
                _copy, operation=f"drive_archive_copy:{item_id}"
            )
            copied += 1
        return copied

    @classmethod
    def archive_project_folder(cls, source_folder_id: str) -> dict[str, Any]:
        """Copy the reconstructable subset of a project into Archive Projets."""
        drive = cls._client()
        source = drive.files().get(
            fileId=source_folder_id,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        project_name = str(source.get("name") or "")
        if not project_name:
            raise RuntimeError("Drive project folder has no name")
        parent_id = settings.google_drive_parent_folder_id
        if not parent_id:
            raise RuntimeError("Google Drive parent folder not configured")

        archive_root = cls._ensure_child_folder(
            cls.ARCHIVE_FOLDER_NAME, parent_id, drive=drive
        )
        archive_project = cls._ensure_child_folder(
            project_name, archive_root["id"], drive=drive
        )
        children = cls.list_children(source_folder_id, drive=drive)
        available_root_files = {
            str(item.get("name") or "")
            for item in children
            if item.get("mimeType") != FOLDER_MIME
        }
        missing_required = cls.ARCHIVE_ROOT_FILES - available_root_files
        if missing_required:
            raise RuntimeError(
                "Cannot archive project; required Drive files are missing: "
                + ", ".join(sorted(missing_required))
            )
        # A previous interrupted attempt may have left a partial archive.
        cls.clear_folder(archive_project["id"], drive=drive)

        copied = 0
        for item in children:
            name = str(item.get("name") or "")
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            if item.get("mimeType") != FOLDER_MIME:
                if name not in cls.ARCHIVE_ROOT_FILES:
                    continue
                copied += cls._copy_archive_tree(
                    source_folder_id,
                    archive_project["id"],
                    drive=drive,
                    allowed_names={name},
                )
                continue
            if name not in cls.ARCHIVE_FULL_FOLDERS and name != "sources":
                continue
            destination = cls._ensure_child_folder(
                name, archive_project["id"], drive=drive
            )
            copied += cls._copy_archive_tree(
                item_id,
                destination["id"],
                drive=drive,
                allowed_names=cls.ARCHIVE_SOURCE_FILES if name == "sources" else None,
            )

        return {
            "folder_id": archive_project["id"],
            "folder_url": archive_project.get("webViewLink", ""),
            "files_copied": copied,
        }

    @classmethod
    def list_children(cls, folder_id: str, *, drive=None) -> list[dict[str, Any]]:
        q = f"trashed=false and '{_escape_query_value(folder_id)}' in parents"
        return cls._query_files(q, drive=drive)

    @classmethod
    def list_children_named(
        cls,
        folder_id: str,
        filename: str,
        *,
        drive=None,
    ) -> list[dict[str, Any]]:
        q = (
            "trashed=false and "
            f"name='{_escape_query_value(filename)}' and "
            f"'{_escape_query_value(folder_id)}' in parents"
        )
        return cls._query_files(q, drive=drive)

    @classmethod
    def delete_file(cls, file_id: str, *, drive=None) -> None:
        drive = drive or cls._client()

        def _delete() -> None:
            drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()

        cls._execute_with_retries(_delete, operation=f"drive_delete:{file_id}")

    @classmethod
    def clear_folder(
        cls,
        folder_id: str,
        *,
        drive=None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        drive = drive or cls._client()
        items = cls.list_children(folder_id, drive=drive)
        if progress_callback is not None:
            progress_callback(
                {
                    "item_count": len(items),
                    "items_completed": 0,
                    "current_item": None,
                }
            )
        if not items:
            return 0

        max_workers = max(1, min(settings.drive_delete_max_parallel, len(items)))
        started_at = time.perf_counter()
        progress_lock = Lock()
        completed_items = 0

        def _delete_item(file_id: str) -> None:
            delete_drive = cls.client()

            def _delete() -> None:
                delete_drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()

            cls._execute_with_retries(_delete, operation=f"drive_delete:{file_id}")

        failures: list[tuple[str, Exception]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_file_id = {
                executor.submit(_delete_item, str(item["id"])): item
                for item in items
                if item.get("id")
            }
            for future in as_completed(future_to_file_id):
                item = future_to_file_id[future]
                file_id = str(item["id"])
                try:
                    future.result()
                    if progress_callback is not None:
                        with progress_lock:
                            completed_items += 1
                            progress_callback(
                                {
                                    "item_count": len(items),
                                    "items_completed": completed_items,
                                    "current_item": str(item.get("name") or ""),
                                }
                            )
                except Exception as exc:  # pragma: no cover - defensive; exercised in tests
                    failures.append((file_id, exc))

        duration = time.perf_counter() - started_at
        logger.info(
            "Drive folder clear finished: folder_id=%s items=%d workers=%d duration_seconds=%.2f failures=%d",
            folder_id,
            len(items),
            max_workers,
            duration,
            len(failures),
        )
        if failures:
            failed_ids = ",".join(file_id for file_id, _ in failures[:3])
            raise RuntimeError(
                f"Failed to clear Drive folder '{folder_id}': {len(failures)} item(s) could not be deleted"
                + (f" (examples: {failed_ids})" if failed_ids else "")
            )
        return len(items)

    @staticmethod
    def _align_chunksize(chunksize: int) -> int:
        quantum = 256 * 1024
        return max(quantum, ((chunksize + quantum - 1) // quantum) * quantum)

    @classmethod
    def _effective_resumable_chunksize(cls, file_size: int, requested_chunksize: int | None) -> int:
        base = requested_chunksize or (16 * 1024 * 1024)
        if file_size >= 1024 * 1024 * 1024:
            base = max(base, 64 * 1024 * 1024)
        elif file_size >= 256 * 1024 * 1024:
            base = max(base, 32 * 1024 * 1024)
        return cls._align_chunksize(base)

    @classmethod
    def _upload_resumable_request(
        cls,
        request,
        *,
        file_size: int,
        operation: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        attempt = 1
        response = None
        while response is None:
            try:
                status, response = request.next_chunk(num_retries=0)
                attempt = 1
            except Exception as exc:
                status_code = cls._http_error_status_code(exc)
                should_retry = cls._is_retryable_http_error(exc)
                if not should_retry or attempt >= max_attempts:
                    raise
                backoff_seconds = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
                logger.warning(
                    "Transient Drive upload chunk error; retrying request: operation=%s status=%s reasons=%s attempt=%d/%d backoff_seconds=%.2f",
                    operation,
                    status_code,
                    sorted(cls._http_error_reason_codes(exc)),
                    attempt,
                    max_attempts,
                    backoff_seconds,
                )
                time.sleep(backoff_seconds)
                attempt += 1
                continue

            if status is not None and progress_callback is not None:
                uploaded_bytes = min(
                    file_size,
                    int(getattr(status, "resumable_progress", 0) or 0),
                )
                progress_callback(
                    {
                        "uploaded_bytes": uploaded_bytes,
                        "total_bytes": file_size,
                        "completed": False,
                    }
                )

        if progress_callback is not None:
            progress_callback(
                {
                    "uploaded_bytes": file_size,
                    "total_bytes": file_size,
                    "completed": True,
                }
            )
        return response

    @classmethod
    def find_subfolder(cls, parent_id: str, name: str, *, drive=None) -> str | None:
        """Return the id of an existing subfolder by exact name, else None."""
        q = (
            f"mimeType='{FOLDER_MIME}' and trashed=false and "
            f"name='{_escape_query_value(name)}' and '{_escape_query_value(parent_id)}' in parents"
        )
        found = cls._query_files(q, fields="files(id,name)", drive=drive)
        return str(found[0]["id"]) if found else None

    @classmethod
    def ensure_subfolder(cls, parent_id: str, name: str, *, drive=None) -> str:
        drive = drive or cls._client()
        q = (
            f"mimeType='{FOLDER_MIME}' and trashed=false and "
            f"name='{_escape_query_value(name)}' and '{_escape_query_value(parent_id)}' in parents"
        )
        found = cls._query_files(q, fields="files(id,name)", drive=drive)
        if found:
            return found[0]["id"]
        created = drive.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    @classmethod
    def ensure_subfolders(cls, base_folder_id: str, parts: Iterable[str], *, drive=None) -> str:
        drive = drive or cls._client()
        parent_id = base_folder_id
        for part in parts:
            parent_id = cls.ensure_subfolder(parent_id, part, drive=drive)
        return parent_id

    @classmethod
    def upload_local_file(
        cls,
        *,
        parent_id: str,
        filename: str,
        local_path: Path,
        chunksize: int | None = None,
        drive=None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        drive = drive or cls._client()
        suffix = local_path.suffix.lower()
        if suffix in FORCE_BINARY_UPLOAD_SUFFIXES:
            mime = "application/octet-stream"
        else:
            mime, _ = mimetypes.guess_type(str(local_path))
        file_size = local_path.stat().st_size
        resumable = file_size > cls._SMALL_FILE_BYTES
        request_chunk_size = (
            cls._effective_resumable_chunksize(file_size, chunksize)
            if resumable
            else -1
        )

        def _create() -> dict[str, Any]:
            media = MediaFileUpload(
                str(local_path),
                mimetype=mime or "application/octet-stream",
                resumable=resumable,
                chunksize=request_chunk_size,
            )
            return drive.files().create(
                body={"name": filename, "parents": [parent_id]},
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()

        if not resumable:
            created = cls._execute_with_retries(_create, operation=f"drive_upload_file:{filename}")
            if progress_callback is not None:
                progress_callback(
                    {
                        "uploaded_bytes": file_size,
                        "total_bytes": file_size,
                        "completed": True,
                    }
                )
            return created

        media = MediaFileUpload(
            str(local_path),
            mimetype=mime or "application/octet-stream",
            resumable=True,
            chunksize=request_chunk_size,
        )
        request = drive.files().create(
            body={"name": filename, "parents": [parent_id]},
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        )
        return cls._upload_resumable_request(
            request,
            file_size=file_size,
            operation=f"drive_upload_file:{filename}",
            progress_callback=progress_callback,
        )

    @classmethod
    def upsert_local_file(
        cls,
        *,
        parent_id: str,
        filename: str,
        local_path: Path,
        chunksize: int | None = None,
        drive=None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        drive = drive or cls._client()
        for existing in cls.list_children_named(parent_id, filename, drive=drive):
            file_id = existing.get("id")
            if file_id:
                cls.delete_file(str(file_id), drive=drive)
        return cls.upload_local_file(
            parent_id=parent_id,
            filename=filename,
            local_path=local_path,
            chunksize=chunksize,
            drive=drive,
            progress_callback=progress_callback,
        )

    @classmethod
    def upload_bytes(
        cls,
        *,
        parent_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "text/plain",
        drive=None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        drive = drive or cls._client()

        def _create() -> dict[str, Any]:
            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
            return drive.files().create(
                body={"name": filename, "parents": [parent_id]},
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()

        created = cls._execute_with_retries(_create, operation=f"drive_upload_bytes:{filename}")
        if progress_callback is not None:
            progress_callback(
                {
                    "uploaded_bytes": len(content),
                    "total_bytes": len(content),
                    "completed": True,
                }
            )
        return created

    @classmethod
    def list_root_video_files(cls, folder_id: str, extensions: set[str]) -> list[dict[str, Any]]:
        files = cls.list_children(folder_id)
        out: list[dict[str, Any]] = []
        for file_data in files:
            if file_data.get("mimeType") == FOLDER_MIME:
                continue
            name = file_data.get("name", "")
            suffix = Path(name).suffix.lower()
            if suffix in extensions:
                out.append(file_data)
        return out

    @classmethod
    def list_root_video_files_by_parent_ids(
        cls,
        parent_ids: list[str],
        extensions: set[str],
        *,
        drive=None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Batch list root-level video files for multiple parent folders."""
        if not parent_ids:
            return {}
        drive = drive or cls._client()
        normalized: list[str] = []
        seen: set[str] = set()
        for item in parent_ids:
            if item and item not in seen:
                seen.add(item)
                normalized.append(item)
        if not normalized:
            return {}

        result: dict[str, list[dict[str, Any]]] = {parent_id: [] for parent_id in normalized}

        # Keep the Drive query reasonably sized.
        chunk_size = 20
        for start in range(0, len(normalized), chunk_size):
            chunk = normalized[start : start + chunk_size]
            parent_clause = " or ".join(
                f"'{_escape_query_value(parent_id)}' in parents" for parent_id in chunk
            )
            q = f"trashed=false and ({parent_clause})"
            files = cls._query_files(
                q,
                fields="files(id,name,mimeType,webViewLink,parents)",
                drive=drive,
            )
            for file_data in files:
                if file_data.get("mimeType") == FOLDER_MIME:
                    continue
                name = str(file_data.get("name") or "")
                suffix = Path(name).suffix.lower()
                if suffix not in extensions:
                    continue
                for parent_id in file_data.get("parents", []):
                    if parent_id in result:
                        result[parent_id].append(file_data)

        return result

    @classmethod
    def set_public_read(cls, file_id: str, *, drive=None) -> None:
        drive = drive or cls._client()

        def _share() -> None:
            drive.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": "reader"},
                fields="id",
                supportsAllDrives=True,
            ).execute()

        # Re-sharing an already-public file is a no-op for Drive, so this is
        # safe to replay after a lost connection.
        cls._execute_with_retries(
            _share,
            operation=f"drive_set_public_read:{file_id}",
            retry_transport_errors=True,
            drive=drive,
        )

    @classmethod
    def get_direct_download_url(cls, file_id: str) -> str:
        return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"

    @classmethod
    def get_web_view_url(cls, file_id: str) -> str:
        drive = cls._client()

        def _get() -> dict[str, Any]:
            return drive.files().get(
                fileId=file_id,
                fields="webViewLink",
                supportsAllDrives=True,
            ).execute()

        info = cls._execute_with_retries(
            _get,
            operation=f"drive_web_view_url:{file_id}",
            retry_transport_errors=True,
            drive=drive,
        )
        return info.get("webViewLink", "")

    @classmethod
    def get_video_duration_seconds(cls, file_id: str) -> float | None:
        """Return Drive's video duration without ever downloading the file.

        ``None`` has one precise meaning: Drive answered successfully but has
        not exposed usable video metadata yet. Transport/API failures raise a
        distinct exception so callers cannot mistake an outage for missing
        metadata and start a large fallback download.
        """
        now = time.monotonic()
        with cls._video_duration_cache_lock:
            cached = cls._video_duration_cache.get(file_id)
            if cached is not None:
                duration, cached_at = cached
                if now - cached_at <= cls._VIDEO_DURATION_CACHE_TTL_SECONDS:
                    return duration
                cls._video_duration_cache.pop(file_id, None)

        info: dict[str, Any] | None = None
        for attempt in range(1, cls._VIDEO_METADATA_MAX_ATTEMPTS + 1):
            try:
                drive = cls._video_metadata_client()
                info = drive.files().get(
                    fileId=file_id,
                    fields="videoMediaMetadata(durationMillis)",
                    supportsAllDrives=True,
                ).execute(num_retries=0)
                break
            except Exception as exc:
                cls._reset_video_metadata_client()
                retryable = (
                    not isinstance(exc, HttpError)
                    or cls._is_retryable_http_error(exc)
                )
                if retryable and attempt < cls._VIDEO_METADATA_MAX_ATTEMPTS:
                    logger.warning(
                        "Transient Drive video metadata lookup failure; retrying: "
                        "file_id=%s attempt=%d/%d error=%s",
                        file_id,
                        attempt,
                        cls._VIDEO_METADATA_MAX_ATTEMPTS,
                        exc,
                    )
                    time.sleep(0.25)
                    continue
                logger.warning(
                    "Drive video metadata lookup failed: file_id=%s attempts=%d error=%s",
                    file_id,
                    attempt,
                    exc,
                )
                raise DriveVideoMetadataLookupError(
                    "Google Drive did not answer the final-video metadata request"
                ) from exc

        if info is None:  # pragma: no cover - loop either returns data or raises
            raise DriveVideoMetadataLookupError(
                "Google Drive did not answer the final-video metadata request"
            )
        metadata = info.get("videoMediaMetadata") or {}
        duration_millis = metadata.get("durationMillis")
        if duration_millis is None:
            return None
        try:
            duration = float(duration_millis) / 1000.0
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        cls._cache_video_duration(file_id, duration)
        return duration

    @classmethod
    def _cache_video_duration(cls, file_id: str, duration: float) -> None:
        with cls._video_duration_cache_lock:
            cls._video_duration_cache[file_id] = (duration, time.monotonic())

    @classmethod
    def _fetch_file_range(cls, file_id: str, start: int, length: int) -> bytes:
        """Fetch a byte range of a Drive file, never the whole file.

        A server that ignores ``Range`` answers 200 with the entire media, so
        the reply is only accepted when Drive confirms partial content.
        """
        drive = cls._video_metadata_client()
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        headers = dict(request.headers or {})
        headers["Range"] = f"bytes={start}-{start + length - 1}"
        response, content = request.http.request(
            request.uri, "GET", headers=headers
        )
        status = int(getattr(response, "status", 0) or 0)
        if status != 206:
            raise DriveVideoMetadataLookupError(
                f"Drive did not serve a byte range (HTTP {status})"
            )
        return content or b""

    @classmethod
    def probe_video_duration_from_header(cls, file_id: str) -> float | None:
        """Read the duration out of the MP4/MOV header itself, via byte ranges.

        Drive fills ``videoMediaMetadata`` asynchronously, so a freshly
        uploaded export answers with no duration for a while — long enough for
        an upload click to land in the gap. The container always knows: the
        ``moov/mvhd`` box carries a timescale and a duration. Walking the
        top-level boxes by their declared sizes reaches ``moov`` in a handful of
        small range requests, so a multi-hundred-megabyte export costs a few KB
        and the media itself is never transferred.

        Returns ``None`` whenever the header cannot be read or does not state a
        usable duration; callers keep their existing fallback.
        """
        try:
            offset = 0
            for _ in range(cls._VIDEO_HEADER_MAX_BOXES):
                header = cls._fetch_file_range(file_id, offset, 16)
                if len(header) < 8:
                    return None
                size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8]
                header_size = 8
                if size == 1:
                    if len(header) < 16:
                        return None
                    size = int.from_bytes(header[8:16], "big")
                    header_size = 16
                if box_type == b"moov":
                    duration = cls._duration_from_moov(file_id, offset + header_size)
                    if duration is not None:
                        # The remaining platform checks in this preflight run
                        # reuse it instead of walking the header again.
                        cls._cache_video_duration(file_id, duration)
                    return duration
                if size == 0 or size < header_size:
                    # Box runs to end of file (or is malformed): nothing left to
                    # walk past, and it is not the moov we need.
                    return None
                offset += size
            return None
        except Exception as exc:
            logger.warning(
                "Drive video header duration probe failed: file_id=%s error=%s",
                file_id,
                exc,
            )
            return None

    @classmethod
    def _duration_from_moov(cls, file_id: str, moov_payload_offset: int) -> float | None:
        """Find ``mvhd`` among moov's children and decode its duration."""
        offset = moov_payload_offset
        for _ in range(cls._VIDEO_HEADER_MAX_BOXES):
            header = cls._fetch_file_range(file_id, offset, 8)
            if len(header) < 8:
                return None
            size = int.from_bytes(header[0:4], "big")
            box_type = header[4:8]
            if box_type == b"mvhd":
                return cls._duration_from_mvhd(
                    cls._fetch_file_range(file_id, offset + 8, 32)
                )
            if size < 8:
                return None
            offset += size
        return None

    @staticmethod
    def _duration_from_mvhd(payload: bytes) -> float | None:
        """Decode ``mvhd``: version, flags, times, timescale, duration."""
        if len(payload) < 4:
            return None
        version = payload[0]
        if version == 1:
            timescale_at, duration_at, duration_width = 20, 24, 8
        else:
            timescale_at, duration_at, duration_width = 12, 16, 4
        if len(payload) < duration_at + duration_width:
            return None
        timescale = int.from_bytes(payload[timescale_at:timescale_at + 4], "big")
        duration = int.from_bytes(
            payload[duration_at:duration_at + duration_width], "big"
        )
        # All-ones is the container's "duration unknown" sentinel.
        if duration in (0, (1 << (duration_width * 8)) - 1):
            return None
        if timescale <= 0:
            return None
        seconds = duration / timescale
        return seconds if seconds > 0 else None

    @classmethod
    def get_file_size(cls, file_id: str) -> int | None:
        """Return the file's byte size, or ``None`` when unavailable."""
        try:
            drive = cls._client()
            info = drive.files().get(
                fileId=file_id,
                fields="size",
                supportsAllDrives=True,
            ).execute()
            return int(info["size"])
        except Exception:
            return None

    @classmethod
    def download_file(cls, file_id: str, destination: Path) -> None:
        drive = cls._client()
        request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

    @classmethod
    def delete_folder(cls, folder_id: str) -> None:
        drive = cls._client()
        drive.files().delete(fileId=folder_id, supportsAllDrives=True).execute()

    @classmethod
    def verify_parent_folder_access(cls) -> tuple[bool, str]:
        """Check if configured Drive parent folder is readable and a folder."""
        if not cls.is_configured():
            return False, "Google Drive is not fully configured"

        parent_id = settings.google_drive_parent_folder_id
        if parent_id is None:
            raise RuntimeError("Google Drive parent folder not configured")
        try:
            drive = cls._client()
            info = drive.files().get(
                fileId=parent_id,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
            mime_type = info.get("mimeType")
            if mime_type != FOLDER_MIME:
                return False, f"Configured parent id is not a folder (mimeType={mime_type})"
            name = info.get("name") or parent_id
            return True, f"Parent folder is accessible: {name}"
        except Exception as exc:
            return False, str(exc)
