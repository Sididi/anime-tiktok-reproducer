# Matching precision rework — Hybrid A + B-fallback (alternative)

**Status:** Alternative design (not selected as main). Provided for the
user to evaluate / implement later if Approach A's accuracy is mostly
good but a tail of hard scenes still produces wrong-shot matches that
A can't catch.

**Sibling docs:**
- `2026-04-27-matching-precision-rework-design.md` — Approach A, main.
- `2026-04-27-matching-precision-rework-approach-b.md` — Approach B
  (full source-window re-ranking).

---

## 1. Concept

Approach A is fast and fixes most failure modes, but on inherently
ambiguous scenes (very short, static talking-head, recycled animation
that defeats line-fit because most candidates land on a near-flat
trajectory) the trajectory fit produces low confidence — and committing
to the top-scoring fit may still be wrong.

The hybrid keeps Approach A as the default fast path and *only* invokes
Approach B's expensive source-window re-ranking when A's fit is weak.
Most scenes pay A's cost; only the hard ~5–15% pay B's cost.

This usually keeps the per-project budget close to A's while recovering
the bulk of A's residual errors.

---

## 2. Architecture

```
                 ┌─────────────────────────┐
                 │   Pass 1 — Approach A   │
                 │  trajectory fit (cheap) │
                 └────────────┬────────────┘
                              │
                              ▼
                ┌──────────────────────────┐
                │  fit confidence check    │
                │  (see §3 weak-fit flag)  │
                └────┬─────────────────┬───┘
                     │                 │
              strong │                 │ weak
                     ▼                 ▼
        ┌──────────────────┐   ┌───────────────────────────┐
        │ commit A's match │   │ Approach-B fallback       │
        │ (no extra cost)  │   │ (decode top-3 hypothesis  │
        └──────────────────┘   │  windows, verify densely) │
                               └────┬──────────────────────┘
                                    │
                                    ▼
                       commit hybrid pick (B's)
```

The continuity merger uses A's cut-pair verifier by default, but escalates
to B's joint-window verifier when cut-pair fails on borderline pairs
(see §5).

---

## 3. Weak-fit detection

After Approach A's RANSAC fit produces `TrajectoryFit`, declare a fit
"weak" if any of:

- `inlier_count < max(3, ceil(0.6 × probe_count))` — fewer than 60% of
  probes landed on the line.
- `rmse > 0.45` (in source seconds) — the inliers themselves spread
  significantly. Compare to `RANSAC_INLIER_TOL_SECONDS = 0.6`; an RMSE
  above 75% of tolerance is borderline.
- `avg_inlier_similarity < 0.45` — even the line-fitting candidates
  don't match well.
- `top-2 episodes have scores within 15% of each other` — close
  competition between hypotheses; B's dense verification is needed
  to disambiguate.
- `slope_penalty < 0.7` — extreme speed implied; B can confirm.

Any one trigger → weak fit. A scene flagged weak runs through B's
fallback (next section).

---

## 4. B-style fallback per weak scene

For each weak scene:

1. Take the **top-3 hypotheses** from A's per-episode trajectory fits
   (not just the global best). Each hypothesis gives an episode + a
   coarse source window (`fit.intercept + fit.slope * scene.start_time`
   to `+ fit.slope * scene.end_time`, padded by 0.5s on each side).
2. For each hypothesis:
   - Decode + embed source frames at native FPS in the window
     (cap at 6s wide → ~150 frames).
   - Use the same TikTok probes already extracted by A.
   - Run dense NN search and a tight RANSAC fit
     (`RANSAC_INLIER_TOL_SECONDS = 0.15`).
3. Pick the hypothesis with the best dense-fit score.

If the dense-fit is also weak (below the same triggers in §3 evaluated
on dense observations), commit to whichever hypothesis has the highest
combined `(A_fit_score + B_fit_score) / 2` — never bail to no-match.

Cost on hard scenes: ~2–4s extra, comparable to a single Approach B
verification pass. Easy scenes pay zero extra.

---

## 5. Continuity merger fallback

Default: Approach A's two-tier check (Tier 1 trajectory consistency +
Tier 2 cut-pair verification, §4 of the main doc).

Escalate to Approach B's joint-window verifier when:

- Tier 1 passes (same episode, source-gap and slopes agree) but Tier 2
  fails on **all** ε-retries (`0.05, 0.10, 0.20`).
- AND at least one side of the pair was flagged weak in §3.

In that case, decode the union window covering both scenes (padded ±1s),
re-embed, and run B's joint-fit verifier (B §5). If the joint fit
strongly supports continuity (≥ 80% inliers, RMSE ≤ 0.15), accept the
merge; otherwise drop the pair.

Cost: at most a handful of joint-window decodes per project. If the
single-scene fallback already decoded one of the involved windows, the
cache (B §7.1) reuses it.

---

## 6. Code organization

### 6.1 New / changed modules

- `backend/app/services/source_window_cache.py` — same as Approach B §7.1.
- `backend/app/services/anime_matcher.py` — Approach A's structure
  plus:
  - `_is_weak_fit(fit, probe_count) -> bool` — §3 triggers.
  - `_b_fallback_verify(scene, hypotheses, video_path, library_type) ->
    TrajectoryFit` — §4.
  - `_top_n_hypotheses(per_probe_candidates, n=3) -> list[TrajectoryFit]`
    — surfaces the per-episode trajectory fits as hypothesis candidates
    for the fallback.
- `backend/app/services/scene_merger.py` — Approach A's merger plus:
  - `_b_fallback_joint_verify(...)` — §5 joint-window check.
  - Hooks into `detect_continuous_pairs` to trigger fallback only on the
    weak-flagged border cases.

