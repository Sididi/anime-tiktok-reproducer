from __future__ import annotations

from enum import Enum
from pathlib import Path
from dataclasses import dataclass


class LibraryType(str, Enum):
    ANIME = "anime"
    SIMPSONS = "simpsons"
    FILMS_SERIES = "films_series"
    DESSIN_ANIME = "dessin_anime"
    # Pure mode: reproduce one of our own published TikToks from its output.
    # With no optional source, the TikTok itself is the only source. Optional
    # project-owned sources use an ANIME LocalLibrary for media operations.
    # Intentionally NOT mirrored into modules/anime_searcher/library_types.py:
    # The PURE enum itself never reaches the indexing CLI or the searcher.
    PURE = "pure"


DEFAULT_LIBRARY_TYPE = LibraryType.ANIME


@dataclass(frozen=True)
class LocalLibrary:
    """An explicit, project-owned matcher library. Never a remote catalog type.

    Pass this value through media operations, including worker threads; do not
    change global settings to select a project's index.
    """

    path: Path
    library_type: LibraryType = LibraryType.ANIME


LibrarySelection = LibraryType | LocalLibrary | str | None

# Types that own a series tree on the Storage Box. PURE is deliberately out:
# a pure project's source is either its TikTok or a private local library,
# so no catalog series root or release ever exists remotely for it — probing one
# only yields a failed listing and a spurious integration error.
STORAGE_BACKED_LIBRARY_TYPES: tuple[LibraryType, ...] = tuple(
    library_type for library_type in LibraryType if library_type is not LibraryType.PURE
)

STATIC_OVERLAY_TITLES: dict[LibraryType, str] = {
    LibraryType.ANIME: "CET ANIME EST INCROYABLE !",
    LibraryType.FILMS_SERIES: "CE FILM EST INCROYABLE !",
    LibraryType.DESSIN_ANIME: "CE DESSIN ANIMÉ EST INCROYABLE !",
    LibraryType.SIMPSONS: "CET EPISODE EST INCROYABLE !",
    LibraryType.PURE: "CETTE VIDÉO EST INCROYABLE !",
}


def coerce_library_type(value: LibrarySelection) -> LibraryType:
    if isinstance(value, LocalLibrary):
        return value.library_type
    if isinstance(value, LibraryType):
        return value
    if value is None:
        return DEFAULT_LIBRARY_TYPE
    return LibraryType(str(value).strip().lower())


def resolve_static_overlay_title(library_type: LibraryType | str | None) -> str:
    resolved = coerce_library_type(library_type)
    return STATIC_OVERLAY_TITLES.get(resolved, STATIC_OVERLAY_TITLES[LibraryType.ANIME])


def resolve_scoped_library_path(
    library_root: Path,
    library_type: LibraryType | str | None,
) -> Path:
    return library_root / coerce_library_type(library_type).value
