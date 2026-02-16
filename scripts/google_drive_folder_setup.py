#!/usr/bin/env python3
from __future__ import annotations

import argparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from _env import env, load_dotenv


FOLDER_MIME = "application/vnd.google-apps.folder"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find or create Google Drive parent folder and print ATR_GOOGLE_DRIVE_PARENT_FOLDER_ID."
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--folder-name",
        default=None,
        help="Single folder name at Drive root (legacy mode).",
    )
    target.add_argument(
        "--folder-path",
        default=None,
        help="Folder path under Drive root, e.g. 'Tiktok/Anime SPM'.",
    )
    parser.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create folder when not found",
    )
    return parser


def _google_credentials() -> Credentials:
    client_id = env("ATR_GOOGLE_CLIENT_ID")
    client_secret = env("ATR_GOOGLE_CLIENT_SECRET")
    refresh_token = env("ATR_GOOGLE_DRIVE_REFRESH_TOKEN") or env("ATR_GOOGLE_REFRESH_TOKEN")
    token_uri = env("ATR_GOOGLE_TOKEN_URI") or "https://oauth2.googleapis.com/token"

    if not client_id or not client_secret or not refresh_token:
        raise SystemExit(
            "Missing Google Drive env values. Required: "
            "ATR_GOOGLE_CLIENT_ID, ATR_GOOGLE_CLIENT_SECRET, "
            "ATR_GOOGLE_DRIVE_REFRESH_TOKEN (or legacy ATR_GOOGLE_REFRESH_TOKEN)."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    return creds


def _escape_q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _query_by_name(drive, parent_id: str, folder_name: str) -> list[dict]:
    safe_name = _escape_q(folder_name)
    query = (
        "trashed=false and "
        f"mimeType='{FOLDER_MIME}' and name='{safe_name}' and '{parent_id}' in parents"
    )
    resp = drive.files().list(
        q=query,
        fields="files(id,name,webViewLink)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return resp.get("files", [])


def _normalize_parts(folder_name: str | None, folder_path: str | None) -> list[str]:
    if folder_path:
        parts = [part.strip() for part in folder_path.split("/") if part.strip()]
        if not parts:
            raise SystemExit("--folder-path must contain at least one non-empty path segment.")
        return parts
    if folder_name:
        return [folder_name.strip()]
    return ["anime-tiktok-reproducer-projects"]


def _resolve_folder_path(drive, parts: list[str], create_if_missing: bool) -> dict:
    parent_id = "root"
    current: dict | None = None

    for index, part in enumerate(parts):
        found = _query_by_name(drive, parent_id=parent_id, folder_name=part)
        if len(found) > 1:
            print(f"Multiple folders named '{part}' under parent id '{parent_id}'.")
            print("Please keep one folder only or use a different path.")
            for item in found:
                print(f"- id={item.get('id')} name={item.get('name')} url={item.get('webViewLink', '')}")
            raise SystemExit(2)

        if found:
            current = found[0]
            parent_id = current["id"]
            continue

        if not create_if_missing:
            missing_path = "/".join(parts[: index + 1])
            raise SystemExit(
                f"Folder path not found at '{missing_path}'. Re-run with --create-if-missing to create it."
            )

        created = drive.files().create(
            body={
                "name": part,
                "mimeType": FOLDER_MIME,
                "parents": [parent_id],
            },
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()
        current = created
        parent_id = created["id"]
        print(f"Created missing folder segment: {part} (id={created['id']})")

    assert current is not None
    return current


def main() -> None:
    args = _parser().parse_args()
    load_dotenv(args.env_file)
    parts = _normalize_parts(args.folder_name, args.folder_path)

    drive = build("drive", "v3", credentials=_google_credentials(), cache_discovery=False)
    resolved = _resolve_folder_path(drive, parts=parts, create_if_missing=args.create_if_missing)
    resolved_path = "/".join(parts)

    print("Resolved folder.")
    print(f"Folder path: {resolved_path}")
    print(f"Folder id: {resolved['id']}")
    print(f"Web link: {resolved.get('webViewLink', '')}")
    print("\nSet in .env:")
    print(f"ATR_GOOGLE_DRIVE_PARENT_FOLDER_ID={resolved['id']}")


if __name__ == "__main__":
    main()
