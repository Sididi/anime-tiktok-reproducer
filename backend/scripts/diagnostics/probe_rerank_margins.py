#!/usr/bin/env python3
"""Measure pixel-NCC margins between the GT truth position and the generated
primary position for every scene of a project: on WP scenes the truth should
win (recall), on exact scenes the two coincide or the primary must not lose
(precision). Decides the rerank gate/margin from data.

Usage: probe_rerank_margins.py <project_id> <generated.json>
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

caps: dict[str, object] = {}


def get_cap(episode: str):
    path = AnimeLibraryService.resolve_episode_path(
        episode, library_type=project.library_type
    )
    if path is None or not path.exists():
        return None
    cap = caps.get(str(path))
    if cap is None:
        cap = cv2.VideoCapture(str(path))
        caps[str(path)] = cap
    return cap


# per GT scene: nearest generated scene by midpoint (crude fold)
for idx, (gs, gm) in enumerate(zip(gt_scenes, gt_matches)):
    if not gm["episode"]:
        continue
    mid = 0.5 * (gs["start_time"] + gs["end_time"])
    best = min(
        range(len(gen_scenes)),
        key=lambda k: abs(
            0.5 * (gen_scenes[k]["start_time"] + gen_scenes[k]["end_time"]) - mid
        ),
    )
    pm = gen_matches[best]
    if pm.get("was_no_match") or not pm.get("episode"):
        continue
    dur_tt = gs["end_time"] - gs["start_time"]
    if dur_tt <= 0.2:
        continue
    # query mid frames of the GT tiktok scene
    q_times = [gs["start_time"] + f * dur_tt for f in (0.2, 0.5, 0.8)]
    frames = AnimeMatcherService.extract_frames(video, q_times)
    q_mids = [
        (t, SceneAlignerService._gray96(fr))
        for t, fr in zip(q_times, frames)
        if fr is not None
    ]
    if not q_mids:
        continue

    def line(episode, s, e):
        rate = max(1e-6, (e - s)) / max(1e-6, dur_tt)
        return lambda t, _r=rate, _s=s: _s + (t - gs["start_time"]) * _r

    truth_fn = line(gm["episode"], gm["start_time"], gm["end_time"])
    prim_fn = line(pm["episode"], pm["start_time"], pm["end_time"])
    res_truth = SceneAlignerService._pixel_score_line(
        q_mids, truth_fn, get_cap, gm["episode"]
    )
    res_prim = SceneAlignerService._pixel_score_line(
        q_mids, prim_fn, get_cap, pm["episode"]
    )
    score_truth = res_truth[0] if res_truth else None
    score_prim = res_prim[0] if res_prim else None
    same_pos = (
        pm["episode"] == gm["episode"]
        and abs(pm["start_time"] - gm["start_time"]) < 2.0
    )
    kind = "SAME" if same_pos else "WP"
    st = "None" if score_truth is None else f"{score_truth:.3f}"
    sp = "None" if score_prim is None else f"{score_prim:.3f}"
    margin = (
        ""
        if score_truth is None or score_prim is None
        else f" margin={score_truth - score_prim:+.3f}"
    )
    print(f"#{idx:02d} {kind}: truth={st} primary={sp}{margin}", flush=True)

for cap in caps.values():
    cap.release()
