#!/usr/bin/env python3
"""Can high-res pixel NCC time-localize a query edge frame WITHIN a
quasi-static source shot? (SSCD is motion-invariant here: margins +-0.001,
measured.) For each owner-confirmed static-trim scene, sweep NCC of the
query edge frame against native source frames and report where the argmax
lands relative to the GT edge.

Usage: probe_static_trim_localization.py
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
from app.services.scene_aligner import SceneAlignerService

# (project, gt_idx, side)  side 0 = start edge, 1 = end edge
CASES = [
    ("85de83ca6323", 13, 0, 1.45),
    ("85de83ca6323", 49, 0, 1.45),
    ("5e85164d9ff8", 32, 1, 1.3),
    ("5e85164d9ff8", 34, 0, 1.3),
]
SIZE = 256


def gray(img) -> np.ndarray:
    return np.asarray(img.convert("L").resize((SIZE, SIZE))).astype(np.float32)


def ncc(a, b) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6
    return float((a * b).sum() / d)


def zoom_gray(img, zoom) -> np.ndarray:
    return gray(SceneAlignerService._zoom_crop(img, zoom))


current_pid = None
for pid, idx, side, zoom in CASES:
    if pid != current_pid:
        project = ProjectService.load(pid)
        AnimeMatcherService._init_searcher(
            AnimeLibraryService.get_library_path(project.library_type),
            project.library_type,
            project.anime_name,
        )
        gt_s = json.load(open(f"backend/data/projects/{pid}/scenes.json"))["scenes"]
        gt_m = json.load(open(f"backend/data/projects/{pid}/matches.json"))["matches"]
        video = Path(project.video_path)
        cv2 = AnimeMatcherService._require_cv2()
        current_pid = pid
    gs, gm = gt_s[idx], gt_m[idx]
    q_t = gs["start_time"] + 0.02 if side == 0 else gs["end_time"] - 0.02
    gt_edge = gm["start_time"] if side == 0 else gm["end_time"]
    q = AnimeMatcherService.extract_frames(video, [q_t])[0]
    q_gray = gray(q)
    ep_path = AnimeLibraryService.resolve_episode_path(
        gm["episode"], library_type=project.library_type
    )
    cap = cv2.VideoCapture(str(ep_path))
    frames = AnimeMatcherService._collect_frames_in_window_from_capture(
        cap, gt_edge - 1.6, gt_edge + 1.6, max_frames=250,
        sample_frames=int(3.2 * 24),
    )
    cap.release()
    times = np.array([t for t, _ in frames])
    best_per_zoom = {}
    for z in (zoom - 0.15, zoom, zoom + 0.15):
        scores = np.array([ncc(q_gray, zoom_gray(im, z)) for _, im in frames])
        k = int(np.argmax(scores))
        prominence = float(scores[k]) - float(np.median(scores))
        best_per_zoom[round(z, 2)] = (
            round(float(times[k] - gt_edge), 3),
            round(float(scores[k]), 3),
            round(prominence, 3),
        )
    print(f"{pid} #{idx} side={side} gt_edge={gt_edge:.2f}:")
    for z, (err, score, prom) in best_per_zoom.items():
        print(f"  z={z}: argmax err={err:+.3f}s score={score} prominence={prom}", flush=True)
