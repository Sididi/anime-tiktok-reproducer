"""Independent, split/merge-invariant matching evaluation primitives.

This module intentionally does not import the historical ground-truth
validator.  It compares two piecewise source timelines directly at 10 Hz.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..models import MatchList, SceneList


GROUND_TRUTH_FILENAMES = ("project.json", "scenes.json", "matches.json")
MEDIA_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".webm"}


def canonical_episode(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/").split("/")[-1]
    suffix = Path(normalized).suffix.lower()
    return Path(normalized).stem if suffix in MEDIA_SUFFIXES else normalized


@dataclass(frozen=True)
class GroundTruthSnapshot:
    project_id: str
    project_dir: Path
    bytes_by_name: dict[str, bytes]
    hashes: dict[str, str]

    @classmethod
    def capture(cls, project_dir: Path) -> "GroundTruthSnapshot":
        resolved = project_dir.resolve()
        values: dict[str, bytes] = {}
        hashes: dict[str, str] = {}
        for name in GROUND_TRUTH_FILENAMES:
            payload = (resolved / name).read_bytes()
            values[name] = payload
            hashes[name] = hashlib.sha256(payload).hexdigest()
        return cls(resolved.name, resolved, values, hashes)

    def assert_unchanged(self) -> None:
        changed = []
        for name, expected in self.hashes.items():
            path = self.project_dir / name
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                changed.append(name)
        if changed:
            raise RuntimeError(
                f"Ground truth {self.project_id} was modified: {', '.join(changed)}"
            )

    def load_scenes(self) -> SceneList:
        return SceneList.model_validate_json(self.bytes_by_name["scenes.json"])

    def load_matches(self) -> MatchList:
        return MatchList.model_validate_json(self.bytes_by_name["matches.json"])


def ensure_safe_output_dir(output_dir: Path, snapshots: list[GroundTruthSnapshot]) -> Path:
    resolved = output_dir.expanduser().resolve()
    for snapshot in snapshots:
        if resolved == snapshot.project_dir or resolved.is_relative_to(snapshot.project_dir):
            raise ValueError(
                f"Evaluation output may not be written inside ground truth {snapshot.project_id}"
            )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _mapping_at(
    timestamp: float,
    scenes: SceneList,
    matches: MatchList,
) -> tuple[str, float, int] | None:
    pairs = list(zip(scenes.scenes, matches.matches, strict=False))
    for index, (scene, match) in enumerate(pairs):
        is_last = index == len(pairs) - 1
        if scene.start_time - 1e-9 <= timestamp and (
            timestamp < scene.end_time - 1e-9
            or (is_last and timestamp <= scene.end_time + 1e-9)
        ):
            if not match.episode or match.end_time <= match.start_time:
                return None
            fraction = min(
                1.0,
                max(
                    0.0,
                    (timestamp - scene.start_time) / max(scene.duration, 1e-9),
                ),
            )
            source = match.start_time + fraction * (match.end_time - match.start_time)
            return canonical_episode(match.episode), float(source), index
    return None


def _alternative_hit(
    timestamp: float,
    gt_episode: str,
    gt_source: float,
    scenes: SceneList,
    matches: MatchList,
    tolerance: float,
) -> bool:
    mapping = _mapping_at(timestamp, scenes, matches)
    if mapping is not None:
        episode, source, _ = mapping
        if episode == gt_episode and abs(source - gt_source) <= tolerance:
            return True
    pairs = list(zip(scenes.scenes, matches.matches, strict=False))
    for index, (scene, match) in enumerate(pairs):
        is_last = index == len(pairs) - 1
        if not (
            scene.start_time - 1e-9 <= timestamp
            and (
                timestamp < scene.end_time - 1e-9
                or (is_last and timestamp <= scene.end_time + 1e-9)
            )
        ):
            continue
        fraction = min(
            1.0,
            max(0.0, (timestamp - scene.start_time) / max(scene.duration, 1e-9)),
        )
        for alternative in match.alternatives[:7]:
            episode = canonical_episode(alternative.episode)
            source = alternative.start_time + fraction * (
                alternative.end_time - alternative.start_time
            )
            if episode == gt_episode and abs(source - gt_source) <= tolerance:
                return True
        break
    return False


def _candidate_diversity(matches: MatchList) -> dict[str, float]:
    cluster_counts: list[int] = []
    episode_counts: list[int] = []
    pairwise_separations: list[float] = []
    for match in matches.matches:
        clusters: list[tuple[str, float]] = []
        for alternative in match.alternatives[:7]:
            episode = canonical_episode(alternative.episode)
            midpoint = 0.5 * (alternative.start_time + alternative.end_time)
            if any(ep == episode and abs(midpoint - other) < 2.0 for ep, other in clusters):
                continue
            clusters.append((episode, midpoint))
        cluster_counts.append(len(clusters))
        episode_counts.append(len({episode for episode, _ in clusters}))
        for left in range(len(clusters)):
            for right in range(left + 1, len(clusters)):
                if clusters[left][0] == clusters[right][0]:
                    pairwise_separations.append(abs(clusters[left][1] - clusters[right][1]))
    return {
        "mean_temporal_clusters": float(np.mean(cluster_counts)) if cluster_counts else 0.0,
        "mean_unique_episodes": float(np.mean(episode_counts)) if episode_counts else 0.0,
        "mean_same_episode_separation": (
            float(np.mean(pairwise_separations)) if pairwise_separations else 0.0
        ),
    }


def evaluate_timeline(
    gt_scenes: SceneList,
    gt_matches: MatchList,
    generated_scenes: SceneList,
    generated_matches: MatchList,
    *,
    step: float = 0.10,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not gt_scenes.scenes:
        raise ValueError("Ground truth has no scenes")
    duration = gt_scenes.scenes[-1].end_time
    sample_count = max(1, int(math.ceil(duration / step)))
    exact = 0
    passed = 0
    episode_correct = 0
    resolved = 0
    abstained = 0
    wrong_episode = 0
    top7 = 0
    source_errors: list[float] = []
    rows: list[dict[str, Any]] = []

    for index in range(sample_count):
        timestamp = min(duration - 1e-6, index * step)
        gt = _mapping_at(timestamp, gt_scenes, gt_matches)
        if gt is None:
            continue
        gt_episode, gt_source, gt_index = gt
        generated = _mapping_at(timestamp, generated_scenes, generated_matches)
        status = "abstained"
        error: float | None = None
        generated_episode = ""
        generated_source: float | None = None
        generated_confidence = 0.0
        generated_index: int | None = None
        if generated is None:
            abstained += 1
        else:
            resolved += 1
            generated_episode, generated_source, generated_index = generated
            generated_confidence = float(
                generated_matches.matches[generated_index].confidence
            )
            if generated_episode == gt_episode:
                episode_correct += 1
                error = abs(generated_source - gt_source)
                source_errors.append(error)
                if error <= 0.5:
                    exact += 1
                    passed += 1
                    status = "exact"
                elif error <= 1.0:
                    passed += 1
                    status = "pass"
                else:
                    status = "timing_error"
            else:
                wrong_episode += 1
                status = "wrong_episode"
        if _alternative_hit(
            timestamp,
            gt_episode,
            gt_source,
            generated_scenes,
            generated_matches,
            1.0,
        ):
            top7 += 1
        rows.append(
            {
                "tiktok_time": round(timestamp, 3),
                "gt_scene": gt_index,
                "gt_episode": gt_episode,
                "gt_source": round(gt_source, 3),
                "generated_episode": generated_episode,
                "generated_source": (
                    round(generated_source, 3) if generated_source is not None else None
                ),
                "generated_scene": generated_index,
                "generated_confidence": generated_confidence,
                "source_error": round(error, 3) if error is not None else None,
                "status": status,
            }
        )

    denominator = max(1, len(rows))
    metrics: dict[str, Any] = {
        "duration_seconds": duration,
        "samples": len(rows),
        "exact_0_5_coverage": exact / denominator,
        "pass_1_0_coverage": passed / denominator,
        "episode_coverage": episode_correct / denominator,
        "resolved_coverage": resolved / denominator,
        "abstained_coverage": abstained / denominator,
        "wrong_episode_coverage": wrong_episode / denominator,
        "resolved_pass_precision": passed / max(1, resolved),
        "top7_pass_recall": top7 / denominator,
        "source_error_p50": (
            float(np.percentile(source_errors, 50)) if source_errors else None
        ),
        "source_error_p95": (
            float(np.percentile(source_errors, 95)) if source_errors else None
        ),
        "generated_scene_count": len(generated_scenes.scenes),
        "ground_truth_scene_count": len(gt_scenes.scenes),
        "fragmentation_ratio": len(generated_scenes.scenes)
        / max(1, len(gt_scenes.scenes)),
        "candidate_diversity": _candidate_diversity(generated_matches),
    }
    return metrics, rows


def calibrate_confidence_leave_one_project_out(
    rows_by_project: dict[str, list[dict[str, Any]]],
    *,
    target_precision: float = 0.98,
) -> dict[str, Any]:
    """Choose the most conservative confidence cutoff across LOPO folds."""
    if len(rows_by_project) < 2:
        return {
            "calibrated": False,
            "reason": "at_least_two_projects_required",
            "target_precision": target_precision,
            "folds": {},
            "threshold": None,
        }

    folds: dict[str, dict[str, float | int | None]] = {}
    thresholds: list[float] = []
    for held_out in sorted(rows_by_project):
        training = [
            row
            for project_id, rows in rows_by_project.items()
            if project_id != held_out
            for row in rows
            if row.get("generated_source") is not None
        ]
        candidates = sorted(
            {0.0, *(float(row.get("generated_confidence") or 0.0) for row in training)}
        )
        selected: float | None = None
        selected_precision = 0.0
        selected_count = 0
        for threshold in candidates:
            eligible = [
                row
                for row in training
                if float(row.get("generated_confidence") or 0.0) >= threshold
            ]
            if not eligible:
                continue
            correct = sum(row.get("status") in {"exact", "pass"} for row in eligible)
            precision = correct / len(eligible)
            if precision >= target_precision:
                selected = threshold
                selected_precision = precision
                selected_count = len(eligible)
                break
        if selected is None:
            selected = 1.000001
            selected_precision = 1.0
            selected_count = 0
        thresholds.append(selected)
        folds[held_out] = {
            "training_threshold": selected,
            "training_precision": selected_precision,
            "training_resolved_samples": selected_count,
        }

    threshold = max(thresholds)
    for held_out, fold in folds.items():
        held_rows = rows_by_project[held_out]
        eligible = [
            row
            for row in held_rows
            if row.get("generated_source") is not None
            and float(row.get("generated_confidence") or 0.0) >= threshold
        ]
        correct = sum(row.get("status") in {"exact", "pass"} for row in eligible)
        fold["held_out_precision"] = correct / len(eligible) if eligible else None
        fold["held_out_resolved_coverage"] = len(eligible) / max(1, len(held_rows))
    return {
        "calibrated": True,
        "target_precision": target_precision,
        "folds": folds,
        "threshold": threshold,
    }


def acceptance_report(
    metrics: list[dict[str, Any]],
    *,
    exact_baselines: dict[str, float] | None = None,
    pass_baselines: dict[str, float] | None = None,
) -> dict[str, Any]:
    total_samples = sum(int(value.get("samples", 0)) for value in metrics)

    def weighted(name: str) -> float:
        return sum(
            float(value.get(name, 0.0)) * int(value.get("samples", 0))
            for value in metrics
        ) / max(1, total_samples)

    speed = {
        value["project_id"]: float(value["cold_seconds"])
        <= (60.0 if float(value["duration_seconds"]) <= 90.0 else 120.0)
        for value in metrics
    }
    pass_regression = (
        pass_baselines is not None
        and all(
            float(value["pass_1_0_coverage"])
            >= pass_baselines.get(value["project_id"], 0.0) - 0.01
            for value in metrics
        )
    )
    exact_regression = (
        exact_baselines is not None
        and all(
            float(value["exact_0_5_coverage"])
            >= exact_baselines.get(value["project_id"], 0.0) - 0.02
            for value in metrics
        )
    )
    exact_baseline_suite = (
        sum(
            exact_baselines.get(value["project_id"], 0.0)
            * int(value.get("samples", 0))
            for value in metrics
        )
        / max(1, total_samples)
        if exact_baselines is not None
        else None
    )
    resolved_samples = sum(
        float(value.get("resolved_coverage", 0.0))
        * int(value.get("samples", 0))
        for value in metrics
    )
    resolved_correct = sum(
        float(value.get("resolved_pass_precision", 0.0))
        * float(value.get("resolved_coverage", 0.0))
        * int(value.get("samples", 0))
        for value in metrics
    )
    resolved_precision = resolved_correct / max(1.0, resolved_samples)
    gates = {
        "cold_speed": all(speed.values()) if speed else False,
        "cold_and_two_warm": bool(metrics)
        and all(len(value.get("warm_seconds", [])) >= 2 for value in metrics),
        "suite_pass_1_0": weighted("pass_1_0_coverage") >= 0.95,
        "resolved_precision": resolved_precision >= 0.98,
        "pass_regression": pass_regression,
        "exact_regression": exact_regression,
        "suite_exact_improvement": exact_baseline_suite is not None
        and weighted("exact_0_5_coverage") > exact_baseline_suite,
    }
    return {
        "release_ready": all(gates.values()),
        "gates": gates,
        "cold_speed_by_project": speed,
        "suite_pass_1_0_coverage": weighted("pass_1_0_coverage"),
        "suite_exact_0_5_coverage": weighted("exact_0_5_coverage"),
        "suite_resolved_precision": resolved_precision,
        "baseline_reason": (
            None
            if exact_baselines is not None and pass_baselines is not None
            else "independent_v2_baselines_not_supplied"
        ),
    }


def mismatch_intervals(rows: list[dict[str, Any]], *, step: float = 0.10) -> list[dict[str, Any]]:
    bad = {"wrong_episode", "timing_error", "abstained"}
    intervals: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in rows:
        if row["status"] not in bad:
            if current is not None:
                intervals.append(current)
                current = None
            continue
        key = (row["status"], row["gt_scene"])
        if current is None or current["key"] != key:
            if current is not None:
                intervals.append(current)
            current = {
                "key": key,
                "status": row["status"],
                "gt_scene": row["gt_scene"],
                "start": row["tiktok_time"],
                "end": row["tiktok_time"] + step,
                "gt_episode": row["gt_episode"],
                "generated_episode": row["generated_episode"],
                "max_source_error": row["source_error"],
            }
        else:
            current["end"] = row["tiktok_time"] + step
            if row["source_error"] is not None:
                current["max_source_error"] = max(
                    current["max_source_error"] or 0.0,
                    row["source_error"],
                )
    if current is not None:
        intervals.append(current)
    for interval in intervals:
        interval.pop("key", None)
    return intervals


def duplicate_ambiguity_intervals(
    scenes: SceneList,
    matches: MatchList,
) -> list[dict[str, Any]]:
    return [
        {
            "status": "duplicate_ambiguity",
            "gt_scene": scene.index,
            "start": scene.start_time,
            "end": scene.end_time,
            "gt_episode": "",
            "generated_episode": match.episode,
            "max_source_error": None,
        }
        for scene, match in zip(scenes.scenes, matches.matches, strict=False)
        if "duplicate_margin" in match.doubt_reasons
    ]


def write_review_html(
    output_path: Path,
    project_id: str,
    metrics: dict[str, Any],
    intervals: list[dict[str, Any]],
    assets: list[dict[str, str]] | None = None,
) -> None:
    rows = []
    assets = assets or []
    for index, interval in enumerate(intervals):
        asset = assets[index] if index < len(assets) else {}
        images = " ".join(
            f'<figure><img src="{html.escape(path)}" width="150"><figcaption>{html.escape(label)}</figcaption></figure>'
            for label, path in asset.items()
            if path
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(interval['status'])}</td>"
            f"<td>{interval['gt_scene']}</td>"
            f"<td>{interval['start']:.2f}–{interval['end']:.2f}</td>"
            f"<td>{html.escape(interval['gt_episode'])}</td>"
            f"<td>{html.escape(interval['generated_episode'] or 'abstained')}</td>"
            f"<td>{html.escape(str(interval['max_source_error']))}</td>"
            f"<td><div class=images>{images}</div></td>"
            "</tr>"
        )
    document = f"""<!doctype html>
<meta charset=\"utf-8\"><title>Matcher V2 review {html.escape(project_id)}</title>
<style>body{{font:14px sans-serif;margin:2rem}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:.35rem}}pre{{white-space:pre-wrap}}.images{{display:flex;gap:.4rem}}figure{{margin:0}}figcaption{{text-align:center;font-size:11px}}</style>
<h1>Matcher V2 review — {html.escape(project_id)}</h1>
<pre>{html.escape(json.dumps(metrics, indent=2, sort_keys=True))}</pre>
<table><thead><tr><th>Status</th><th>GT scene</th><th>TikTok</th><th>Expected</th><th>Generated</th><th>Max error</th><th>Visual comparison</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
"""
    output_path.write_text(document, encoding="utf-8")
