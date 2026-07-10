#!/usr/bin/env python3
"""Offline scorer-variant bench: for each 85de WP/SAME scene, compute the
margin (truth - primary) under several pixel scorers. The goal is a scorer
whose WP margins are positive and SAME margins hover at zero.

Usage: probe_scorer_variants.py <project_id> <generated.json>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from app.services.anime_library import AnimeLibraryService
from app.services.anime_matcher import AnimeMatcherService
from app.services.project_service import ProjectService

pid, gen_path = sys.argv[1], sys.argv[2]
project = ProjectService.load(pid)
video = Path(project.video_path)
AnimeMatcherService._init_searcher(
    AnimeLibraryService.get_library_path(project.library_type),
    project.library_type,
    project.anime_name,
)
cv2 = AnimeMatcherService._require_cv2()

gt_scenes = json.load(open(f"backend/data/projects/{pid}/scenes.json"))["scenes"]
gt_matches = json.load(open(f"backend/data/projects/{pid}/matches.json"))["matches"]
gen = json.load(open(gen_path))
gen_scenes = gen["scenes"]["scenes"]
gen_matches = gen["matches"]["matches"]

SIZE = 128


def gray(img) -> np.ndarray:
    return np.asarray(img.convert("L").resize((SIZE, SIZE))).astype(np.float32)


def grad(g: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return np.sqrt(gx * gx + gy * gy)


_CENTER = None


def center_mask() -> np.ndarray:
    global _CENTER
    if _CENTER is None:
        y = np.hanning(SIZE)[:, None]
        x = np.hanning(SIZE)[None, :]
        _CENTER = (0.25 + 0.75 * y * x).astype(np.float32)
    return _CENTER


def ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    if mask is not None:
        a = a * mask
        b = b * mask
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6
    return float((a * b).sum() / d)


GEOMS = [
    (zoom, dx, dy)
    for zoom in (1.0, 1.15, 1.3, 1.45)
    for dx, dy in ((0.0, 0.0), (0.05, 0.0), (-0.05, 0.0), (0.0, 0.05), (0.0, -0.05))
]


def crops(g: np.ndarray) -> list[np.ndarray]:
    h, w = g.shape
    out = []
    for zoom, dx, dy in GEOMS:
        ch, cw = int(h / zoom), int(w / zoom)
        y0 = min(max(int((h - ch) / 2 + dy * h), 0), h - ch)
        x0 = min(max(int((w - cw) / 2 + dx * w), 0), w - cw)
        out.append(cv2.resize(g[y0 : y0 + ch, x0 : x0 + cw], (SIZE, SIZE)))
    return out


def score_variants(q_gray: np.ndarray, s_gray: np.ndarray) -> dict[str, float]:
    cs = crops(s_gray)
    q_grad = grad(q_gray)
    return {
        "gray": max(ncc(q_gray, c) for c in cs),
        "grad": max(ncc(q_grad, grad(c)) for c in cs),
        "grad_mask": max(ncc(q_grad, grad(c), center_mask()) for c in cs),
    }


caps: dict[str, object] = {}


def get_cap(episode: str):
    path = AnimeLibraryService.resolve_episode_path(
        episode, library_type=project.library_type
    )
    cap = caps.get(str(path))
    if cap is None:
        cap = cv2.VideoCapture(str(path))
        caps[str(path)] = cap
    return cap


def best_scores(q_gray_list, episode, s0, rate, ts0, dur_tt):
    cap = get_cap(episode)
    lo = s0 - 0.7
    hi = s0 + rate * dur_tt + 0.7
    frames = AnimeMatcherService._collect_frames_in_window_from_capture(
        cap, lo, hi, max_frames=int((hi - lo) * 65) + 8,
        sample_frames=max(8, int((hi - lo) * 12)),
    )
    if not frames:
        return None
    times = np.array([t for t, _ in frames])
    grays = [gray(im) for _, im in frames]
    best: dict[str, float] = {}
    for delta in np.arange(-0.6, 0.6 + 1e-6, 1.0 / 12.0):
        per: dict[str, list[float]] = {}
        ok = 0
        for (tq, qg) in q_gray_list:
            target = s0 + rate * (tq - ts0) + delta
            pos = int(np.argmin(np.abs(times - target)))
            if abs(times[pos] - target) > 0.15:
                continue
            ok += 1
            for name, val in score_variants(qg, grays[pos]).items():
                per.setdefault(name, []).append(val)
        if ok < max(1, len(q_gray_list) * 2 // 3):
            continue
        for name, vals in per.items():
            mean = float(np.mean(vals))
            if name not in best or mean > best[name]:
                best[name] = mean
    return best or None


for idx, (gs, gm) in enumerate(zip(gt_scenes, gt_matches)):
    if not gm["episode"]:
        continue
    mid = 0.5 * (gs["start_time"] + gs["end_time"])
    k = min(
        range(len(gen_scenes)),
        key=lambda k: abs(
            0.5 * (gen_scenes[k]["start_time"] + gen_scenes[k]["end_time"]) - mid
        ),
    )
    pm = gen_matches[k]
    if pm.get("was_no_match") or not pm.get("episode"):
        continue
    dur_tt = gs["end_time"] - gs["start_time"]
    if dur_tt <= 0.2:
        continue
    q_times = [gs["start_time"] + f * dur_tt for f in (0.2, 0.5, 0.8)]
    frames = AnimeMatcherService.extract_frames(video, q_times)
    q_list = [(t, gray(fr)) for t, fr in zip(q_times, frames) if fr is not None]
    if not q_list:
        continue
    rate_t = (gm["end_time"] - gm["start_time"]) / dur_tt
    rate_p = (pm["end_time"] - pm["start_time"]) / dur_tt
    truth = best_scores(q_list, gm["episode"], gm["start_time"], rate_t,
                        gs["start_time"], dur_tt)
    prim = best_scores(q_list, pm["episode"], pm["start_time"], rate_p,
                       gs["start_time"], dur_tt)
    same = (
        pm["episode"] == gm["episode"]
        and abs(pm["start_time"] - gm["start_time"]) < 2.0
    )
    kind = "SAME" if same else "WP"
    if truth is None or prim is None:
        print(f"#{idx:02d} {kind}: decode-miss")
        continue
    margins = " ".join(
        f"{name}={truth[name] - prim[name]:+.3f}"
        for name in ("gray", "grad", "grad_mask")
        if name in truth and name in prim
    )
    print(f"#{idx:02d} {kind}: {margins}", flush=True)

for cap in caps.values():
    cap.release()
