#!/usr/bin/env python
"""Evaluate /matches output against curated project ground truth.

This script intentionally does not save to the ground-truth project folders.
It runs the same scene detection / matching / merge services as the API route
and compares the generated result to an existing project's scenes + matches.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models import MatchList, Scene, SceneList
from app.services import AnimeLibraryService, AnimeMatcherService, SceneMergerService
from app.services.project_service import ProjectService
from app.services.scene_detector import SceneDetectorService


@dataclass
class GeneratedResult:
    scenes: SceneList
    matches: MatchList
    elapsed_seconds: float
    phase_timings: dict[str, float] = field(default_factory=dict)
    matcher_stats: dict[str, float] = field(default_factory=dict)


@dataclass
class StrictValidationResult:
    project_id: str
    passed: bool
    generated_scene_count: int
    ground_truth_scene_count: int
    elapsed_seconds: float
    scene_exact: int = 0
    scene_loose: int = 0
    scene_failed: int = 0
    source_exact: int = 0
    source_loose: int = 0
    wrong_primary_with_candidate: int = 0
    source_failed: int = 0
    rows: list[str] = field(default_factory=list)


def _timing_bucket(
    got_start: float,
    got_end: float,
    expected_start: float,
    expected_end: float,
    *,
    exact_tolerance: float,
    loose_tolerance: float,
) -> str:
    start_delta = abs(got_start - expected_start)
    end_delta = abs(got_end - expected_end)
    if start_delta <= exact_tolerance and end_delta <= exact_tolerance:
        return "exact"
    if start_delta <= loose_tolerance and end_delta <= loose_tolerance:
        return "loose"
    return "fail"


def _load_required(project_id: str) -> tuple[object, SceneList, MatchList]:
    project = ProjectService.load(project_id)
    if project is None:
        raise RuntimeError(f"Project not found: {project_id}")
    scenes = ProjectService.load_scenes(project_id)
    if scenes is None or not scenes.scenes:
        raise RuntimeError(f"Project has no scenes: {project_id}")
    matches = ProjectService.load_matches(project_id)
    if matches is None or not matches.matches:
        raise RuntimeError(f"Project has no matches: {project_id}")
    return project, scenes, matches


def _record_phase(
    phase_timings: dict[str, float],
    name: str,
    started_at: float,
    *,
    verbose: bool,
    project_id: str,
) -> None:
    elapsed = time.perf_counter() - started_at
    phase_timings[name] = phase_timings.get(name, 0.0) + elapsed
    if verbose:
        print(f"[{project_id}] {name}: {elapsed:.1f}s", flush=True)


async def _collect_match_result(
    *args,
    verbose: bool = False,
    project_id: str = "",
    **kwargs,
) -> MatchList:
    result: MatchList | None = None
    async for progress in AnimeMatcherService.match_scenes(*args, **kwargs):
        if verbose and progress.status == "matching" and progress.current_scene:
            label = kwargs.get("pass_label") or ""
            print(
                f"[{project_id}] {label}scene "
                f"{progress.current_scene}/{progress.total_scenes}: {progress.message}",
                flush=True,
            )
        if progress.status == "error":
            raise RuntimeError(progress.error or "match_scenes failed")
        if progress.status == "complete":
            result = progress.matches
    if result is None:
        raise RuntimeError("match_scenes completed without matches")
    return result


async def _generate(
    project_id: str,
    *,
    use_ground_truth_scenes: bool,
    merge_continuous: bool,
    scene_threshold: float,
    min_scene_len: int,
    max_scenes: int | None = None,
    visual_merge_threshold: float | None = None,
    verbose: bool = False,
) -> GeneratedResult:
    project, gt_scenes, _ = _load_required(project_id)
    video_path = Path(project.video_path)
    if not video_path.exists():
        raise RuntimeError(f"Video missing: {video_path}")

    AnimeMatcherService.reset_runtime_stats()
    phase_timings: dict[str, float] = {}
    start_clock = time.perf_counter()
    if use_ground_truth_scenes:
        phase_start = time.perf_counter()
        scenes = gt_scenes.model_copy(deep=True)
        if max_scenes is not None:
            scenes.scenes = scenes.scenes[:max_scenes]
            scenes.renumber()
        _record_phase(
            phase_timings,
            "load_ground_truth_scenes",
            phase_start,
            verbose=verbose,
            project_id=project_id,
        )
    else:
        phase_start = time.perf_counter()
        scenes = SceneList(
            scenes=SceneDetectorService._detect_sync(
                video_path,
                scene_threshold,
                min_scene_len,
                AnimeLibraryService.get_library_path(project.library_type),
                project.library_type,
                project.anime_name,
            )
        )
        _record_phase(
            phase_timings,
            "scene_detection",
            phase_start,
            verbose=verbose,
            project_id=project_id,
        )
        if visual_merge_threshold is not None:
            phase_start = time.perf_counter()
            scenes = await _visual_merge_scenes(
                video_path,
                scenes,
                visual_merge_threshold,
                project.library_type,
                project.anime_name,
            )
            _record_phase(
                phase_timings,
                "diagnostic_visual_merge",
                phase_start,
                verbose=verbose,
                project_id=project_id,
            )

    tiny_threshold = 0.35
    phase_start = time.perf_counter()
    scenes, _ = scenes.merge_tiny_scenes(tiny_threshold)
    _record_phase(
        phase_timings,
        "tiny_scene_merge",
        phase_start,
        verbose=verbose,
        project_id=project_id,
    )

    library_path = AnimeLibraryService.get_library_path(project.library_type)
    phase_start = time.perf_counter()
    first_pass = await _collect_match_result(
        video_path,
        scenes,
        library_path,
        project.library_type,
        anime_name=project.anime_name,
        pass_label="eval pass 1: " if merge_continuous else "",
        verbose=verbose,
        project_id=project_id,
    )
    _record_phase(
        phase_timings,
        "match_pass_1",
        phase_start,
        verbose=verbose,
        project_id=project_id,
    )

    final_scenes = scenes
    final_matches = first_pass
    if merge_continuous:
        index_fps = AnimeMatcherService.get_index_fps()
        phase_start = time.perf_counter()
        pairs = SceneMergerService.detect_continuous_pairs(
            scenes,
            first_pass,
            index_fps=index_fps,
        )
        _record_phase(
            phase_timings,
            "continuity_pair_detection",
            phase_start,
            verbose=verbose,
            project_id=project_id,
        )
        phase_start = time.perf_counter()
        chains = (
            SceneMergerService.build_merge_chains(
                pairs,
                scenes,
                first_pass,
                index_fps=index_fps,
                video_path=video_path,
                library_path=library_path,
                library_type=project.library_type,
                anime_name=project.anime_name,
            )
            if pairs
            else []
        )
        _record_phase(
            phase_timings,
            "continuity_chain_build",
            phase_start,
            verbose=verbose,
            project_id=project_id,
        )
        if chains:
            phase_start = time.perf_counter()
            final_scenes, merged_matches, _ = SceneMergerService.merge_scenes_and_matches(
                scenes,
                first_pass,
                chains,
            )
            _record_phase(
                phase_timings,
                "merge_apply",
                phase_start,
                verbose=verbose,
                project_id=project_id,
            )
            merged_indices = [
                i
                for i, match in enumerate(merged_matches.matches)
                if match.merged_from is not None
            ]
            phase_start = time.perf_counter()
            pass2 = await _collect_match_result(
                video_path,
                final_scenes,
                library_path,
                project.library_type,
                anime_name=project.anime_name,
                scene_indices_to_match=merged_indices,
                existing_matches=merged_matches,
                pass_label="eval pass 2: ",
                verbose=verbose,
                project_id=project_id,
            )
            for i in merged_indices:
                pass2.matches[i].merged_from = merged_matches.matches[i].merged_from
            final_matches = pass2
            _record_phase(
                phase_timings,
                "match_pass_2",
                phase_start,
                verbose=verbose,
                project_id=project_id,
            )

    phase_start = time.perf_counter()
    final_scenes = SceneMergerService.snap_dense_visual_boundaries(
        video_path,
        final_scenes,
    )
    _record_phase(
        phase_timings,
        "dense_boundary_snap",
        phase_start,
        verbose=verbose,
        project_id=project_id,
    )

    elapsed = time.perf_counter() - start_clock
    return GeneratedResult(
        final_scenes,
        final_matches,
        elapsed,
        phase_timings=phase_timings,
        matcher_stats=AnimeMatcherService.get_runtime_stats(),
    )


def _load_generated(path: Path) -> GeneratedResult:
    payload = json.loads(path.read_text())
    return GeneratedResult(
        scenes=SceneList.model_validate(payload["scenes"]),
        matches=MatchList.model_validate(payload["matches"]),
        elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
        phase_timings=dict(payload.get("phase_timings", {})),
        matcher_stats=dict(payload.get("matcher_stats", {})),
    )


def _save_generated(path: Path, generated: GeneratedResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scenes": generated.scenes.model_dump(),
                "matches": generated.matches.model_dump(),
                "elapsed_seconds": generated.elapsed_seconds,
                "phase_timings": generated.phase_timings,
                "matcher_stats": generated.matcher_stats,
            },
            indent=2,
        )
    )


async def _visual_merge_scenes(
    video_path: Path,
    scenes: SceneList,
    threshold: float,
    library_type,
    anime_name: str | None,
) -> SceneList:
    """Merge visually near-identical detector boundaries for diagnostics."""
    if len(scenes.scenes) < 2:
        return scenes

    library_path = AnimeLibraryService.get_library_path(library_type)
    AnimeMatcherService._init_searcher(library_path, library_type, anime_name)
    processor = AnimeMatcherService._query_processor
    if processor is None:
        return scenes

    ranges = [(scene.start_time, scene.end_time) for scene in scenes.scenes]
    changed = True
    while changed:
        changed = False
        merged: list[tuple[float, float]] = []
        index = 0
        while index < len(ranges):
            if index < len(ranges) - 1:
                boundary = ranges[index][1]
                timestamps = [
                    max(ranges[index][0], boundary - 0.08),
                    min(ranges[index + 1][1], boundary + 0.08),
                ]
                frames = AnimeMatcherService.extract_frames(video_path, timestamps)
                if not any(frame is None for frame in frames):
                    embeddings = processor.embedder.embed_batch(
                        [frame.convert("RGB") for frame in frames]
                    )
                    similarity = float(embeddings[0] @ embeddings[1])
                    if similarity >= threshold:
                        ranges[index + 1] = (ranges[index][0], ranges[index + 1][1])
                        changed = True
                        index += 1
                        continue
            merged.append(ranges[index])
            index += 1
        ranges = merged

    result = SceneList(
        scenes=[
            Scene(index=index, start_time=start, end_time=end)
            for index, (start, end) in enumerate(ranges)
        ]
    )
    return result


def _candidate_contains(match, gt_match, tolerance: float) -> bool:
    if not gt_match.episode:
        return False
    for alt in match.alternatives:
        if (
            alt.episode == gt_match.episode
            and abs(alt.start_time - gt_match.start_time) <= tolerance
            and abs(alt.end_time - gt_match.end_time) <= tolerance
        ):
            return True
    for start in match.start_candidates:
        if start.episode != gt_match.episode:
            continue
        for end in match.end_candidates:
            if end.episode != gt_match.episode:
                continue
            if (
                abs(start.timestamp - gt_match.start_time) <= tolerance
                and abs(end.timestamp - gt_match.end_time) <= tolerance
            ):
                return True
    return False


def _validate_strict(
    project_id: str,
    generated: GeneratedResult,
    *,
    max_scenes: int | None = None,
    exact_tolerance: float = 0.3,
    loose_tolerance: float = 1.0,
    max_scene_loose: int = 3,
    max_source_loose: int = 3,
    max_wrong_primary: int = 2,
) -> StrictValidationResult:
    _, gt_scenes, gt_matches = _load_required(project_id)
    if max_scenes is not None:
        gt_scenes = gt_scenes.model_copy(deep=True)
        gt_matches = gt_matches.model_copy(deep=True)
        gt_scenes.scenes = gt_scenes.scenes[:max_scenes]
        gt_matches.matches = gt_matches.matches[:max_scenes]

    result = StrictValidationResult(
        project_id=project_id,
        passed=True,
        generated_scene_count=len(generated.scenes.scenes),
        ground_truth_scene_count=len(gt_scenes.scenes),
        elapsed_seconds=generated.elapsed_seconds,
    )

    if result.generated_scene_count != result.ground_truth_scene_count:
        result.passed = False
        result.rows.append(
            "scene count mismatch: generated={generated} gt={gt}".format(
                generated=result.generated_scene_count,
                gt=result.ground_truth_scene_count,
            )
        )

    if len(generated.matches.matches) != len(generated.scenes.scenes):
        result.passed = False
        result.rows.append(
            "match count mismatch: matches={matches} generated_scenes={scenes}".format(
                matches=len(generated.matches.matches),
                scenes=len(generated.scenes.scenes),
            )
        )

    compare_count = min(
        len(generated.scenes.scenes),
        len(generated.matches.matches),
        len(gt_scenes.scenes),
        len(gt_matches.matches),
    )
    for index in range(compare_count):
        scene = generated.scenes.scenes[index]
        match = generated.matches.matches[index]
        gt_scene = gt_scenes.scenes[index]
        gt_match = gt_matches.matches[index]

        scene_bucket = _timing_bucket(
            scene.start_time,
            scene.end_time,
            gt_scene.start_time,
            gt_scene.end_time,
            exact_tolerance=exact_tolerance,
            loose_tolerance=loose_tolerance,
        )
        if scene_bucket == "exact":
            result.scene_exact += 1
        elif scene_bucket == "loose":
            result.scene_loose += 1
            result.rows.append(
                "scene#{idx} loose timing: generated=({gs:.2f},{ge:.2f}) "
                "gt=({ts:.2f},{te:.2f})".format(
                    idx=index,
                    gs=scene.start_time,
                    ge=scene.end_time,
                    ts=gt_scene.start_time,
                    te=gt_scene.end_time,
                )
            )
        else:
            result.scene_failed += 1
            result.passed = False
            result.rows.append(
                "scene#{idx} failed timing: generated=({gs:.2f},{ge:.2f}) "
                "gt=({ts:.2f},{te:.2f}) delta=({ds:.2f},{de:.2f})".format(
                    idx=index,
                    gs=scene.start_time,
                    ge=scene.end_time,
                    ts=gt_scene.start_time,
                    te=gt_scene.end_time,
                    ds=scene.start_time - gt_scene.start_time,
                    de=scene.end_time - gt_scene.end_time,
                )
            )

        if match.episode == gt_match.episode:
            source_bucket = _timing_bucket(
                match.start_time,
                match.end_time,
                gt_match.start_time,
                gt_match.end_time,
                exact_tolerance=exact_tolerance,
                loose_tolerance=loose_tolerance,
            )
            if source_bucket == "exact":
                result.source_exact += 1
                continue
            if source_bucket == "loose":
                result.source_loose += 1
                result.rows.append(
                    "source#{idx} loose timing: episode={ep} generated=({ms:.2f},{me:.2f}) "
                    "gt=({gms:.2f},{gme:.2f})".format(
                        idx=index,
                        ep=match.episode or "<none>",
                        ms=match.start_time,
                        me=match.end_time,
                        gms=gt_match.start_time,
                        gme=gt_match.end_time,
                    )
                )
                continue

        candidate_ok = _candidate_contains(match, gt_match, loose_tolerance)
        if candidate_ok:
            result.wrong_primary_with_candidate += 1
            result.rows.append(
                "source#{idx} wrong primary but candidate exposed: "
                "primary={ep} ({ms:.2f},{me:.2f}) gt={gep} ({gms:.2f},{gme:.2f})".format(
                    idx=index,
                    ep=match.episode or "<none>",
                    ms=match.start_time,
                    me=match.end_time,
                    gep=gt_match.episode or "<none>",
                    gms=gt_match.start_time,
                    gme=gt_match.end_time,
                )
            )
        else:
            result.source_failed += 1
            result.passed = False
            result.rows.append(
                "source#{idx} failed primary and missing candidate: "
                "primary={ep} ({ms:.2f},{me:.2f}) gt={gep} ({gms:.2f},{gme:.2f})".format(
                    idx=index,
                    ep=match.episode or "<none>",
                    ms=match.start_time,
                    me=match.end_time,
                    gep=gt_match.episode or "<none>",
                    gms=gt_match.start_time,
                    gme=gt_match.end_time,
                )
            )

    if result.scene_loose > max_scene_loose:
        result.passed = False
        result.rows.append(
            f"too many loose scene timings: {result.scene_loose} > {max_scene_loose}"
        )
    if result.source_loose > max_source_loose:
        result.passed = False
        result.rows.append(
            f"too many loose source timings: {result.source_loose} > {max_source_loose}"
        )
    if result.wrong_primary_with_candidate > max_wrong_primary:
        result.passed = False
        result.rows.append(
            "too many wrong primaries even with exposed candidates: "
            f"{result.wrong_primary_with_candidate} > {max_wrong_primary}"
        )

    return result


def _print_strict_result(result: StrictValidationResult) -> None:
    total = result.ground_truth_scene_count
    print(f"\nProject {result.project_id}")
    print(
        "Generated scenes: "
        f"{result.generated_scene_count} / GT scenes: {result.ground_truth_scene_count}"
    )
    print(f"Elapsed: {result.elapsed_seconds:.1f}s")
    if total:
        print(
            "Scene timing: "
            f"exact={result.scene_exact}/{total}, "
            f"loose={result.scene_loose}, failed={result.scene_failed}"
        )
        print(
            "Source timing: "
            f"exact={result.source_exact}/{total}, "
            f"loose={result.source_loose}, "
            f"wrong_primary_with_candidate={result.wrong_primary_with_candidate}, "
            f"failed={result.source_failed}"
        )
    print(f"Strict result: {'PASS' if result.passed else 'FAIL'}")
    print("Details:")
    for row in result.rows[:120]:
        print(f"  {row}")
    if len(result.rows) > 120:
        print(f"  ... {len(result.rows) - 120} more")
    if not result.rows:
        print("  none")


def _print_profile(generated: GeneratedResult) -> None:
    if generated.phase_timings:
        print("Phase timings:")
        for name, seconds in sorted(
            generated.phase_timings.items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            print(f"  {name}: {seconds:.2f}s")
    if generated.matcher_stats:
        print("Matcher profile:")
        for name, value in sorted(generated.matcher_stats.items()):
            if name.endswith("_seconds"):
                print(f"  {name}: {value:.2f}s")
            else:
                print(f"  {name}: {value:.0f}")


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_ids", nargs="+")
    parser.add_argument("--gt-scenes", action="store_true", help="skip fresh scene detection")
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument("--threshold", type=float, default=16.0)
    parser.add_argument("--min-scene-len", type=int, default=10)
    parser.add_argument("--exact-tolerance", type=float, default=0.3)
    parser.add_argument("--loose-tolerance", type=float, default=1.0)
    parser.add_argument("--max-scene-loose", type=int, default=3)
    parser.add_argument("--max-source-loose", type=int, default=3)
    parser.add_argument("--max-wrong-primary", type=int, default=2)
    parser.add_argument("--max-scenes", type=int, default=None)
    parser.add_argument("--visual-merge-threshold", type=float, default=None)
    parser.add_argument("--load-generated-json", type=Path, default=None)
    parser.add_argument("--save-generated-json", type=Path, default=None)
    parser.add_argument("--quiet-profile", action="store_true")
    args = parser.parse_args()

    exit_code = 0
    for project_id in args.project_ids:
        print(f"\n[{project_id}] starting fresh evaluation", flush=True)
        if args.load_generated_json is not None:
            generated = _load_generated(args.load_generated_json)
        else:
            generated = await _generate(
                project_id,
                use_ground_truth_scenes=args.gt_scenes,
                merge_continuous=not args.no_merge,
                scene_threshold=args.threshold,
                min_scene_len=args.min_scene_len,
                max_scenes=args.max_scenes,
                visual_merge_threshold=args.visual_merge_threshold,
                verbose=not args.quiet_profile,
            )
            if args.save_generated_json is not None:
                _save_generated(args.save_generated_json, generated)
        result = _validate_strict(
            project_id,
            generated,
            max_scenes=args.max_scenes,
            exact_tolerance=args.exact_tolerance,
            loose_tolerance=args.loose_tolerance,
            max_scene_loose=args.max_scene_loose,
            max_source_loose=args.max_source_loose,
            max_wrong_primary=args.max_wrong_primary,
        )
        _print_strict_result(result)
        if not args.quiet_profile:
            _print_profile(generated)
        exit_code |= 0 if result.passed else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
