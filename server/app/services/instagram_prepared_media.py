"""Helpers for public, tokenized Instagram prepared media files."""
from __future__ import annotations

import secrets
import time
from pathlib import Path

_PROJECT_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_TOKEN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_DEFAULT_MAX_AGE_SECONDS = 30 * 60 * 60


def new_prepared_media_token() -> str:
    return secrets.token_urlsafe(24)


def validate_prepared_media_id(value: str, *, label: str) -> str:
    if not value or any(ch not in _PROJECT_ID_CHARS for ch in value):
        raise ValueError(f"invalid {label}")
    return value


def validate_prepared_media_token(token: str) -> str:
    if not token or any(ch not in _TOKEN_CHARS for ch in token):
        raise ValueError("invalid token")
    return token


def prepared_media_filename(project_id: str, token: str) -> str:
    project_id = validate_prepared_media_id(project_id, label="project_id")
    token = validate_prepared_media_token(token)
    return f"{project_id}-{token}.mp4"


def prepared_media_path(root: Path, project_id: str, token: str) -> Path:
    return root / prepared_media_filename(project_id, token)


def prepared_media_public_url(
    *,
    public_base_url: str,
    project_id: str,
    token: str,
) -> str:
    validate_prepared_media_id(project_id, label="project_id")
    validate_prepared_media_token(token)
    return (
        f"{public_base_url.rstrip('/')}/api/instagram/prepared/"
        f"{project_id}/{token}.mp4"
    )


def cleanup_expired_prepared_media(
    root: Path,
    *,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
) -> int:
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in root.glob("*.mp4"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
