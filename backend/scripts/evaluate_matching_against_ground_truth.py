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
from app.models.match import SceneMatch
from app.services import (
    AnimeLibraryService,
    AnimeMatcherService,
    SceneAlignerService,
    SceneMergerService,
)
from app.services.project_service import ProjectService
from app.services.scene_detector import SceneDetectorService


@dataclass
class GeneratedResult:
    scenes: SceneList
    matches: MatchList
    elapsed_seconds: float
    phase_timings: dict[str, float] = field(default_factory=dict)
    matcher_stats: dict[str, float] = field(default_factory=dict)
    aligner_debug: dict[str, object] = field(default_factory=dict)


@dataclass
class ReviewEntry:
    """One doubtful scene for the §8 owner-review HTML."""

    gt_scene_index: int
    axis: str  # "scene" | "source"
    reason: str  # bucket / equivalence / waiver-candidate label
    tiktok_interval: tuple[float, float]
    generated_episode: str
    generated_interval: tuple[float, float]
    gt_episode: str
    gt_interval: tuple[float, float]
    numbers: str
    doubt_reasons: list[str] = field(default_factory=list)


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
    waived: int = 0  # owner-approved §8 waivers counted as exact
    ceiling_report: bool = False  # PASS built on >3 waivers is not a PASS
    rows: list[str] = field(default_factory=list)
    review_entries: list[ReviewEntry] = field(default_factory=list)


WAIVERS_PATH = BACKEND_ROOT / "data" / "eval_waivers.json"


