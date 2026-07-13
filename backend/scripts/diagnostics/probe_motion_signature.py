#!/usr/bin/env python3
"""D1 motion/temporal-signature probe on the §4 labeled bench (offline).

Frames lie; trajectories don't: duplicate still-shots and lookalike
instances differ in WHEN and HOW things move. For every bench entry this
computes, per stream (query clip / truth window / wrong pick / distractor):
  - E(t): frame-difference energy over a ~1/12s gap (global + 3x3 cells),
  - v(t): global phase-correlation shift velocity (dx, dy in px/s),
then correlates query-vs-candidate series under the candidate's line
mapping, maximized over a small offset sweep, and reports per-scene
margins (truth minus wrong) plus the control false-switch rate.

Decoded signatures are cached (npz) so score-function iteration is instant.

Usage:
  pixi run python backend/scripts/diagnostics/probe_motion_signature.py \
      [--bench ~/.cache/atr-eval/bench] [--labels H1,H2,TOL,CTRL]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import cv2
import numpy as np

GRID_HZ = 12.0
MAX_GRID_HZ = 24.0  # short-scene densification cap: below ~40ms the frame
MIN_SAMPLES = 16    # difference is compression noise, not motion (v2 lesson)
GRAY_H = 240
OFFSETS = np.arange(-0.6, 0.6 + 1e-6, 1.0 / 24.0)
OFFSET_PENALTY = 0.04  # counter max-over-trials bias (v61 lesson)
CELLS = 3
SIG_VERSION = 3


def decode_gray(path: Path) -> tuple[np.ndarray, list[np.ndarray]]:
    cap = cv2.VideoCapture(str(path))
    times, grays = [], []
    while True:
        ok = cap.grab()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        ok, frame = cap.retrieve()
        if not ok:
            break
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        w = max(1, int(g.shape[1] * GRAY_H / g.shape[0]))
        grays.append(cv2.resize(g, (w, GRAY_H)).astype(np.float32))
        times.append(t)
    cap.release()
    return np.array(times), grays


def frame_at_frac(path: Path, frac: float) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if n > 2:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((n - 1) * frac))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    w = max(1, int(g.shape[1] * GRAY_H / g.shape[0]))
    return cv2.resize(g, (w, GRAY_H)).astype(np.float32)


def _register_pair(q: np.ndarray, s: np.ndarray) -> tuple[float, float, float, float] | None:
    orb = cv2.ORB_create(1500)
    kq, dq = orb.detectAndCompute(q.astype(np.uint8), None)
    ks, ds = orb.detectAndCompute(s.astype(np.uint8), None)
    if dq is None or ds is None or len(kq) < 30 or len(ks) < 30:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(dq, ds)
    if len(matches) < 20:
        return None
    qpts = np.float32([kq[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    spts = np.float32([ks[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    T, inliers = cv2.estimateAffinePartial2D(qpts, spts, ransacReprojThreshold=3.0)
    if T is None or inliers is None or int(inliers.sum()) < 15:
        return None
    h, w = q.shape
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=np.float32)
    mapped = corners @ T[:, :2].T + T[:, 2]
    sh, sw = s.shape
    x0 = float(np.clip(mapped[:, 0].min() / sw, 0.0, 0.95))
    x1 = float(np.clip(mapped[:, 0].max() / sw, x0 + 0.05, 1.0))
    y0 = float(np.clip(mapped[:, 1].min() / sh, 0.0, 0.95))
    y1 = float(np.clip(mapped[:, 1].max() / sh, y0 + 0.05, 1.0))
    if (x1 - x0) * (y1 - y0) > 0.9:
        return None  # effectively full frame: skip the crop
    return (x0, y0, x1, y1)


def query_footprint(q_path: Path, t_path: Path) -> tuple[float, float, float, float] | None:
    """The query frame's footprint inside the source plane (fractional
    x0,y0,x1,y1), from ORB+RANSAC partial-affine registration of a query
    frame onto a candidate frame. The edit is a zoomed crop of the source;
    signatures must not integrate source motion the edit never shows.
    Several frame pairs are tried: a single blurred/low-texture frame must
    not kill the whole entry's geometry."""
    for qf, tf in ((0.5, 0.5), (0.25, 0.25), (0.75, 0.75), (0.5, 0.25), (0.5, 0.75)):
        q = frame_at_frac(q_path, qf)
        s = frame_at_frac(t_path, tf)
        if q is None or s is None:
            continue
        rect = _register_pair(q, s)
        if rect is not None:
            return rect
    return None


