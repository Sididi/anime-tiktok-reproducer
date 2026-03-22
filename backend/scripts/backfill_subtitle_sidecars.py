from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.library_types import LibraryType
from app.services.anime_library import AnimeLibraryService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill subtitle sidecars for already-normalized library episodes "
            "from original source files."
        )
    )
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        metavar="NORMALIZED=ORIGINAL",
        help="One normalized/original source pair. Can be repeated.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "Auto-discover library episodes missing subtitle sidecars "
            "and backfill from original sources tracked in .atr_source.json."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print discovered pairs without executing backfill.",
    )
    return parser


def _parse_pair(raw_pair: str) -> tuple[Path, Path]:
    if "=" not in raw_pair:
        raise ValueError(f"Invalid pair '{raw_pair}'. Expected NORMALIZED=ORIGINAL.")
    normalized_raw, original_raw = raw_pair.split("=", 1)
    normalized = Path(normalized_raw).expanduser().resolve()
    original = Path(original_raw).expanduser().resolve()
    return normalized, original


def _discover_pairs() -> list[tuple[Path, Path]]:
    """Walk all library directories and find episodes missing subtitle sidecars."""
    pairs: list[tuple[Path, Path]] = []
    for library_type in LibraryType:
        library_path = AnimeLibraryService.get_library_path(library_type)
        if not library_path.exists():
            continue
        for series_dir in sorted(library_path.iterdir()):
            if not series_dir.is_dir() or series_dir.name.startswith("."):
                continue
            for video_file in sorted(series_dir.glob("*.mp4")):
                if AnimeLibraryService.get_subtitle_sidecar_manifest_path(video_file).exists():
                    continue
                manifest = AnimeLibraryService._load_source_import_manifest_sync(video_file)
                if manifest is None:
                    continue
                raw_source = manifest.get("source_path")
                if not isinstance(raw_source, str) or not raw_source:
                    continue
                original = Path(raw_source)
                if original.exists() and original.suffix.lower() in {".mkv", ".avi", ".webm", ".mov"}:
                    pairs.append((video_file, original))
    return pairs


async def _run(pairs: list[tuple[Path, Path]]) -> None:
    for normalized, original in pairs:
        print(f"Backfilling subtitles for {normalized.name}")
        await AnimeLibraryService.backfill_subtitle_sidecar(
            normalized_target_path=normalized,
            original_source_path=original,
        )
        print(f"  Done: {normalized.name}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    pairs: list[tuple[Path, Path]] = []

    if args.auto:
        print("Auto-discovering library episodes missing subtitle sidecars...")
        pairs = _discover_pairs()
        if not pairs:
            print("No episodes found needing subtitle backfill.")
            return
        print(f"Found {len(pairs)} episode(s) to backfill:")
        for normalized, original in pairs:
            print(f"  {normalized.name} <- {original.name}")
        if args.dry_run:
            print("\n[dry-run] No changes made.")
            return
        print()
    elif args.pair:
        pairs = [_parse_pair(raw_pair) for raw_pair in args.pair]
    else:
        parser.error("Provide --auto or at least one --pair NORMALIZED=ORIGINAL.")

    asyncio.run(_run(pairs))


if __name__ == "__main__":
    main()
