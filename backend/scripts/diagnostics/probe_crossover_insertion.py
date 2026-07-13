#!/usr/bin/env python3
"""D4 bench probe (GOAL v4.2 §3): source-side cut INSERTION at the A/B
score crossover, for evidence-hole boundaries (85de #10, #20).

No TikTok-side pixel cut exists at these GT boundaries (v134 measured at
detector threshold 8). But the machine knows the two neighbouring lines:
this probe scores the TikTok samples over t under line A (the content
shown before the boundary) and line B (the content after), on registered
footprints with per-line offset calibration on each line's HOME region,
and asks whether sB(t)-sA(t) crosses zero cleanly at the GT boundary.

Controls (owner-passed scenes from the §4 bench manifest): line A = the
scene's own truth line, line B = the NEXT GT scene's truth line extended
backward — a clean instrument must NOT fire an insertion inside an
owner-passed scene (>=3 consecutive B-wins away from the true end).

Usage:
  pixi run python backend/scripts/diagnostics/probe_crossover_insertion.py \
      [--bench ~/.cache/atr-eval/bench] [--min-run 3] [--json-out ...]
"""
import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from probe_motion_signature import decode_bgr, query_footprint

GRID_HZ = 12.0
SRC_PAD = 1.0
MATCH_TOL = 0.15          # candidate-frame proximity to the mapped time
OFFSET_SWEEP = np.arange(-0.4, 0.4 + 1e-6, 1.0 / 24.0)


@dataclass
class Line:
    """src(t) = s0 + (t - t0) * rate, for TikTok time t (absolute)."""
    episode: str
    t0: float
    s0: float
    rate: float

    def src(self, t: float) -> float:
        return self.s0 + (t - self.t0) * self.rate


@dataclass
class Case:
    pid: str
    name: str
    kind: str                 # "positive" | "control"
    span: tuple[float, float]  # TikTok span holding the (potential) boundary
    line_a: Line
    line_b: Line
    home_a: tuple[float, float]  # region where A is believed (calibration)
    home_b: tuple[float, float]
    boundary_gt: float | None  # expected crossover (positives only)


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
    if res.returncode != 0:
        print(f"  ffmpeg FAILED for {out.name}: {res.stderr[-200:]}")
        return False
    return True


def embed_clip(path: Path, cache: Path, crop, every: int):
    from PIL import Image
    import cv2

    from app.services.anime_matcher import AnimeMatcherService

    crop_tag = "_c" + "-".join(f"{v:.2f}" for v in crop) if crop else ""
    key = cache / (path.stem + f"_xemb{crop_tag}_e{every}.npz")
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
    pils = [Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)) for f in frames]
    embs = AnimeMatcherService._embed_pil_batch(pils)
    t = np.array(times)
    np.savez_compressed(key, t=t, emb=embs)
    return t, embs


def per_sample_scores(q, c, q_times_abs, span_start, line: Line,
                      win_start: float, off: float) -> np.ndarray:
    """Best cosine near the mapped time for every query sample (nan when
    no candidate frame lies within MATCH_TOL)."""
    qt, qe = q
    ct, ce = c
    sims = qe @ ce.T
    out = np.full(len(q_times_abs), np.nan)
    for i, t_abs in enumerate(q_times_abs):
        qi = int(np.argmin(np.abs(qt - (t_abs - span_start))))
        m = line.src(t_abs) - win_start + off
        lo = np.searchsorted(ct, m - MATCH_TOL)
        hi = np.searchsorted(ct, m + MATCH_TOL)
        if hi <= lo:
            continue
        out[i] = float(sims[qi, lo:hi].max())
    return out


def calibrate_offset(q, c, q_times_abs, span_start, line: Line,
                     win_start: float, home: tuple[float, float]) -> float:
    mask = (q_times_abs >= home[0]) & (q_times_abs <= home[1])
    if mask.sum() < 3:
        mask = np.ones_like(q_times_abs, dtype=bool)
    best_off, best_score = 0.0, -1e9
    for off in OFFSET_SWEEP:
        s = per_sample_scores(q, c, q_times_abs[mask], span_start, line,
                              win_start, off)
        valid = ~np.isnan(s)
        if valid.sum() < max(2, 0.5 * mask.sum()):
            continue
        score = float(np.nanmean(s)) - 0.02 * abs(off)
        if score > best_score:
            best_off, best_score = float(off), score
    return best_off