def _load_waivers(project_id: str) -> dict[tuple[int, str], dict]:
    """Owner verdicts keyed by (gt_scene_index, axis); §8 protocol."""
    if not WAIVERS_PATH.exists():
        return {}
    try:
        entries = json.loads(WAIVERS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[tuple[int, str], dict] = {}
    for entry in entries:
        if entry.get("project_id") != project_id:
            continue
        result[(int(entry["gt_scene_index"]), str(entry["axis"]))] = entry
    return result


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
    matcher: str = "legacy",
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
    if matcher == "aligner":
        align_diagnostics = None
        async for progress in SceneAlignerService.align_scenes_progress(
            video_path,
            scenes,
            library_path,
            project.library_type,
            anime_name=project.anime_name,
        ):
            if verbose and progress.status == "matching":
                print(f"[{project_id}] aligner: {progress.message}", flush=True)
            if progress.status == "error":
                raise RuntimeError(progress.error or "aligner failed")
            if progress.status == "complete":
                align_diagnostics = SceneAlignerService.get_last_diagnostics()
        align_result = SceneAlignerService.get_last_result()
        if align_result is None:
            raise RuntimeError("aligner completed without results")
        final_scenes = align_result.scenes
        final_matches = align_result.matches
        _record_phase(
            phase_timings,
            "aligner",
            phase_start,
            verbose=verbose,
            project_id=project_id,
        )
        matcher_stats = AnimeMatcherService.get_runtime_stats()
        matcher_stats.update(align_diagnostics.stats() if align_diagnostics else {})
        recalled, recall_total = _stage3_evidence_recall(project_id)
        matcher_stats["aligner_stage3_evidence_recall"] = float(recalled)
        matcher_stats["aligner_stage3_evidence_total"] = float(recall_total)
        aligner_debug = (
            {
                "decoded_candidates": align_diagnostics.decoded_candidates,
                "decoded_fragments": align_diagnostics.decoded_fragments,
                "stage4_attempts": align_diagnostics.stage4_attempts,
                "stage4_groups": align_diagnostics.stage4_groups,
            }
            if align_diagnostics
            else {}
        )
        elapsed = time.perf_counter() - start_clock
        return GeneratedResult(
            final_scenes,
            final_matches,
            elapsed,
            phase_timings=phase_timings,
            matcher_stats=matcher_stats,
            aligner_debug=aligner_debug,
        )

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
        aligner_debug=dict(payload.get("aligner_debug", {})),
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
                "aligner_debug": generated.aligner_debug,
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


def _lookalike_equivalent(
    match,
    gt_match,
    *,
    series: str | None = None,
    require_same_episode: bool = True,
    min_cos: float = 0.90,
) -> bool:
    """Owner-approved visual equivalence: the generated source interval and
    the GT interval show the same content for the same duration (static or
    repeated footage), so the rendered clip is identical. Verified with the
    library's own index embeddings sampled along both intervals."""
    from app.services.scene_aligner import SceneAlignerService

    if not gt_match.episode or not match.episode:
        return False
    if require_same_episode and match.episode != gt_match.episode:
        return False
    dur_gen = match.end_time - match.start_time
    dur_gt = gt_match.end_time - gt_match.start_time
    if dur_gen <= 0 or dur_gt <= 0:
        return False
    if abs(dur_gen - dur_gt) > max(0.35, 0.25 * dur_gt):
        # duration only matters visually when the content moves: a still
        # frame renders identically at any played length
        def interval_static(episode: str, start: float, dur: float) -> bool:
            va = SceneAlignerService._index_embedding_at(series, episode, start + 0.1 * dur)
            vb = SceneAlignerService._index_embedding_at(series, episode, start + 0.9 * dur)
            return va is not None and vb is not None and float(va @ vb) >= 0.92

        if not (
            interval_static(match.episode, match.start_time, dur_gen)
            and interval_static(gt_match.episode, gt_match.start_time, dur_gt)
            and abs(dur_gen - dur_gt) <= max(1.5, 0.6 * dur_gt)
        ):
            return False
    for frac in (0.1, 0.5, 0.9):
        v_gen = SceneAlignerService._index_embedding_at(
            series, match.episode, match.start_time + frac * dur_gen
        )
        v_gt = SceneAlignerService._index_embedding_at(
            series, gt_match.episode, gt_match.start_time + frac * dur_gt
        )
        if v_gen is None or v_gt is None:
            return False
        if float(v_gen @ v_gt) < min_cos:
            return False
    return True


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


def _stage3_evidence_recall(project_id: str, tolerance: float = 0.5) -> tuple[int, int]:
    """Count GT scenes whose source interval is present in aligner hypotheses."""
    _, gt_scenes, gt_matches = _load_required(project_id)
    segments = SceneAlignerService.get_last_diagnostics().segments
    if not segments:
        return 0, len(gt_scenes.scenes)

    recalled = 0
    for gt_scene, gt_match in zip(gt_scenes.scenes, gt_matches.matches, strict=False):
        center = (gt_scene.start_time + gt_scene.end_time) / 2.0
        for segment in segments:
            if segment.episode != gt_match.episode:
                continue
            if not (segment.tiktok_start - tolerance <= center <= segment.tiktok_end + tolerance):
                continue
            start = segment.source_at(gt_scene.start_time)
            end = segment.source_at(gt_scene.end_time)
            if (
                abs(start - gt_match.start_time) <= tolerance
                and abs(end - gt_match.end_time) <= tolerance
            ):
                recalled += 1
                break
    return recalled, len(gt_scenes.scenes)


def _fold_generated(
    generated_scenes: list[Scene],
    gt_scenes: list[Scene],
    tolerance: float,
) -> list[list[int]]:
    """Assign each GT scene the run of generated scenes covering it.

    Anchor-based: generated end boundaries matching GT end boundaries within
    tolerance re-synchronize the walk, so a single missing/misplaced cut
    costs its own region instead of cascading through the whole comparison.
    """
    n = len(generated_scenes)
    m = len(gt_scenes)
    anchors: list[tuple[int, int]] = []
    gi = 0
    for ti in range(m):
        te = gt_scenes[ti].end_time
        best: tuple[int, float] | None = None
        for gj in range(gi, n):
            delta = abs(generated_scenes[gj].end_time - te)
            if delta <= tolerance and (best is None or delta < best[1]):
                best = (gj, delta)
            if generated_scenes[gj].end_time > te + tolerance:
                break
        if best is not None:
            anchors.append((best[0], ti))
            gi = best[0] + 1

    if not anchors or anchors[-1] != (n - 1, m - 1):
        anchors.append((n - 1, m - 1))

    folds: list[list[int]] = [[] for _ in range(m)]
    prev_gen = -1
    prev_gt = -1
    for gen_idx, gt_idx in anchors:
        if gen_idx <= prev_gen or gt_idx <= prev_gt:
            continue
        gen_range = list(range(prev_gen + 1, gen_idx + 1))
        gt_range = list(range(prev_gt + 1, gt_idx + 1))
        if len(gt_range) == 1:
            folds[gt_range[0]] = gen_range
        else:
            for g in gen_range:
                mid = (
                    generated_scenes[g].start_time + generated_scenes[g].end_time
                ) / 2.0
                target = next(
                    (
                        t
                        for t in gt_range
                        if gt_scenes[t].start_time - 1e-6
                        <= mid
                        < gt_scenes[t].end_time + 1e-6
                    ),
                    gt_range[-1],
                )
                folds[target].append(g)
        prev_gen, prev_gt = gen_idx, gt_idx
    return folds


def _chained_pieces(
    matches: list[SceneMatch],
    series: str | None,
    gap_tolerance: float = 0.75,
) -> bool:
    """True when consecutive generated matches play the source continuously
    (same episode, negligible skip): cutting such a run renders identically
    to one clip, so it is visually equivalent to a GT merge (owner-approved
    equivalence rule, 2026-07-06). A piece sitting on a lookalike duplicate
    of the continuation position also renders identically and chains."""
    from app.services.scene_aligner import SceneAlignerService

    for left, right in zip(matches, matches[1:]):
        if left.was_no_match or right.was_no_match:
            return False
        if left.episode != right.episode:
            return False
        if abs(right.start_time - left.end_time) <= gap_tolerance:
            continue
        # lookalike escape: content at the piece's actual position matches
        # the content the continuation would have shown
        duration = right.end_time - right.start_time
        if duration <= 0:
            return False
        sims = []
        for frac in (0.15, 0.5, 0.85):
            v_actual = SceneAlignerService._index_embedding_at(
                series, right.episode, right.start_time + frac * duration
            )
            v_expected = SceneAlignerService._index_embedding_at(
                series, left.episode, left.end_time + frac * duration
            )
            if v_actual is not None and v_expected is not None:
                sims.append(float(v_actual @ v_expected))
        if len(sims) < 2 or sorted(sims)[len(sims) // 2] < 0.87:
            return False
    return True


def _merged_match_view(matches: list[SceneMatch]) -> SceneMatch:
    first, last = matches[0], matches[-1]
    alternatives = []
    for m in matches:
        alternatives.extend(m.alternatives)
    return SceneMatch(
        scene_index=first.scene_index,
        episode=first.episode,
        start_time=first.start_time,
        end_time=last.end_time,
        confidence=min(m.confidence for m in matches),
        speed_ratio=first.speed_ratio,
        was_no_match=any(m.was_no_match for m in matches),
        alternatives=alternatives,
        start_candidates=first.start_candidates,
        middle_candidates=first.middle_candidates,
        end_candidates=last.end_candidates,
    )


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
    project, gt_scenes, gt_matches = _load_required(project_id)
    series_name = getattr(project, "anime_name", None)
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
    waivers = _load_waivers(project_id)
    waived_indices: set[int] = set()

    def waived(index: int, axis: str, current=None) -> bool:
        entry = waivers.get((index, axis))
        if entry is None or entry.get("verdict") != "pass":
            return False
        reviewed = entry.get("generated")
        if current is not None and reviewed and len(reviewed) == 2:
            # a waiver certifies the SPECIFIC reviewed interval; once the
            # algorithm moves it beyond microadaptation range the owner must
            # re-validate (owner protocol 2026-07-10)
            if (
                abs(current[0] - float(reviewed[0])) > 0.35
                or abs(current[1] - float(reviewed[1])) > 0.35
            ):
                result.rows.append(
                    "{axis}#{idx} waiver STALE (reviewed {r0:.2f},{r1:.2f} vs "
                    "current {c0:.2f},{c1:.2f}) - needs re-review".format(
                        axis=axis, idx=index, r0=float(reviewed[0]),
                        r1=float(reviewed[1]), c0=current[0], c1=current[1],
                    )
                )
                return False
        result.waived += 1
        waived_indices.add(index)
        result.rows.append(
            "{axis}#{idx} owner-waived (§8): {note}".format(
                axis=axis, idx=index, note=entry.get("note", "")
            )
        )
        return True

    def add_review(
        index: int,
        axis: str,
        reason: str,
        scene: Scene | None,
        match,
        gt_scene: Scene,
        gt_match,
        numbers: str,
    ) -> None:
        result.review_entries.append(
            ReviewEntry(
                gt_scene_index=index,
                axis=axis,
                reason=reason,
                tiktok_interval=(
                    (scene.start_time, scene.end_time)
                    if scene is not None
                    else (gt_scene.start_time, gt_scene.end_time)
                ),
                generated_episode=(match.episode if match is not None else ""),
                generated_interval=(
                    (match.start_time, match.end_time)
                    if match is not None
                    else (0.0, 0.0)
                ),
                gt_episode=gt_match.episode or "",
                gt_interval=(gt_match.start_time, gt_match.end_time),
                numbers=numbers,
                doubt_reasons=list(getattr(match, "doubt_reasons", []) or []),
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
        return result

    folds = _fold_generated(
        generated.scenes.scenes, gt_scenes.scenes, exact_tolerance
    )
    folded_groups = sum(1 for g in folds if len(g) > 1)
    if result.generated_scene_count != result.ground_truth_scene_count:
        result.rows.append(
            "scene count differs: generated={generated} gt={gt} "
            "(equivalence folding: {folded} groups)".format(
                generated=result.generated_scene_count,
                gt=result.ground_truth_scene_count,
                folded=folded_groups,
            )
        )

    for index in range(len(gt_scenes.scenes)):
        group = folds[index] if index < len(folds) else []
        gt_scene = gt_scenes.scenes[index]
        gt_match = gt_matches.matches[index]
        if not group:
            if waived(index, "scene"):
                result.scene_exact += 1
            else:
                result.scene_failed += 1
                result.passed = False
                result.rows.append(f"scene#{index} has no generated coverage")
                add_review(
                    index, "scene", "no_coverage", None, None, gt_scene, gt_match, ""
                )
            if waived(index, "source"):
                result.source_exact += 1
            else:
                result.source_failed += 1
                result.passed = False
            continue
        group_scenes = [generated.scenes.scenes[i] for i in group]
        group_matches = [generated.matches.matches[i] for i in group]
        if len(group) > 1:
            if not _chained_pieces(group_matches, series_name) and not waived(
                index, "scene"
            ):
                result.scene_failed += 1
                result.source_failed += 1
                result.passed = False
                result.rows.append(
                    "scene#{idx} folded {n} generated scenes but pieces do "
                    "not chain continuously".format(idx=index, n=len(group))
                )
                add_review(
                    index,
                    "scene",
                    "fold_no_chain",
                    Scene(
                        index=index,
                        start_time=group_scenes[0].start_time,
                        end_time=group_scenes[-1].end_time,
                    ),
                    _merged_match_view(group_matches),
                    gt_scene,
                    gt_match,
                    f"{len(group)} pieces",
                )
                continue
            result.rows.append(
                "scene#{idx} equivalence-folded from {n} generated scenes".format(
                    idx=index, n=len(group)
                )
            )
        scene = Scene(
            index=index,
            start_time=group_scenes[0].start_time,
            end_time=group_scenes[-1].end_time,
        )
        match = (
            group_matches[0]
            if len(group_matches) == 1
            else _merged_match_view(group_matches)
        )

        scene_bucket = _timing_bucket(
            scene.start_time,
            scene.end_time,
            gt_scene.start_time,
            gt_scene.end_time,
            exact_tolerance=exact_tolerance,
            loose_tolerance=loose_tolerance,
        )
        scene_numbers = (
            "generated=({gs:.2f},{ge:.2f}) gt=({ts:.2f},{te:.2f})".format(
                gs=scene.start_time,
                ge=scene.end_time,
                ts=gt_scene.start_time,
                te=gt_scene.end_time,
            )
        )
        if scene_bucket == "exact":
            result.scene_exact += 1
        elif waived(index, "scene", (match.start_time, match.end_time)):
            result.scene_exact += 1
        elif scene_bucket == "loose":
            result.scene_loose += 1
            result.rows.append(f"scene#{index} loose timing: {scene_numbers}")
            add_review(
                index, "scene", "scene_loose", scene, match, gt_scene, gt_match,
                scene_numbers,
            )
        else:
            result.scene_failed += 1
            result.passed = False
            result.rows.append(
                "scene#{idx} failed timing: {numbers} delta=({ds:.2f},{de:.2f})".format(
                    idx=index,
                    numbers=scene_numbers,
                    ds=scene.start_time - gt_scene.start_time,
                    de=scene.end_time - gt_scene.end_time,
                )
            )
            add_review(
                index, "scene", "scene_failed", scene, match, gt_scene, gt_match,
                scene_numbers,
            )

        source_numbers = (
            "primary={ep} ({ms:.2f},{me:.2f}) gt={gep} ({gms:.2f},{gme:.2f})".format(
                ep=match.episode or "<none>",
                ms=match.start_time,
                me=match.end_time,
                gep=gt_match.episode or "<none>",
                gms=gt_match.start_time,
                gme=gt_match.end_time,
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
            if source_bucket != "exact" and _lookalike_equivalent(
                match, gt_match, series=series_name
            ):
                # owner-approved visual equivalence (2026-07-06): the chosen
                # interval renders identically to the GT interval (indexed
                # content matches along the whole interval, same duration)
                result.source_exact += 1
                result.rows.append(
                    f"source#{index} lookalike-equivalent: {source_numbers}"
                )
                add_review(
                    index, "source", "equivalence_accepted", scene, match,
                    gt_scene, gt_match, source_numbers,
                )
                continue
            if waived(index, "source", (match.start_time, match.end_time)):
                result.source_exact += 1
                continue
            if source_bucket == "loose":
                result.source_loose += 1
                result.rows.append(
                    f"source#{index} loose timing: {source_numbers}"
                )
                add_review(
                    index, "source", "source_loose", scene, match,
                    gt_scene, gt_match, source_numbers,
                )
                continue
        elif _lookalike_equivalent(
            match, gt_match, series=series_name, require_same_episode=False
        ):
            result.source_exact += 1
            result.rows.append(
                f"source#{index} lookalike-equivalent (cross-episode duplicate)"
            )
            add_review(
                index, "source", "equivalence_accepted_cross_episode", scene,
                match, gt_scene, gt_match, source_numbers,
            )
            continue

        if waived(index, "source", (match.start_time, match.end_time)):
            result.source_exact += 1
            continue
        candidate_ok = _candidate_contains(match, gt_match, loose_tolerance)
        if candidate_ok:
            result.wrong_primary_with_candidate += 1
            result.rows.append(
                f"source#{index} wrong primary but candidate exposed: {source_numbers}"
            )
            add_review(
                index, "source", "wrong_primary", scene, match, gt_scene,
                gt_match, source_numbers,
            )
        else:
            result.source_failed += 1
            result.passed = False
            result.rows.append(
                f"source#{index} failed primary and missing candidate: {source_numbers}"
            )
            add_review(
                index, "source", "source_failed", scene, match, gt_scene,
                gt_match, source_numbers,
            )

    if len(waived_indices) > 3:
        # §8: a PASS built on more than 3 waivers/project is a ceiling
        # report, not a PASS
        result.ceiling_report = True
        result.passed = False
        result.rows.append(
            f"waiver ceiling exceeded: {len(waived_indices)} scenes waived > 3"
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
        if result.waived:
            print(f"Owner waivers applied (§8): {result.waived}")
    verdict = "PASS" if result.passed else "FAIL"
    if result.ceiling_report:
        verdict = "CEILING-REPORT (waivers > 3)"
    print(f"Strict result: {verdict}")
    print("Details:")
    for row in result.rows[:120]:
        print(f"  {row}")
    if len(result.rows) > 120:
        print(f"  ... {len(result.rows) - 120} more")
    if not result.rows:
        print("  none")


def _thumb_data_uri(image) -> str:
    import base64
    import io

    if image is None:
        return ""
    thumb = image.copy()
    thumb.thumbnail((214, 120))
    buffer = io.BytesIO()
    thumb.convert("RGB").save(buffer, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _render_review_html(
    project_id: str,
    result: StrictValidationResult,
    out_path: Path,
) -> None:
    """§8 owner-review page: one self-contained HTML per project with
    side-by-side frame strips (TikTok | generated source | GT source) for
    every doubtful scene, embedded as data URIs."""
    project, _, _ = _load_required(project_id)
    video_path = Path(project.video_path)
    fracs = (0.02, 0.35, 0.65, 0.98)

    # batch all decodes: one pass over the TikTok, one per episode
    tiktok_targets: list[float] = []
    episode_targets: dict[str, list[float]] = {}
    for entry in result.review_entries:
        s, e = entry.tiktok_interval
        tiktok_targets.extend(s + (e - s) * f for f in fracs)
        for episode, (s0, e0) in (
            (entry.generated_episode, entry.generated_interval),
            (entry.gt_episode, entry.gt_interval),
        ):
            if episode:
                episode_targets.setdefault(episode, []).extend(
                    s0 + (e0 - s0) * f for f in fracs
                )
    tiktok_frames = AnimeMatcherService.extract_frames(video_path, tiktok_targets)
    episode_frames: dict[str, list] = {}
    for episode, targets in episode_targets.items():
        episode_path = AnimeLibraryService.resolve_episode_path(
            episode, library_type=project.library_type
        )
        if episode_path is None or not episode_path.exists():
            episode_frames[episode] = [None] * len(targets)
            continue
        episode_frames[episode] = AnimeMatcherService.extract_frames(
            episode_path, targets
        )
    episode_cursor: dict[str, int] = {episode: 0 for episode in episode_targets}

    def take_episode_strip(episode: str) -> list:
        if not episode:
            return [None] * len(fracs)
        cursor = episode_cursor[episode]
        episode_cursor[episode] = cursor + len(fracs)
        return episode_frames[episode][cursor : cursor + len(fracs)]

    sections: list[str] = []
    for k, entry in enumerate(result.review_entries):
        tiktok_strip = tiktok_frames[k * len(fracs) : (k + 1) * len(fracs)]
        generated_strip = take_episode_strip(entry.generated_episode)
        gt_strip = take_episode_strip(entry.gt_episode)

        def strip_html(frames) -> str:
            return "".join(
                f'<img src="{_thumb_data_uri(f)}" loading="lazy">' for f in frames
            )

        doubt = (
            " | aligner doubts: " + ", ".join(entry.doubt_reasons)
            if entry.doubt_reasons
            else ""
        )
        sections.append(
            """
<div class="entry">
<h3>GT scene #{idx} — {axis} — {reason}</h3>
<p>{numbers}{doubt}</p>
<table>
<tr><th>TikTok {ti0:.2f}-{ti1:.2f}</th><td>{tiktok}</td></tr>
<tr><th>Generated {gep} {gi0:.2f}-{gi1:.2f}</th><td>{generated}</td></tr>
<tr><th>GT {tep} {ki0:.2f}-{ki1:.2f}</th><td>{gt}</td></tr>
</table>
<p class="verdict">waiver JSON: <code>{{"project_id": "{pid}", "gt_scene_index": {idx},
 "axis": "{axis}", "generated": [{gi0:.2f}, {gi1:.2f}], "verdict": "pass|fail",
 "note": "", "date": "2026-07-10"}}</code></p>
</div>""".format(
                idx=entry.gt_scene_index,
                axis=entry.axis,
                reason=entry.reason,
                numbers=entry.numbers,
                doubt=doubt,
                ti0=entry.tiktok_interval[0],
                ti1=entry.tiktok_interval[1],
                tiktok=strip_html(tiktok_strip),
                gep=entry.generated_episode or "<none>",
                gi0=entry.generated_interval[0],
                gi1=entry.generated_interval[1],
                generated=strip_html(generated_strip),
                tep=entry.gt_episode or "<none>",
                ki0=entry.gt_interval[0],
                ki1=entry.gt_interval[1],
                gt=strip_html(gt_strip),
                pid=project_id,
            )
        )

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Review {pid}</title>
<style>
body {{ font-family: sans-serif; background: #111; color: #eee; margin: 2em; }}
.entry {{ border: 1px solid #444; padding: 1em; margin-bottom: 2em; }}
img {{ margin: 2px; max-height: 120px; }}
th {{ text-align: left; padding-right: 1em; font-weight: normal; color: #9cf; white-space: nowrap; }}
h3 {{ margin: 0 0 .3em; }}
.verdict code {{ color: #fc9; font-size: .85em; }}
</style></head><body>
<h1>{pid} — {n} doubtful scenes ({date})</h1>
{body}
</body></html>""".format(
        pid=project_id,
        n=len(result.review_entries),
        date=time.strftime("%Y-%m-%d %H:%M"),
        body="\n".join(sections),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"Review HTML written: {out_path} ({len(result.review_entries)} entries)")


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
    parser.add_argument("--matcher", choices=("legacy", "aligner"), default="legacy")
    parser.add_argument("--load-generated-json", type=Path, default=None)
    parser.add_argument("--save-generated-json", type=Path, default=None)
    parser.add_argument("--review", type=Path, default=None,
                        help="write §8 owner-review HTML (per-project suffix added)")
    parser.add_argument("--quiet-profile", action="store_true")
    args = parser.parse_args()

    exit_code = 0
    for project_id in args.project_ids:
        print(f"\n[{project_id}] starting fresh evaluation", flush=True)
        if args.load_generated_json is not None:
            generated = _load_generated(args.load_generated_json)
            project, _, _ = _load_required(project_id)
            AnimeMatcherService._init_searcher(
                AnimeLibraryService.get_library_path(project.library_type),
                project.library_type,
                project.anime_name,
            )
        else:
            generated = await _generate(
                project_id,
                use_ground_truth_scenes=args.gt_scenes,
                merge_continuous=not args.no_merge,
                scene_threshold=args.threshold,
                min_scene_len=args.min_scene_len,
                max_scenes=args.max_scenes,
                visual_merge_threshold=args.visual_merge_threshold,
                matcher=args.matcher,
                verbose=not args.quiet_profile,
            )
            if args.save_generated_json is not None:
                out = args.save_generated_json
                if len(args.project_ids) > 1:
                    out = out.with_name(f"{out.stem}_{project_id}{out.suffix}")
                _save_generated(out, generated)
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
        if args.review is not None and result.review_entries:
            review_out = args.review
            if len(args.project_ids) > 1:
                review_out = review_out.with_name(
                    f"{review_out.stem}_{project_id}{review_out.suffix}"
                )
            _render_review_html(project_id, result, review_out)
        if not args.quiet_profile:
            _print_profile(generated)
        exit_code |= 0 if result.passed else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
