# Matching Precision Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 3-frame triple-search matcher and endpoint-proximity continuity merger with RANSAC trajectory-fit matching and two-tier cut-pair continuity verification, materially reducing wrong-shot matches and chain over-merging.

**Architecture:** Pass 1 of `/matches` extracts 5–7 TikTok probes per scene, runs SSCD top-K per probe, then a per-episode RANSAC line fit picks the best monotonic source trajectory; the line slope yields `speed_ratio`. Pass 2 applies episode + source-window priors. Continuity merging requires both Tier 1 (same-episode + small source gap + similar speed_ratio) and Tier 2 (cut-pair frame agreement). Stitching and alternative-vote evidence are removed.

**Tech Stack:** Python 3.11+, FastAPI backend, NumPy for line fits, OpenCV for frame I/O, anime_searcher submodule (frozen) for SSCD embeddings + FAISS retrieval, pytest for unit tests, pixi for env (`pixi run test` runs pytest from `backend/`).

**Reference spec:** `docs/superpowers/specs/2026-04-27-matching-precision-rework-design.md`. Sibling specs (B and Hybrid) are not in scope here.

**Working directory:** All paths below are relative to repo root `/home/sid/Projects/anime-tiktok-reproducer/`.

**Run tests with:** `pixi run test -k <pattern>` from repo root, or `cd backend && pytest -k <pattern>` after activating the pixi env.

---

## File structure

**New:**
- `backend/app/services/trajectory_fit.py` — pure-Python module with the `TrajectoryFit` dataclass, `Pass2Prior` dataclass, `_extract_probe_times`, `_apply_slope_penalty`, `_fit_trajectory`, `_static_shot_fit`. Importable from `anime_matcher` and from tests with no GPU/SSCD dependency.
- `backend/app/services/cut_pair_verifier.py` — `CutPairResult` dataclass and `_verify_cut_pair` function. Takes a video-frame-extractor callable and an SSCD-search callable as injected dependencies for testability.
- `backend/tests/test_trajectory_fit.py` — unit tests for the new pure functions.
- `backend/tests/test_cut_pair_verifier.py` — unit tests for the cut-pair verifier with mocked extractor + searcher.
- `backend/tests/test_scene_merger_pair_score.py` — unit tests for `_pair_score`.
- `backend/scripts/validate_matching_against_ground_truth.py` — runs the full pipeline on a TikTok and compares against ground truth JSON.

**Modified:**
- `backend/app/services/anime_matcher.py` — wire trajectory fit into `match_scenes`, add `pass2_priors` parameter, remove `_find_temporal_match`.
- `backend/app/services/scene_merger.py` — replace `detect_continuous_pairs` body, replace pair-scoring, drop stitching + alternative-vote evidence.
- `backend/app/api/routes/matching.py` — build `pass2_priors` dict at the Pass 2 call site.

**Untouched:**
- `backend/app/models/match.py` — schema unchanged.
- `backend/app/services/scene_detector.py`, `raw_scene_detector.py` — out of scope.
- `modules/anime_searcher/` — frozen.
- Frontend — confidence semantics preserved, schema unchanged.

---

## Conventions

- Tests follow the existing pattern in `backend/tests/test_account_service.py`: `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` then `from app.services.X import Y`.
- All new constants live as module-level UPPER_CASE in `trajectory_fit.py` / `cut_pair_verifier.py`. The merger reuses its existing `CONTINUITY_GAP_TOLERANCE` constant.
- New pure-function modules avoid imports of `anime_matcher` / `scene_merger` to prevent cycles. Cross-module dataclasses are imported from `trajectory_fit`.
- Every task ends with a commit. Use a HEREDOC for commit messages and include the `Co-Authored-By` trailer per repo convention.

---

## Task 1: `TrajectoryFit` + `Pass2Prior` dataclasses, `_extract_probe_times`

**Files:**
- Create: `backend/app/services/trajectory_fit.py`
- Test: `backend/tests/test_trajectory_fit.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_trajectory_fit.py
"""Tests for trajectory_fit pure-function module."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trajectory_fit import (
    Pass2Prior,
    TrajectoryFit,
    _extract_probe_times,
)


def _approx_eq(actual: list[float], expected: list[float], tol: float = 1e-6) -> bool:
    if len(actual) != len(expected):
        return False
    return all(abs(a - e) < tol for a, e in zip(actual, expected))


class TestExtractProbeTimes:
    def test_short_scene_two_probes(self):
        # D = 0.3s, scene_start = 1.0s -> probes at 0.20D, 0.80D from start
        result = _extract_probe_times(scene_start=1.0, scene_duration=0.3)
        assert _approx_eq(result, [1.0 + 0.06, 1.0 + 0.24])

    def test_medium_scene_three_probes(self):
        # D = 1.0s -> probes at 0.15D, 0.50D, 0.85D
        result = _extract_probe_times(scene_start=10.0, scene_duration=1.0)
        assert _approx_eq(result, [10.15, 10.50, 10.85])

    def test_longer_scene_five_probes(self):
        # D = 2.0s -> 5 probes evenly spaced with 0.15D end-offsets
        result = _extract_probe_times(scene_start=0.0, scene_duration=2.0)
        # First probe at 0.30s, last at 1.70s, 5 probes -> spacing 0.35s
        assert _approx_eq(result, [0.30, 0.65, 1.00, 1.35, 1.70])

    def test_long_scene_seven_probes(self):
        # D = 4.0s -> 7 probes
        result = _extract_probe_times(scene_start=100.0, scene_duration=4.0)
        # First at 100.6, last at 103.4, 7 probes -> spacing (103.4-100.6)/6 = 0.4666...
        assert len(result) == 7
        assert abs(result[0] - 100.60) < 1e-6
        assert abs(result[-1] - 103.40) < 1e-6

    def test_boundary_at_0_5(self):
        # D == 0.5 -> 3 probes (>= 0.5 falls into 3-probe bucket)
        result = _extract_probe_times(scene_start=0.0, scene_duration=0.5)
        assert len(result) == 3

    def test_boundary_at_1_5(self):
        # D == 1.5 -> 5 probes
        result = _extract_probe_times(scene_start=0.0, scene_duration=1.5)
        assert len(result) == 5

    def test_boundary_at_4_0(self):
        # D == 4.0 -> 7 probes
        result = _extract_probe_times(scene_start=0.0, scene_duration=4.0)
        assert len(result) == 7

    def test_zero_duration_returns_two_probes_clamped(self):
        # Pathological zero-duration scene -> still return 2 probes at the start
        result = _extract_probe_times(scene_start=5.0, scene_duration=0.0)
        assert len(result) == 2
        assert all(t == 5.0 for t in result)


class TestDataclasses:
    def test_trajectory_fit_default_score_zero(self):
        fit = TrajectoryFit(
            episode="ep1",
            m=1.0,
            b=0.0,
            inlier_count=3,
            rmse=0.1,
            avg_inlier_similarity=0.7,
            score=0.5,
            candidates_per_probe=[None, None, None],
        )
        assert fit.score == 0.5
        assert fit.episode == "ep1"

    def test_pass2_prior_fields(self):
        prior = Pass2Prior(episode="ep1", source_lo=10.0, source_hi=20.0)
        assert prior.episode == "ep1"
        assert prior.source_lo == 10.0
        assert prior.source_hi == 20.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.trajectory_fit'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/trajectory_fit.py
"""Pure-function trajectory fitting for the anime matcher.

This module has no dependencies on anime_searcher, OpenCV, or any GPU-bound
code so it can be imported and unit-tested without loading SSCD models.

See docs/superpowers/specs/2026-04-27-matching-precision-rework-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Probe-count rules: §3.1 of the spec.
_PROBE_BREAKS = (0.5, 1.5, 4.0)
_PROBE_COUNTS = (2, 3, 5, 7)
_PROBE_END_OFFSET_FRACTION = 0.15


@dataclass
class TrajectoryFit:
    """Result of a per-episode RANSAC line fit.

    The line is `source_t = m * tiktok_t + b`. `speed_ratio = 1 / m` is
    the legacy matcher convention.
    """

    episode: str
    m: float
    b: float
    inlier_count: int
    rmse: float
    avg_inlier_similarity: float
    score: float
    # One MatchCandidate (or None) per probe, indexed by probe order.
    # Typed loosely (Any) to avoid importing app.models here.
    candidates_per_probe: list = field(default_factory=list)


@dataclass
class Pass2Prior:
    """Per-merged-scene priors for Pass 2 trajectory fitting."""

    episode: str
    source_lo: float
    source_hi: float


def _extract_probe_times(scene_start: float, scene_duration: float) -> list[float]:
    """Return the absolute TikTok timestamps for probes covering one scene.

    Probe count and placement follow the piecewise rule in §3.1 of the spec:

    | duration D       | count | placement                              |
    | ---------------- | ----- | -------------------------------------- |
    | D < 0.5          | 2     | 0.20·D, 0.80·D                         |
    | 0.5 <= D < 1.5   | 3     | 0.15·D, 0.50·D, 0.85·D                 |
    | 1.5 <= D < 4.0   | 5     | evenly spaced, 0.15·D end-offsets      |
    | D >= 4.0         | 7     | evenly spaced, 0.15·D end-offsets      |
    """
    if scene_duration <= 0:
        # Pathological: collapse all probes to the scene start.
        return [scene_start, scene_start]

    if scene_duration < _PROBE_BREAKS[0]:
        offsets = [0.20 * scene_duration, 0.80 * scene_duration]
    elif scene_duration < _PROBE_BREAKS[1]:
        offsets = [0.15 * scene_duration, 0.50 * scene_duration, 0.85 * scene_duration]
    else:
        count = _PROBE_COUNTS[2] if scene_duration < _PROBE_BREAKS[2] else _PROBE_COUNTS[3]
        end_offset = _PROBE_END_OFFSET_FRACTION * scene_duration
        first = end_offset
        last = scene_duration - end_offset
        if count == 1:
            offsets = [(first + last) / 2.0]
        else:
            step = (last - first) / (count - 1)
            offsets = [first + i * step for i in range(count)]

    return [scene_start + o for o in offsets]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: PASS for all 11 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/trajectory_fit.py backend/tests/test_trajectory_fit.py
git commit -m "$(cat <<'EOF'
feat(matcher): add trajectory_fit module with probe-time helper

First step of the matching precision rework. Introduces the pure-function
trajectory_fit module with TrajectoryFit and Pass2Prior dataclasses and the
piecewise probe-time rule from spec §3.1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `_apply_slope_penalty`

**Files:**
- Modify: `backend/app/services/trajectory_fit.py`
- Test: `backend/tests/test_trajectory_fit.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_trajectory_fit.py`:

```python
from app.services.trajectory_fit import _apply_slope_penalty


