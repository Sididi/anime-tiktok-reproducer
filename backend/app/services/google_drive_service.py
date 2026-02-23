from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import mimetypes
from pathlib import Path
from threading import Lock
from typing import Iterable, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload, MediaIoBaseDownload

from ..config import settings


FOLDER_MIME = "application/vnd.google-apps.folder"


def _escape_query_value(s: str) -> str:
    """Escape a value for use in Drive API query strings."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveService:
    """Google Drive utilities for project-level folder and file management."""
    _lock = Lock()
    _credentials_cache: Credentials | None = None
    _client_cache = None
    _client_creds_ref: Credentials | None = None

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
                cached.refresh(Request())
            return cached

    @classmethod
    def credentials(cls) -> Credentials:
        """Return refreshed Google credentials for integrations checks/calls."""
        return cls._credentials()

    @classmethod
    def client(cls):
        """Return a cached Drive API client bound to refreshed credentials."""
        creds = cls._credentials()
        with cls._lock:
            if cls._client_cache is None or cls._client_creds_ref is not creds:
                cls._client_cache = build("drive", "v3", credentials=creds, cache_discovery=False)
                cls._client_creds_ref = creds
            return cls._client_cache

    @classmethod
    def _client(cls):
        return cls.client()

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
            response = drive.files().list(
                q=q,
                fields=f"nextPageToken,{fields}",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
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
    def list_children(cls, folder_id: str, *, drive=None) -> list[dict[str, Any]]:
        q = f"trashed=false and '{_escape_query_value(folder_id)}' in parents"
        return cls._query_files(q, drive=drive)

    @classmethod
    def clear_folder(cls, folder_id: str, *, drive=None) -> None:
        drive = drive or cls._client()
        for item in cls.list_children(folder_id, drive=drive):
            drive.files().delete(fileId=item["id"], supportsAllDrives=True).execute()

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
        drive=None,
    ) -> dict[str, Any]:
        drive = drive or cls._client()
        mime, _ = mimetypes.guess_type(str(local_path))
        file_size = local_path.stat().st_size
        resumable = file_size > cls._SMALL_FILE_BYTES
        media = MediaFileUpload(
            str(local_path),
            mimetype=mime or "application/octet-stream",
            resumable=resumable,
        )
        created = drive.files().create(
            body={"name": filename, "parents": [parent_id]},
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        return created

    @classmethod
    def upload_bytes(
        cls,
        *,
        parent_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "text/plain",
        drive=None,
    ) -> dict[str, Any]:
        drive = drive or cls._client()
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        created = drive.files().create(
            body={"name": filename, "parents": [parent_id]},
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
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
    def set_public_read(cls, file_id: str) -> None:
        drive = cls._client()
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
            supportsAllDrives=True,
        ).execute()

    @classmethod
    def get_direct_download_url(cls, file_id: str) -> str:
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    @classmethod
    def get_web_view_url(cls, file_id: str) -> str:
        drive = cls._client()
        info = drive.files().get(
            fileId=file_id,
            fields="webViewLink",
            supportsAllDrives=True,
        ).execute()
        return info.get("webViewLink", "")

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
