#!/usr/bin/env python3
"""Sweep every published series for release-manifest drift.

A release manifest records ``size_bytes`` + ``sha256`` for each episode. If the
bytes sitting at the release path no longer match, hydration hard-fails with
"Checksum mismatch" and the series becomes un-downloadable — which is how
My Happy Marriage and Lovely Complex surfaced during the corruption triage.

This probe is metadata-only: one ``scandir`` per release payload directory,
compared against the manifest's recorded sizes. No video bytes are transferred,
so the whole catalog costs a few minutes. Size drift is a strict subset of
checksum drift (same size + different bytes would slip through), but every case
observed so far changes the size, and a full hash sweep would mean downloading
the entire library.

Run with the backend stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.library_types import coerce_library_type  # noqa: E402
from app.services.library_state_db import LibraryStateDb  # noqa: E402
from app.services.storage_box_repository import StorageBoxRepository  # noqa: E402
from app.services.storage_box_sftp_client import StorageBoxSftpClient  # noqa: E402

COLUMNS = ("series_id", "series", "verdict", "episodes", "drift", "absent",
           "detail")


async def probe_series(library_type: str, series_id: str, name: str) -> dict:
    row = {"series_id": series_id, "series": name, "verdict": "", "episodes": 0,
           "drift": 0, "absent": 0, "detail": ""}
    scoped = coerce_library_type(library_type)
    manifest = await StorageBoxRepository.get_series_manifest(library_type, series_id)
    release_id = manifest.get("release_id")
    if not release_id:
        row["verdict"] = "NO_RELEASE"
        return row
    root = PurePosixPath(str(StorageBoxRepository._release_root(scoped, series_id, release_id)))
    episodes = [e for e in manifest.get("episodes", []) if isinstance(e, dict)]
    row["episodes"] = len(episodes)
    if not episodes:
        row["verdict"] = "NO_EPISODES"
        return row

    # One scandir per distinct payload directory, not one stat per file.
    sizes: dict[str, int] = {}
    for directory in {PurePosixPath(e["media"]["relative_path"]).parent for e in episodes}:
        try:
            entries = await StorageBoxSftpClient.scandir(root / directory)
        except Exception as exc:  # noqa: BLE001 - recorded, not raised
            row["verdict"] = "SCAN_ERROR"
            row["detail"] = f"{type(exc).__name__}: {exc}"[:200]
            return row
        for entry in entries:
            sizes[(directory / entry.filename).as_posix()] = int(entry.attrs.size or 0)

    drifted: list[str] = []
    absent: list[str] = []
    for episode in episodes:
        media = episode["media"]
        key = PurePosixPath(media["relative_path"]).as_posix()
        actual = sizes.get(key)
        expected = int(media.get("size_bytes") or 0)
        if actual is None:
            absent.append(PurePosixPath(key).name)
        elif actual != expected:
            drifted.append(f"{PurePosixPath(key).name}({actual - expected:+d})")

    row["drift"] = len(drifted)
    row["absent"] = len(absent)
    row["verdict"] = "CLEAN" if not drifted and not absent else "DRIFT"
    row["detail"] = "; ".join([*drifted[:6], *(f"ABSENT {n}" for n in absent[:6])])
    return row


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-type", default="anime")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "docs/audit/manifest-drift-anime.tsv")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    LibraryStateDb.initialize()
    catalog = await StorageBoxRepository.list_catalog(args.library_type)

    done: set[str] = set()
    if args.resume and args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip():
                done.add(line.split("\t")[0])

    todo = [e for e in catalog if str(e["series_id"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"catalog={len(catalog)} already probed={len(done)} this run={len(todo)}",
          file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fresh = not args.out.exists()
    handle = args.out.open("a", encoding="utf-8")
    if fresh:
        handle.write("\t".join(COLUMNS) + "\n")
        handle.flush()

    for position, entry in enumerate(todo, start=1):
        series_id, name = str(entry["series_id"]), str(entry["name"])
        try:
            row = await probe_series(args.library_type, series_id, name)
        except Exception as exc:  # noqa: BLE001 - one bad series must not end the sweep
            row = {"series_id": series_id, "series": name, "verdict": "ERROR",
                   "episodes": 0, "drift": 0, "absent": 0,
                   "detail": f"{type(exc).__name__}: {exc}"[:200]}
        line = "\t".join(str(row[c]).replace("\t", " ").replace("\n", " ") for c in COLUMNS)
        handle.write(line + "\n")
        handle.flush()
        if row["verdict"] != "CLEAN":
            print(f"[{position}/{len(todo)}] {row['verdict']:12} {name}", file=sys.stderr)

    handle.close()


if __name__ == "__main__":
    asyncio.run(main_async())
