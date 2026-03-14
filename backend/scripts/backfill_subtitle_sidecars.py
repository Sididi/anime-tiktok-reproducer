from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

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
    return parser


def _parse_pair(raw_pair: str) -> tuple[Path, Path]:
    if "=" not in raw_pair:
        raise ValueError(f"Invalid pair '{raw_pair}'. Expected NORMALIZED=ORIGINAL.")
    normalized_raw, original_raw = raw_pair.split("=", 1)
    normalized = Path(normalized_raw).expanduser().resolve()
    original = Path(original_raw).expanduser().resolve()
    return normalized, original


async def _run(pairs: list[tuple[Path, Path]]) -> None:
    for normalized, original in pairs:
        print(f"Backfilling subtitles for {normalized.name}")
        await AnimeLibraryService.backfill_subtitle_sidecar(
            normalized_target_path=normalized,
            original_source_path=original,
        )


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.pair:
        parser.error("Provide at least one --pair NORMALIZED=ORIGINAL.")

    pairs = [_parse_pair(raw_pair) for raw_pair in args.pair]
    asyncio.run(_run(pairs))


if __name__ == "__main__":
    main()
