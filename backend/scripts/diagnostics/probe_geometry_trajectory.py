#!/usr/bin/env python3
"""D3 bench probe (GOAL v4.2 §3): registered SCALE-VELOCITY and LOOP-PHASE
signatures for the signature-dead duplicate pairs (411f #51, 5e85 #11;
411f #28 measured as the owner's design-hint case).

Static signals (SSCD any zoom, pixel NCC any geometry) measure |margin|
<= 0.01 on these pairs (journal v33-v66, M1 round-1). What no instrument
compared yet is the TIME-DERIVATIVE of the registered geometry: ORB+RANSAC
partial-affine registration of query frame t onto the candidate frame at
the mapped time yields scale(t), tx(t), ty(t) — a correct instance/phase
keeps the trajectory COHERENT (smooth, low residual; a progressive
zoom-out is a clean monotone scale(t)), while a wrong loop phase or a
different instance wanders or drops registration.

Per candidate this reports:
  - reg_rate: fraction of samples that register (>=15 RANSAC inliers)
  - scale_slope: robust linear slope of scale(t) [the SCALE-VELOCITY]
  - scale_rough / shift_rough: median |second difference| of scale(t) and
    (tx,ty)(t) — trajectory roughness [the LOOP-PHASE coherence]
  - coherence: reg_rate - w_r * roughness (the candidate discriminator)
and the truth-minus-rival margins, plus control margins (false-switch).

Usage:
  pixi run python backend/scripts/diagnostics/probe_geometry_trajectory.py \
      [--bench ~/.cache/atr-eval/bench] [--json-out ...]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

GRID_HZ = 12.0
GRAY_H = 360          # registration needs more texture than the D1 240px
MIN_INLIERS = 15
OFFSET_SWEEP = np.arange(-0.25, 0.25 + 1e-6, 1.0 / 12.0)
SRC_PAD = 1.25        # bench clips carry this pad around the interval


def decode_gray_times(path: Path) -> tuple[np.ndarray, list[np.ndarray]]:
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
        grays.append(cv2.resize(g, (w, GRAY_H)))
        times.append(t)
    cap.release()
    return np.array(times), grays


def register(q: np.ndarray, c: np.ndarray):
    """ORB+RANSAC partial-affine q->c; returns (scale, tx, ty, inliers)."""
    orb = cv2.ORB_create(1500)
    kq, dq = orb.detectAndCompute(q, None)
    kc, dc = orb.detectAndCompute(c, None)
    if dq is None or dc is None or len(kq) < 25 or len(kc) < 25:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(dq, dc)
    if len(matches) < 18:
        return None
    qp = np.float32([kq[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    cp = np.float32([kc[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    T, inl = cv2.estimateAffinePartial2D(qp, cp, ransacReprojThreshold=3.0)
    if T is None or inl is None or int(inl.sum()) < MIN_INLIERS:
        return None
    scale = float(np.sqrt(abs(np.linalg.det(T[:, :2]))))
    return scale, float(T[0, 2]), float(T[1, 2]), int(inl.sum())


def trajectory(q_times, q_grays, c_times, c_grays, q_span, c_span, off):
    """Registered-geometry series under t_c = pad + off + (t_q)*rate."""
    rate = (c_span[1] - c_span[0]) / max(1e-6, q_span[1] - q_span[0])
    step = max(1, int(round(len(q_times) / max(1.0, (q_times[-1] - q_times[0]) * GRID_HZ))))
    rows = []
    for i in range(0, len(q_grays), step):
        tq = q_times[i]
        tc = SRC_PAD + off + tq * rate
        j = int(np.argmin(np.abs(c_times - tc)))
        if abs(c_times[j] - tc) > 0.15:
            continue
        r = register(q_grays[i], c_grays[j])
        rows.append((tq, *(r if r else (np.nan, np.nan, np.nan, 0))))
    return np.array(rows) if rows else None


def robust_slope(t: np.ndarray, v: np.ndarray) -> float:
    if t.size < 4:
        return float("nan")
    A = np.vstack([t, np.ones_like(t)]).T
    sol, *_ = np.linalg.lstsq(A, v, rcond=None)
    return float(sol[0])


def roughness(v: np.ndarray) -> float:
    if v.size < 4:
        return float("nan")
    return float(np.median(np.abs(np.diff(v, 2))))


def summarize(traj: np.ndarray | None, frame_w: float) -> dict | None:
    if traj is None or traj.shape[0] < 4:
        return None
    t = traj[:, 0]
    ok = ~np.isnan(traj[:, 1])
    reg_rate = float(ok.mean())
    out = {"n": int(traj.shape[0]), "reg_rate": reg_rate}
    if ok.sum() < 4:
        out.update(scale_slope=float("nan"), scale_rough=float("nan"),
                   shift_rough=float("nan"), coherence=0.0)
        return out
    ts, sc = t[ok], traj[ok, 1]
    tx, ty = traj[ok, 2] / frame_w, traj[ok, 3] / frame_w
    out["scale_slope"] = robust_slope(ts, sc)
    out["scale_rough"] = roughness(sc)
    out["shift_rough"] = float(np.nanmean([roughness(tx), roughness(ty)]))
    # trajectory coherence: registration persistence minus wander
    rough = np.nanmean([out["scale_rough"] * 4.0, out["shift_rough"] * 8.0])
    out["coherence"] = float(reg_rate - min(1.0, rough))
    return out


def best_candidate_summary(q_times, q_grays, c_path: Path, q_span, c_span,
                           frame_w: float) -> dict | None:
    c_times, c_grays = decode_gray_times(c_path)
    if len(c_grays) < 4:
        return None
    best = None
    for off in OFFSET_SWEEP:
        traj = trajectory(q_times, q_grays, c_times, c_grays, q_span, c_span, off)
        s = summarize(traj, frame_w)
        if s is None:
            continue
        s["offset"] = float(off)
        if best is None or s["coherence"] > best["coherence"]:
            best = s
    return best


def ffmpeg_cut(src: Path, start: float, end: float, out: Path) -> bool:
    if out.exists():
        return True
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start):.3f}", "-to", f"{end:.3f}", "-i", str(src),
        "-vf", "scale=-2:360", "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "22", str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default=str(Path.home() / ".cache/atr-eval/bench"))
    ap.add_argument("--labels", default="H1,H2,TOL,CTRL")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    bench = Path(args.bench).expanduser()
    labels = set(args.labels.split(","))
    manifest = json.loads((bench / "manifest.json").read_text())

    # extra rivals for 5e85#11 (no pick clip: the recovery's loop
    # lookalikes, journal v106 — the g12-line back-extension near 250.4 and
    # the alt cluster near 260.5), extracted on first run
    extra_rivals = {
        "5e85_11": [
            ("loopA", (250.36, 252.23)),
            ("loopB", (260.52, 262.39)),
        ],
    }
    from app.services.anime_library import AnimeLibraryService

    from evaluate_matching_against_ground_truth import _load_required

    rows = []
    for e in manifest:
        if e["label"] not in labels or not e.get("ok"):
            continue
        tag = f"{e['pid'][:4]}_{e['gt_index']:02d}"
        qf = bench / f"{tag}_query.mp4"
        if not qf.exists():
            continue
        q_times, q_grays = decode_gray_times(qf)
        if len(q_grays) < 4:
            print(f"{tag}: query decode failed")
            continue
        frame_w = float(q_grays[0].shape[1])
        q_span = tuple(e["tiktok"])
        out = {"tag": tag, "label": e["label"], "note": e["note"]}
        tf = bench / f"{tag}_truth.mp4"
        out["truth"] = (
            best_candidate_summary(q_times, q_grays, tf, q_span,
                                   tuple(e["truth"]["interval"]), frame_w)
            if tf.exists() else None
        )
        rivals = []
        if e.get("pick") and not e["pick"].get("same_as_truth", False):
            pf = bench / f"{tag}_pick.mp4"
            if pf.exists():
                s = best_candidate_summary(q_times, q_grays, pf, q_span,
                                           tuple(e["pick"]["interval"]), frame_w)
                if s is not None:
                    rivals.append(("pick", s))
        elif e.get("distractor"):
            df = bench / f"{tag}_distractor.mp4"
            if df.exists():
                s = best_candidate_summary(q_times, q_grays, df, q_span,
                                           tuple(e["distractor"]["interval"]),
                                           frame_w)
                if s is not None:
                    rivals.append(("distractor", s))
        if tag in extra_rivals:
            project, _, _ = _load_required(e["pid"])
            ep_path = AnimeLibraryService.resolve_episode_path(
                e["truth"]["episode"], library_type=project.library_type)
            for name, (lo, hi) in extra_rivals[tag]:
                rf = bench / f"{tag}_rival_{name}.mp4"
                if ffmpeg_cut(ep_path, lo - SRC_PAD, hi + SRC_PAD, rf):
                    s = best_candidate_summary(q_times, q_grays, rf, q_span,
                                               (lo, hi), frame_w)
                    if s is not None:
                        rivals.append((name, s))
        out["rivals"] = rivals
        margin = None
        if out["truth"] is not None and rivals:
            margin = out["truth"]["coherence"] - max(s["coherence"]
                                                     for _, s in rivals)
        out["margin"] = margin
        rows.append(out)

        def fmt(s):
            if s is None:
                return "FAIL"
            return (f"coh={s['coherence']:+.3f} reg={s['reg_rate']:.2f} "
                    f"slope={s['scale_slope']:+.4f} "
                    f"rough=({s['scale_rough']:.4f},{s['shift_rough']:.4f}) "
                    f"off={s['offset']:+.2f}")
        print(f"{tag} {e['label']:4s} truth[{fmt(out['truth'])}]")
        for name, s in rivals:
            print(f"          {name:10s}[{fmt(s)}]")
        if margin is not None:
            print(f"          margin={margin:+.3f}")

    print()
    for lab in ("H1", "H2", "TOL", "CTRL"):
        ms = [r["margin"] for r in rows if r["label"] == lab
              and r["margin"] is not None]
        if not ms:
            continue
        arr = np.array(ms)
        print(f"{lab}: n={arr.size} positive={int((arr > 0).sum())}/{arr.size} "
              f"min={arr.min():+.3f} median={np.median(arr):+.3f} "
              f"max={arr.max():+.3f}")
    if args.json_out:
        Path(args.json_out).expanduser().write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