class TestApplySlopePenalty:
    def test_inside_free_range_returns_one(self):
        assert _apply_slope_penalty(0.65) == 1.0
        assert _apply_slope_penalty(1.00) == 1.0
        assert _apply_slope_penalty(1.60) == 1.0

    def test_below_free_above_hard_lo_ramps(self):
        # speed_ratio = 0.475 is halfway between hard.lo=0.30 and free.lo=0.65
        result = _apply_slope_penalty(0.475)
        assert abs(result - 0.5) < 1e-6

    def test_above_free_below_hard_hi_ramps(self):
        # speed_ratio = 2.05 is halfway between free.hi=1.60 and hard.hi=2.50
        result = _apply_slope_penalty(2.05)
        assert abs(result - 0.5) < 1e-6

    def test_at_hard_lo_zero(self):
        assert _apply_slope_penalty(0.30) == 0.0

    def test_at_hard_hi_zero(self):
        assert _apply_slope_penalty(2.50) == 0.0

    def test_below_hard_lo_zero(self):
        assert _apply_slope_penalty(0.10) == 0.0

    def test_above_hard_hi_zero(self):
        assert _apply_slope_penalty(3.50) == 0.0

    def test_negative_or_zero_zero(self):
        assert _apply_slope_penalty(0.0) == 0.0
        assert _apply_slope_penalty(-1.0) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: FAIL with `ImportError` on `_apply_slope_penalty`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/trajectory_fit.py`:

```python
# Slope-penalty bounds: §3.5 of the spec. Operate on speed_ratio = 1 / m.
SLOPE_PENALTY_FREE_RANGE = (0.65, 1.60)
SLOPE_HARD_BOUNDS = (0.30, 2.50)


def _apply_slope_penalty(speed_ratio: float) -> float:
    """Return a multiplicative score factor in [0.0, 1.0] for a speed ratio.

    1.0 inside the free range, linearly decaying to 0.0 at the hard bounds,
    0.0 outside the hard bounds. See §3.5.
    """
    free_lo, free_hi = SLOPE_PENALTY_FREE_RANGE
    hard_lo, hard_hi = SLOPE_HARD_BOUNDS

    if speed_ratio <= 0.0:
        return 0.0
    if free_lo <= speed_ratio <= free_hi:
        return 1.0
    if hard_lo <= speed_ratio < free_lo:
        return (speed_ratio - hard_lo) / (free_lo - hard_lo)
    if free_hi < speed_ratio <= hard_hi:
        return (hard_hi - speed_ratio) / (hard_hi - free_hi)
    return 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: PASS for all 19 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/trajectory_fit.py backend/tests/test_trajectory_fit.py
git commit -m "$(cat <<'EOF'
feat(matcher): add slope penalty for soft speed-ratio gating

Replaces the hard MIN_SPEED/MAX_SPEED bounds with a soft penalty that decays
linearly outside the [0.65, 1.60] free range and zeroes outside [0.30, 2.50].
Allows extreme x0.5 / x2.0 clips when the trajectory fit is otherwise tight.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `_fit_trajectory` (RANSAC + LSQ refinement)

**Files:**
- Modify: `backend/app/services/trajectory_fit.py`
- Test: `backend/tests/test_trajectory_fit.py`

The RANSAC fit is the algorithmic core. Tests use simple synthetic candidate lists. We model `MatchCandidate` as a tiny duck-typed object so the trajectory module stays decoupled from `app.models`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_trajectory_fit.py`:

```python
import random
from dataclasses import dataclass

from app.services.trajectory_fit import _fit_trajectory


@dataclass
class _FakeCandidate:
    """Stand-in for app.models.MatchCandidate in tests."""

    episode: str
    timestamp: float
    similarity: float
    series: str = "test"


