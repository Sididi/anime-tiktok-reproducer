"""Resolve media tool binaries with env overrides and pixi-aware fallbacks."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from ..config import PROCESS_START_ENV, PROJECT_ROOT, settings

_MEDIA_BINARY_NAMES = {"ffmpeg", "ffprobe"}


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


def _managed_env_prefixes() -> list[Path]:
    prefixes: list[Path] = []
    seen: set[Path] = set()
    candidates = [
        PROCESS_START_ENV.get("CONDA_PREFIX"),
        os.environ.get("CONDA_PREFIX"),
        str(Path(sys.executable).resolve().parent.parent),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        prefixes.append(resolved)
    return prefixes


def _is_within_prefix(path_value: str, prefix: Path) -> bool:
    try:
        candidate = Path(path_value).expanduser().resolve(strict=False)
    except OSError:
        return False
    try:
        return candidate.is_relative_to(prefix)
    except ValueError:
        return False


def _is_media_binary(binary_value: str | None) -> bool:
    return bool(binary_value) and Path(binary_value).name in _MEDIA_BINARY_NAMES


def _is_managed_binary(binary_value: str) -> bool:
    candidate = binary_value
    if not Path(candidate).is_absolute():
        resolved = shutil.which(candidate)
        if resolved is None:
            return False
        candidate = resolved

    return any(
        _is_within_prefix(candidate, prefix)
        for prefix in _managed_env_prefixes()
    )


def _sanitize_ld_library_path() -> str | None:
    entries: list[str] = []
    managed_prefixes = _managed_env_prefixes()
    for raw_value in (
        PROCESS_START_ENV.get("LD_LIBRARY_PATH"),
        os.environ.get("LD_LIBRARY_PATH"),
    ):
        if not raw_value:
            continue
        for entry in raw_value.split(os.pathsep):
            if not entry:
                continue
            if any(_is_within_prefix(entry, prefix) for prefix in managed_prefixes):
                continue
            if entry not in entries:
                entries.append(entry)
    return os.pathsep.join(entries) if entries else None


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


def get_media_subprocess_env(
    cmd: Sequence[str],
    *,
    extra_binary: str | None = None,
) -> dict[str, str] | None:
    binary = cmd[0] if cmd and _is_media_binary(cmd[0]) else None
    if binary is None and _is_media_binary(extra_binary):
        binary = extra_binary
    if binary is None or _is_managed_binary(binary):
        return None

    env = dict(os.environ)
    sanitized_ld_library_path = _sanitize_ld_library_path()
    if sanitized_ld_library_path is None:
        env.pop("LD_LIBRARY_PATH", None)
    else:
        env["LD_LIBRARY_PATH"] = sanitized_ld_library_path
    return env


def get_ytdlp_ffmpeg_location() -> str | None:
    value, resolved = _resolve_binary(
        binary_name="ffmpeg",
        explicit_override=settings.ffmpeg_binary,
        env_name="ATR_FFMPEG_BINARY",
    )
    return value if resolved else None
