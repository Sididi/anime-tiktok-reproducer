"""Canonical episode-name helpers shared by routes and matcher services.

Episode identifiers flow through the system in several shapes (absolute
paths, relative paths, bare stems, with or without media extensions).
Whenever two of those shapes must be compared — e.g. a user-supplied
episode whitelist against index metadata — both sides are reduced to the
same canonical stem: basename without any known media extension.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

KNOWN_MEDIA_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
    ".wav",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".aiff",
    ".aif",
)


def strip_known_media_extension(name: str) -> str:
    """Strip only supported media extensions from a filename-like value."""
    clean_name = str(name or "").strip()
    lower_name = clean_name.lower()
    for ext in KNOWN_MEDIA_EXTENSIONS:
        if lower_name.endswith(ext):
            return clean_name[: -len(ext)]
    return clean_name


def canonical_episode_stem(episode: str) -> str:
    """Reduce any episode identifier shape to a comparable canonical stem."""
    clean_episode = str(episode or "").strip()
    if not clean_episode:
        return ""
    basename = PurePosixPath(clean_episode).name
    if "\\" in basename:
        basename = PureWindowsPath(basename).name
    return strip_known_media_extension(basename or clean_episode)


def normalize_episode_whitelist(
    episodes: list[str] | None,
) -> frozenset[str] | None:
    """Canonicalize a user-supplied episode subset; None means "all episodes"."""
    if episodes is None:
        return None
    stems = {
        stem for episode in episodes if (stem := canonical_episode_stem(episode))
    }
    return frozenset(stems) or None
