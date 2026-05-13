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
from dataclasses import dataclass
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


async def _collect_match_result(*args, **kwargs) -> MatchList:
    result: MatchList | None = None
    async for progress in AnimeMatcherService.match_scenes(*args, **kwargs):
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
) -> GeneratedResult:
    project, gt_scenes, _ = _load_required(project_id)
    video_path = Path(project.video_path)
    if not video_path.exists():
        raise RuntimeError(f"Video missing: {video_path}")

    start_clock = time.perf_counter()
    if use_ground_truth_scenes:
        scenes = gt_scenes.model_copy(deep=True)
        if max_scenes is not None:
            scenes.scenes = scenes.scenes[:max_scenes]
            scenes.renumber()
    else:
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
        if visual_merge_threshold is not None:
            scenes = await _visual_merge_scenes(
                video_path,
                scenes,
                visual_merge_threshold,
                project.library_type,
                project.anime_name,
            )

    tiny_threshold = 0.35
    scenes, _ = scenes.merge_tiny_scenes(tiny_threshold)

    library_path = AnimeLibraryService.get_library_path(project.library_type)
    first_pass = await _collect_match_result(
        video_path,
        scenes,
        library_path,
        project.library_type,
        anime_name=project.anime_name,
        pass_label="eval pass 1: " if merge_continuous else "",
    )

    final_scenes = scenes
    final_matches = first_pass
    if merge_continuous:
        index_fps = AnimeMatcherService.get_index_fps()
        pairs = SceneMergerService.detect_continuous_pairs(
            scenes,
            first_pass,
            index_fps=index_fps,
        )
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
        if chains:
            final_scenes, merged_matches, _ = SceneMergerService.merge_scenes_and_matches(
                scenes,
                first_pass,
                chains,
            )
            merged_indices = [
                i
                for i, match in enumerate(merged_matches.matches)
                if match.merged_from is not None
            ]
            pass2 = await _collect_match_result(
                video_path,
                final_scenes,
                library_path,
                project.library_type,
                anime_name=project.anime_name,
                scene_indices_to_match=merged_indices,
                existing_matches=merged_matches,
                pass_label="eval pass 2: ",
            )
            for i in merged_indices:
                pass2.matches[i].merged_from = merged_matches.matches[i].merged_from
            final_matches = pass2

    final_scenes = SceneMergerService.snap_dense_visual_boundaries(
        video_path,
        final_scenes,
    )

    elapsed = time.perf_counter() - start_clock
    return GeneratedResult(final_scenes, final_matches, elapsed)


def _load_generated(path: Path) -> GeneratedResult:
    payload = json.loads(path.read_text())
    return GeneratedResult(
        scenes=SceneList.model_validate(payload["scenes"]),
        matches=MatchList.model_validate(payload["matches"]),
        elapsed_seconds=float(payload.get("elapsed_seconds", 0.0)),
    )


