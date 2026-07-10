#!/usr/bin/env python3
"""Attribute scene-axis errors: for each GT interior boundary, was it
(a) present in the detector's (pre-DP, post-presnap) boundary list,
(b) present in the final generated scene list?
detector-missing -> detector/presnap owns it; detector-present but
final-missing -> DP merge owns it; final-present-offset -> placement."""
import json, sys
from pathlib import Path

PROJ = Path("/home/sid/Projects/anime-tiktok-reproducer/backend/data/projects")
CACHE = Path.home() / ".cache/atr-eval"
TOL = 0.3

for pid in sys.argv[1:]:
    gt = json.load(open(PROJ / pid / "scenes.json"))["scenes"]
    gen = json.load(open(CACHE / f"v57_newgt_{pid}.json"))
    gt_bounds = [s["end_time"] for s in gt[:-1]]
    final_bounds = [s["end_time"] for s in gen["scenes"]["scenes"][:-1]]
    det_bounds = [
        b["boundary"]
        for b in gen["aligner_debug"].get("stage4_attempts", [])
        if isinstance(b, dict) and "boundary" in b
    ]
    det_missing, dp_merged, placed_off, ok = [], [], [], 0
    for tb in gt_bounds:
        d_det = min((abs(b - tb) for b in det_bounds), default=9e9)
        d_fin = min((abs(b - tb) for b in final_bounds), default=9e9)
        if d_fin <= TOL:
            ok += 1
        elif d_det > TOL:
            det_missing.append((round(tb, 2), round(d_det, 2)))
        else:
            dp_merged.append((round(tb, 2), round(d_det, 2)))
    # excess final boundaries (over-cuts, fold back if they chain)
    excess = [
        round(b, 2)
        for b in final_bounds
        if min((abs(b - tb) for tb in gt_bounds), default=9e9) > TOL
    ]
    print(f"== {pid}: GT interior boundaries={len(gt_bounds)}")
    print(f"   matched<= {TOL}s: {ok}")
    print(f"   DETECTOR-missing (no pre-DP boundary within {TOL}s): {det_missing}")
    print(f"   DP-merged (detector had it, final lost it): {dp_merged}")
    print(f"   excess final boundaries (fold-back candidates): {len(excess)}")
