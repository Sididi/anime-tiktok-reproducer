from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent


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


def _resolve_explicit_binary(value: str, *, env_name: str) -> str:
    if _is_override_path(value):
        candidate = Path(value).expanduser()
        if _is_executable(candidate):
            return str(candidate.resolve())
        raise FileNotFoundError(
            f"Invalid {env_name} override: {value!r} does not resolve to an executable"
        )

    resolved = shutil.which(value)
    if resolved:
        return resolved
    raise FileNotFoundError(
        f"Invalid {env_name} override: {value!r} does not resolve to an executable"
    )


def _find_default_binary(binary_name: str) -> str | None:
    current_env_candidate = Path(sys.executable).resolve().parent / binary_name
    if _is_executable(current_env_candidate):
        return str(current_env_candidate)

    repo_pixi_candidate = REPO_ROOT / ".pixi" / "envs" / "default" / "bin" / binary_name
    if _is_executable(repo_pixi_candidate):
        return str(repo_pixi_candidate)

    return shutil.which(binary_name)


def _resolve_binary(binary_name: str, env_name: str) -> str:
    override = _normalize_override(os.getenv(env_name))
    if override is not None:
        return _resolve_explicit_binary(override, env_name=env_name)

    resolved = _find_default_binary(binary_name)
    return resolved if resolved is not None else binary_name


def get_ffmpeg_binary() -> str:
    return _resolve_binary("ffmpeg", "ATR_FFMPEG_BINARY")


def get_ffprobe_binary() -> str:
    return _resolve_binary("ffprobe", "ATR_FFPROBE_BINARY")


def rewrite_media_command(cmd: Sequence[str]) -> list[str]:
    if not cmd:
        return []

    rewritten = list(cmd)
    if rewritten[0] == "ffmpeg":
        rewritten[0] = get_ffmpeg_binary()
    elif rewritten[0] == "ffprobe":
        rewritten[0] = get_ffprobe_binary()
    return rewritten
