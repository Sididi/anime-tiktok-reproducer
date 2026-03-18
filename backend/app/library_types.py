from __future__ import annotations

from enum import Enum
from pathlib import Path


class LibraryType(str, Enum):
    ANIME = "anime"
    SIMPSONS = "simpsons"
    FILMS_SERIES = "films_series"
    DESSIN_ANIME = "dessin_anime"


DEFAULT_LIBRARY_TYPE = LibraryType.ANIME

LIBRARY_TYPE_LABELS: dict[LibraryType, str] = {
    LibraryType.ANIME: "Anime",
    LibraryType.SIMPSONS: "Simpsons",
    LibraryType.FILMS_SERIES: "Films/Séries",
    LibraryType.DESSIN_ANIME: "Dessin Animé",
}


def coerce_library_type(value: LibraryType | str | None) -> LibraryType:
    if isinstance(value, LibraryType):
        return value
    if value is None:
        return DEFAULT_LIBRARY_TYPE
    return LibraryType(str(value).strip().lower())


def resolve_scoped_library_path(
    library_root: Path,
    library_type: LibraryType | str | None,
) -> Path:
    return library_root / coerce_library_type(library_type).value
