#!/usr/bin/env python3
"""Registered edge localization probe: ORB+RANSAC affine registration of the
query edge frame onto the source plane, then (a) motion-masked NCC over the
shot's native frames, (b) phase-correlation shift-vs-time zero crossing (for
linear pans). Measures localization error vs GT on the owner-confirmed
static-trim and swoosh scenes BEFORE wiring anything into the aligner.
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

# (project, gt_idx, side)  side 0 = start edge, 1 = end edge
CASES = [
    ("85de83ca6323", 13, 0),
    ("85de83ca6323", 49, 0),
    ("5e85164d9ff8", 32, 1),
    ("5e85164d9ff8", 34, 0),
    ("5e85164d9ff8", 25, 0),  # owner: fast right-to-left swoosh
    ("5e85164d9ff8", 25, 1),
]
H = 360


def small_gray(img) -> np.ndarray:
    import PIL.Image
    w, h = img.size
    scale = H / h
    return np.asarray(
        img.convert("L").resize((int(w * scale), H))
    ).astype(np.float32)


def register(cv2, q_gray, s_gray):
    orb = cv2.ORB_create(1500)
    kq, dq = orb.detectAndCompute(q_gray.astype(np.uint8), None)
    ks, ds = orb.detectAndCompute(s_gray.astype(np.uint8), None)
    if dq is None or ds is None or len(kq) < 30 or len(ks) < 30:
        return None
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = matcher.match(dq, ds)
    if len(matches) < 20:
        return None
    qpts = np.float32([kq[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    spts = np.float32([ks[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    T, inliers = cv2.estimateAffinePartial2D(
        qpts, spts, ransacReprojThreshold=3.0
    )
    if T is None or inliers is None or int(inliers.sum()) < 15:
        return None
    warped = cv2.warpAffine(q_gray, T, (s_gray.shape[1], s_gray.shape[0]))
    valid = (
        cv2.warpAffine(np.ones_like(q_gray), T, (s_gray.shape[1], s_gray.shape[0]))
        > 0.5
    )
    return warped, valid, int(inliers.sum())


def masked_ncc(a, b, mask) -> float:
    av = a[mask]
    bv = b[mask]
    av = av - av.mean()
    bv = bv - bv.mean()
    d = np.sqrt((av * av).sum() * (bv * bv).sum()) + 1e-6
    return float((av * bv).sum() / d)


current_pid = None
for pid, idx, side in CASES:
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
    q_gray = small_gray(q)
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
    grays = [small_gray(im) for _, im in frames]
    ref_idx = int(np.argmin(np.abs(times - gt_edge)))
    reg = None
    # try registering against a few reference frames (the nearest may blur)
    for probe_ref in (ref_idx, max(0, ref_idx - 6), min(len(grays) - 1, ref_idx + 6)):
        reg = register(cv2, q_gray, grays[probe_ref])
        if reg is not None:
            break
    if reg is None:
        print(f"{pid} #{idx} side={side}: REGISTRATION FAILED", flush=True)
        continue
    warped, valid, n_inliers = reg
    stack = np.stack(grays)
    std = stack.std(axis=0)
    motion = std >= np.percentile(std, 75)
    mask = valid & motion
    if mask.sum() < 500:
        mask = valid
    scores = np.array([masked_ncc(warped, g, mask) for g in grays])
    k = int(np.argmax(scores))
    prom = float(scores[k]) - float(np.median(scores))
    # phase-correlation shift-vs-time (pan handling)
    shifts = []
    win = np.outer(np.hanning(warped.shape[0]), np.hanning(warped.shape[1]))
    for g in grays:
        (dx, dy), resp = cv2.phaseCorrelate(
            (warped * win).astype(np.float64), (g * win).astype(np.float64)
        )
        shifts.append((dx, resp))
    dxs = np.array([s[0] for s in shifts])
    resps = np.array([s[1] for s in shifts])
    # zero-crossing of dx(t) weighted by response
    best_zero = None
    for n in range(len(times) - 1):
        if dxs[n] == 0 or (dxs[n] < 0) != (dxs[n + 1] < 0):
            frac = abs(dxs[n]) / (abs(dxs[n]) + abs(dxs[n + 1]) + 1e-9)
            t0 = times[n] + frac * (times[n + 1] - times[n])
            if best_zero is None or resps[n] > best_zero[1]:
                best_zero = (t0, float(resps[n]))
    zero_txt = (
        f"panzero_err={best_zero[0]-gt_edge:+.3f}s resp={best_zero[1]:.3f}"
        if best_zero
        else "no-zero-crossing"
    )
    print(
        f"{pid} #{idx} side={side} gt={gt_edge:.2f}: inliers={n_inliers} "
        f"maskedNCC argmax_err={times[k]-gt_edge:+.3f}s score={scores[k]:.3f} "
        f"prom={prom:.3f} | {zero_txt}",
        flush=True,
    )