class TestFitTrajectoryRansac:
    def setup_method(self):
        # Deterministic seeding so RANSAC tests don't flake.
        random.seed(42)

    def test_clean_line_recovered_within_tol(self):
        # Probes at tiktok_t = 0, 0.5, 1.0, 1.5, 2.0
        # True source line: source_t = 100.0 + 1.0 * tiktok_t
        probe_times = [0.0, 0.5, 1.0, 1.5, 2.0]
        per_probe = [
            [_FakeCandidate("ep1", 100.0, 0.85)],
            [_FakeCandidate("ep1", 100.5, 0.90)],
            [_FakeCandidate("ep1", 101.0, 0.92)],
            [_FakeCandidate("ep1", 101.5, 0.88)],
            [_FakeCandidate("ep1", 102.0, 0.86)],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        assert fit is not None
        assert fit.episode == "ep1"
        assert abs(fit.m - 1.0) < 0.05
        assert abs(fit.b - 100.0) < 0.05
        assert fit.inlier_count == 5
        assert fit.score > 0.5

    def test_outlier_probe_excluded(self):
        # Four good points on line + one outlier (different scene of same episode)
        probe_times = [0.0, 0.5, 1.0, 1.5, 2.0]
        per_probe = [
            [_FakeCandidate("ep1", 100.0, 0.85)],
            [_FakeCandidate("ep1", 100.5, 0.90)],
            [_FakeCandidate("ep1", 200.0, 0.91)],  # Outlier: jumped to a different scene
            [_FakeCandidate("ep1", 101.5, 0.88)],
            [_FakeCandidate("ep1", 102.0, 0.86)],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        assert fit is not None
        # Inlier count: 4 of 5 probes lie on the line (probe 2 is the outlier).
        assert fit.inlier_count == 4
        assert abs(fit.m - 1.0) < 0.1

    def test_two_episodes_picks_better_fit(self):
        # Episode A: 5 candidates on a clean line.
        # Episode B: 5 candidates scattered.
        probe_times = [0.0, 0.5, 1.0, 1.5, 2.0]
        per_probe = [
            [
                _FakeCandidate("epA", 100.0, 0.85),
                _FakeCandidate("epB", 50.0, 0.90),
            ],
            [
                _FakeCandidate("epA", 100.5, 0.85),
                _FakeCandidate("epB", 250.0, 0.85),
            ],
            [
                _FakeCandidate("epA", 101.0, 0.85),
                _FakeCandidate("epB", 12.0, 0.80),
            ],
            [
                _FakeCandidate("epA", 101.5, 0.85),
                _FakeCandidate("epB", 800.0, 0.78),
            ],
            [
                _FakeCandidate("epA", 102.0, 0.85),
                _FakeCandidate("epB", 0.0, 0.75),
            ],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        assert fit is not None
        assert fit.episode == "epA"

    def test_no_candidates_returns_none(self):
        probe_times = [0.0, 0.5, 1.0]
        per_probe = [[], [], []]
        fit = _fit_trajectory(probe_times, per_probe)
        assert fit is None

    def test_only_one_probe_with_candidates_returns_none(self):
        probe_times = [0.0, 0.5, 1.0]
        per_probe = [
            [_FakeCandidate("ep1", 100.0, 0.85)],
            [],
            [],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        assert fit is None

    def test_two_probes_only_lsq_path(self):
        # Two probes, single candidate each -> two-point exact fit, fit_quality=1.
        probe_times = [0.0, 1.0]
        per_probe = [
            [_FakeCandidate("ep1", 50.0, 0.80)],
            [_FakeCandidate("ep1", 51.0, 0.85)],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        assert fit is not None
        assert fit.inlier_count == 2
        assert abs(fit.m - 1.0) < 1e-6
        assert abs(fit.b - 50.0) < 1e-6

    def test_negative_slope_rejected(self):
        # Source timestamps in reversed order across probes.
        probe_times = [0.0, 1.0, 2.0]
        per_probe = [
            [_FakeCandidate("ep1", 102.0, 0.85)],
            [_FakeCandidate("ep1", 101.0, 0.85)],
            [_FakeCandidate("ep1", 100.0, 0.85)],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        # All hypotheses have m < 0, all rejected. Fit returns None.
        assert fit is None

    def test_speed_ratio_outside_hard_bounds_rejected(self):
        # 5x speed-up: tiktok=2s, source=0.4s -> speed_ratio=5, outside hard bound.
        probe_times = [0.0, 0.5, 1.0, 1.5, 2.0]
        per_probe = [
            [_FakeCandidate("ep1", 100.0, 0.85)],
            [_FakeCandidate("ep1", 100.1, 0.85)],
            [_FakeCandidate("ep1", 100.2, 0.85)],
            [_FakeCandidate("ep1", 100.3, 0.85)],
            [_FakeCandidate("ep1", 100.4, 0.85)],
        ]
        fit = _fit_trajectory(probe_times, per_probe)
        # speed_ratio = 2.0 / 0.4 = 5.0; rejected by hard bound.
        assert fit is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: FAIL on `ImportError` for `_fit_trajectory`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/trajectory_fit.py`:

```python
import random
from typing import Any

# RANSAC tuning: §3.3 / §3.4 of the spec.
RANSAC_ITERATIONS = 30
RANSAC_INLIER_TOL_SECONDS = 0.6


def _fit_line_lsq(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares line `y = m * x + b` over (x, y) points. ≥2 points required."""
    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0.0:
        # Degenerate: all x are the same. Return horizontal line through mean y.
        return 0.0, sum_y / n
    m = (n * sum_xy - sum_x * sum_y) / denom
    b = (sum_y - m * sum_x) / n
    return m, b


def _evaluate_hypothesis(
    m: float,
    b: float,
    probe_times: list[float],
    per_probe_candidates: list[list[Any]],
) -> tuple[int, float, float, list[Any | None]]:
    """Count inliers and compute (inlier_count, rmse, avg_inlier_sim, picks)."""
    picks: list[Any | None] = []
    inlier_squared_residuals: list[float] = []
    inlier_similarities: list[float] = []

    for tiktok_t, candidates in zip(probe_times, per_probe_candidates):
        if not candidates:
            picks.append(None)
            continue
        line_y = m * tiktok_t + b
        # Closest candidate to the line (vertically).
        best_residual = float("inf")
        best_cand = None
        for cand in candidates:
            residual = abs(cand.timestamp - line_y)
            if residual < best_residual:
                best_residual = residual
                best_cand = cand
        if best_cand is not None and best_residual <= RANSAC_INLIER_TOL_SECONDS:
            picks.append(best_cand)
            inlier_squared_residuals.append(best_residual * best_residual)
            inlier_similarities.append(best_cand.similarity)
        else:
            picks.append(None)

    inlier_count = len(inlier_squared_residuals)
    if inlier_count == 0:
        return 0, 0.0, 0.0, picks
    rmse = (sum(inlier_squared_residuals) / inlier_count) ** 0.5
    avg_sim = sum(inlier_similarities) / inlier_count
    return inlier_count, rmse, avg_sim, picks


def _score_hypothesis(
    inlier_count: int,
    probe_count: int,
    rmse: float,
    avg_inlier_similarity: float,
    speed_ratio: float,
) -> float:
    """Trajectory score per §3.4."""
    if probe_count <= 0:
        return 0.0
    inlier_ratio = inlier_count / probe_count
    fit_quality = max(0.0, 1.0 - rmse / RANSAC_INLIER_TOL_SECONDS)
    penalty = _apply_slope_penalty(speed_ratio)
    return avg_inlier_similarity * inlier_ratio * fit_quality * penalty


def _fit_trajectory_for_episode(
    probe_times: list[float],
    per_probe_candidates: list[list[Any]],
    episode: str,
) -> TrajectoryFit | None:
    """Run RANSAC + LSQ refinement on one episode's per-probe candidates.

    `per_probe_candidates[i]` must contain only candidates from `episode`.
    """
    probes_with_any = [i for i, cs in enumerate(per_probe_candidates) if cs]
    if len(probes_with_any) < 2:
        return None

    probe_count = len(probe_times)
    best_score = -1.0
    best_inlier_count = 0
    best_m = 0.0
    best_b = 0.0
    best_rmse = 0.0
    best_avg_sim = 0.0
    best_picks: list[Any | None] = []

    if len(probes_with_any) == 2:
        # Direct two-point fit, no RANSAC needed.
        i, j = probes_with_any
        for ci in per_probe_candidates[i]:
            for cj in per_probe_candidates[j]:
                points = [(probe_times[i], ci.timestamp), (probe_times[j], cj.timestamp)]
                m, b = _fit_line_lsq(points)
                if m <= 0:
                    continue
                speed_ratio = 1.0 / m
                if not (SLOPE_HARD_BOUNDS[0] <= speed_ratio <= SLOPE_HARD_BOUNDS[1]):
                    continue
                inlier_count, rmse, avg_sim, picks = _evaluate_hypothesis(
                    m, b, probe_times, per_probe_candidates
                )
                score = _score_hypothesis(inlier_count, probe_count, rmse, avg_sim, speed_ratio)
                if score > best_score:
                    best_score = score
                    best_inlier_count = inlier_count
                    best_m = m
                    best_b = b
                    best_rmse = rmse
                    best_avg_sim = avg_sim
                    best_picks = picks
    else:
        # RANSAC.
        for _ in range(RANSAC_ITERATIONS):
            # Pick two distinct probe indices.
            i, j = random.sample(probes_with_any, 2)
            # Sample one candidate from each.
            ci = random.choice(per_probe_candidates[i])
            cj = random.choice(per_probe_candidates[j])
            if probe_times[i] == probe_times[j]:
                continue
            points = [(probe_times[i], ci.timestamp), (probe_times[j], cj.timestamp)]
            m, b = _fit_line_lsq(points)
            if m <= 0:
                continue
            speed_ratio = 1.0 / m
            if not (SLOPE_HARD_BOUNDS[0] <= speed_ratio <= SLOPE_HARD_BOUNDS[1]):
                continue
            inlier_count, rmse, avg_sim, picks = _evaluate_hypothesis(
                m, b, probe_times, per_probe_candidates
            )
            score = _score_hypothesis(inlier_count, probe_count, rmse, avg_sim, speed_ratio)
            if score > best_score:
                best_score = score
                best_inlier_count = inlier_count
                best_m = m
                best_b = b
                best_rmse = rmse
                best_avg_sim = avg_sim
                best_picks = picks

        # Refine on the inlier set via LSQ.
        if best_inlier_count >= 2:
            inlier_points = [
                (probe_times[i], best_picks[i].timestamp)
                for i in range(probe_count)
                if best_picks[i] is not None
            ]
            refined_m, refined_b = _fit_line_lsq(inlier_points)
            if refined_m > 0:
                refined_speed_ratio = 1.0 / refined_m
                if SLOPE_HARD_BOUNDS[0] <= refined_speed_ratio <= SLOPE_HARD_BOUNDS[1]:
                    refined_inlier_count, refined_rmse, refined_avg_sim, refined_picks = (
                        _evaluate_hypothesis(
                            refined_m, refined_b, probe_times, per_probe_candidates
                        )
                    )
                    refined_score = _score_hypothesis(
                        refined_inlier_count,
                        probe_count,
                        refined_rmse,
                        refined_avg_sim,
                        refined_speed_ratio,
                    )
                    if refined_score >= best_score:
                        best_score = refined_score
                        best_inlier_count = refined_inlier_count
                        best_m = refined_m
                        best_b = refined_b
                        best_rmse = refined_rmse
                        best_avg_sim = refined_avg_sim
                        best_picks = refined_picks

    if best_score <= 0 or best_inlier_count < 2:
        return None

    return TrajectoryFit(
        episode=episode,
        m=best_m,
        b=best_b,
        inlier_count=best_inlier_count,
        rmse=best_rmse,
        avg_inlier_similarity=best_avg_sim,
        score=best_score,
        candidates_per_probe=best_picks,
    )


def _fit_trajectory(
    probe_times: list[float],
    per_probe_candidates: list[list[Any]],
) -> TrajectoryFit | None:
    """Run trajectory fit per episode and return the best fit globally.

    `per_probe_candidates[i]` is a list of `MatchCandidate`-like objects
    with `episode: str`, `timestamp: float`, `similarity: float` attributes
    (any duck-typed object works).
    """
    if len(probe_times) != len(per_probe_candidates):
        raise ValueError("probe_times and per_probe_candidates must have the same length")

    episodes_seen: dict[str, list[list[Any]]] = {}
    for i, candidates in enumerate(per_probe_candidates):
        for cand in candidates:
            if cand.episode not in episodes_seen:
                episodes_seen[cand.episode] = [[] for _ in probe_times]
            episodes_seen[cand.episode][i].append(cand)

    best_fit: TrajectoryFit | None = None
    for episode, per_probe_for_ep in episodes_seen.items():
        fit = _fit_trajectory_for_episode(probe_times, per_probe_for_ep, episode)
        if fit is None:
            continue
        if best_fit is None or fit.score > best_fit.score:
            best_fit = fit

    return best_fit
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: PASS for all 27 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/trajectory_fit.py backend/tests/test_trajectory_fit.py
git commit -m "$(cat <<'EOF'
feat(matcher): add RANSAC trajectory fit for per-episode line fitting

Implements §3.3 of the spec: per-episode RANSAC line fit on
(probe_time, candidate_source_time) points with LSQ refinement on inliers,
plus the hypothesis scoring from §3.4. Trajectories with non-positive slope
or speed_ratio outside [0.30, 2.50] are rejected.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `_static_shot_fit` helper

**Files:**
- Modify: `backend/app/services/trajectory_fit.py`
- Test: `backend/tests/test_trajectory_fit.py`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_trajectory_fit.py`:

```python
from app.services.trajectory_fit import _static_shot_fit


class TestStaticShotFit:
    def test_clustered_candidates_recognized_as_static(self):
        # All candidates within ~1 source frame.
        probe_times = [0.0, 0.5, 1.0]
        per_probe = [
            [_FakeCandidate("ep1", 100.00, 0.85)],
            [_FakeCandidate("ep1", 100.02, 0.86)],
            [_FakeCandidate("ep1", 100.04, 0.84)],
        ]
        fit = _static_shot_fit(
            probe_times=probe_times,
            per_probe_candidates=per_probe,
            scene_start=10.0,
            scene_duration=1.0,
            source_fps=24.0,
        )
        assert fit is not None
        assert fit.episode == "ep1"
        # Source center is at 100.02 (median); start=center - 0.5, end=center + 0.5
        assert abs((fit.b + fit.m * 10.0) - (100.02 - 0.5)) < 0.05
        assert abs((fit.b + fit.m * 11.0) - (100.02 + 0.5)) < 0.05

    def test_spread_candidates_not_static(self):
        probe_times = [0.0, 0.5, 1.0]
        per_probe = [
            [_FakeCandidate("ep1", 100.0, 0.85)],
            [_FakeCandidate("ep1", 100.5, 0.86)],
            [_FakeCandidate("ep1", 101.0, 0.84)],
        ]
        fit = _static_shot_fit(
            probe_times=probe_times,
            per_probe_candidates=per_probe,
            scene_start=10.0,
            scene_duration=1.0,
            source_fps=24.0,
        )
        assert fit is None  # 1.0s spread is not a static shot.

    def test_no_candidates_returns_none(self):
        probe_times = [0.0, 0.5, 1.0]
        per_probe = [[], [], []]
        fit = _static_shot_fit(
            probe_times=probe_times,
            per_probe_candidates=per_probe,
            scene_start=10.0,
            scene_duration=1.0,
            source_fps=24.0,
        )
        assert fit is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: FAIL on `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/app/services/trajectory_fit.py`:

```python
STATIC_SHOT_TOL_FRAMES = 1.0


def _static_shot_fit(
    probe_times: list[float],
    per_probe_candidates: list[list[Any]],
    scene_start: float,
    scene_duration: float,
    source_fps: float,
) -> TrajectoryFit | None:
    """Detect a freeze-frame / static shot and return a unit-slope fit.

    All probe candidates from the best episode landing within
    `STATIC_SHOT_TOL_FRAMES / source_fps + slack` source-time of each other
    indicates a held shot. We synthesize a slope=1 fit centered on the
    median candidate timestamp so source_t spans `±scene_duration/2`.
    """
    # Identify the most-supported episode.
    counts: dict[str, list[Any]] = {}
    for candidates in per_probe_candidates:
        for cand in candidates:
            counts.setdefault(cand.episode, []).append(cand)
    if not counts:
        return None
    best_ep, best_cands = max(counts.items(), key=lambda kv: len(kv[1]))
    if len(best_cands) < 2:
        return None

    timestamps = [c.timestamp for c in best_cands]
    spread = max(timestamps) - min(timestamps)
    threshold = STATIC_SHOT_TOL_FRAMES / max(source_fps, 1e-3) + 0.05
    if spread > threshold:
        return None

    sorted_ts = sorted(timestamps)
    median = sorted_ts[len(sorted_ts) // 2]
    avg_sim = sum(c.similarity for c in best_cands) / len(best_cands)

    # Construct a slope=1 line through (scene midpoint, median):
    # source_t = median + 1.0 * (tiktok_t - scene_midpoint)
    scene_midpoint = scene_start + scene_duration / 2.0
    m = 1.0
    b = median - m * scene_midpoint

    return TrajectoryFit(
        episode=best_ep,
        m=m,
        b=b,
        inlier_count=len(best_cands),
        rmse=spread / 2.0,
        avg_inlier_similarity=avg_sim,
        score=avg_sim * 0.9,  # Slight discount vs a true trajectory fit.
        candidates_per_probe=[],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run test -k test_trajectory_fit -v`
Expected: PASS for all 30 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/trajectory_fit.py backend/tests/test_trajectory_fit.py
git commit -m "$(cat <<'EOF'
feat(matcher): add static-shot detector for freeze-frame scenes

Implements the §3.6 freeze-frame edge case: when all probe candidates from
the best-supported episode cluster within one source frame, treat as a held
shot with slope=1 centered on the median candidate timestamp.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire Pass 1 trajectory fit into `match_scenes`

**Files:**
- Modify: `backend/app/services/anime_matcher.py`

This is an integration change without unit tests (matching is GPU/IO-heavy and tested end-to-end in Task 13). Run the full test suite afterward to catch regressions on existing tests.

- [ ] **Step 1: Read existing `match_scenes` body to plan the edit**

Read: `backend/app/services/anime_matcher.py:754-1006`. The relevant block is the per-scene loop (~lines 814-996), specifically the section that computes `scene_duration`, extracts 3 frames, runs `_search_image_batch(top_n=25)`, calls `_find_temporal_match`, and stores the result.

- [ ] **Step 2: Replace the 3-frame extraction + triple-search block**

Edit `backend/app/services/anime_matcher.py`. Inside `match_scenes`, replace the body of the per-scene try block (approximately lines 843-981) with the trajectory-fit flow.

Add at the top of the file (after the existing imports):

```python
from .trajectory_fit import (
    Pass2Prior,
    TrajectoryFit,
    _apply_slope_penalty,
    _extract_probe_times,
    _fit_trajectory,
    _static_shot_fit,
)
```

The new per-scene body (replacing the old try block contents):

```python
try:
    probe_times = _extract_probe_times(
        scene_start=scene.start_time,
        scene_duration=scene.end_time - scene.start_time,
    )

    # Single-pass frame extraction.
    probe_frames = await loop.run_in_executor(
        None,
        cls.extract_frames,
        video_path,
        probe_times,
    )
    valid_indices = [i for i, f in enumerate(probe_frames) if f is not None]
    if len(valid_indices) < 2:
        # Cannot fit a line with fewer than 2 frames.
        matches.matches.append(
            SceneMatch(
                scene_index=scene.index,
                episode="",
                start_time=0,
                end_time=0,
                confidence=0,
                speed_ratio=1.0,
                was_no_match=True,
            )
        )
        continue

    valid_frames = [probe_frames[i] for i in valid_indices]
    valid_probe_times = [probe_times[i] for i in valid_indices]

    # Per-probe SSCD top-K. Returns one list of formatted results per probe.
    search_batch = partial(
        cls._search_image_batch,
        valid_frames,
        top_n=25,
        threshold=None,
        flip=False,
        series=anime_name,
    )
    per_probe_results = await loop.run_in_executor(None, search_batch)

    def _to_candidates(results) -> list[MatchCandidate]:
        return [
            MatchCandidate(
                episode=r.episode,
                timestamp=r.timestamp,
                similarity=r.similarity,
                series=r.series,
            )
            for r in results
        ]

    per_probe_candidates: list[list[MatchCandidate]] = [
        _to_candidates(results) for results in per_probe_results
    ]

    # Pass 2 priors (episode + source-window filtering).
    prior = pass2_priors.get(i) if pass2_priors else None
    if prior is not None:
        per_probe_candidates = [
            [
                c
                for c in cs
                if c.episode == prior.episode
                and prior.source_lo - SOURCE_WINDOW_PRIOR_PAD_SECONDS
                <= c.timestamp
                <= prior.source_hi + SOURCE_WINDOW_PRIOR_PAD_SECONDS
            ]
            for cs in per_probe_candidates
        ]

    fit = _fit_trajectory(valid_probe_times, per_probe_candidates)
    if fit is None:
        # Try the static-shot path before giving up.
        source_fps = (
            cls._get_video_fps(
                AnimeLibraryService.resolve_episode_path(
                    prior.episode, library_type=library_type
                )
            )
            if prior is not None
            else None
        ) or 24.0
        fit = _static_shot_fit(
            probe_times=valid_probe_times,
            per_probe_candidates=per_probe_candidates,
            scene_start=scene.start_time,
            scene_duration=scene.end_time - scene.start_time,
            source_fps=source_fps,
        )

    # Pass 2 union-endpoints fallback when fit is impossible.
    if fit is None and prior is not None:
        match = SceneMatch(
            scene_index=scene.index,
            episode=prior.episode,
            start_time=prior.source_lo,
            end_time=prior.source_hi,
            confidence=0.0,
            speed_ratio=(scene.end_time - scene.start_time)
            / max(prior.source_hi - prior.source_lo, 1e-3),
            was_no_match=False,
            merged_from=(
                existing_matches.matches[i].merged_from
                if existing_matches and i < len(existing_matches.matches)
                else None
            ),
        )
        matches.matches.append(match)
        continue

    if fit is None:
        # Nothing useful at all (no candidate landed). Commit a no-match.
        matches.matches.append(
            SceneMatch(
                scene_index=scene.index,
                episode="",
                start_time=0,
                end_time=0,
                confidence=0,
                speed_ratio=1.0,
                was_no_match=True,
            )
        )
        continue

    src_start = fit.b + fit.m * scene.start_time
    src_end = fit.b + fit.m * scene.end_time
    speed_ratio = 1.0 / fit.m if fit.m > 0 else 1.0

    match = SceneMatch(
        scene_index=scene.index,
        episode=fit.episode,
        start_time=max(0.0, src_start),
        end_time=max(src_start + 1e-3, src_end),
        confidence=fit.score,
        speed_ratio=speed_ratio,
        was_no_match=False,
    )

    # Native-FPS boundary refinement (unchanged).
    refined = await loop.run_in_executor(
        None,
        cls._refine_boundaries,
        video_path,
        scene,
        match.episode,
        match.start_time,
        match.end_time,
        library_type,
    )
    if refined is not None:
        refined_start, refined_end = refined
        refined_duration = refined_end - refined_start
        if refined_duration > 0:
            match.start_time = refined_start
            match.end_time = refined_end
            match.speed_ratio = scene.duration / refined_duration

    # Preserve the existing frontend-facing candidate fields by mapping
    # probes -> {start, middle, end}. Frontend reads start_candidates etc.
    if per_probe_candidates:
        match.start_candidates = per_probe_candidates[0]
        match.middle_candidates = per_probe_candidates[len(per_probe_candidates) // 2]
        match.end_candidates = per_probe_candidates[-1]

    # Alternatives stay on the top-5 slice of those three positions.
    match.alternatives = cls._compute_alternatives(
        match.start_candidates[:5],
        match.middle_candidates[:5],
        match.end_candidates[:5],
        scene.duration,
    )
    if existing_matches and i < len(existing_matches.matches):
        match.merged_from = existing_matches.matches[i].merged_from

    matches.matches.append(match)
except Exception as e:
    matches.matches.append(
        SceneMatch(
            scene_index=scene.index,
            episode="",
            start_time=0,
            end_time=0,
            confidence=0,
            speed_ratio=1.0,
            was_no_match=True,
        )
    )
    print(f"Error matching scene {i}: {e}")
```

Add the constant at the top of the class (or as a module-level constant):

```python
SOURCE_WINDOW_PRIOR_PAD_SECONDS = 2.0
```

Update the `match_scenes` signature to accept `pass2_priors`:

```python
@classmethod
async def match_scenes(
    cls,
    video_path: Path,
    scenes: SceneList,
    library_path: Path,
    library_type: LibraryType | str,
    anime_name: str | None = None,
    scene_indices_to_match: list[int] | None = None,
    existing_matches: MatchList | None = None,
    pass_label: str = "",
    pass2_priors: dict[int, Pass2Prior] | None = None,
) -> AsyncIterator[MatchProgress]:
```

- [ ] **Step 3: Remove `_find_temporal_match`**

Delete the entire `_find_temporal_match` classmethod (lines 416-508 in the original). The slope penalty and same-episode logic now live in `trajectory_fit.py`.

- [ ] **Step 4: Run the existing test suite**

Run: `pixi run test`
Expected: PASS, no new failures. (The test suite has no `match_scenes` integration tests; existing tests cover other services.)

- [ ] **Step 5: Quick syntax check by importing the module**

Run: `pixi run python -c "from app.services.anime_matcher import AnimeMatcherService; print(AnimeMatcherService)"` from `backend/`.
Expected: prints the class without error.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/anime_matcher.py
git commit -m "$(cat <<'EOF'
feat(matcher): wire trajectory fit into Pass 1/2 of match_scenes

Replaces the 3-frame triple search with the densified probe + RANSAC
trajectory fit pipeline. Adds the optional pass2_priors parameter for the
Pass 2 episode + source-window filtering. Removes _find_temporal_match;
its constraints (same episode, monotonic source order, speed bounds) are
now subsumed by the per-episode RANSAC fit and slope penalty.

Frontend-facing start/middle/end_candidates are still populated by mapping
the first / middle / last probe results, preserving the API surface.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `CutPairResult` + `_verify_cut_pair`

**Files:**
- Create: `backend/app/services/cut_pair_verifier.py`
- Test: `backend/tests/test_cut_pair_verifier.py`

The verifier takes a TikTok-frame extractor callable and an SSCD-search callable as injected dependencies, so unit tests can mock both without loading SSCD models.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_cut_pair_verifier.py
"""Tests for cut_pair_verifier."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.cut_pair_verifier import (
    CUT_PAIR_EPSILONS,
    CUT_PAIR_MIN_SIMILARITY,
    CutPairResult,
    _verify_cut_pair,
)


@dataclass
class _FakeFrame:
    tag: str  # for routing in the fake search


@dataclass
class _FakeResult:
    episode: str
    timestamp: float
    similarity: float


def _make_extractor(frame_map: dict[float, str | None]):
    """Build an extractor that returns a tagged frame per requested tiktok_t.

    `frame_map[t] = "tag"` means extracting at time t returns _FakeFrame("tag").
    `frame_map[t] = None` means extraction failed.
    Times are matched within 1e-6 tolerance.
    """

    def extractor(tiktok_t: float):
        for t, tag in frame_map.items():
            if abs(t - tiktok_t) < 1e-6:
                if tag is None:
                    return None
                return _FakeFrame(tag)
        raise AssertionError(f"Extractor called with unexpected time {tiktok_t}")

    return extractor


def _make_searcher(tag_to_results: dict[str, list[_FakeResult]]):
    """Build a searcher that returns results based on the input frame's tag."""

    def searcher(frame, episode: str):
        if frame is None:
            return []
        return tag_to_results.get(frame.tag, [])

    return searcher


def test_continuous_pair_passes_first_epsilon():
    extractor = _make_extractor({
        10.0 - 0.05: "before",
        10.0 + 0.05: "after",
    })
    searcher = _make_searcher({
        "before": [_FakeResult("ep1", 100.00, 0.80)],
        "after": [_FakeResult("ep1", 100.04, 0.82)],
    })

    result = _verify_cut_pair(
        scene_n_end=10.0,
        scene_n1_start=10.0,
        episode="ep1",
        extractor=extractor,
        searcher=searcher,
        source_fps=24.0,
        index_fps=2.0,
    )
    assert result.passed is True
    assert result.epsilon_used == 0.05


def test_blurry_first_epsilon_recovers_at_larger_epsilon():
    # ε=0.05 returns a too-low similarity; ε=0.10 succeeds.
    extractor = _make_extractor({
        10.0 - 0.05: "blur_before",
        10.0 + 0.05: "blur_after",
        10.0 - 0.10: "before",
        10.0 + 0.10: "after",
        10.0 - 0.20: "before2",
        10.0 + 0.20: "after2",
    })
    searcher = _make_searcher({
        "blur_before": [_FakeResult("ep1", 100.00, 0.30)],  # too weak
        "blur_after": [_FakeResult("ep1", 100.05, 0.30)],
        "before": [_FakeResult("ep1", 100.00, 0.55)],
        "after": [_FakeResult("ep1", 100.04, 0.60)],
    })

    result = _verify_cut_pair(
        scene_n_end=10.0,
        scene_n1_start=10.0,
        episode="ep1",
        extractor=extractor,
        searcher=searcher,
        source_fps=24.0,
        index_fps=2.0,
    )
    assert result.passed is True
    assert result.epsilon_used == 0.10


def test_wrong_episode_top_hit_fails():
    extractor = _make_extractor({
        10.0 - 0.05: "before",
        10.0 + 0.05: "after",
        10.0 - 0.10: "before",
        10.0 + 0.10: "after",
        10.0 - 0.20: "before",
        10.0 + 0.20: "after",
    })
    # ep1 is the requested episode. But the searcher (filtered by ep1) returns
    # nothing for these tags -> top hit not in ep1 -> always fail.
    searcher = _make_searcher({})

    result = _verify_cut_pair(
        scene_n_end=10.0,
        scene_n1_start=10.0,
        episode="ep1",
        extractor=extractor,
        searcher=searcher,
        source_fps=24.0,
        index_fps=2.0,
    )
    assert result.passed is False


def test_large_source_gap_fails():
    extractor = _make_extractor({
        10.0 - 0.05: "before",
        10.0 + 0.05: "after",
        10.0 - 0.10: "before",
        10.0 + 0.10: "after",
        10.0 - 0.20: "before",
        10.0 + 0.20: "after",
    })
    # 5s apart in source -> way too big.
    searcher = _make_searcher({
        "before": [_FakeResult("ep1", 100.00, 0.80)],
        "after": [_FakeResult("ep1", 105.00, 0.80)],
    })

    result = _verify_cut_pair(
        scene_n_end=10.0,
        scene_n1_start=10.0,
        episode="ep1",
        extractor=extractor,
        searcher=searcher,
        source_fps=24.0,
        index_fps=2.0,
    )
    assert result.passed is False


def test_below_similarity_floor_fails():
    extractor = _make_extractor({
        t: "before" if t < 10 else "after"
        for t in [10.0 - 0.05, 10.0 + 0.05, 10.0 - 0.10, 10.0 + 0.10, 10.0 - 0.20, 10.0 + 0.20]
    })
    searcher = _make_searcher({
        "before": [_FakeResult("ep1", 100.00, 0.10)],  # below 0.40 floor
        "after": [_FakeResult("ep1", 100.04, 0.10)],
    })

    result = _verify_cut_pair(
        scene_n_end=10.0,
        scene_n1_start=10.0,
        episode="ep1",
        extractor=extractor,
        searcher=searcher,
        source_fps=24.0,
        index_fps=2.0,
    )
    assert result.passed is False


def test_extraction_failure_skips_epsilon():
    extractor = _make_extractor({
        10.0 - 0.05: None,  # extraction fails
        10.0 + 0.05: "after",
        10.0 - 0.10: "before",
        10.0 + 0.10: "after",
        10.0 - 0.20: "before2",
        10.0 + 0.20: "after2",
    })
    searcher = _make_searcher({
        "before": [_FakeResult("ep1", 100.00, 0.55)],
        "after": [_FakeResult("ep1", 100.04, 0.60)],
    })

    result = _verify_cut_pair(
        scene_n_end=10.0,
        scene_n1_start=10.0,
        episode="ep1",
        extractor=extractor,
        searcher=searcher,
        source_fps=24.0,
        index_fps=2.0,
    )
    assert result.passed is True
    assert result.epsilon_used == 0.10


def test_constants_exposed():
    assert CUT_PAIR_EPSILONS == (0.05, 0.10, 0.20)
    assert CUT_PAIR_MIN_SIMILARITY == 0.40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test -k test_cut_pair_verifier -v`
Expected: FAIL with `ModuleNotFoundError: app.services.cut_pair_verifier`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/cut_pair_verifier.py
"""Cut-pair frame verification for the continuity merger.

See §4.2 of docs/superpowers/specs/2026-04-27-matching-precision-rework-design.md.

The function takes injected `extractor` and `searcher` callables so unit
tests can run without loading anime_searcher / OpenCV. The integration
caller passes thin wrappers over AnimeMatcherService.extract_frame /
_search_image_batch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

CUT_PAIR_EPSILONS: tuple[float, ...] = (0.05, 0.10, 0.20)
CUT_PAIR_MIN_SIMILARITY: float = 0.40
CUT_PAIR_MAX_FRAME_GAP_FACTOR: float = 1.0


@dataclass
class CutPairResult:
    passed: bool
    sim_before: float = 0.0
    sim_after: float = 0.0
    src_before: float = 0.0
    src_after: float = 0.0
    epsilon_used: float = 0.0


def _max_source_gap(source_fps: float, index_fps: float) -> float:
    """Allowed |src_after - src_before| in seconds.

    One source-frame gap (frame-perfect cut) plus half an index-grid step
    of slack to absorb the 2-FPS retrieval quantization.
    """
    return CUT_PAIR_MAX_FRAME_GAP_FACTOR / max(source_fps, 1e-3) + 0.5 / max(index_fps, 1e-3)


def _verify_cut_pair(
    *,
    scene_n_end: float,
    scene_n1_start: float,
    episode: str,
    extractor: Callable[[float], object | None],
    searcher: Callable[[object, str], list],
    source_fps: float,
    index_fps: float,
) -> CutPairResult:
    """Test whether two adjacent scenes are continuous in the source episode.

    `extractor(tiktok_t)` returns one decoded frame (any opaque type) or None.
    `searcher(frame, episode)` returns a list of `(episode, timestamp, similarity)`
    structs (objects with `.episode`, `.timestamp`, `.similarity` attributes)
    for SSCD top-K filtered to that episode.

    Returns a `CutPairResult` with `passed=True` on the first epsilon that
    satisfies the §4.2 conditions; `passed=False` if all fail.
    """
    max_gap = _max_source_gap(source_fps, index_fps)

    for epsilon in CUT_PAIR_EPSILONS:
        t_before = scene_n_end - epsilon
        t_after = scene_n1_start + epsilon

        frame_before = extractor(t_before)
        frame_after = extractor(t_after)
        if frame_before is None or frame_after is None:
            continue

        results_before = searcher(frame_before, episode)
        results_after = searcher(frame_after, episode)
        if not results_before or not results_after:
            continue

        top_before = results_before[0]
        top_after = results_after[0]

        if top_before.episode != episode or top_after.episode != episode:
            continue
        if top_before.similarity < CUT_PAIR_MIN_SIMILARITY:
            continue
        if top_after.similarity < CUT_PAIR_MIN_SIMILARITY:
            continue
        if abs(top_after.timestamp - top_before.timestamp) > max_gap:
            continue

        return CutPairResult(
            passed=True,
            sim_before=top_before.similarity,
            sim_after=top_after.similarity,
            src_before=top_before.timestamp,
            src_after=top_after.timestamp,
            epsilon_used=epsilon,
        )

    return CutPairResult(passed=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run test -k test_cut_pair_verifier -v`
Expected: PASS for all 7 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/cut_pair_verifier.py backend/tests/test_cut_pair_verifier.py
git commit -m "$(cat <<'EOF'
feat(merger): add cut-pair frame verifier

Implements §4.2: per adjacent scene pair, extract TikTok frames just before
and after the cut, look them up in SSCD filtered to the candidate episode,
and confirm both top hits land in that episode within ~1 source-frame of
each other. Retries with three increasing epsilons to survive motion-blur
on the cut frames.

Dependency-injected extractor + searcher keep the unit tests free of
GPU / SSCD model setup.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `_pair_score`

**Files:**
- Modify: `backend/app/services/scene_merger.py`
- Test: `backend/tests/test_scene_merger_pair_score.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_scene_merger_pair_score.py
"""Tests for SceneMergerService._pair_score."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.scene_merger import SceneMergerService


def test_perfect_pair_scores_one():
    score = SceneMergerService._pair_score(
        sim_before=1.0,
        sim_after=1.0,
        source_gap=0.0,
        speed_ratio_n=1.0,
        speed_ratio_n1=1.0,
        max_gap=0.5,
    )
    assert abs(score - 1.0) < 1e-6


def test_full_gap_scores_zero():
    score = SceneMergerService._pair_score(
        sim_before=1.0,
        sim_after=1.0,
        source_gap=0.5,
        speed_ratio_n=1.0,
        speed_ratio_n1=1.0,
        max_gap=0.5,
    )
    assert abs(score) < 1e-6


def test_speed_inconsistency_zeros_score():
    score = SceneMergerService._pair_score(
        sim_before=1.0,
        sim_after=1.0,
        source_gap=0.0,
        speed_ratio_n=1.0,
        speed_ratio_n1=1.5,  # delta = 0.5 > tol = 0.35
        max_gap=0.5,
    )
    assert abs(score) < 1e-6


def test_negative_gap_treated_as_zero():
    # Tiny negative jitter at exact-touch boundary -> still strong score.
    score = SceneMergerService._pair_score(
        sim_before=0.9,
        sim_after=0.9,
        source_gap=-0.01,
        speed_ratio_n=1.0,
        speed_ratio_n1=1.0,
        max_gap=0.5,
    )
    assert abs(score - 0.9) < 1e-6


def test_partial_gap_partial_speed_drift():
    # source_gap = 0.25 (half max) -> gap_weight = 0.5
    # speed delta = 0.175 (half tol) -> slope_consistency = 0.5
    # similarity_avg = (0.8 + 0.6) / 2 = 0.7
    # score = 0.7 * 0.5 * 0.5 = 0.175
    score = SceneMergerService._pair_score(
        sim_before=0.8,
        sim_after=0.6,
        source_gap=0.25,
        speed_ratio_n=1.0,
        speed_ratio_n1=1.175,
        max_gap=0.5,
    )
    assert abs(score - 0.175) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test -k test_scene_merger_pair_score -v`
Expected: FAIL on `AttributeError: type object 'SceneMergerService' has no attribute '_pair_score'`.

- [ ] **Step 3: Add the method**

In `backend/app/services/scene_merger.py`, add a class-level constant and the static method:

```python
# Add near the existing class constants (around line 17-37):
SLOPE_CONSISTENCY_TOL = 0.35
```

```python
# Add as a @staticmethod on SceneMergerService:
@staticmethod
def _pair_score(
    *,
    sim_before: float,
    sim_after: float,
    source_gap: float,
    speed_ratio_n: float,
    speed_ratio_n1: float,
    max_gap: float,
) -> float:
    """Compute the pair continuity score per §4.3 of the spec.

    Returns a value in [0, 1].
    """
    if max_gap <= 0:
        gap_weight = 1.0 if source_gap <= 0 else 0.0
    else:
        positive_gap = max(source_gap, 0.0)
        gap_weight = 1.0 - min(positive_gap / max_gap, 1.0)

    slope_delta = abs(speed_ratio_n - speed_ratio_n1)
    slope_consistency = 1.0 - min(
        slope_delta / SceneMergerService.SLOPE_CONSISTENCY_TOL,
        1.0,
    )

    similarity_weight = (sim_before + sim_after) / 2.0
    return similarity_weight * gap_weight * slope_consistency
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run test -k test_scene_merger_pair_score -v`
Expected: PASS for all 5 tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scene_merger.py backend/tests/test_scene_merger_pair_score.py
git commit -m "$(cat <<'EOF'
feat(merger): add _pair_score helper for new continuity scoring

Score combines TikTok cut-pair similarity, source-time gap weight, and
speed-ratio consistency between the two scenes (§4.3 of the design spec).
Returns [0, 1] for compatibility with the existing chain interval-scheduling
selector.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Replace `detect_continuous_pairs` body and `build_merge_chains` scoring

**Files:**
- Modify: `backend/app/services/scene_merger.py`

This is an integration change touching the existing pair-detection flow. The new flow uses the cut-pair verifier (Task 6) and the pair score (Task 7).

- [ ] **Step 1: Add imports**

At the top of `backend/app/services/scene_merger.py`, add:

```python
from .cut_pair_verifier import (
    CUT_PAIR_EPSILONS,
    CUT_PAIR_MIN_SIMILARITY,
    CutPairResult,
    _verify_cut_pair,
)
```

- [ ] **Step 2: Replace `detect_continuous_pairs`**

The new method takes the additional arguments needed for cut-pair verification (video path, source FPS, and the embedder/searcher accessors) and returns `(scene_idx_n, scene_idx_n1, pair_score)` tuples — letting `build_merge_chains` use the score directly.

Replace the existing `detect_continuous_pairs` method body:

```python
@classmethod
def detect_continuous_pairs(
    cls,
    scenes: SceneList,
    matches: MatchList,
    *,
    index_fps: float | None = None,
    video_path: Path | None = None,
    extractor: Callable | None = None,
    searcher: Callable | None = None,
    source_fps_lookup: Callable | None = None,
) -> list[tuple[int, int, float]]:
    """Find adjacent scene pairs that are continuous in the anime source.

    Each returned tuple is `(scene_index, scene_index + 1, pair_score)`.
    `extractor`, `searcher`, and `source_fps_lookup` are injected so this
    function can run without loading anime_searcher / OpenCV in tests.

    Tier 1 (trajectory consistency) is checked from the Pass 1 matches.
    Tier 2 (cut-pair verification) calls `_verify_cut_pair` per pair.
    """
    pairs: list[tuple[int, int, float]] = []
    gap_tolerance = cls._continuity_gap_tolerance(index_fps)
    eps_idx_fps = index_fps or 2.0

    for n in range(len(scenes.scenes) - 1):
        match_n = matches.matches[n] if n < len(matches.matches) else None
        match_n1 = matches.matches[n + 1] if (n + 1) < len(matches.matches) else None

        if not match_n or not match_n1:
            continue
        if not match_n.episode or not match_n1.episode:
            continue
        if match_n.episode != match_n1.episode:
            continue

        # Tier 1
        source_gap = match_n1.start_time - match_n.end_time
        if not (-cls.CONTINUITY_EPSILON <= source_gap <= gap_tolerance):
            continue
        slope_delta = abs(match_n.speed_ratio - match_n1.speed_ratio)
        if slope_delta > cls.SLOPE_CONSISTENCY_TOL:
            continue

        # Tier 2
        if extractor is None or searcher is None:
            # Without injected dependencies, accept Tier 1 alone with a
            # neutral score. Production callers must inject these.
            pairs.append(
                (
                    n,
                    n + 1,
                    cls._pair_score(
                        sim_before=match_n.confidence,
                        sim_after=match_n1.confidence,
                        source_gap=source_gap,
                        speed_ratio_n=match_n.speed_ratio,
                        speed_ratio_n1=match_n1.speed_ratio,
                        max_gap=max(0.5, 1.1 / eps_idx_fps),
                    ),
                )
            )
            continue

        scene_n = scenes.scenes[n]
        scene_n1 = scenes.scenes[n + 1]
        source_fps = source_fps_lookup(match_n.episode) if source_fps_lookup else 24.0
        cut_pair: CutPairResult = _verify_cut_pair(
            scene_n_end=scene_n.end_time,
            scene_n1_start=scene_n1.start_time,
            episode=match_n.episode,
            extractor=extractor,
            searcher=searcher,
            source_fps=source_fps,
            index_fps=eps_idx_fps,
        )
        if not cut_pair.passed:
            continue

        score = cls._pair_score(
            sim_before=cut_pair.sim_before,
            sim_after=cut_pair.sim_after,
            source_gap=source_gap,
            speed_ratio_n=match_n.speed_ratio,
            speed_ratio_n1=match_n1.speed_ratio,
            max_gap=max(0.5, 1.1 / eps_idx_fps),
        )
        pairs.append((n, n + 1, score))

    return pairs
```

Add at the top of the file (alongside other imports):

```python
from pathlib import Path
from typing import Callable
```

- [ ] **Step 3: Replace `build_merge_chains`**

Update `build_merge_chains` to receive the new tuple shape and skip the candidate-aggregation logic. The chain builder no longer calls `_get_*_candidates` or `_get_best_pair_continuity`. Stitching is removed.

```python
@classmethod
def build_merge_chains(
    cls,
    pairs: list[tuple[int, int, float]],
    scenes: SceneList,
    matches: MatchList,
    *,
    index_fps: float | None = None,
) -> list[list[int]]:
    """Build transitive merge chains from continuity-scored pairs.

    `pairs` is the list returned by `detect_continuous_pairs` —
    `(scene_idx_n, scene_idx_n1, pair_score)`. Same-episode contiguous
    chains are formed greedily; the highest-scoring non-overlapping subset
    is selected via weighted interval scheduling.
    """
    if not pairs:
        return []

    pair_continuity: dict[int, tuple[str, float]] = {}
    for n, n1, score in pairs:
        if n1 != n + 1:
            continue
        if n >= len(matches.matches):
            continue
        episode = matches.matches[n].episode
        if not episode:
            continue
        pair_continuity[n] = (episode, score)

    if not pair_continuity:
        return []

    chain_candidates = cls._build_chain_candidates(pair_continuity)
    return cls._select_non_overlapping_chains(chain_candidates)
```

- [ ] **Step 4: Remove dead code**

Delete from `backend/app/services/scene_merger.py`:

- `_get_end_candidates` (~lines 154-203)
- `_get_start_candidates` (~lines 205-255)
- `_get_best_pair_continuity` (~lines 257-323)
- `_dedupe_candidates` (~lines 134-151)
- `_get_chain_bridge_continuity` (~lines 405-487)
- `_stitch_adjacent_chains` (~lines 489-584)

Also delete the obsolete class constants:

- `MIN_PAIR_CONTINUITY_SCORE`
- `MIN_EPISODE_SUPPORT`
- `MIN_ALT_CONFIDENCE`
- `MIN_RAW_CANDIDATE_CONFIDENCE`
- `CANDIDATE_TIME_ROUNDING`
- `CHAIN_BRIDGE_WINDOW`
- `CHAIN_BRIDGE_GAP_TOLERANCE`
- `CHAIN_BRIDGE_MIN_SCORE`
- `CHAIN_BRIDGE_STRONG_SCORE`
- `CHAIN_BRIDGE_MIN_SUPPORT`

Also delete the `_normalize_confidence` helper (~lines 129-132) — it was only used by `_get_*_candidates`.

Also delete the `_get_scene_half_duration` helper (~lines 55-71) — it was used only by the dropped middle-frame projection logic.

- [ ] **Step 5: Verify the existing tests still pass**

Run: `pixi run test`
Expected: PASS, no regressions. (The test suite has no scene_merger integration tests; existing tests cover other services.)

If any test fails because it imported a removed symbol, update the test to use the new API.

- [ ] **Step 6: Quick syntax check**

Run: `pixi run python -c "from app.services.scene_merger import SceneMergerService; print(SceneMergerService.detect_continuous_pairs)"` from `backend/`.
Expected: prints the method without ImportError.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/scene_merger.py
git commit -m "$(cat <<'EOF'
refactor(merger): two-tier continuity merger using cut-pair verifier

detect_continuous_pairs now returns (n, n+1, pair_score) tuples and runs
the §4.1 trajectory-consistency check followed by §4.2 cut-pair frame
verification with three retry epsilons. build_merge_chains drops the
candidate-aggregation, alternative-vote, and chain-stitching logic; chain
construction reuses the existing _build_chain_candidates +
_select_non_overlapping_chains from before.

Removes dead code that fed the old continuity scoring (alternative-vote
fallback, raw-top-K aggregation, middle-frame projection, chain bridging)
plus their constants.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Wire matching route — Pass 2 priors and merger dependencies

**Files:**
- Modify: `backend/app/api/routes/matching.py`

This task builds the `pass2_priors` dict at the route layer and injects the cut-pair verifier dependencies into `detect_continuous_pairs`.

- [ ] **Step 1: Read the existing Pass 2 call site**

Read `backend/app/api/routes/matching.py:411-490`. The relevant section calls `SceneMergerService.detect_continuous_pairs`, `SceneMergerService.build_merge_chains`, `SceneMergerService.merge_scenes_and_matches`, then re-runs `AnimeMatcherService.match_scenes` for the merged scenes.

- [ ] **Step 2: Build extractor / searcher / source_fps_lookup callables**

Add helper construction immediately before the call to `detect_continuous_pairs` (replacing lines 411-414):

```python
from ..services.cut_pair_verifier import CutPairResult  # noqa: F401  (for typing)
from ..services.trajectory_fit import Pass2Prior

index_fps = AnimeMatcherService.get_index_fps()

def _extractor(tiktok_t: float):
    return AnimeMatcherService.extract_frame(video_path, tiktok_t)

def _searcher(frame, episode_filter: str):
    if frame is None:
        return []
    # Reuse the singleton-loaded query processor's embedder + index
    # (already loaded by Pass 1). One-frame batch.
    results_per_image = AnimeMatcherService._search_image_batch(
        [frame],
        top_n=10,
        threshold=None,
        flip=False,
        series=anime_name,
    )
    if not results_per_image:
        return []
    # Filter to episode after retrieval (anime_searcher series filter
    # narrows to a series, not a single episode).
    return [r for r in results_per_image[0] if r.episode == episode_filter]

def _source_fps_lookup(episode: str) -> float:
    from ..services.anime_library import AnimeLibraryService

    path = AnimeLibraryService.resolve_episode_path(
        episode, library_type=project.library_type
    )
    if path is None or not path.exists():
        return 24.0
    fps = AnimeMatcherService._get_video_fps(path)
    return fps if fps and fps > 0 else 24.0

pairs = SceneMergerService.detect_continuous_pairs(
    scenes,
    first_pass_matches,
    index_fps=index_fps,
    video_path=video_path,
    extractor=_extractor,
    searcher=_searcher,
    source_fps_lookup=_source_fps_lookup,
)
```

- [ ] **Step 3: Build `pass2_priors` after `merge_scenes_and_matches`**

Replace the existing Pass 2 call (around lines 460-466) with one that constructs and passes `pass2_priors`:

```python
# Build per-merged-scene priors from the Pass 1 source endpoints.
pass2_priors: dict[int, Pass2Prior] = {}
for merged_idx, merged_match in enumerate(merged_matches.matches):
    if not merged_match.merged_from:
        continue
    pre_merge = [
        first_pass_matches.matches[i]
        for i in merged_match.merged_from
        if i < len(first_pass_matches.matches)
    ]
    if not pre_merge:
        continue
    episodes = {m.episode for m in pre_merge if m.episode}
    if len(episodes) != 1:
        # Should not happen per Tier 1, but defend anyway.
        continue
    (episode,) = episodes
    source_lo = min(m.start_time for m in pre_merge)
    source_hi = max(m.end_time for m in pre_merge)
    pass2_priors[merged_idx] = Pass2Prior(
        episode=episode,
        source_lo=source_lo,
        source_hi=source_hi,
    )

pass2_matches: MatchList | None = None
async for progress in AnimeMatcherService.match_scenes(
    video_path, merged_scenes, source_path,
    project.library_type,
    anime_name=anime_name,
    scene_indices_to_match=merged_indices,
    existing_matches=merged_matches,
    pass_label="Pass 2: ",
    pass2_priors=pass2_priors,
):
    if progress.status == "complete" and progress.matches:
        # Preserve merged_from metadata on re-matched scenes.
        for i in merged_indices:
            if i < len(progress.matches.matches) and i < len(merged_matches.matches):
                progress.matches.matches[i].merged_from = (
                    merged_matches.matches[i].merged_from
                )
        pass2_matches = progress.matches
        continue

    yield f"data: {json.dumps(progress.to_dict())}\n\n"
    if progress.status == "error":
        ProjectService.save_matches(project_id, merged_matches)
        project.phase = ProjectPhase.MATCH_VALIDATION
        ProjectService.save(project)
        return
```

- [ ] **Step 4: Update the manual-merge call site (`prepare_manual_merge_with_previous`) too**

Read around lines 815-840 — the manual-merge endpoint calls `match_scenes` for the merged scene to re-fetch a primary. It currently does NOT pass `pass2_priors`. Build a single-entry dict for the manually merged scene index:

```python
# Just before the AnimeMatcherService.match_scenes call inside the manual
# merge endpoint, construct the prior:
merged_match_obj = merged_matches.matches[merged_scene_index]
pass2_priors_single: dict[int, Pass2Prior] = {}
if merged_match_obj.merged_from:
    pre_merge = []
    for orig_idx in merged_match_obj.merged_from:
        if orig_idx < len(backup.get("matches", [])):
            pre_merge.append(backup["matches"][orig_idx])
    if pre_merge:
        episodes = {m.get("episode", "") for m in pre_merge if m.get("episode")}
        if len(episodes) == 1:
            (episode,) = episodes
            pass2_priors_single[merged_scene_index] = Pass2Prior(
                episode=episode,
                source_lo=min(m.get("start_time", 0.0) for m in pre_merge),
                source_hi=max(m.get("end_time", 0.0) for m in pre_merge),
            )

# Then pass `pass2_priors=pass2_priors_single` into the match_scenes call.
```

- [ ] **Step 5: Run the test suite**

Run: `pixi run test`
Expected: PASS for all existing tests.

- [ ] **Step 6: Quick smoke test by importing the route**

Run: `pixi run python -c "from app.api.routes.matching import router; print(router)"` from `backend/`.
Expected: prints the FastAPI router without import errors.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/routes/matching.py
git commit -m "$(cat <<'EOF'
feat(matching-route): wire cut-pair deps and Pass 2 priors

Inject the extractor / searcher / source_fps_lookup callables into
detect_continuous_pairs so the cut-pair verifier can run without circular
imports. Build pass2_priors (episode + source_lo/source_hi) from Pass 1
matches before re-running match_scenes on merged scenes. Same prior is
constructed for the manual-merge re-match path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Remove obsolete `_compute_alternatives` middle-projection logic

**Files:**
- Modify: `backend/app/services/anime_matcher.py`

`_compute_alternatives` is preserved (frontend reads it), but the spec drops middle-frame projection from the merger evidence. Verify the alternatives surface still works correctly with the new probe layout (mid-probe is now arbitrary among 5–7 probes).

- [ ] **Step 1: Verify the alternatives output shape**

Read `backend/app/services/anime_matcher.py:510-752`. Confirm `_compute_alternatives` reads `start_candidates`, `middle_candidates`, `end_candidates` lists. Task 5 wired these as `per_probe_candidates[0]`, `per_probe_candidates[len/2]`, `per_probe_candidates[-1]` — the spec keeps this contract.

No structural change is needed if `_compute_alternatives` works on the per-probe top-25 lists trimmed to top-5. Confirm by reading the function.

- [ ] **Step 2: If the function expects exactly 3 probe positions, no change**

`_compute_alternatives` already loops over `('start', 'middle', 'end')` positions and treats each as an independent candidate list. The new code passes top-5 slices of the per-probe lists for the 0th, mid, and last probes — same shape. No code change required.

- [ ] **Step 3: Add a sanity test**

Append to `backend/tests/test_trajectory_fit.py`:

```python
def test_alternatives_compute_does_not_crash_with_empty_lists():
    # Imports are resolved here so the test only runs once anime_matcher is loadable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.services.anime_matcher import AnimeMatcherService

    alts = AnimeMatcherService._compute_alternatives([], [], [], scene_duration=2.0)
    assert alts == []
```

- [ ] **Step 4: Run test**

Run: `pixi run test -k test_alternatives_compute_does_not_crash_with_empty_lists -v`
Expected: PASS.

- [ ] **Step 5: Commit (only if test was added)**

```bash
git add backend/tests/test_trajectory_fit.py
git commit -m "$(cat <<'EOF'
test(matcher): smoke-test _compute_alternatives with empty inputs

Confirms the alternatives helper still tolerates empty per-probe candidate
lists after the trajectory-fit rewiring. The function is unchanged but the
new probe layout means it can be called with empty lists when fits fail.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: End-to-end validation script

**Files:**
- Create: `backend/scripts/validate_matching_against_ground_truth.py`

A self-contained script that runs the new pipeline on a fresh project id (cloning a TikTok / series_id from a ground-truth project) and reports the metrics from §10 of the spec.

- [ ] **Step 1: Write the script**

```python
# backend/scripts/validate_matching_against_ground_truth.py
"""Validate the new matching pipeline against a ground-truth project.

Usage:
    pixi run python -m backend.scripts.validate_matching_against_ground_truth \\
        --ground-truth dcd74148c7ec
    pixi run python -m backend.scripts.validate_matching_against_ground_truth \\
        --ground-truth 85de83ca6323

The script:
1. Loads the ground-truth project's tiktok.mp4 + project.json metadata.
2. Creates a fresh project id, copies the inputs, runs the full pipeline:
     /scenes (existing detector) -> /matches (new pipeline).
3. Reads the resulting scenes.json + matches.json.
4. Compares per scene against the ground-truth scenes.json + matches.json
   using the §10 metrics:
     - Episode accuracy
     - Source-side boundary accuracy (±0.3s)
     - Chain composition F1 over merged_from groups
     - Over-merge severity
5. Prints a markdown-formatted report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import MatchList, SceneList
from app.services.anime_library import AnimeLibraryService
from app.services.anime_matcher import AnimeMatcherService
from app.services.project_service import ProjectService
from app.services.scene_detector import SceneDetectorService
from app.services.scene_merger import SceneMergerService
from app.services.trajectory_fit import Pass2Prior


@dataclass
class ProjectComparison:
    project_id: str
    ground_truth_id: str
    scene_count_gt: int
    scene_count_new: int
    episode_correct: int
    source_boundaries_correct: int
    over_merge_pairs: int
    chain_f1: float


async def run_pipeline(project_id: str, video_path: Path, anime_name: str, library_type: str):
    """Run scene detection + matching + merge end-to-end."""
    library_path = AnimeLibraryService.get_library_path(library_type)

    # Scene detection.
    scene_list: SceneList | None = None
    async for progress in SceneDetectorService.detect_scenes(
        video_path,
        library_path=library_path,
        library_type=library_type,
        anime_name=anime_name,
    ):
        if progress.status == "complete" and progress.scenes:
            scene_list = SceneList(scenes=progress.scenes)
    assert scene_list is not None
    ProjectService.save_scenes(project_id, scene_list)

    # Pass 1.
    first_pass: MatchList | None = None
    async for progress in AnimeMatcherService.match_scenes(
        video_path, scene_list, library_path, library_type, anime_name=anime_name
    ):
        if progress.status == "complete" and progress.matches:
            first_pass = progress.matches
    assert first_pass is not None

    # Build cut-pair callables.
    def extractor(t: float):
        return AnimeMatcherService.extract_frame(video_path, t)

    def searcher(frame, episode_filter: str):
        if frame is None:
            return []
        results_per_image = AnimeMatcherService._search_image_batch(
            [frame], top_n=10, threshold=None, flip=False, series=anime_name
        )
        if not results_per_image:
            return []
        return [r for r in results_per_image[0] if r.episode == episode_filter]

    def fps_lookup(episode: str) -> float:
        path = AnimeLibraryService.resolve_episode_path(episode, library_type=library_type)
        if path is None or not path.exists():
            return 24.0
        return AnimeMatcherService._get_video_fps(path) or 24.0

    pairs = SceneMergerService.detect_continuous_pairs(
        scene_list,
        first_pass,
        index_fps=AnimeMatcherService.get_index_fps(),
        video_path=video_path,
        extractor=extractor,
        searcher=searcher,
        source_fps_lookup=fps_lookup,
    )
    chains = SceneMergerService.build_merge_chains(
        pairs, scene_list, first_pass, index_fps=AnimeMatcherService.get_index_fps()
    )

    if not chains:
        ProjectService.save_matches(project_id, first_pass)
        return scene_list, first_pass

    merged_scenes, merged_matches, _backup = SceneMergerService.merge_scenes_and_matches(
        scene_list, first_pass, chains
    )

    # Pass 2.
    pass2_priors: dict[int, Pass2Prior] = {}
    for merged_idx, mm in enumerate(merged_matches.matches):
        if not mm.merged_from:
            continue
        pre = [first_pass.matches[i] for i in mm.merged_from if i < len(first_pass.matches)]
        if not pre:
            continue
        episodes = {m.episode for m in pre if m.episode}
        if len(episodes) != 1:
            continue
        (episode,) = episodes
        pass2_priors[merged_idx] = Pass2Prior(
            episode=episode,
            source_lo=min(m.start_time for m in pre),
            source_hi=max(m.end_time for m in pre),
        )
    merged_indices = [i for i, m in enumerate(merged_matches.matches) if m.merged_from]

    pass2: MatchList | None = None
    async for progress in AnimeMatcherService.match_scenes(
        video_path,
        merged_scenes,
        library_path,
        library_type,
        anime_name=anime_name,
        scene_indices_to_match=merged_indices,
        existing_matches=merged_matches,
        pass2_priors=pass2_priors,
    ):
        if progress.status == "complete" and progress.matches:
            for i in merged_indices:
                if i < len(progress.matches.matches) and i < len(merged_matches.matches):
                    progress.matches.matches[i].merged_from = merged_matches.matches[
                        i
                    ].merged_from
            pass2 = progress.matches

    final = pass2 or merged_matches
    ProjectService.save_matches(project_id, final)
    return merged_scenes, final


def _scene_signature(scene_dict, match_dict):
    return {
        "tiktok_start": round(scene_dict["start_time"], 3),
        "tiktok_end": round(scene_dict["end_time"], 3),
        "episode": match_dict.get("episode", ""),
        "source_start": round(match_dict.get("start_time", 0.0), 3),
        "source_end": round(match_dict.get("end_time", 0.0), 3),
        "merged_from": match_dict.get("merged_from"),
    }


def compare(gt_dir: Path, new_dir: Path, ground_truth_id: str, project_id: str) -> ProjectComparison:
    gt_scenes = json.load((gt_dir / "scenes.json").open())["scenes"]
    gt_matches = json.load((gt_dir / "matches.json").open())["matches"]
    new_scenes = json.load((new_dir / "scenes.json").open())["scenes"]
    new_matches = json.load((new_dir / "matches.json").open())["matches"]

    # Align by overlap on TikTok-time.
    aligned: list[tuple[dict, dict, dict, dict]] = []
    for gs, gm in zip(gt_scenes, gt_matches):
        center = (gs["start_time"] + gs["end_time"]) / 2.0
        match_idx = None
        for i, ns in enumerate(new_scenes):
            if ns["start_time"] <= center <= ns["end_time"]:
                match_idx = i
                break
        if match_idx is None:
            continue
        aligned.append((gs, gm, new_scenes[match_idx], new_matches[match_idx]))

    episode_correct = sum(
        1 for _, gm, _, nm in aligned if (gm.get("episode") or "") == (nm.get("episode") or "")
    )
    source_correct = sum(
        1
        for _, gm, _, nm in aligned
        if abs(gm.get("start_time", 0) - nm.get("start_time", 0)) <= 0.3
        and abs(gm.get("end_time", 0) - nm.get("end_time", 0)) <= 0.3
    )

    # Chain F1: treat merged_from as the chain set.
    def chain_set(matches):
        return {tuple(m["merged_from"]) for m in matches if m.get("merged_from")}

    gt_chains = chain_set(gt_matches)
    new_chains = chain_set(new_matches)
    if gt_chains or new_chains:
        tp = len(gt_chains & new_chains)
        precision = tp / len(new_chains) if new_chains else 0.0
        recall = tp / len(gt_chains) if gt_chains else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    else:
        f1 = 1.0

    # Over-merge: pairs (i, j) in new same chain but in different GT chains.
    def chain_membership(matches):
        membership: dict[int, tuple[int, ...]] = {}
        for m in matches:
            indices = m.get("merged_from") or [m.get("scene_index", -1)]
            key = tuple(sorted(indices))
            for idx in indices:
                membership[idx] = key
        return membership

    gt_membership = chain_membership(gt_matches)
    new_membership = chain_membership(new_matches)
    over_merge = 0
    for idx_a, gkey_a in gt_membership.items():
        for idx_b, gkey_b in gt_membership.items():
            if idx_a >= idx_b:
                continue
            if gkey_a == gkey_b:
                continue
            new_a = new_membership.get(idx_a)
            new_b = new_membership.get(idx_b)
            if new_a is None or new_b is None:
                continue
            if new_a == new_b:
                over_merge += 1

    return ProjectComparison(
        project_id=project_id,
        ground_truth_id=ground_truth_id,
        scene_count_gt=len(gt_scenes),
        scene_count_new=len(new_scenes),
        episode_correct=episode_correct,
        source_boundaries_correct=source_correct,
        over_merge_pairs=over_merge,
        chain_f1=f1,
    )


def report(cmp: ProjectComparison) -> str:
    n = max(cmp.scene_count_gt, 1)
    return (
        f"\n## Validation: GT={cmp.ground_truth_id} → run={cmp.project_id}\n"
        f"\n- Scene count: GT={cmp.scene_count_gt}, new={cmp.scene_count_new}"
        f"\n- Episode accuracy: {cmp.episode_correct}/{n} = {cmp.episode_correct / n:.1%}"
        f"\n- Source-boundary ±0.3s accuracy: {cmp.source_boundaries_correct}/{n} = "
        f"{cmp.source_boundaries_correct / n:.1%}"
        f"\n- Chain composition F1: {cmp.chain_f1:.2f}"
        f"\n- Over-merge pairs (different GT chains, same new chain): {cmp.over_merge_pairs}"
    )


async def main_async(ground_truth_id: str) -> None:
    gt_dir = settings.projects_path / ground_truth_id
    gt_project = json.load((gt_dir / "project.json").open())

    # Create fresh project.
    new_id = uuid.uuid4().hex[:12]
    new_dir = settings.projects_path / new_id
    new_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(gt_dir / "tiktok.mp4", new_dir / "tiktok.mp4")
    project_payload = {
        **gt_project,
        "id": new_id,
        "phase": "scene_detection",
        "video_path": str(new_dir / "tiktok.mp4"),
    }
    (new_dir / "project.json").write_text(json.dumps(project_payload, indent=2))

    print(f"Running pipeline for fresh project {new_id} from GT {ground_truth_id}")
    await run_pipeline(
        project_id=new_id,
        video_path=Path(project_payload["video_path"]),
        anime_name=project_payload["anime_name"],
        library_type=project_payload["library_type"],
    )

    cmp = compare(gt_dir, new_dir, ground_truth_id, new_id)
    print(report(cmp))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--ground-truth",
        required=True,
        help="Ground-truth project id under backend/data/projects/.",
    )
    args = p.parse_args()
    asyncio.run(main_async(args.ground_truth))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script against `dcd74148c7ec`**

Run from repo root:
```
pixi run python backend/scripts/validate_matching_against_ground_truth.py \
    --ground-truth dcd74148c7ec
```

Expected: a markdown-formatted report block. The metrics targets per spec §10.3:
- Episode accuracy ≥ 80%
- Source-boundary ±0.3s accuracy ≥ 75%
- Chain F1 ≥ 0.70
- Over-merge pairs spanning ≥ 4 originally-distinct scenes = 0 (this script reports the raw count; eyeball that no single new chain bridges across more than 3 GT chains by inspecting the JSON.)

Capture the report output. If metrics are below target, tune constants and re-run (no commit needed for tuning iterations).

- [ ] **Step 3: Run the script against `85de83ca6323`**

Run:
```
pixi run python backend/scripts/validate_matching_against_ground_truth.py \
    --ground-truth 85de83ca6323
```

Same metric targets.

- [ ] **Step 4: Commit the script**

```bash
git add backend/scripts/validate_matching_against_ground_truth.py
git commit -m "$(cat <<'EOF'
test(matcher): add ground-truth validation script for /matches

Runs the full pipeline on a fresh project cloned from a ground-truth
project's TikTok and reports episode accuracy, ±0.3s source boundary
accuracy, chain composition F1, and over-merge pairs against the
hand-curated reference. Used to validate the §10 metrics from the
matching-precision-rework design.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Tune constants if metrics fall short, then final report

**Files:**
- Modify (if needed): `backend/app/services/trajectory_fit.py`,
  `backend/app/services/cut_pair_verifier.py`,
  `backend/app/services/scene_merger.py`

This is a tuning task — there is no fixed implementation. Use the validation script from Task 11 to inform changes.

- [ ] **Step 1: Re-run validation against both ground-truth projects**

Already done in Task 11. Compare metrics to the §10.3 targets:
- Episode accuracy ≥ 80%
- Source-boundary ±0.3s accuracy ≥ 75%
- Chain F1 ≥ 0.70
- 0 over-merges spanning ≥ 4 originally-distinct GT scenes

- [ ] **Step 2: If metrics meet targets, skip to Step 6**

If both projects pass, no tuning is needed.

- [ ] **Step 3: If episode accuracy is low, suspect the slope penalty / RANSAC tolerance**

Possible knobs:
- Loosen `RANSAC_INLIER_TOL_SECONDS` (currently 0.6) to 0.8 if too many true fits fail to find ≥3 inliers.
- Tighten `SLOPE_PENALTY_FREE_RANGE` (currently `(0.65, 1.60)`) if extreme slopes are creeping in.

Commit each tuning change separately with a message describing what metric improved.

- [ ] **Step 4: If chain F1 is low / over-merge is high, suspect cut-pair similarity threshold**

Possible knobs:
- Raise `CUT_PAIR_MIN_SIMILARITY` (currently 0.40) to 0.50 to demand stronger frame agreement.
- Tighten `CUT_PAIR_MAX_FRAME_GAP_FACTOR` (currently 1.0 source frame) to 0.5.

- [ ] **Step 5: If under-merging is high, check Tier 1 gate**

- Loosen `SLOPE_CONSISTENCY_TOL` (currently 0.35) to 0.45 if real continuities are getting rejected for slope drift.
- Loosen `CONTINUITY_GAP_TOLERANCE` (currently 0.30s + index_step) to 0.5s.

- [ ] **Step 6: Write a final report**

Append the validation reports for both ground-truth projects to a new file:

```bash
cat > docs/superpowers/specs/2026-04-27-matching-precision-rework-validation.md <<'EOF'
# Matching precision rework — Validation report

Reports from running `backend/scripts/validate_matching_against_ground_truth.py`
against the two ground-truth projects from the design.

[Paste the two report blocks from Task 11 / Task 12 here.]
EOF
```

- [ ] **Step 7: Commit the validation report**

```bash
git add docs/superpowers/specs/2026-04-27-matching-precision-rework-validation.md
git commit -m "$(cat <<'EOF'
docs(specs): record matching precision rework validation results

Captures the metrics from running the new pipeline against the two
ground-truth projects (dcd74148c7ec and 85de83ca6323).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Definition of done

- All 12 tasks completed and committed.
- `pixi run test` passes from a clean checkout.
- Validation against both ground-truth projects meets the §10.3 targets, OR a deliberate exception is documented in `2026-04-27-matching-precision-rework-validation.md`.
- The frontend is unchanged and continues to render `start_candidates` / `middle_candidates` / `end_candidates` / `alternatives` per the existing schema.
- `_find_temporal_match`, `_stitch_adjacent_chains`, `_get_chain_bridge_continuity`, `_get_end_candidates`, `_get_start_candidates`, `_get_best_pair_continuity`, `_dedupe_candidates`, `_normalize_confidence`, `_get_scene_half_duration` are all removed.
