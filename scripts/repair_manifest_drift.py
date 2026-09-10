#!/usr/bin/env python3
"""Re-point a release manifest at the bytes that actually sit on the Storage Box.

Found by :mod:`audit_manifest_drift`: a handful of episodes were replaced on the
box (different encode — same show, same runtime, AAC instead of Opus audio)
without the release manifest being rewritten. Hydration verifies every download
against ``sha256``/``size_bytes``, so those series became impossible to pull:
"Checksum mismatch for payload/library/...".

The payload is fine; only the bookkeeping is stale. This repairs the bookkeeping:
download each drifted artifact, prove it is a real video of a plausible runtime,
then rewrite its ``size_bytes``/``sha256`` everywhere the manifest mentions it
(``artifacts[]``, ``episodes[].media``, ``episodes[].sidecars[]``) and update
``total_size_bytes``.

The manifest is rewritten in place rather than published as a new release: it is
one small JSON file, the change strictly moves it toward reality, and the normal
rebuild that follows supersedes it with a freshly-hashed release anyway.
``current.json``'s ``manifest_checksum`` is refreshed to match (nothing verifies
it today, but leaving it stale would plant a trap for whatever does later).

Run with the backend stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.library_types import coerce_library_type  # noqa: E402
from app.services.library_hydration_service import LibraryHydrationService  # noqa: E402
from app.services.library_state_db import LibraryStateDb  # noqa: E402
from app.services.storage_box_rclone import StorageBoxRclone  # noqa: E402
from app.services.storage_box_repository import StorageBoxRepository  # noqa: E402
from app.services.storage_box_sftp_client import StorageBoxSftpClient  # noqa: E402

COLUMNS = ("series_id", "series", "stage", "repaired", "detail")

# A replacement encode is allowed to differ in size and audio codec, but it must
# still be the same episode. Runtime is the cheap invariant that catches a
# truncated download or an outright wrong file.
#
# The envelope is calibrated per series rather than fixed: real runtimes vary a
# lot within one show (My Happy Marriage S2 spans 1424-1602s because some
# episodes carry extra ED/preview footage, while its S01 sits at 1420s), so a
# tight window produces false rejections. Sample siblings spread evenly across
# the series — not the nearest by name, which would sample a single season and
# then judge the other one against it — and take the span they actually occupy
# plus a margin. The check exists to catch a truncated or outright wrong file,
# so a series-wide envelope is the right granularity.
SIBLING_SAMPLE_SIZE = 8
DURATION_ENVELOPE_MARGIN_SECONDS = 120.0

VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".webm"}


def _spread_sample(items: list, count: int) -> list:
    """Pick up to ``count`` items spread evenly across ``items``."""
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def probe_media(path: Path) -> tuple[bool, str, float]:
    """Return (looks_like_video, description, duration_seconds)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False, f"ffprobe failed: {proc.stderr.strip()[:120]}", 0.0
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False, "ffprobe emitted no parsable JSON", 0.0
    streams = payload.get("streams", [])
    duration = float(payload.get("format", {}).get("duration") or 0.0)
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video or duration <= 0:
        return False, "no decodable video stream", duration
    codecs = f"{video[0].get('codec_name')}+{'/'.join(sorted({str(s.get('codec_name')) for s in audio})) or 'none'}"
    return True, f"{codecs} {duration:.0f}s", duration


def apply_correction(manifest: dict, relative_path: str, size: int, digest: str) -> int:
    """Patch every mention of ``relative_path``. Returns the number of sites updated."""
    updated = 0

    def patch(entry: object) -> None:
        nonlocal updated
        if isinstance(entry, dict) and str(entry.get("relative_path") or "") == relative_path:
            entry["size_bytes"] = size
            entry["sha256"] = digest
            updated += 1

    for artifact in manifest.get("artifacts", []):
        patch(artifact)
    for episode in manifest.get("episodes", []):
        if not isinstance(episode, dict):
            continue
        patch(episode.get("media"))
        for sidecar in episode.get("sidecars", []) or []:
            patch(sidecar)
    return updated


