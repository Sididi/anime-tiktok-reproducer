import json
from pathlib import Path

from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService
from app.services.indexation_preflight import IndexationPreflightService


def _write_import_manifest(
    *,
    source_file: Path,
    prepared_file: Path,
) -> None:
    manifest_path = prepared_file.with_name(
        prepared_file.name + AnimeLibraryService.SOURCE_IMPORT_MANIFEST_SUFFIX
    )
    manifest_path.write_text(
        json.dumps(
            {
                "source_path": str(source_file),
                "prepared_path": str(prepared_file),
            }
        ),
        encoding="utf-8",
    )


def test_interrupted_import_is_resumable_for_exact_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "episode-001.mkv"
    source_file.write_bytes(b"source")

    library_root = tmp_path / "library"
    local_dir = library_root / "Hunter x Hunter"
    local_dir.mkdir(parents=True)
    prepared_file = local_dir / "episode-001.mp4"
    prepared_file.write_bytes(b"prepared")
    _write_import_manifest(source_file=source_file, prepared_file=prepared_file)

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: type(
                "Scan",
                (),
                {"readable_files": (source_file,), "invalid_files": ()},
            )()
        ),
    )

    assert IndexationPreflightService._is_resumable_local_import(
        source_path=source_dir,
        library_type=LibraryType.ANIME,
        display_name="Hunter x Hunter",
    )


def test_interrupted_import_rejects_different_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    requested_dir = tmp_path / "requested"
    requested_dir.mkdir()
    requested_file = requested_dir / "episode-001.mkv"
    requested_file.write_bytes(b"requested")
    other_file = tmp_path / "other" / "episode-001.mkv"
    other_file.parent.mkdir()
    other_file.write_bytes(b"other")

    library_root = tmp_path / "library"
    local_dir = library_root / "Hunter x Hunter"
    local_dir.mkdir(parents=True)
    prepared_file = local_dir / "episode-001.mp4"
    prepared_file.write_bytes(b"prepared")
    _write_import_manifest(source_file=other_file, prepared_file=prepared_file)

    monkeypatch.setattr(
        AnimeLibraryService,
        "get_library_path",
        classmethod(lambda cls, library_type=None: library_root),
    )
    monkeypatch.setattr(
        AnimeLibraryService,
        "scan_direct_video_files_sync",
        classmethod(
            lambda cls, folder: type(
                "Scan",
                (),
                {"readable_files": (requested_file,), "invalid_files": ()},
            )()
        ),
    )

    assert not IndexationPreflightService._is_resumable_local_import(
        source_path=requested_dir,
        library_type=LibraryType.ANIME,
        display_name="Hunter x Hunter",
    )
