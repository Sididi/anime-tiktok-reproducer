"""Resolve media tool binaries with env overrides and pixi-aware fallbacks."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from ..config import PROJECT_ROOT, settings


def _normalize_override(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _is_override_path(value: str) -> bool:
    return (
        Path(value).is_absolute()
        or os.path.sep in value
        or (os.path.altsep is not None and os.path.altsep in value)
    )


def _invalid_override(env_name: str, value: str) -> FileNotFoundError:
    return FileNotFoundError(
        f"Invalid {env_name} override: {value!r} does not resolve to an executable"
    )


def is_media_binary_override_error(exc: FileNotFoundError) -> bool:
    message = str(exc)
    return "Invalid ATR_FFMPEG_BINARY override:" in message or "Invalid ATR_FFPROBE_BINARY override:" in message


def _resolve_explicit_binary(value: str, *, env_name: str) -> str:
    if _is_override_path(value):
        candidate = Path(value).expanduser()
        if _is_executable(candidate):
            return str(candidate.resolve())
        raise _invalid_override(env_name, value)

    resolved = shutil.which(value)
    if resolved:
        return resolved
    raise _invalid_override(env_name, value)


def _find_default_binary(binary_name: str) -> str | None:
    current_env_candidate = Path(sys.executable).resolve().parent / binary_name
    if _is_executable(current_env_candidate):
        return str(current_env_candidate)

    repo_pixi_candidate = PROJECT_ROOT / ".pixi" / "envs" / "default" / "bin" / binary_name
    if _is_executable(repo_pixi_candidate):
        return str(repo_pixi_candidate)

    return shutil.which(binary_name)


def _resolve_binary(
    *,
    binary_name: str,
    explicit_override: str | None,
    env_name: str,
) -> tuple[str, bool]:
    override = _normalize_override(explicit_override)
    if override is not None:
        return _resolve_explicit_binary(override, env_name=env_name), True

    resolved = _find_default_binary(binary_name)
    if resolved is not None:
        return resolved, True

    return binary_name, False


def get_ffmpeg_binary() -> str:
    value, _ = _resolve_binary(
        binary_name="ffmpeg",
        explicit_override=settings.ffmpeg_binary,
        env_name="ATR_FFMPEG_BINARY",
    )
    return value


def get_ffprobe_binary() -> str:
    value, _ = _resolve_binary(
        binary_name="ffprobe",
        explicit_override=settings.ffprobe_binary,
        env_name="ATR_FFPROBE_BINARY",
    )
    return value


def rewrite_media_command(cmd: Sequence[str]) -> list[str]:
    if not cmd:
        return []

    rewritten = list(cmd)
    if rewritten[0] == "ffmpeg":
        rewritten[0] = get_ffmpeg_binary()
    elif rewritten[0] == "ffprobe":
        rewritten[0] = get_ffprobe_binary()
    return rewritten


def get_ytdlp_ffmpeg_location() -> str | None:
    value, resolved = _resolve_binary(
        binary_name="ffmpeg",
        explicit_override=settings.ffmpeg_binary,
        env_name="ATR_FFMPEG_BINARY",
    )
    return value if resolved else None