async def repair_series(library_type: str, series_id: str, name: str,
                        *, dry_run: bool) -> dict:
    row = {"series_id": series_id, "series": name, "stage": "", "repaired": 0,
           "detail": ""}
    scoped = coerce_library_type(library_type)

    current = await StorageBoxRepository.get_current_release(library_type, series_id)
    release_id = str(current["release_id"])
    manifest = await StorageBoxRepository.get_series_manifest(
        library_type, series_id, release_id
    )
    root = PurePosixPath(
        str(StorageBoxRepository._release_root(scoped, series_id, release_id))
    )

    # Find drifted artifacts by size (one scandir per payload directory).
    row["stage"] = "scan"
    artifacts = [a for a in manifest.get("artifacts", []) if isinstance(a, dict)]
    remote_sizes: dict[str, int] = {}
    for directory in {PurePosixPath(str(a["relative_path"])).parent for a in artifacts}:
        for entry in await StorageBoxSftpClient.scandir(root / directory):
            remote_sizes[(directory / entry.filename).as_posix()] = int(entry.attrs.size or 0)

    drifted = [
        a for a in artifacts
        if remote_sizes.get(str(a["relative_path"])) is not None
        and remote_sizes[str(a["relative_path"])] != int(a.get("size_bytes") or 0)
    ]
    if not drifted:
        row["stage"] = "clean"
        row["detail"] = "manifest already matches the box"
        return row

    # The expected runtime envelope comes from sibling episodes that did NOT
    # drift, so a replacement file is checked against this series' own norm
    # rather than a guess. The manifest records no durations, so the siblings
    # are downloaded and probed alongside the drifted files. Siblings are
    # chosen by filename-prefix similarity, which keeps a multi-season series
    # (My Happy Marriage carries S01 and S02 in one directory) comparing
    # like with like.
    drifted_paths = {str(a["relative_path"]) for a in drifted}
    candidates = sorted(
        (
            a for a in artifacts
            if str(a.get("artifact_type") or "") == "library"
            and str(a["relative_path"]) not in drifted_paths
            and PurePosixPath(str(a["relative_path"])).suffix.lower() in VIDEO_SUFFIXES
        ),
        key=lambda a: str(a["relative_path"]),
    )
    siblings = _spread_sample(candidates, SIBLING_SAMPLE_SIZE)

    if dry_run:
        row["stage"] = "dry-run"
        row["detail"] = "; ".join(
            f"{PurePosixPath(str(a['relative_path'])).name}"
            f"({remote_sizes[str(a['relative_path'])] - int(a['size_bytes']):+d})"
            for a in drifted
        )
        row["repaired"] = len(drifted)
        return row

    row["stage"] = "download"
    temp_root = Path(tempfile.mkdtemp(prefix="atr_drift_repair_", dir=settings.cache_dir))
    try:
        items = [PurePosixPath(str(a["relative_path"])) for a in drifted]
        sibling_items = [PurePosixPath(str(a["relative_path"])) for a in siblings]
        await StorageBoxRclone.download_batch(
            [*items, *sibling_items], remote_base=root, dest_root=temp_root,
            total_bytes=sum(remote_sizes[str(a["relative_path"])] for a in drifted),
        )

        row["stage"] = "verify"
        sibling_durations: list[float] = []
        for sibling_item in sibling_items:
            sibling_local = temp_root / sibling_item
            if not sibling_local.exists():
                continue
            ok, _, duration = probe_media(sibling_local)
            if ok:
                sibling_durations.append(duration)
        envelope = (
            (min(sibling_durations) - DURATION_ENVELOPE_MARGIN_SECONDS,
             max(sibling_durations) + DURATION_ENVELOPE_MARGIN_SECONDS)
            if sibling_durations
            else None
        )
        corrections: list[tuple[str, int, str, str]] = []
        rejected: list[str] = []
        for artifact, item in zip(drifted, items):
            local = temp_root / item
            filename = item.name
            if not local.exists():
                rejected.append(f"{filename}: download produced no file")
                continue
            actual_size = local.stat().st_size
            if actual_size != remote_sizes[str(artifact["relative_path"])]:
                rejected.append(f"{filename}: short download")
                continue
            description = "sidecar"
            if local.suffix.lower() in VIDEO_SUFFIXES:
                ok, description, duration = probe_media(local)
                if not ok:
                    rejected.append(f"{filename}: {description}")
                    continue
                if envelope is None:
                    rejected.append(f"{filename}: no probeable sibling to check runtime against")
                    continue
                if not envelope[0] <= duration <= envelope[1]:
                    rejected.append(
                        f"{filename}: runtime {duration:.0f}s outside sibling envelope "
                        f"{envelope[0]:.0f}-{envelope[1]:.0f}s"
                    )
                    continue
            corrections.append(
                (str(artifact["relative_path"]), actual_size, sha256_file(local), description)
            )

        if rejected:
            row["stage"] = "rejected"
            row["detail"] = "; ".join(rejected[:4])
            return row

        row["stage"] = "rewrite"
        sites = 0
        for relative_path, size, digest, _ in corrections:
            sites += apply_correction(manifest, relative_path, size, digest)
        manifest["total_size_bytes"] = sum(
            int(a.get("size_bytes") or 0)
            for a in manifest.get("artifacts", [])
            if isinstance(a, dict)
        )

        manifest_text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
        await StorageBoxSftpClient.write_text(root / "series_manifest.json", manifest_text)

        updated_current = dict(current)
        updated_current["manifest_checksum"] = hashlib.sha256(
            manifest_text.encode("utf-8")
        ).hexdigest()
        await StorageBoxSftpClient.write_text(
            StorageBoxRepository._current_path(scoped, series_id),
            json.dumps(updated_current, indent=2, sort_keys=True, ensure_ascii=False),
        )

        # The rewrite keeps the same release_id, so the locally cached copy of
        # this manifest sits at an unchanged path and still holds the old
        # hashes. LibraryHydrationService._load_or_fetch_manifest prefers that
        # cache over the box, so leaving it in place means hydration keeps
        # failing the checksum gate against bytes we just proved correct.
        cached = LibraryHydrationService._manifest_cache_path(
            scoped, series_id, release_id
        )
        cached.unlink(missing_ok=True)

        row["stage"] = "done"
        row["repaired"] = len(corrections)
        row["detail"] = f"{sites} manifest site(s) updated: " + "; ".join(
            f"{PurePosixPath(p).name} [{d}]" for p, _, _, d in corrections[:4]
        )
        return row
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


