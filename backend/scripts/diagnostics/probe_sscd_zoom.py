#!/usr/bin/env python3
"""Zoom-aligned SSCD probe: cos(query emb, emb(zoom-cropped source frame))
margins truth-vs-primary. Tests whether SSCD separates duplicates once the
source is cropped to the edit's geometry (plain native SSCD prefers the
wrong instance; pixels are too noisy — measured 2026-07-10).

Usage: probe_sscd_zoom.py <project_id> <generated.json> [zoom]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from app.services.anime_library import AnimeLibraryService
from app.services.anime_matcher import AnimeMatcherService
from app.services.project_service import ProjectService

pid, gen_path = sys.argv[1], sys.argv[2]
ZOOMS = [float(z) for z in (sys.argv[3].split(",") if len(sys.argv) > 3 else ["1.0", "1.3"])]
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
    cap = caps.get(str(path))
    if cap is None:
        cap = cv2.VideoCapture(str(path))
        caps[str(path)] = cap
    return cap


def zoom_crop(img: Image.Image, zoom: float) -> Image.Image:
    w, h = img.size
    cw, ch = int(w / zoom), int(h / zoom)
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return img.crop((x0, y0, x0 + cw, y0 + ch))


def cand_frames(episode: str, s0: float, rate: float, ts0: float, q_times):
    cap = get_cap(episode)
    targets = [s0 + rate * (t - ts0) for t in q_times]
    lo, hi = min(targets) - 0.3, max(targets) + 0.3
    frames = AnimeMatcherService._collect_frames_in_window_from_capture(
        cap, lo, hi, max_frames=int((hi - lo) * 65) + 8,
        sample_frames=max(8, int((hi - lo) * 12)),
    )
    if not frames:
        return None
    times = np.array([t for t, _ in frames])
    out = []
    for tg in targets:
        pos = int(np.argmin(np.abs(times - tg)))
        if abs(times[pos] - tg) > 0.2:
            return None
        out.append(frames[pos][1])
    return out


rows = []
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
    q_frames = [f for f in AnimeMatcherService.extract_frames(video, q_times) if f]
    if len(q_frames) < 3:
        continue
    q_embs = AnimeMatcherService._embed_pil_batch([f.convert("RGB") for f in q_frames])
    rate_t = (gm["end_time"] - gm["start_time"]) / dur_tt
    rate_p = (pm["end_time"] - pm["start_time"]) / dur_tt
    ft = cand_frames(gm["episode"], gm["start_time"], rate_t, gs["start_time"], q_times)
    fp = cand_frames(pm["episode"], pm["start_time"], rate_p, gs["start_time"], q_times)
    same = (
        pm["episode"] == gm["episode"]
        and abs(pm["start_time"] - gm["start_time"]) < 2.0
    )
    kind = "SAME" if same else "WP"
    if ft is None or fp is None:
        print(f"#{idx:02d} {kind}: decode-miss")
        continue
    margins = []
    for z in ZOOMS:
        et = AnimeMatcherService._embed_pil_batch(
            [zoom_crop(f, z).convert("RGB") for f in ft]
        )
        ep_ = AnimeMatcherService._embed_pil_batch(
            [zoom_crop(f, z).convert("RGB") for f in fp]
        )
        st = float(np.mean([q_embs[i] @ et[i] for i in range(3)]))
        sp = float(np.mean([q_embs[i] @ ep_[i] for i in range(3)]))
        margins.append(f"z{z}={st - sp:+.3f}")
    print(f"#{idx:02d} {kind}: " + " ".join(margins), flush=True)

for cap in caps.values():
    cap.release()
