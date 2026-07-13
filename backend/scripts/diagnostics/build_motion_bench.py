#!/usr/bin/env python3
"""Build the GOAL v4 §4 labeled offline bench for the D1 motion instrument.

For each owner-labeled target scene (18 hard + 6 tolerable) extract:
  - the TikTok clip over the GT scene interval,
  - the GT-truth source window (owner-validated),
  - the current machine pick window (the wrong instance where applicable),
plus a control set of owner-passed scenes (with a distant distractor window
from the generated alternatives when one exists) across all four projects.

Clips land in ~/.cache/atr-eval/bench/ as small 360p mp4s at native fps with
a manifest.json describing every entry. GT folders are only read.

Usage:
  pixi run python backend/scripts/diagnostics/build_motion_bench.py \
      --generated ~/.cache/atr-eval/v101_fresh
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.anime_library import AnimeLibraryService

from evaluate_matching_against_ground_truth import (
    _fold_generated,
    _load_generated,
    _load_required,
)

# Owner-labeled failure set, GOAL v4 §2 (round-5 verdicts, 2026-07-11).
TARGETS: dict[str, list[tuple[int, str, str]]] = {
    "dcd74148c7ec": [
        (6, "H2", "missed 16.03 cut inside static content (+#7 fold)"),
    ],
    "85de83ca6323": [
        (0, "H2", "fold-no-chain"),
        (3, "H1", "wrong instance"),
        (10, "H1", "wrong instance"),
        (11, "H1", "wrong instance"),
        (13, "TOL", "quasi-static trim: first frame too soon"),
        (17, "H1", "wrong instance"),
        (19, "H1", "wrong instance"),
        (20, "H1", "wrong instance; start way too early"),
        (22, "H1", "wrong instance"),
        (24, "H1", "wrong instance"),
        (40, "H1", "wrong instance"),
        (49, "TOL", "quasi-static trim: first frame too soon"),
        (53, "H1", "wrong instance"),
    ],
    "411f73d26c1d": [
        (7, "TOL", "0.59x slow-mo action burst: retrieval evidence hole"),
        (8, "TOL", "0.59x slow-mo action burst: retrieval evidence hole"),
        (28, "H1", "wrong instance; first frame too soon"),
        (51, "H1", "wrong instance"),
    ],
    "5e85164d9ff8": [
        (11, "H1", "wrong instance"),
        (25, "H2", "extent 2.0s short; fast linear right-to-left swoosh"),
        (26, "H2", "extent 2.0s short (pair of #25)"),
        (32, "TOL", "quasi-static trim: too late"),
        (34, "TOL", "quasi-static trim: a bit too early"),
        (45, "H1", "wrong instance"),
    ],
}
CONTROLS_PER_PROJECT = 6
SRC_PAD = 1.25  # seconds of context around each source window


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


def merged_interval(matches: list, fold: list[int]) -> tuple[str, float, float] | None:
    picks = [matches[g] for g in fold if not matches[g].was_no_match]
    if not picks:
        return None
    return picks[0].episode, picks[0].start_time, picks[-1].end_time


def machine_claim(generated, gs) -> tuple[str, float, float] | None:
    """The machine's claimed source window over the GT scene's TikTok span:
    the max-overlap generated match's line evaluated at the GT boundaries.
    Robust to folds, no-coverage scenes and fold-no-chain (unlike merging
    generated intervals, which is incoherent across broken chains)."""
    best = None
    for g, gen_sc in enumerate(generated.scenes.scenes):
        ov = min(gen_sc.end_time, gs.end_time) - max(gen_sc.start_time, gs.start_time)
        if ov <= 0:
            continue
        m = generated.matches.matches[g]
        if m.was_no_match:
            continue
        if best is None or ov > best[0]:
            best = (ov, g)
    if best is None:
        return None
    g = best[1]
    gen_sc = generated.scenes.scenes[g]
    m = generated.matches.matches[g]
    dur = max(1e-6, gen_sc.end_time - gen_sc.start_time)
    rate = (m.end_time - m.start_time) / dur
    s = m.start_time + (gs.start_time - gen_sc.start_time) * rate
    return m.episode, s, s + (gs.end_time - gs.start_time) * rate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generated", required=True,
                    help="generated-json prefix (per-project suffix added)")
    ap.add_argument("--out", default=str(Path.home() / ".cache/atr-eval/bench"))
    ap.add_argument("--exact-tolerance", type=float, default=0.3)
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    gen_prefix = Path(args.generated).expanduser()

    manifest: list[dict] = []
    for pid, targets in TARGETS.items():
        project, gt_scene_list, gt_match_list = _load_required(pid)
        gt_scenes = gt_scene_list.scenes
        gt_matches = gt_match_list.matches
        video = Path(project.video_path)
        gen_path = gen_prefix.parent / f"{gen_prefix.name}_{pid}.json"
        generated = _load_generated(gen_path)
        folds = _fold_generated(generated.scenes.scenes, gt_scenes, 1.0)
        target_idx = {t[0] for t in targets}

        # controls: owner-passed scenes scoring exact on both axes, spread
        # across the timeline, longest-first inside each stride bucket
        controls: list[int] = []
        candidates: list[tuple[int, float]] = []
        for gi, (gs, gm) in enumerate(zip(gt_scenes, gt_matches)):
            if gi in target_idx or not folds[gi]:
                continue
            got = merged_interval(generated.matches.matches, folds[gi])
            if got is None:
                continue
            ep, s, e = got
            if ep != gm.episode:
                continue
            if (abs(s - gm.start_time) > args.exact_tolerance
                    or abs(e - gm.end_time) > args.exact_tolerance):
                continue
            dur = gs.end_time - gs.start_time
            if dur < 0.8:
                continue
            candidates.append((gi, dur))
        stride = max(1, len(candidates) // CONTROLS_PER_PROJECT)
        for k in range(0, len(candidates), stride):
            bucket = candidates[k:k + stride]
            controls.append(max(bucket, key=lambda c: c[1])[0])
            if len(controls) >= CONTROLS_PER_PROJECT:
                break

        entries = [(gi, label, note) for gi, label, note in targets]
        entries += [(gi, "CTRL", "owner-passed control") for gi in controls]
        for gi, label, note in entries:
            gs, gm = gt_scenes[gi], gt_matches[gi]
            got = machine_claim(generated, gs)
            entry = {
                "pid": pid, "gt_index": gi, "label": label, "note": note,
                "tiktok": [gs.start_time, gs.end_time],
                "truth": {
                    "episode": gm.episode,
                    "interval": [gm.start_time, gm.end_time],
                },
                "pick": None,
                "distractor": None,
            }
            tag = f"{pid[:4]}_{gi:02d}"
            ok = ffmpeg_cut(video, gs.start_time, gs.end_time,
                            out_dir / f"{tag}_query.mp4")
            ep_path = AnimeLibraryService.resolve_episode_path(
                gm.episode, library_type=project.library_type)
            ok &= ffmpeg_cut(ep_path, gm.start_time - SRC_PAD,
                             gm.end_time + SRC_PAD,
                             out_dir / f"{tag}_truth.mp4")
            if got is not None:
                ep, s, e = got
                entry["pick"] = {"episode": ep, "interval": [s, e]}
                same = (ep == gm.episode
                        and abs(s - gm.start_time) <= 0.35
                        and abs(e - gm.end_time) <= 0.35)
                entry["pick"]["same_as_truth"] = same
                if not same:
                    pk_path = AnimeLibraryService.resolve_episode_path(
                        ep, library_type=project.library_type)
                    ok &= ffmpeg_cut(pk_path, s - SRC_PAD, e + SRC_PAD,
                                     out_dir / f"{tag}_pick.mp4")
            # distant distractor from generated alternatives (controls: the
            # false-switch probe needs a plausible duplicate-style rival)
            if label == "CTRL" and folds[gi]:
                best = None
                for g in folds[gi]:
                    for alt in generated.matches.matches[g].alternatives:
                        distant = (alt.episode != gm.episode
                                   or abs(alt.start_time - gm.start_time) > 5.0)
                        if not distant:
                            continue
                        if best is None or alt.confidence > best.confidence:
                            best = alt
                if best is not None:
                    dur = gs.end_time - gs.start_time
                    entry["distractor"] = {
                        "episode": best.episode,
                        "interval": [best.start_time,
                                     best.start_time + dur],
                    }
                    d_path = AnimeLibraryService.resolve_episode_path(
                        best.episode, library_type=project.library_type)
                    ok &= ffmpeg_cut(
                        d_path, best.start_time - SRC_PAD,
                        best.start_time + dur + SRC_PAD,
                        out_dir / f"{tag}_distractor.mp4")
            entry["ok"] = ok
            manifest.append(entry)
            print(f"{pid} #{gi:2d} {label:4s} "
                  f"pick={'same' if entry['pick'] and entry['pick'].get('same_as_truth') else ('yes' if entry['pick'] else 'none')} "
                  f"distractor={'yes' if entry['distractor'] else 'no'} ok={ok}")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    n_ctrl = sum(1 for e in manifest if e["label"] == "CTRL")
    n_hard = sum(1 for e in manifest if e["label"] in ("H1", "H2"))
    print(f"\n{len(manifest)} entries ({n_hard} hard, "
          f"{sum(1 for e in manifest if e['label'] == 'TOL')} tolerable, "
          f"{n_ctrl} controls) -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