async def main_async() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-type", default="anime")
    parser.add_argument("--drift-tsv", type=Path,
                        default=REPO_ROOT / "docs/audit/manifest-drift-anime.tsv")
    parser.add_argument("--series-id", action="append", default=[],
                        help="Repair only these series ids; defaults to every DRIFT row.")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "docs/audit/manifest-drift-repair.tsv")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    LibraryStateDb.initialize()

    targets: list[tuple[str, str]] = []
    if args.drift_tsv.exists():
        for line in args.drift_tsv.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split("\t")
            if len(fields) > 2 and fields[2] == "DRIFT":
                targets.append((fields[0], fields[1]))
    if args.series_id:
        wanted = set(args.series_id)
        targets = [t for t in targets if t[0] in wanted]

    print(f"drifted series to repair: {len(targets)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fresh = not args.out.exists()
    handle = args.out.open("a", encoding="utf-8")
    if fresh:
        handle.write("\t".join(COLUMNS) + "\n")
        handle.flush()

    for position, (series_id, name) in enumerate(targets, start=1):
        print(f"[{position}/{len(targets)}] {name}", file=sys.stderr)
        try:
            row = await repair_series(args.library_type, series_id, name,
                                      dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - recorded per series, sweep continues
            row = {"series_id": series_id, "series": name, "stage": "error",
                   "repaired": 0, "detail": f"{type(exc).__name__}: {exc}"[:300]}
        line = "\t".join(
            str(row[c]).replace("\t", " ").replace("\n", " ") for c in COLUMNS
        )
        print(line)
        handle.write(line + "\n")
        handle.flush()

    handle.close()


if __name__ == "__main__":
    asyncio.run(main_async())
