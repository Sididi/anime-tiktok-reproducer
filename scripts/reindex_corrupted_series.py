#!/usr/bin/env python
"""Overnight reindex of squish-corrupted series from local files.

Selects every indexed series whose local episode files provide at least the
coverage recorded in the index state (count-based), skipping the three
measured-healthy series. Each series is reindexed sequentially through the
CLI with its stored fps preserved, then verified by re-embedding 3 frames
from a real episode and comparing to the stored FAISS vectors (bit-identical
pipeline => expected cosine 1.0).

Run under the pixi env with PYTHONPATH pointing at modules/anime_searcher:
  cd modules/anime_searcher && PYTHONPATH=$PWD \
    pixi run --manifest-path ../../pixi.toml \
    python ../../scripts/reindex_corrupted_series.py [--dry-run|--only NAME]

Log lines are one per series; the run is resumable (done-file) and never
aborts on a single-series failure. Terminal markers: SUMMARY / FATAL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEARCHER = REPO / "modules" / "anime_searcher"
LIB = SEARCHER / "library" / "anime"
MODEL = SEARCHER / "sscd_disc_mixup.torchscript.pt"
WORKDIR = Path.home() / ".cache" / "atr-night-reindex"
DONE_FILE = WORKDIR / "done.txt"
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".webm", ".mov"}
HEALTHY_SKIP = {"Alien Father", "Binchou-tan", "Ojisan to Marshmallow"}
FRAME_ID_MASK = (1 << 63) - 1
PER_SERIES_TIMEOUT_S = 4 * 3600
MIN_FREE_GB = 20
# Not 0.9999: cuDNN kernel selection differs between the indexer's batch-64
# and the verifier's batch-3, which can cost ~2e-4 cosine on rare frames
# (measured: same pixels, batch64-vs-batch3 = 0.999840). Corruption sits at
# 0.6-0.88, so 0.999 still separates the two by two orders of magnitude.
VERIFY_MIN_COS = 0.999

_embedder = None


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def read_index_files() -> tuple[dict, dict]:
    manifest = json.loads((LIB / ".index" / "manifest.json").read_text())
    state = json.loads((LIB / ".index" / "state.json").read_text())["files"]
    return manifest, state


def select_series() -> tuple[list[tuple[str, float, int]], list[tuple[str, str]]]:
    manifest, state = read_index_files()
    default_fps = float(manifest.get("config", {}).get("default_fps") or 2.0)
    jobs: list[tuple[str, float, int]] = []
    skipped: list[tuple[str, str]] = []
    for series in sorted(manifest["series"]):
        fps = float(manifest["series"][series].get("fps") or default_fps)
        if series in HEALTHY_SKIP:
            skipped.append((series, "healthy"))
            continue
        folder = LIB / series
        on_disk = (
            [p for p in folder.rglob("*") if p.suffix.lower() in VIDEO_EXT]
            if folder.exists()
            else []
        )
        state_count = sum(1 for p in state if p.split("/", 1)[0] == series)
        if not on_disk:
            skipped.append((series, "no-local-files"))
            continue
        if len(on_disk) < state_count:
            skipped.append((series, f"coverage-loss({len(on_disk)}<{state_count})"))
            continue
        jobs.append((series, fps, len(on_disk)))
    return jobs, skipped


def compose_frame_id(rel_path: str, ordinal: int) -> int:
    digest = hashlib.blake2b(
        f"{rel_path}\0{ordinal}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") & FRAME_ID_MASK


def verify_series(series: str, fps: float) -> tuple[bool, str]:
    """Re-embed 3 frames of one episode via the reference path, compare stored."""
    global _embedder
    import faiss
    import numpy as np
    from PIL import Image

    manifest, state = read_index_files()
    key = manifest["series"][series]["key"]
    candidates = sorted(
        (p, s) for p, s in state.items() if p.split("/", 1)[0] == series
    )
    if not candidates:
        return False, "no state entries after reindex"
    # Prefer a file long enough to sample past the intro.
    rel_path, file_state = max(candidates, key=lambda item: item[1]["frame_count"])
    frame_count = file_state["frame_count"]
    if frame_count < 3:
        return False, f"sample file too short ({frame_count} frames)"
    last = min(42, frame_count - 1)
    ordinals = [last - 2, last - 1, last]

    index = faiss.read_index(str(LIB / ".index" / "series" / key / "faiss.index"))
    stored = np.stack(
        [index.reconstruct(compose_frame_id(rel_path, k)) for k in ordinals]
    )

    video = LIB / rel_path
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    width, height = int(probe[0]), int(probe[1])
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-i", str(video),
         "-vf", f"fps={fps}", "-frames:v", str(last + 1),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    frame_size = width * height * 3
    frames = [raw[i:i + frame_size] for i in range(0, len(raw), frame_size)]
    if len(frames) <= last or len(frames[last]) != frame_size:
        return False, f"decoded only {len(frames)} frames (needed {last + 1})"

    if _embedder is None:
        from anime_searcher.indexer.embedder import SSCDEmbedder

        _embedder = SSCDEmbedder(MODEL)
    images = [Image.frombuffer("RGB", (width, height), frames[k], "raw", "RGB", 0, 1) for k in ordinals]
    recomputed = _embedder.embed_batch(images)
    cos = np.sum(stored * recomputed, axis=1) / (
        np.linalg.norm(stored, axis=1) * np.linalg.norm(recomputed, axis=1) + 1e-12
    )
    if cos.min() > VERIFY_MIN_COS:
        return True, f"cos_min={cos.min():.6f}"
    return False, f"cos_min={cos.min():.4f} (expected ~1.0)"


def reindex_series(series: str, fps: float) -> tuple[bool, str]:
    cmd = [
        "pixi", "run", "--manifest-path", "../../pixi.toml",
        "python", "-m", "anime_searcher.cli", "index",
        str(LIB.parent), "--type", "anime",
        "--fps", str(fps), "--series", series, "--force",
        "--decode-backend", "auto", "--precision", "fp32",
        "--model", str(MODEL),
    ]
    try:
        result = subprocess.run(
            cmd, cwd=SEARCHER, capture_output=True, text=True,
            timeout=PER_SERIES_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {PER_SERIES_TIMEOUT_S}s"
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
        return False, "cli failed: " + " | ".join(tail)
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", help="process a single series then exit")
    args = parser.parse_args()

    WORKDIR.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if DONE_FILE.exists():
        done = {line for line in DONE_FILE.read_text().splitlines() if line}

    check = subprocess.run(
        ["pgrep", "-f", "anime_searcher.cli index"], capture_output=True
    )
    if check.returncode == 0:
        log("FATAL another anime_searcher indexing process is running")
        return 2
    free_gb = shutil.disk_usage(LIB).free / 1e9
    if free_gb < MIN_FREE_GB:
        log(f"FATAL only {free_gb:.0f}GB free (need {MIN_FREE_GB})")
        return 2

    jobs, skipped = select_series()
    if args.only:
        jobs = [j for j in jobs if j[0] == args.only]
        if not jobs:
            log(f"FATAL series {args.only!r} not eligible")
            return 2

    pending = [j for j in jobs if j[0] not in done]
    log(f"eligible={len(jobs)} done_before={len(jobs) - len(pending)} "
        f"pending={len(pending)} skipped={len(skipped)} free={free_gb:.0f}GB")
    for series, reason in skipped:
        log(f"SKIP {series} :: {reason}")
    if args.dry_run:
        for series, fps, files in pending:
            log(f"PLAN {series} fps={fps:g} files={files}")
        return 0

    started = time.time()
    ok_count = 0
    failures: list[str] = []
    for pos, (series, fps, files) in enumerate(pending, 1):
        t0 = time.time()
        success, detail = reindex_series(series, fps)
        if success:
            verified, vdetail = verify_series(series, fps)
            if verified:
                ok_count += 1
                with open(DONE_FILE, "a", encoding="utf-8") as f:
                    f.write(series + "\n")
                log(f"OK [{pos}/{len(pending)}] {series} files={files} "
                    f"dur={time.time() - t0:.0f}s {vdetail}")
            else:
                failures.append(series)
                log(f"FAIL [{pos}/{len(pending)}] {series} verify: {vdetail}")
        else:
            failures.append(series)
            log(f"FAIL [{pos}/{len(pending)}] {series} index: {detail}")

    hours = (time.time() - started) / 3600
    log(f"SUMMARY ok={ok_count} fail={len(failures)} "
        f"skipped={len(skipped)} elapsed={hours:.1f}h")
    for name in failures:
        log(f"SUMMARY-FAIL {name}")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - single terminal marker for the monitor
        log(f"FATAL {type(exc).__name__}: {exc}")
        sys.exit(2)