def _save_generated(path: Path, generated: GeneratedResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scenes": generated.scenes.model_dump(),
                "matches": generated.matches.model_dump(),
                "elapsed_seconds": generated.elapsed_seconds,
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


def _interval_iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return overlap / union if union > 0 else 0.0


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


def _score(
    project_id: str,
    generated: GeneratedResult,
    tolerance: float,
    *,
    max_scenes: int | None = None,
) -> int:
    _, gt_scenes, gt_matches = _load_required(project_id)
    if max_scenes is not None:
        gt_scenes = gt_scenes.model_copy(deep=True)
        gt_matches = gt_matches.model_copy(deep=True)
        gt_scenes.scenes = gt_scenes.scenes[:max_scenes]
        gt_matches.matches = gt_matches.matches[:max_scenes]

    matched_gt: set[int] = set()
    correct = 0
    source_correct = 0
    candidate_hit = 0
    rows: list[str] = []

    for gen_idx, (scene, match) in enumerate(
        zip(generated.scenes.scenes, generated.matches.matches, strict=False)
    ):
        best_gt_idx = None
        best_iou = -1.0
        for gt_idx, gt_scene in enumerate(gt_scenes.scenes):
            if gt_idx in matched_gt:
                continue
            iou = _interval_iou(
                scene.start_time,
                scene.end_time,
                gt_scene.start_time,
                gt_scene.end_time,
            )
            if iou > best_iou:
                best_iou = iou
                best_gt_idx = gt_idx

        if best_gt_idx is None or best_iou <= 0.0:
            rows.append(f"extra gen#{gen_idx}: no gt overlap")
            continue

        matched_gt.add(best_gt_idx)
        gt_scene = gt_scenes.scenes[best_gt_idx]
        gt_match = gt_matches.matches[best_gt_idx]
        scene_ok = (
            abs(scene.start_time - gt_scene.start_time) <= tolerance
            and abs(scene.end_time - gt_scene.end_time) <= tolerance
        )
        source_ok = (
            match.episode == gt_match.episode
            and abs(match.start_time - gt_match.start_time) <= tolerance
            and abs(match.end_time - gt_match.end_time) <= tolerance
        )
        cand_ok = source_ok or _candidate_contains(match, gt_match, tolerance)
        if source_ok:
            source_correct += 1
        if cand_ok:
            candidate_hit += 1
        if scene_ok and source_ok:
            correct += 1
        else:
            rows.append(
                "gen#{gen} -> gt#{gt} iou={iou:.2f} scene_ok={scene_ok} "
                "source_ok={source_ok} cand_ok={cand_ok} "
                "scene=({gs:.2f},{ge:.2f}) gt_scene=({ts:.2f},{te:.2f}) "
                "src={ep} ({ms:.2f},{me:.2f}) gt_src={gep} ({gms:.2f},{gme:.2f})".format(
                    gen=gen_idx,
                    gt=best_gt_idx,
                    iou=best_iou,
                    scene_ok=scene_ok,
                    source_ok=source_ok,
                    cand_ok=cand_ok,
                    gs=scene.start_time,
                    ge=scene.end_time,
                    ts=gt_scene.start_time,
                    te=gt_scene.end_time,
                    ep=match.episode or "<none>",
                    ms=match.start_time,
                    me=match.end_time,
                    gep=gt_match.episode or "<none>",
                    gms=gt_match.start_time,
                    gme=gt_match.end_time,
                )
            )

    for gt_idx in range(len(gt_scenes.scenes)):
        if gt_idx not in matched_gt:
            gt_scene = gt_scenes.scenes[gt_idx]
            rows.append(
                f"missing gt#{gt_idx}: scene=({gt_scene.start_time:.2f},{gt_scene.end_time:.2f})"
            )

    total = len(gt_scenes.scenes)
    print(f"\nProject {project_id}")
    print(f"Generated scenes: {len(generated.scenes.scenes)} / GT scenes: {total}")
    print(f"Elapsed: {generated.elapsed_seconds:.1f}s")
    print(f"Strict scene+source accuracy: {correct}/{total} = {correct / total:.1%}")
    print(f"Source timing accuracy: {source_correct}/{total} = {source_correct / total:.1%}")
    print(f"GT source timing in candidates: {candidate_hit}/{total} = {candidate_hit / total:.1%}")
    print("Mismatches:")
    for row in rows[:80]:
        print(f"  {row}")
    if len(rows) > 80:
        print(f"  ... {len(rows) - 80} more")
    return 0 if correct / total >= 0.9 and candidate_hit / total >= 0.9 else 1


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_ids", nargs="+")
    parser.add_argument("--gt-scenes", action="store_true", help="skip fresh scene detection")
    parser.add_argument("--no-merge", action="store_true")
    parser.add_argument("--threshold", type=float, default=16.0)
    parser.add_argument("--min-scene-len", type=int, default=10)
    parser.add_argument("--tolerance", type=float, default=1.5)
    parser.add_argument("--max-scenes", type=int, default=None)
    parser.add_argument("--visual-merge-threshold", type=float, default=None)
    parser.add_argument("--load-generated-json", type=Path, default=None)
    parser.add_argument("--save-generated-json", type=Path, default=None)
    args = parser.parse_args()

    exit_code = 0
    for project_id in args.project_ids:
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
            )
            if args.save_generated_json is not None:
                _save_generated(args.save_generated_json, generated)
        exit_code |= _score(
            project_id,
            generated,
            args.tolerance,
            max_scenes=args.max_scenes,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