def run_case(case: Case, bench: Path, cache: Path, video: Path,
             ep_paths: dict[str, Path], min_run: int) -> dict:
    tag = f"x_{case.pid[:4]}_{case.name}"
    t0, t1 = case.span
    qf = bench / f"{tag}_q.mp4"
    ok = ffmpeg_cut(video, t0, t1, qf)
    win = {}
    for side, line in (("a", case.line_a), ("b", case.line_b)):
        lo = min(line.src(t0), line.src(t1)) - SRC_PAD
        hi = max(line.src(t0), line.src(t1)) + SRC_PAD
        f = bench / f"{tag}_{side}.mp4"
        ok &= ffmpeg_cut(ep_paths[line.episode], lo, hi, f)
        win[side] = (f, lo)
    if not ok:
        return {"tag": tag, "kind": case.kind, "error": "extract"}

    q = embed_clip(qf, cache, None, 2)
    if q is None:
        return {"tag": tag, "kind": case.kind, "error": "query decode"}
    grid = np.arange(t0 + 0.04, t1 - 0.04, 1.0 / GRID_HZ)

    scores = {}
    for side, line in (("a", case.line_a), ("b", case.line_b)):
        f, lo = win[side]
        crop = query_footprint(qf, f)
        c = embed_clip(f, cache, crop, 2)
        if c is None:
            return {"tag": tag, "kind": case.kind, "error": f"window {side}"}
        home = case.home_a if side == "a" else case.home_b
        off = calibrate_offset(q, c, grid, t0, line, lo, home)
        scores[side] = per_sample_scores(q, c, grid, t0, line, lo, off)
        scores[side + "_off"] = off
        scores[side + "_crop"] = crop
    d = scores["b"] - scores["a"]

    # longest B-winning run and the first clean crossover
    runs = []
    start = None
    for i, v in enumerate(d):
        if not np.isnan(v) and v > 0:
            if start is None:
                start = i
        else:
            if start is not None:
                runs.append((start, i - 1))
                start = None
    if start is not None:
        runs.append((start, len(d) - 1))
    cross = None
    for s, e in runs:
        if e - s + 1 >= min_run and e == len(d) - 1:
            # insertion semantics: B out-explains A from here TO THE END
            cross = 0.5 * (grid[s] + (grid[s - 1] if s > 0 else grid[s]))
            run = (s, e)
            break
    interior_fire = None
    if case.kind == "control":
        # any >=min_run B-run that ends >0.3s before the scene end is a
        # phantom insertion (B = the NEXT scene's line must only win at
        # the very end, if at all)
        for s, e in runs:
            if e - s + 1 >= min_run and grid[e] < t1 - 0.3:
                interior_fire = {
                    "at": float(grid[s]),
                    "len": int(e - s + 1),
                    "mean_margin": float(np.nanmean(d[s:e + 1])),
                }
                break
    out = {
        "tag": tag, "kind": case.kind, "span": [t0, t1],
        "boundary_gt": case.boundary_gt,
        "off_a": scores["a_off"], "off_b": scores["b_off"],
        "grid": [round(float(g), 3) for g in grid],
        "sa": [None if np.isnan(v) else round(float(v), 4) for v in scores["a"]],
        "sb": [None if np.isnan(v) else round(float(v), 4) for v in scores["b"]],
        "crossover": cross,
        "runs": [[int(s), int(e)] for s, e in runs],
        "interior_fire": interior_fire,
    }
    if cross is not None and case.boundary_gt is not None:
        out["cross_err"] = float(cross - case.boundary_gt)
        s, e = run
        out["run_margins"] = [round(float(v), 4) for v in d[s:min(e + 1, s + 6)]]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default=str(Path.home() / ".cache/atr-eval/bench"))
    ap.add_argument("--min-run", type=int, default=3)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--controls", type=int, default=24)
    args = ap.parse_args()
    bench = Path(args.bench).expanduser()
    cache = bench / "sigcache"
    cache.mkdir(exist_ok=True)

    from app.services.anime_library import AnimeLibraryService

    from evaluate_matching_against_ground_truth import _load_required

    manifest = json.loads((bench / "manifest.json").read_text())

    cases: list[Case] = []
    gt_cache: dict[str, tuple] = {}

    def gt_for(pid):
        if pid not in gt_cache:
            gt_cache[pid] = _load_required(pid)
        return gt_cache[pid]

    def truth_line(pid, gi) -> Line:
        _, gt_scenes, gt_matches = gt_for(pid)
        gs, gm = gt_scenes.scenes[gi], gt_matches.matches[gi]
        rate = (gm.end_time - gm.start_time) / max(1e-6, gs.end_time - gs.start_time)
        return Line(gm.episode, gs.start_time, gm.start_time, rate)

    # --- positive case 85de#10: line A = the machine's OWN chain line over
    # GT#9+#10 (owner-passed for #9), line B = #10's truth line.
    pid = "85de83ca6323"
    _, gt_scenes, _ = gt_for(pid)
    g9, g10 = gt_scenes.scenes[9], gt_scenes.scenes[10]
    cases.append(Case(
        pid, "10ins", "positive", (g9.start_time, g10.end_time),
        line_a=Line("[ASW] Class de 2-banme ni Kawaii Onnanoko to Tomodachi ni"
                    " Natta - 01 [1080p HEVC][379F1232]",
                    11.25, 254.37, 1.0),   # generated g11 (v137)
        line_b=truth_line(pid, 10),
        home_a=(g9.start_time, g10.start_time - 0.1),
        home_b=(g10.start_time + 0.1, g10.end_time),
        boundary_gt=g10.start_time,
    ))
    # --- positive case 85de#20: line A = #19's truth line (the content
    # before the boundary; the machine's piece there is no-match), line B =
    # #20's truth line.
    g19, g20 = gt_scenes.scenes[19], gt_scenes.scenes[20]
    cases.append(Case(
        pid, "20ins", "positive", (g19.start_time, min(g20.end_time, g19.end_time + 2.0)),
        line_a=truth_line(pid, 19),
        line_b=truth_line(pid, 20),
        home_a=(g19.start_time, g20.start_time - 0.1),
        home_b=(g20.start_time + 0.1, min(g20.end_time, g19.end_time + 2.0)),
        boundary_gt=g20.start_time,
    ))

    # --- controls from the CTRL manifest entries
    n_ctrl = 0
    for e in manifest:
        if e["label"] != "CTRL" or not e.get("ok") or n_ctrl >= args.controls:
            continue
        pid = e["pid"]
        gi = e["gt_index"]
        _, gt_scenes, gt_matches = gt_for(pid)
        if gi + 1 >= len(gt_scenes.scenes):
            continue
        nxt = gt_matches.matches[gi + 1]
        if nxt.was_no_match if hasattr(nxt, "was_no_match") else False:
            continue
        gs = gt_scenes.scenes[gi]
        gs1 = gt_scenes.scenes[gi + 1]
        if gs1.start_time - gs.end_time > 0.5:
            continue
        cases.append(Case(
            pid, f"c{gi:02d}", "control", (gs.start_time, gs.end_time),
            line_a=truth_line(pid, gi),
            line_b=truth_line(pid, gi + 1),
            home_a=(gs.start_time, gs.end_time),
            home_b=(gs1.start_time, gs1.end_time),
            boundary_gt=None,
        ))
        n_ctrl += 1

    rows = []
    cur_pid = None
    video = None
    ep_paths: dict[str, Path] = {}
    for case in cases:
        if case.pid != cur_pid:
            project, _, _ = gt_for(case.pid)
            video = Path(project.video_path)
            cur_pid = case.pid
            from app.services.anime_matcher import AnimeMatcherService

            AnimeMatcherService._init_searcher(
                AnimeLibraryService.get_library_path(project.library_type),
                project.library_type,
                project.anime_name,
            )
        for line in (case.line_a, case.line_b):
            if line.episode not in ep_paths:
                project, _, _ = gt_for(case.pid)
                ep_paths[line.episode] = AnimeLibraryService.resolve_episode_path(
                    line.episode, library_type=project.library_type)
        r = run_case(case, bench, cache, video, ep_paths, args.min_run)
        rows.append(r)
        if "error" in r:
            print(f"{r['tag']} {r['kind']}: ERROR {r['error']}")
            continue
        if case.kind == "positive":
            ctxt = (f"crossover={r['crossover']:.3f} err={r['cross_err']:+.3f} "
                    f"run_margins={r['run_margins']}"
                    if r.get("crossover") is not None else "crossover=NONE")
            print(f"{r['tag']} POS gt={case.boundary_gt:.3f} {ctxt} "
                  f"offs=({r['off_a']:+.2f},{r['off_b']:+.2f})")
        else:
            fire = r["interior_fire"]
            print(f"{r['tag']} CTRL interior_fire="
                  f"{'NONE' if fire is None else fire}")

    n_fire = sum(1 for r in rows if r.get("interior_fire"))
    n_ctrl = sum(1 for r in rows if r["kind"] == "control" and "error" not in r)
    print(f"\ncontrols: {n_ctrl}, phantom insertions: {n_fire}")
    if args.json_out:
        Path(args.json_out).expanduser().write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