def signature(
    path: Path,
    cache_dir: Path,
    crop: tuple[float, float, float, float] | None = None,
) -> dict[str, np.ndarray] | None:
    """Motion signature series at the clip's native frame grid."""
    crop_tag = (
        "_c" + "-".join(f"{v:.2f}" for v in crop) if crop is not None else ""
    )
    key = cache_dir / (path.stem + f"_sig{SIG_VERSION}_h{GRAY_H}{crop_tag}.npz")
    if key.exists():
        z = np.load(key)
        return {k: z[k] for k in z.files}
    times, grays = decode_gray(path)
    if len(grays) < 4:
        return None
    if crop is not None:
        h, w = grays[0].shape
        ys, ye = int(crop[1] * h), max(int(crop[1] * h) + 8, int(crop[3] * h))
        xs, xe = int(crop[0] * w), max(int(crop[0] * w) + 8, int(crop[2] * w))
        grays = [g[ys:ye, xs:xe] for g in grays]
    fps = 1.0 / max(1e-6, float(np.median(np.diff(times))))
    step = max(1, int(round(fps / GRID_HZ)))
    # short clips: densify so Pearson never runs on a handful of points,
    # but never sample faster than MAX_GRID_HZ (compression-noise floor)
    min_step = max(1, int(round(fps / MAX_GRID_HZ)))
    while step > min_step and (len(grays) - step) // step < MIN_SAMPLES:
        step -= 1
    win = np.outer(np.hanning(grays[0].shape[0]), np.hanning(grays[0].shape[1]))
    t_out, energy, cells, vel = [], [], [], []
    h, w = grays[0].shape
    ys = np.linspace(0, h, CELLS + 1).astype(int)
    xs = np.linspace(0, w, CELLS + 1).astype(int)
    for i in range(0, len(grays) - step, step):
        a, b = grays[i], grays[i + step]
        dt = float(times[i + step] - times[i])
        if dt <= 0:
            continue
        d = np.abs(b - a)
        energy.append(float(d.mean()) / dt)
        cells.append(
            [
                float(d[ys[r]:ys[r + 1], xs[c]:xs[c + 1]].mean()) / dt
                for r in range(CELLS)
                for c in range(CELLS)
            ]
        )
        (dx, dy), resp = cv2.phaseCorrelate(
            (a * win).astype(np.float64), (b * win).astype(np.float64)
        )
        vel.append((dx / dt, dy / dt, resp))
        t_out.append(0.5 * (times[i] + times[i + step]))
    sig = {
        "t": np.array(t_out),
        "energy": np.array(energy),
        "cells": np.array(cells),
        "vel": np.array(vel),
    }
    np.savez_compressed(key, **sig)
    return sig


def decode_bgr(path: Path, every: int = 2) -> tuple[list[float], list[np.ndarray]]:
    cap = cv2.VideoCapture(str(path))
    times, frames = [], []
    n = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if n % every == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            frames.append(frame)
            times.append(t)
        n += 1
    cap.release()
    return times, frames


def sscd_embed(
    path: Path,
    cache_dir: Path,
    crop: tuple[float, float, float, float] | None,
    every: int,
) -> tuple[np.ndarray, np.ndarray] | None:
    """(times, embeddings) for a bench clip, footprint-cropped like the
    production zoom-crop (the query's geometry applied to the source)."""
    from PIL import Image

    from app.services.anime_matcher import AnimeMatcherService

    crop_tag = "_c" + "-".join(f"{v:.2f}" for v in crop) if crop else ""
    key = cache_dir / (path.stem + f"_sscd{crop_tag}_e{every}.npz")
    if key.exists():
        z = np.load(key)
        return z["t"], z["emb"]
    times, frames = decode_bgr(path, every)
    if not frames:
        return None
    if crop is not None:
        h, w = frames[0].shape[:2]
        ys, ye = int(crop[1] * h), max(int(crop[1] * h) + 8, int(crop[3] * h))
        xs, xe = int(crop[0] * w), max(int(crop[0] * w) + 8, int(crop[2] * w))
        frames = [f[ys:ye, xs:xe] for f in frames]
    pils = [
        Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames
    ]
    embs = AnimeMatcherService._embed_pil_batch(pils)
    t = np.array(times)
    np.savez_compressed(key, t=t, emb=embs)
    return t, embs


