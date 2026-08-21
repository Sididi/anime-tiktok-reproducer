#!/usr/bin/env python3
"""Read-only cold/warm benchmark for the independent bounded timeline metric."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))

from app.models import SceneList  # noqa: E402
from app.services.anime_library import AnimeLibraryService  # noqa: E402
from app.services.anime_matcher import AnimeMatcherService  # noqa: E402
from app.services.matching_ground_truth_v2 import (  # noqa: E402
    GroundTruthSnapshot,
    acceptance_report,
    calibrate_confidence_leave_one_project_out,
    duplicate_ambiguity_intervals,
    ensure_safe_output_dir,
    evaluate_timeline,
    mismatch_intervals,
    write_review_html,
)
from app.services.project_service import ProjectService  # noqa: E402
from app.services.scene_aligner import SceneAlignerService  # noqa: E402
from app.services.scene_detector import SceneDetectorService  # noqa: E402


DEFAULT_PROJECTS = (
    "dcd74148c7ec",
    "85de83ca6323",
    "411f73d26c1d",
    "5e85164d9ff8",
)


def build_review_assets(
    output_dir: Path,
    project_id: str,
    video: Path,
    library_type,
    intervals: list[dict],
    timeline_rows: list[dict],
) -> list[dict[str, str]]:
    """Render focused query/expected/generated frames outside GT trees."""
    asset_dir = output_dir / f"{project_id}_review_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    midpoints = [0.5 * (item["start"] + item["end"]) for item in intervals]
    query_frames = AnimeMatcherService.extract_frames(video, midpoints)
    assets: list[dict[str, str]] = []
    for index, (interval, midpoint) in enumerate(zip(intervals, midpoints, strict=False)):
        row = min(
            timeline_rows,
            key=lambda value: abs(float(value["tiktok_time"]) - midpoint),
        )
        item: dict[str, str] = {}

        def save(label: str, image) -> None:
            filename = f"{index:03d}_{label}.jpg"
            image.convert("RGB").save(asset_dir / filename, quality=88)
            item[label] = f"{asset_dir.name}/{filename}"

        if index < len(query_frames):
            save("query", query_frames[index])
        for label, episode_key, source_key in (
            ("expected", "gt_episode", "gt_source"),
            ("generated", "generated_episode", "generated_source"),
        ):
            episode = row.get(episode_key)
            source = row.get(source_key)
            if not episode or source is None:
                continue
            path = AnimeLibraryService.resolve_episode_path(
                episode,
                library_type=library_type,
            )
            if path is None or not path.exists():
                continue
            frames = AnimeMatcherService.extract_frames(path, [float(source)])
            if frames:
                save(label, frames[0])
        assets.append(item)
    return assets


def gpu_reset() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:
        pass


def gpu_peak() -> dict[str, float]:
    try:
        import torch

        if torch.cuda.is_available():
            return {
                "allocated_mib": torch.cuda.max_memory_allocated() / 1024**2,
                "reserved_mib": torch.cuda.max_memory_reserved() / 1024**2,
            }
    except Exception:
        pass
    return {"allocated_mib": 0.0, "reserved_mib": 0.0}


def evaluate_project(
    snapshot: GroundTruthSnapshot,
    matcher: str,
    runs: int,
    output_dir: Path,
    *,
    build_review: bool = True,
) -> tuple[dict, list[dict]]:
    project = ProjectService.load(snapshot.project_id)
    if project is None or not project.video_path:
        raise RuntimeError(f"Project unavailable: {snapshot.project_id}")
    video = Path(project.video_path)
    library = AnimeLibraryService.get_library_path(project.library_type)

    # Detection is outside the requested Match-click budget and is deliberately
    # run without library context so it cannot warm the matcher model/index.
    detected = SceneDetectorService._detect_sync(video, 16.0, 10, None, None, None)
    input_scenes = SceneList(scenes=detected)

    previous_flag = os.environ.get("ATR_MATCHER_V2")
    # The flag is inverted for rollout: v2 means the old compatibility
    # matcher, while bounded is the new default matcher.
    os.environ["ATR_MATCHER_V2"] = "1" if matcher == "v2" else "0"
    elapsed_values: list[float] = []
    latest = None
    latest_runtime: dict[str, float] = {}
    latest_gpu: dict[str, float] = {}
    try:
        for run_index in range(max(1, runs)):
            if run_index == 0:
                AnimeMatcherService.release_matching_resources(
                    reason="matching_bounded_cold_benchmark"
                )
            AnimeMatcherService.reset_runtime_stats()
            gpu_reset()
            started = time.perf_counter()
            initialized = AnimeMatcherService._init_searcher(
                library, project.library_type, project.anime_name
            )
            if not initialized:
                raise RuntimeError("anime_searcher initialization failed")
            latest = SceneAlignerService.align_scenes_sync(
                video,
                input_scenes.model_copy(deep=True),
                project.library_type,
                project.anime_name,
            )
            elapsed_values.append(time.perf_counter() - started)
            latest_runtime = AnimeMatcherService.get_runtime_stats()
            latest_gpu = gpu_peak()
    finally:
        if previous_flag is None:
            os.environ.pop("ATR_MATCHER_V2", None)
        else:
            os.environ["ATR_MATCHER_V2"] = previous_flag

    assert latest is not None
    metrics, rows = evaluate_timeline(
        snapshot.load_scenes(),
        snapshot.load_matches(),
        latest.scenes,
        latest.matches,
    )
    mismatch_items = mismatch_intervals(rows)
    duplicate_items = duplicate_ambiguity_intervals(latest.scenes, latest.matches)
    intervals = mismatch_items + duplicate_items
    metrics.update(
        {
            "project_id": snapshot.project_id,
            "matcher": matcher,
            "input_scene_count": len(input_scenes.scenes),
            "cold_seconds": elapsed_values[0],
            "warm_seconds": elapsed_values[1:],
            "warm_median_seconds": (
                statistics.median(elapsed_values[1:]) if len(elapsed_values) > 1 else None
            ),
            "phase_seconds": latest.diagnostics.phase_timings,
            "matcher_counters": latest.diagnostics.counters,
            "runtime_stats": latest_runtime,
            "peak_gpu": latest_gpu,
            "ground_truth_hashes": snapshot.hashes,
            "review_interval_count": len(intervals),
            "mismatch_interval_count": len(mismatch_items),
            "duplicate_ambiguity_count": len(duplicate_items),
        }
    )
    payload = {
        "metrics": metrics,
        "scenes": latest.scenes.model_dump(),
        "matches": latest.matches.model_dump(),
        "review_intervals": intervals,
        "timeline_rows": rows,
    }
    (output_dir / f"{snapshot.project_id}_{matcher}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    if build_review:
        review_assets = build_review_assets(
            output_dir,
            snapshot.project_id,
            video,
            project.library_type,
            intervals,
            rows,
        )
        write_review_html(
            output_dir / f"{snapshot.project_id}_{matcher}_review.html",
            snapshot.project_id,
            metrics,
            intervals,
            review_assets,
        )
    snapshot.assert_unchanged()
    return metrics, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("projects", nargs="*", default=list(DEFAULT_PROJECTS))
    parser.add_argument(
        "--matcher",
        choices=("bounded", "v2"),
        default="bounded",
        help="bounded selects the new default matcher; v2 selects the old compatibility matcher",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--skip-review-assets",
        action="store_true",
        help="skip frame extraction/HTML while retaining metrics and hash checks",
    )
    parser.add_argument(
        "--baseline-json",
        type=Path,
        help="Independent bounded-matcher baseline mapping used only for release gates",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / ".cache" / "atr-matching-v2",
    )
    args = parser.parse_args()

    snapshots = [
        GroundTruthSnapshot.capture(BACKEND / "data" / "projects" / project_id)
        for project_id in args.projects
    ]
    output_dir = ensure_safe_output_dir(args.output_dir, snapshots)
    results = []
    rows_by_project: dict[str, list[dict]] = {}
    try:
        for snapshot in snapshots:
            metrics, rows = evaluate_project(
                snapshot,
                args.matcher,
                max(1, args.runs),
                output_dir,
                build_review=not args.skip_review_assets,
            )
            results.append(metrics)
            rows_by_project[snapshot.project_id] = rows
            print(json.dumps(metrics, sort_keys=True), flush=True)
    finally:
        for snapshot in snapshots:
            snapshot.assert_unchanged()
        AnimeMatcherService.release_matching_resources(
            reason="matching_bounded_benchmark_complete"
        )
    (output_dir / f"summary_{args.matcher}.json").write_text(
        json.dumps(results, indent=2, sort_keys=True), encoding="utf-8"
    )
    calibration = calibrate_confidence_leave_one_project_out(rows_by_project)
    (output_dir / f"calibration_{args.matcher}.json").write_text(
        json.dumps(calibration, indent=2, sort_keys=True), encoding="utf-8"
    )
    exact_baselines = None
    pass_baselines = None
    if args.baseline_json is not None:
        baseline_payload = json.loads(args.baseline_json.read_text(encoding="utf-8"))
        exact_baselines = {
            project_id: float(value["exact_0_5_coverage"])
            for project_id, value in baseline_payload.items()
        }
        pass_baselines = {
            project_id: float(value["pass_1_0_coverage"])
            for project_id, value in baseline_payload.items()
        }
    acceptance = acceptance_report(
        results,
        exact_baselines=exact_baselines,
        pass_baselines=pass_baselines,
    )
    acceptance["confidence_calibration"] = calibration
    (output_dir / f"acceptance_{args.matcher}.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