### 6.2 Sequence (Pass 1)

```python
fits = trajectory_fit_per_episode(per_probe_candidates)
best = max(fits, key=score)

if _is_weak_fit(best, probe_count):
    hypotheses = _top_n_hypotheses(per_probe_candidates, n=3)
    best = _b_fallback_verify(scene, hypotheses, ...)

match = build_match_from_fit(best, scene)
match = refine_boundaries(match)            # only if not from B (B is already native FPS)
```

### 6.3 Sequence (continuity)

```python
tier1 = check_tier1(scene_n, scene_n1)
if not tier1.passed:
    return None

tier2 = verify_cut_pair(scene_n, scene_n1, tier1.episode)
if tier2.passed:
    return Continuity(score=tier2.score, episode=tier1.episode)

# Tier 2 failed. Escalate only on weak-flagged borders.
if scene_n.weak_flag or scene_n1.weak_flag:
    joint = b_fallback_joint_verify(scene_n, scene_n1, tier1.episode)
    if joint.passed:
        return Continuity(score=joint.score, episode=tier1.episode)

return None
```

`weak_flag` is stored on the `SceneMatch` (an internal attribute, not a
public schema field) when `_is_weak_fit` triggered during Pass 1 / Pass 2.

---

## 7. Tunables

A's tunables (main doc §8) plus:

| Constant                              | Value         | Where           |
|---------------------------------------|---------------|------------------|
| `WEAK_INLIER_FRACTION`                | 0.60          | matcher          |
| `WEAK_RMSE_THRESHOLD_SECONDS`         | 0.45          | matcher          |
| `WEAK_AVG_SIMILARITY_FLOOR`           | 0.45          | matcher          |
| `WEAK_TOP2_SCORE_GAP`                 | 0.15          | matcher          |
| `WEAK_SLOPE_PENALTY_THRESHOLD`        | 0.70          | matcher          |
| `B_FALLBACK_HYPOTHESES_PER_SCENE`     | 3             | matcher          |
| `B_FALLBACK_WINDOW_PAD_SECONDS`       | 0.5           | matcher          |
| `B_FALLBACK_MAX_WINDOW_WIDTH`         | 6.0           | matcher          |
| `B_FALLBACK_DENSE_TOL_SECONDS`        | 0.15          | matcher          |
| `MERGER_FALLBACK_JOINT_PAD_SECONDS`   | 1.0           | merger           |
| `MERGER_FALLBACK_INLIER_FLOOR`        | 0.80          | merger           |

---

## 8. Cost analysis

Per-project cost ≈ A's cost + (fraction-of-weak-scenes) × (B per-scene cost).

Example: 50-scene project, 12% weak rate, A at 1s/scene, B at 3s/scene.

```
A baseline: 50s
B fallback: 0.12 × 50 × 3s = 18s
Total:      ~68s
```

vs Approach B's full cost: 50 × 3s = 150s.

The hybrid pays roughly half of B's cost for most of B's accuracy
benefit, assuming the weak-flag is well-calibrated.

---

## 9. Validation plan

Same metrics as Approach A (main doc §10). Targets land between A's
and B's:

- ≥85% scenes with correct episode.
- ≥80% scenes with correct boundaries within ±0.3s.
- ≥75% chain composition F1.
- 0 over-merges spanning ≥3 originally-distinct scenes.

Track separately:
- `weak_fit_rate` — fraction of scenes flagged weak. If >25%, recalibrate
  triggers (§3) — too aggressive; falling back almost everywhere defeats
  the purpose.
- `b_fallback_changed_episode_rate` — fraction of weak scenes where B's
  pick differed from A's. If close to 0%, B isn't earning its keep
  → simplify back to plain A. If high (>30%), B is rescuing real errors
  → consider promoting to full Approach B.

---

## 10. Risks and mitigations

| Risk                                              | Likelihood | Mitigation                                                                  |
|---------------------------------------------------|-----------|-----------------------------------------------------------------------------|
| Weak-fit triggers calibrated wrong                | medium    | Track `weak_fit_rate`; tune §3 thresholds based on validation runs.         |
| B fallback triggers on legit hard scenes that B can't fix either | medium    | B's fallback combines A+B scores; never bails to no-match.                  |
| Source-window cache thrashing across weak scenes  | low       | Cache size 40 windows; weak scenes typically share episodes → high hit rate.|
| Merger-side fallback becomes the dominant path    | low       | Only triggers when both border scenes are flagged AND Tier 2 fails. Rare.   |
| Confidence semantics drift across A and B fits    | low       | Both produce scores in `[0, 1]` with similar shape; UI styling unchanged.   |

---

## 11. Implementation milestones

1. Implement Approach A in full (per its main doc).
2. Add `SourceWindow` cache module (B §7.1).
3. Implement `_is_weak_fit` and instrument metrics on it.
4. Implement `_b_fallback_verify` for Pass 1 weak scenes.
5. Same for Pass 2.
6. Merger fallback (`_b_fallback_joint_verify`).
7. Validation run; tune §3 triggers.
8. Decide: stay hybrid, simplify back to A, or promote to full B.

---

## 12. When to choose the hybrid

- Approach A is shipped and meets ~80% of the goals but a meaningful tail
  of wrong-shot matches remains.
- Per-project compute can grow modestly (e.g. +30–80% over A) but full B
  is still too slow.
- Source IO is fast enough that occasional window decodes don't block
  user-visible progress.

If A meets all targets, do not adopt the hybrid — extra complexity for
no win. If A is far below targets and source IO is fast, jump straight
to full B.
