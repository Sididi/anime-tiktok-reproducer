#!/usr/bin/env python3
"""Feasibility probe: can native-decoded frames separate duplicate-instance
candidates that index-level SSCD cannot?

For chosen 85de WP cases (GT truth instance vs wrongly picked instance), we:
 1. extract the TikTok query frames for the scene (native res),
 2. decode source frames around BOTH candidate source instants,
 3. score each candidate by (a) SSCD cos on native frames, (b) gray pixel NCC
    after center-crop alignment (zoomed edit!), at the best temporal offset.
Prints per-case margins. If truth wins clearly under (a) or (b), native
re-ranking is viable; if neither separates, duplicates need assignment-level
reasoning instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, "/home/sid/Projects/anime-tiktok-reproducer/backend")
import numpy as np

from app.services.anime_matcher import AnimeMatcherService
from app.services.anime_library import AnimeLibraryService
from app.services.project_service import ProjectService

PID = "85de83ca6323"
# (tiktok_start, tiktok_end, truth_src_start, picked_src_start, episode_suffix)
CASES = [
    (25.58, 26.95, 683.07, 615.36),
    (27.88, 28.80, 691.90, 615.41),
    (38.62, 39.95, 787.24, 18.77),
    (47.43, 48.25, 906.70, 974.39),
]

project = ProjectService.load(PID)
video = Path(project.video_path)
library_path = AnimeLibraryService.get_library_path(project.library_type)
AnimeMatcherService._init_searcher(library_path, project.library_type, project.anime_name)

# GT episode for these scenes (single dominant episode edit)
import json
gtm = json.load(open(f"/home/sid/Projects/anime-tiktok-reproducer/backend/data/projects/{PID}/matches.json"))["matches"]
episode = gtm[0]["episode"]
ep_path = AnimeLibraryService.resolve_episode_path(episode, library_type=project.library_type)
print("episode path:", ep_path)

cv2 = AnimeMatcherService._require_cv2()


def gray_small(img, size=96):
    a = np.asarray(img.convert("L").resize((size, size)))
    return a.astype(np.float32)


def ncc(a, b):
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum()) + 1e-6
    return float((a * b).sum() / d)


def best_ncc_zoom(q, s):
    """NCC maximized over center-crop zoom factors of the SOURCE (edit is
    zoomed: tiktok shows a crop of the source)."""
    best = -1.0
    h, w = s.shape
    for z in (1.0, 1.15, 1.3, 1.45):
        ch, cw = int(h / z), int(w / z)
        y0, x0 = (h - ch) // 2, (w - cw) // 2
        crop = s[y0:y0 + ch, x0:x0 + cw]
        crop = np.asarray(
            __import__("PIL.Image", fromlist=["Image"]).fromarray(crop.astype(np.uint8)).resize((96, 96))
        ).astype(np.float32)
        best = max(best, ncc(q, crop))
    return best


cap = cv2.VideoCapture(str(ep_path))
for ts, te, truth, picked in CASES:
    dur = te - ts
    q_times = [ts + f * dur for f in (0.2, 0.5, 0.8)]
    q_frames = AnimeMatcherService.extract_frames(video, q_times)
    q_embs = AnimeMatcherService._embed_pil_batch([f.convert("RGB") for f in q_frames])
    q_grays = [gray_small(f) for f in q_frames]
    row = {"tiktok": (ts, te)}
    for label, src0 in (("truth", truth), ("picked", picked)):
        frames = AnimeMatcherService._collect_frames_in_window_from_capture(
            cap, src0 - 0.6, src0 + dur + 0.6, max_frames=160, sample_frames=int((dur + 1.2) * 12),
        )
        if not frames:
            row[label] = None
            continue
        times = np.array([t for t, _ in frames])
        embs = AnimeMatcherService._embed_pil_batch([im.convert("RGB") for _, im in frames])
        grays = [gray_small(im) for _, im in frames]
        # sweep offset: mean over the 3 query frames of best score
        offsets = np.arange(-0.5, 0.5 + 1e-6, 1.0 / 24)
        best_sscd, best_pix = -1, -1
        for off in offsets:
            targets = [src0 + (t - ts) + off for t in q_times]
            cols = np.clip(np.searchsorted(times, targets), 0, len(times) - 1)
            if max(abs(times[c] - tt) for c, tt in zip(cols, targets)) > 0.25:
                continue
            sscd = float(np.mean([q_embs[k] @ embs[c] for k, c in enumerate(cols)]))
            pix = float(np.mean([best_ncc_zoom(q_grays[k], grays[c]) for k, c in enumerate(cols)]))
            best_sscd = max(best_sscd, sscd)
            best_pix = max(best_pix, pix)
        row[label] = (round(best_sscd, 3), round(best_pix, 3))
    t, p = row.get("truth"), row.get("picked")
    verdict = "?"
    if t and p:
        verdict = f"SSCD margin={t[0]-p[0]:+.3f}  PIX margin={t[1]-p[1]:+.3f}"
    print(f"case tiktok=({ts},{te}) truth@{truth} vs picked@{picked}: truth={t} picked={p}  {verdict}")
cap.release()