def sscd_score(
    q: tuple[np.ndarray, np.ndarray],
    c: tuple[np.ndarray, np.ndarray],
    q_span: tuple[float, float],
    c_span: tuple[float, float],
    pad: float,
) -> float | None:
    """Best mean cosine of query frames against candidate frames along the
    candidate line, swept +-0.6s (the production zoom-SSCD analogue)."""
    if q is None or c is None:
        return None
    qt, qe = q
    ct, ce = c
    rate = (c_span[1] - c_span[0]) / max(1e-6, q_span[1] - q_span[0])
    sims = qe @ ce.T
    best = None
    for off in np.arange(-0.6, 0.6 + 1e-6, 1.0 / 12.0):
        tc = pad + off + qt * rate
        cols = np.clip(np.searchsorted(ct, tc), 0, len(ct) - 1)
        prev_cols = np.clip(cols - 1, 0, len(ct) - 1)
        use_prev = np.abs(ct[prev_cols] - tc) < np.abs(ct[cols] - tc)
        cols = np.where(use_prev, prev_cols, cols)
        valid = np.abs(ct[cols] - tc) <= 0.15
        if valid.sum() < max(1, len(qt) * 2 // 3):
            continue
        score = float(np.mean(sims[np.arange(len(qt)), cols][valid]))
        if best is None or score > best:
            best = score
    return best


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 4 or a.std() < 1e-9 or b.std() < 1e-9:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def score_candidate(
    q: dict[str, np.ndarray],
    c: dict[str, np.ndarray],
    q_span: tuple[float, float],
    c_span: tuple[float, float],
    pad: float,
) -> dict[str, float] | None:
    """Correlate query and candidate signatures under the line mapping
    t_c = pad + (t_q - q0) * rate (+ offset sweep). Returns the best
    combined score and its components."""
    if q is None or c is None:
        return None
    q0, q1 = q_span
    c0, c1 = c_span
    rate = (c1 - c0) / max(1e-6, q1 - q0)
    grid = q["t"]
    keep = (grid >= 0.05) & (grid <= (q1 - q0) - 0.05)
    if keep.sum() < 5:
        keep = np.ones_like(grid, dtype=bool)
    grid = grid[keep]
    best: dict[str, float] | None = None
    for off in OFFSETS:
        tc = pad + off + grid * rate
        inside = (tc >= c["t"][0] - 0.05) & (tc <= c["t"][-1] + 0.05)
        if inside.sum() < max(5, 0.6 * grid.size):
            continue
        r_e = pearson(
            q["energy"][keep][inside], np.interp(tc[inside], c["t"], c["energy"])
        )
        cell_rs = [
            pearson(
                q["cells"][keep][inside, k],
                np.interp(tc[inside], c["t"], c["cells"][:, k]),
            )
            for k in range(q["cells"].shape[1])
        ]
        cell_rs = [r for r in cell_rs if not np.isnan(r)]
        r_cell = float(np.mean(cell_rs)) if cell_rs else float("nan")
        # shift components only where the QUERY actually moves (a still
        # shot's phase-correlation velocity is pure noise and symmetric
        # gating on the query keeps the comparison fair across candidates)
        r_dx = r_dy = float("nan")
        qdx = q["vel"][keep][inside, 0]
        qdy = q["vel"][keep][inside, 1]
        if qdx.size and float(qdx.max() - qdx.min()) >= 30.0:
            r_dx = pearson(qdx, np.interp(tc[inside], c["t"], c["vel"][:, 0]))
        if qdy.size and float(qdy.max() - qdy.min()) >= 30.0:
            r_dy = pearson(qdy, np.interp(tc[inside], c["t"], c["vel"][:, 1]))
        comps = {"r_e": r_e, "r_cell": r_cell, "r_dx": r_dx, "r_dy": r_dy}
        vals = [v for v in (r_e, r_cell) if not np.isnan(v)]
        shift_vals = [v for v in (r_dx, r_dy) if not np.isnan(v)]
        if not vals:
            continue
        combined = float(np.mean(vals))
        if shift_vals:
            combined = 0.6 * combined + 0.4 * float(np.mean(shift_vals))
        combined -= OFFSET_PENALTY * abs(float(off))
        if best is None or combined > best["score"]:
            best = {"score": combined, "offset": float(off), **comps}
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default=str(Path.home() / ".cache/atr-eval/bench"))
    ap.add_argument("--labels", default="H1,H2,TOL,CTRL")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--sscd", action="store_true",
                    help="also score zoom-SSCD margins (GPU)")
    args = ap.parse_args()
    bench = Path(args.bench).expanduser()
    labels = set(args.labels.split(","))
    manifest = json.loads((bench / "manifest.json").read_text())
    cache = bench / "sigcache"
    cache.mkdir(exist_ok=True)

    current_pid = None
    rows = []
    for e in manifest:
        if e["label"] not in labels or not e.get("ok"):
            continue
        if args.sscd and e["pid"] != current_pid:
            from app.services.anime_library import AnimeLibraryService
            from app.services.anime_matcher import AnimeMatcherService
            from app.services.project_service import ProjectService

            project = ProjectService.load(e["pid"])
            AnimeMatcherService._init_searcher(
                AnimeLibraryService.get_library_path(project.library_type),
                project.library_type,
                project.anime_name,
            )
            current_pid = e["pid"]
        tag = f"{e['pid'][:4]}_{e['gt_index']:02d}"
        q_sig = signature(bench / f"{tag}_query.mp4", cache)
        if q_sig is None:
            print(f"{tag} {e['label']}: query decode failed")
            continue
        q_span = tuple(e["tiktok"])
        out = {"tag": tag, "label": e["label"], "note": e["note"]}
        # production-faithful: each candidate is registered independently
        # (the aligner cannot register against an unknown truth)
        crop = (
            query_footprint(
                bench / f"{tag}_query.mp4", bench / f"{tag}_truth.mp4"
            )
            if (bench / f"{tag}_truth.mp4").exists()
            else None
        )
        out["crop"] = crop
        truth = score_candidate(
            q_sig,
            signature(bench / f"{tag}_truth.mp4", cache, crop),
            q_span,
            tuple(e["truth"]["interval"]),
            pad=1.25,
        ) if (bench / f"{tag}_truth.mp4").exists() else None
        out["truth"] = truth
        rival_file, rival_span, rival_kind = None, None, ""
        if e.get("pick") and not e["pick"].get("same_as_truth", False):
            rival_file = bench / f"{tag}_pick.mp4"
            rival_span = tuple(e["pick"]["interval"])
            rival_kind = (
                "near"
                if (
                    e["pick"]["episode"] == e["truth"]["episode"]
                    and abs(rival_span[0] - e["truth"]["interval"][0]) <= 1.5
                )
                else "distant"
            )
        elif e.get("distractor"):
            rival_file = bench / f"{tag}_distractor.mp4"
            rival_span = tuple(e["distractor"]["interval"])
            rival_kind = "distant"
        rival = None
        rival_crop = None
        if rival_file is not None and rival_file.exists():
            rival_crop = query_footprint(
                bench / f"{tag}_query.mp4", rival_file
            )
            out["rival_crop"] = rival_crop
            c_sig = signature(rival_file, cache, rival_crop)
            if c_sig is not None:
                rival = score_candidate(q_sig, c_sig, q_span, rival_span, pad=1.25)
        out["rival"] = rival
        out["rival_kind"] = rival_kind
        margin = (
            truth["score"] - rival["score"]
            if truth is not None and rival is not None
            else None
        )
        out["margin"] = margin
        stxt = ""
        if args.sscd:
            q_emb = sscd_embed(bench / f"{tag}_query.mp4", cache, None, 5)
            s_truth = (
                sscd_score(
                    q_emb,
                    sscd_embed(bench / f"{tag}_truth.mp4", cache, crop, 2),
                    q_span, tuple(e["truth"]["interval"]), pad=1.25,
                )
                if (bench / f"{tag}_truth.mp4").exists()
                else None
            )
            s_rival = (
                sscd_score(
                    q_emb,
                    sscd_embed(rival_file, cache, rival_crop, 2),
                    q_span, rival_span, pad=1.25,
                )
                if rival_file is not None and rival_file.exists()
                else None
            )
            out["sscd_truth"] = s_truth
            out["sscd_rival"] = s_rival
            out["sscd_margin"] = (
                s_truth - s_rival
                if s_truth is not None and s_rival is not None
                else None
            )
            stxt = (
                f" sscd_m={out['sscd_margin']:+.3f}"
                if out["sscd_margin"] is not None
                else " sscd_m=n/a"
            )
        rows.append(out)
        mtxt = f"margin={margin:+.3f}" if margin is not None else "margin=n/a"
        ttxt = f"truth={truth['score']:.3f}" if truth else "truth=FAIL"
        rtxt = f"rival={rival['score']:.3f}({rival_kind})" if rival else "rival=none"
        print(f"{tag} {e['label']:4s} {ttxt} {rtxt} {mtxt}{stxt}  | {e['note'][:50]}")

    print()
    for lab, kind in (
        ("H1", "distant"), ("H1", "near"), ("H2", ""), ("TOL", ""), ("CTRL", ""),
    ):
        ms = [
            r["margin"]
            for r in rows
            if r["label"] == lab and r["margin"] is not None
            and (not kind or r["rival_kind"] == kind)
        ]
        if not ms:
            continue
        arr = np.array(ms)
        pos = int((arr > 0).sum())
        print(
            f"{lab}{'/' + kind if kind else ''}: n={arr.size} "
            f"positive-margin={pos}/{arr.size} "
            f"min={arr.min():+.3f} median={np.median(arr):+.3f} max={arr.max():+.3f}"
        )
    if args.json_out:
        Path(args.json_out).expanduser().write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
