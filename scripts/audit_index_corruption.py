#!/usr/bin/env python3
"""Audit indexed shards for the 288x288-squish corruption.

Background: until 2026-07-06 (anime_searcher 4917bc4) the ``auto`` decode
backend could select a CUDA path that resized frames to 288x288 *ignoring
aspect ratio*. SSCD embeddings built on squished frames land far from the
embedding the same frame produces through the correct short-edge-288 resize,
so every query against such a shard pays a permanent similarity tax
(measured cos 0.67-0.84 instead of ~1.0).

``engine_profile`` is useless as a marker -- it has read "sscd_exact_resize_v1"
since 2026-03-18, before the bug existed and after it was fixed. Detection is
therefore empirical: re-decode real frames through the current clean path,
re-embed them, and compare against the vector stored in the shard.

Verdicts are emitted per indexed *file*, not per series: a series indexed in
July and topped up in August legitimately holds both clean and corrupt files.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
# modules/anime_searcher here, modules/anime-searcher on the Windows fork.
for _candidate in (REPO_ROOT / "modules" / "anime_searcher", REPO_ROOT / "modules" / "anime-searcher"):
    if (_candidate / "anime_searcher" / "__init__.py").exists():
        sys.path.insert(0, str(_candidate))
        break

import faiss  # noqa: E402
from anime_searcher.config import (  # noqa: E402
    DEFAULT_MODEL_NAME,
    FAISS_INDEX_FILE,
    INDEX_DIR_NAME,
    MANIFEST_FILE,
    METADATA_FILE,
    SERIES_DIR_NAME,
)
from anime_searcher.indexer.embedder import SSCDEmbedder  # noqa: E402
from anime_searcher.indexer.frame_extractor import extract_frames  # noqa: E402

# A clean shard reproduces itself near-exactly; the squish tax sits far below.
# The gap between the bands is wide on purpose -- anything landing inside it is
# reported as GRAY and resampled rather than guessed at.
CLEAN_THRESHOLD = 0.95
CORRUPT_THRESHOLD = 0.90

TSV_HEADER = [
    "series",
    "file",
    "verdict",
    "cos_median",
    "cos_p10",
    "cos_min",
    "frames_scored",
    "max_ts_delta",
    "note",
]


@dataclass
class FileProbe:
    series: str
    rel_path: str
    verdict: str
    cos_median: float | None
    cos_p10: float | None
    cos_min: float | None
    scored: int
    max_ts_delta: float | None
    note: str

    def to_row(self) -> str:
        def num(value: float | None, digits: int = 4) -> str:
            return "NA" if value is None else f"{value:.{digits}f}"

        return "\t".join([
            self.series,
            self.rel_path,
            self.verdict,
            num(self.cos_median),
            num(self.cos_p10),
            num(self.cos_min),
            str(self.scored),
            num(self.max_ts_delta, 3),
            self.note,
        ])


def load_manifest(library_path: Path) -> dict:
    manifest_path = library_path / INDEX_DIR_NAME / MANIFEST_FILE
    if not manifest_path.exists():
        raise SystemExit(f"No index manifest at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_shard(library_path: Path, shard_key: str) -> tuple[faiss.Index, list[dict]]:
    shard_dir = library_path / INDEX_DIR_NAME / SERIES_DIR_NAME / shard_key
    index = faiss.read_index(str(shard_dir / FAISS_INDEX_FILE))
    meta = json.loads((shard_dir / METADATA_FILE).read_text(encoding="utf-8"))
    return index, meta.get("frames", [])


def normalize_shard_path(raw: str) -> str:
    """Shards written on the Windows fork store `Series\\episode.mp4`.

    On Linux that is one filename containing backslashes, so every lookup
    misses and the series reports FILE_MISSING. Normalize to POSIX separators
    before touching the filesystem.
    """
    return (raw or "").replace("\\", "/")


def group_frames_by_file(frames: list[dict]) -> dict[str, list[dict]]:
    """Frames are appended in decode order, so list position == frame ordinal."""
    grouped: dict[str, list[dict]] = {}
    for frame in frames:
        grouped.setdefault(normalize_shard_path(frame.get("file_path") or ""), []).append(frame)
    return grouped


def pick_ordinals(total: int, start: int, count: int, stride: int) -> list[int]:
    """Sample past the intro, spread out, without running off the end.

    Early frames are logos/black and late frames risk the ED card; both are
    poor identity probes because near-duplicate frames score high even when
    the ordinal alignment has slipped.
    """
    if total <= 0:
        return []
    if total <= count:
        return list(range(total))
    span_start = min(start, max(0, total - count * stride))
    ordinals = [span_start + i * stride for i in range(count)]
    ordinals = [o for o in ordinals if o < total]
    if len(ordinals) < count:
        # Short file: fall back to an even spread across whatever exists.
        step = max(1, total // count)
        ordinals = sorted({min(total - 1, i * step) for i in range(count)})
    return ordinals


def probe_file(
    *,
    embedder: SSCDEmbedder,
    index: faiss.Index,
    library_path: Path,
    series: str,
    rel_path: str,
    frames: list[dict],
    fps: float,
    ordinals: list[int],
) -> FileProbe:
    video_path = library_path / rel_path
    if not video_path.exists():
        return FileProbe(series, rel_path, "FILE_MISSING", None, None, None, 0, None,
                         "video absent locally; hydrate before probing")
    if not ordinals:
        return FileProbe(series, rel_path, "NO_FRAMES", None, None, None, 0, None,
                         "file has no indexed frames")

    wanted = set(ordinals)
    stop_after = max(ordinals)
    images: dict[int, object] = {}
    decoded_ts: dict[int, float] = {}
    try:
        for ordinal, (timestamp, image) in enumerate(extract_frames(video_path, fps)):
            if ordinal in wanted:
                images[ordinal] = image
                decoded_ts[ordinal] = timestamp
            if ordinal >= stop_after:
                break
    except Exception as exc:  # decode failures are data, not crashes
        return FileProbe(series, rel_path, "DECODE_ERROR", None, None, None, 0, None,
                         f"{type(exc).__name__}: {exc}"[:200])

    usable = [o for o in ordinals if o in images]
    if not usable:
        return FileProbe(series, rel_path, "DECODE_SHORT", None, None, None, 0, None,
                         f"decoded {len(images)} of {len(ordinals)} requested frames")

    fresh = embedder.embed_batch([images[o] for o in usable])

    stored_rows = []
    kept = []
    for position, ordinal in enumerate(usable):
        frame_id = int(frames[ordinal]["id"])
        try:
            stored_rows.append(index.reconstruct(frame_id))
            kept.append(position)
        except Exception:
            continue
    if not stored_rows:
        return FileProbe(series, rel_path, "NO_STORED_VECTORS", None, None, None, 0, None,
                         "shard holds no reconstructable vectors for these ids")

    stored = np.asarray(stored_rows, dtype=np.float32)
    fresh_kept = fresh[kept]
    cos = np.sum(stored * fresh_kept, axis=1) / (
        np.linalg.norm(stored, axis=1) * np.linalg.norm(fresh_kept, axis=1) + 1e-12
    )

    ts_deltas = [
        abs(decoded_ts[usable[p]] - float(frames[usable[p]].get("timestamp", 0.0)))
        for p in kept
    ]

    median = float(statistics.median(cos))
    p10 = float(np.percentile(cos, 10))
    lowest = float(np.min(cos))

    if median >= CLEAN_THRESHOLD:
        verdict = "CLEAN"
    elif median <= CORRUPT_THRESHOLD:
        verdict = "CORRUPT"
    else:
        verdict = "GRAY"

    return FileProbe(series, rel_path, verdict, median, p10, lowest, len(kept),
                     max(ts_deltas) if ts_deltas else None, "")


def resolve_model(library_path: Path, override: Path | None) -> Path:
    if override:
        return override
    candidates = [
        library_path / DEFAULT_MODEL_NAME,
        REPO_ROOT / "modules" / "anime_searcher" / DEFAULT_MODEL_NAME,
        REPO_ROOT / "modules" / "anime-searcher" / DEFAULT_MODEL_NAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit(f"SSCD model not found; looked in {[str(c) for c in candidates]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--library-root", type=Path,
                        default=REPO_ROOT / "modules" / "anime_searcher" / "library")
    parser.add_argument("--library-type", default="anime")
    parser.add_argument("--series", action="append", default=None,
                        help="Series display name; repeatable. Default: every series in the manifest.")
    parser.add_argument("--files-per-series", type=int, default=1,
                        help="Files to probe per series (0 = all files).")
    parser.add_argument("--start-ordinal", type=int, default=60)
    parser.add_argument("--count", type=int, default=24)
    # 3, not 10: the probe decodes sequentially up to the highest ordinal, so
    # stride sets the decode cost; corruption is whole-file, so a tight
    # cluster past the intro detects it as well as a wide spread. See the
    # rationale in rebuild_corrupt_series.py.
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None, help="Append results to this TSV.")
    parser.add_argument("--resume", action="store_true",
                        help="Skip series already present in --out.")
    args = parser.parse_args()

    library_path = (args.library_root / args.library_type).resolve()
    manifest = load_manifest(library_path)
    series_map = manifest.get("series", {})

    targets = args.series or sorted(series_map.keys())
    missing = [s for s in targets if s not in series_map]
    if missing:
        raise SystemExit(f"Not in manifest: {missing}")

    done: set[str] = set()
    out_handle = None
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.exists():
            if args.resume:
                for line in args.out.read_text(encoding="utf-8").splitlines()[1:]:
                    if line.strip():
                        done.add(line.split("\t")[0])
            out_handle = args.out.open("a", encoding="utf-8")
        else:
            out_handle = args.out.open("w", encoding="utf-8")
            out_handle.write("\t".join(TSV_HEADER) + "\n")
            out_handle.flush()

    todo = [s for s in targets if s not in done]
    if done:
        print(f"resume: skipping {len(done)} series already scored", file=sys.stderr)

    embedder = SSCDEmbedder(resolve_model(library_path, args.model))

    for position, series in enumerate(todo, start=1):
        entry = series_map[series]
        fps = float(entry.get("fps") or manifest.get("config", {}).get("default_fps") or 2.0)
        try:
            index, frames = load_shard(library_path, str(entry["key"]))
        except Exception as exc:
            probe = FileProbe(series, "-", "SHARD_ERROR", None, None, None, 0, None,
                              f"{type(exc).__name__}: {exc}"[:200])
            print(probe.to_row())
            if out_handle:
                out_handle.write(probe.to_row() + "\n")
                out_handle.flush()
            continue

        grouped = group_frames_by_file(frames)
        files = sorted(grouped.keys())
        if args.files_per_series > 0:
            # Prefer files that are actually on disk so triage does not waste a
            # slot reporting FILE_MISSING for a series that has other episodes.
            present = [f for f in files if (library_path / f).exists()]
            files = (present or files)[: args.files_per_series]

        print(f"[{position}/{len(todo)}] {series} ({len(files)} file(s), fps={fps})",
              file=sys.stderr)
        for rel_path in files:
            file_frames = grouped[rel_path]
            ordinals = pick_ordinals(len(file_frames), args.start_ordinal,
                                     args.count, args.stride)
            probe = probe_file(
                embedder=embedder, index=index, library_path=library_path,
                series=series, rel_path=rel_path, frames=file_frames,
                fps=fps, ordinals=ordinals,
            )
            print(probe.to_row())
            if out_handle:
                out_handle.write(probe.to_row() + "\n")
                out_handle.flush()

    if out_handle:
        out_handle.close()


if __name__ == "__main__":
    main()
