# Matching precision rework — Design (Approach A, main)

**Status:** Approved (brainstorming session 2026-04-27)
**Goal:** Materially reduce wrong-shot matches and chain over-merging in `/matches`
without breaking the tight per-scene compute budget.

This document describes the **main, approved** design (Approach A). Two
alternative designs are kept as siblings:

- `2026-04-27-matching-precision-rework-approach-b.md` — heavier source-window
  re-ranking variant.
- `2026-04-27-matching-precision-rework-hybrid.md` — Approach A with a cheap
  Approach-B-style fallback when A's trajectory fit is weak.

The main implementation track is this document. The siblings are written so
the user can swap them in for testing later if A under-delivers.

---

## 1. Problem statement

Current pipeline (`scene_detector` → `anime_matcher` → `scene_merger`) lives at
~80–90% accuracy on real projects. The two failure modes hurting the most are:

1. **Wrong-shot match (failure mode `c`).** SSCD top-K returns visually
   plausible but wrong frames — frequently from the right episode but the
   wrong scene — driven by recycled animation, repeated character setups,
   and static talking-head shots. The current `_find_temporal_match` triple
   search only verifies that `(start, middle, end)` are in increasing source
   order on the same episode within a speed-ratio gate; this is too easy
   to satisfy by coincidence.
2. **Chain over-merging (failure mode `a`).** The continuity merger uses
   too many evidence sources (primary, alternatives, raw top-K) and accepts
   endpoint proximity in source as sufficient — never observing the cut
   itself. The optional "stitching" pass crosses uncertain boundaries to
   recover, which compounds the problem.

Constraints (from brainstorming):

- **Tight compute budget:** ~1–2s per scene, total project pass under a few
  minutes.
- **Always commit to a primary match** (never prefer no-match over best-guess).
- **anime_searcher submodule is frozen** — any change to it requires a
  separate user approval. This design touches only the consumer code.

Ground truth references (do not mutate):

- `backend/data/projects/dcd74148c7ec` — manually curated, 50 raw scenes →
  20 final scenes (11 chains).
- `backend/data/projects/85de83ca6323` — manually curated, 71 raw scenes →
  55 final scenes (~16 chains).

End-goal validation: re-run the matcher on the same TikTok / series with
fresh project ids and compare scene composition, chains, episode selection,
and timestamps (±0.3s tolerance) to the ground-truth files.

---

## 2. Approach overview

Two independent changes, applied together:

1. **Pass 1 — RANSAC trajectory fit on densified probes.** Replace the
   3-frame triple search with 5–7 frame probes per scene and a per-episode
   RANSAC line fit on `(probe_time, candidate_source_timestamp)` points.
   The trajectory's slope is the speed ratio. Wrong-shot matches typically
   fit at most 1–2 probes; real matches fit all of them.

2. **Continuity merger — two-tier cut-pair verification.** Continuity
   between adjacent scenes requires *both* (i) Tier 1: trajectory
   consistency (same episode, small source gap, similar speed ratio) and
   (ii) Tier 2: cut-pair verification — observing TikTok frames immediately
   bracketing the cut and confirming they map to consecutive source frames.
   Drop alternatives/raw-top-K as evidence. Drop the stitching pass.

These attack the two failure modes at their roots: trajectory fit makes
wrong-shot matches geometrically inconsistent; cut-pair verification makes
over-merging require the cut transition to actually be observed in source.

Speed ratio handling becomes implicit: trajectory slope *is* the speed
ratio. Hard bounds become a soft confidence penalty; extreme x0.5 / x2.0
clips work as long as the trajectory fit is tight.

---

## 3. Pass 1 — Trajectory-fit matching

### 3.1 Probe extraction

For a scene of duration `D` (seconds):

| `D` range          | Probe count | Probe placement (relative to scene start) |
|--------------------|-------------|-------------------------------------------|
| `D < 0.5`          | 2           | `0.20·D`, `0.80·D`                        |
| `0.5 ≤ D < 1.5`    | 3           | `0.15·D`, `0.50·D`, `0.85·D`              |
| `1.5 ≤ D < 4.0`    | 5           | evenly spaced, `0.15·D` end-offsets       |
| `D ≥ 4.0`          | 7           | evenly spaced, `0.15·D` end-offsets       |

Frame extraction goes through one `cv2.VideoCapture` pass via the existing
`AnimeMatcherService.extract_frames`. End-offsets keep probes off transitions
and fades; the cut-pair verifier in §4 uses different (cut-bracketing) frames.

### 3.2 Per-probe SSCD retrieval

Reuse `_search_image_batch` with `top_n=25`, `flip=False`, `series=anime_name`.
One batch call per scene. Each probe gets up to 25 `MatchCandidate` rows.

### 3.3 RANSAC trajectory fit per episode

Group all per-probe candidates by `episode`. For each episode with ≥2
probes-with-candidates:

**Line parameterization.** Fit a line of the form
`source_t = m · tiktok_t + b`, where `tiktok_t` is the absolute TikTok
video time of a probe and `source_t` is the source-episode timestamp of
its candidate. Then `m = source_duration / scene_duration` (positive,
dimensionless). The existing `speed_ratio` is the inverse:
`speed_ratio = scene_duration / source_duration = 1 / m`. The slope
penalty (§3.5) and merger slope-consistency check (§4.3) operate on
`speed_ratio`, never on `m` directly.

1. Build the point set
   `P = {(tiktok_t_i, candidate_source_timestamp) for all probes i and all candidates of probe i in this episode}`.
2. RANSAC loop, `RANSAC_ITERATIONS = 30`:
   - Sample two **distinct probe indices** `(i, j)` uniformly at random,
     then sample one candidate from each.
   - Fit the line through those two points.
   - Reject if `m ≤ 0` (non-monotonic / time-reversed) or
     `speed_ratio = 1 / m` lies outside `SLOPE_HARD_BOUNDS` (§3.5).
   - Count inliers across all probes: a probe is an inlier if at least
     one of its candidates lies within `RANSAC_INLIER_TOL_SECONDS = 0.6`
     of the line vertically (in source seconds). Keep the candidate
     closest to the line per inlier probe.
   - Score the iteration's hypothesis (see §3.4).
3. Refine the best hypothesis on its inlier set via least-squares.
4. Output the episode's best `TrajectoryFit`:
   ```
   TrajectoryFit(
       episode: str,
       m: float,                     # source slope (= source_duration / scene_duration)
       b: float,                     # source_t at tiktok_t = 0
       inlier_count: int,
       rmse: float,                  # in source seconds
       avg_inlier_similarity: float,
       score: float,                 # see 3.4
       candidates_per_probe: list[MatchCandidate | None],
   )
   ```

`RANSAC_INLIER_TOL_SECONDS = 0.6` ≈ one 2-FPS index step (0.5s) plus a small
native-frame slack. Most legit candidates land within this tolerance because
the index resolution is the dominant retrieval error.

### 3.4 Trajectory scoring

```
inlier_ratio  = inlier_count / probe_count
fit_quality   = max(0.0, 1.0 - rmse / RANSAC_INLIER_TOL_SECONDS)
slope_penalty = soft penalty (see 3.5)

trajectory_score = (
    avg_inlier_similarity     # how good are the SSCD hits
    * inlier_ratio            # how many probes does this fit explain
    * fit_quality             # how tightly do they line up
    * slope_penalty           # is the implied speed reasonable
)
```

Pick the episode with the highest `trajectory_score` globally. The selected
fit becomes the `SceneMatch` primary. Note: `SceneMatch.start_time` and
`SceneMatch.end_time` hold *source* timestamps (in the matched episode),
not TikTok timestamps:

```python
match.episode      = fit.episode
match.start_time   = fit.b + fit.m * scene.start_time   # source_t at scene start
match.end_time     = fit.b + fit.m * scene.end_time     # source_t at scene end
match.confidence   = fit.score                          # ∈ [0, 1]
match.speed_ratio  = 1.0 / fit.m                        # legacy convention
```

The current `_refine_boundaries` runs as today on the resulting endpoints —
unchanged, since native-FPS argmax refinement is independent of how we
arrived at the coarse boundaries.

### 3.5 Slope penalty (replaces hard speed bounds)

The penalty operates on `speed_ratio = 1 / m`:

```
SLOPE_PENALTY_FREE_RANGE = (0.65, 1.60)   # speed_ratio
SLOPE_HARD_BOUNDS        = (0.30, 2.50)   # speed_ratio

if speed_ratio ∈ free range:
    penalty = 1.0
elif hard.lo ≤ speed_ratio < free.lo:
    penalty = (speed_ratio - hard.lo) / (free.lo - hard.lo)   # linear ramp
elif free.hi < speed_ratio ≤ hard.hi:
    penalty = (hard.hi - speed_ratio) / (hard.hi - free.hi)   # linear ramp
else:
    penalty = 0.0   # outside hard bounds: rejected
```

Outside hard bounds the score is zeroed and the fit is rejected.
Inside the free range there is no penalty. Between the free and hard
bounds the penalty decays linearly to zero.

### 3.6 Edge cases

**Static / freeze-frame scene.** If `max(candidate_source_timestamp) -
min(candidate_source_timestamp) < 1 / source_fps + slack` for the best
episode's candidates, treat as a static shot:

```
source_center = median candidate timestamp
match.start_time = source_center - scene.duration / 2
match.end_time   = source_center + scene.duration / 2
match.speed_ratio = 1.0
```

This is rare but happens on reaction shots / freeze frames.

**No episode has ≥2 probes-with-candidates.** Falls back to a single-anchor
projection from the highest-similarity probe (current behavior preserved
for ultra-degenerate scenes). This is the only path that can still produce
`was_no_match=True`, and only when no probe returned any candidate.

**Two probes only (very short scenes).** The RANSAC loop is replaced by a
single least-squares fit on the 2 points. `fit_quality = 1.0` (line passes
through both). Slope penalty still applies.

### 3.7 Cost analysis

Per scene:
- Frame I/O: 1 `VideoCapture` pass, 5–7 frames decoded — comparable to today.
- SSCD embed: 5–7 frames vs current 3 — ~2× embed cost.
- FAISS search: 5–7 lookups at top_n=25 — ~2× retrieval cost.
- RANSAC: ~30 iterations × ~10 episodes × 7 inlier checks = ~2K ops,
  well under 50ms CPU work per scene. Negligible vs FAISS.
- Boundary refinement: unchanged.

Total per scene: ~1.5–2× current. Within the tight budget.

---

## 4. Continuity merger — Two-tier cut-pair verification

### 4.1 Tier 1 — Trajectory consistency

Cheap, derived from Pass 1 results. For each adjacent scene pair `(N, N+1)`:

- Both scenes have a Pass 1 primary (`match.episode != ""`).
- Same episode.
- `source_gap = match_{N+1}.start_time − match_N.end_time` is in
  `[−0.05, max(0.5, 1.1 / index_fps)]`.
- `|match_N.speed_ratio − match_{N+1}.speed_ratio| ≤
  SLOPE_CONSISTENCY_TOL = 0.35` (absolute, in the same units as
  `speed_ratio`).

If Tier 1 fails, skip Tier 2 — the pair is not continuous.

### 4.2 Tier 2 — Cut-pair frame verification

Ε-retry sequence with `CUT_PAIR_EPSILONS = (0.05, 0.10, 0.20)`:

For each `ε` in order:
1. Extract TikTok frame at `t_before = scene_N.end_time − ε`.
2. Extract TikTok frame at `t_after  = scene_{N+1}.start_time + ε`.
3. SSCD lookup each, **filtered to the Tier 1 episode**.
4. Verify all of:
   - Both top-1 results land in the Tier 1 episode.
   - `|src_after − src_before| ≤ 1 / source_fps + 0.5 / index_fps`.
   - Both raw similarities ≥ `CUT_PAIR_MIN_SIMILARITY = 0.40`.
5. If all hold, return the pair as continuous. Otherwise advance `ε`.

If all `ε` values fail, the pair is not continuous.

`source_fps` defaults to 24 when unknown (a safe overestimate for anime;
errs toward stricter frame agreement).

### 4.3 Pair score

```
max_gap            = max(0.5, 1.1 / index_fps)
gap_weight         = 1 − clamp(max(source_gap, 0) / max_gap, 0, 1)
slope_consistency  = 1 − clamp(|match_N.speed_ratio − match_{N+1}.speed_ratio|
                               / SLOPE_CONSISTENCY_TOL, 0, 1)
similarity_weight  = (sim_before + sim_after) / 2
pair_score         = similarity_weight · gap_weight · slope_consistency
```

This score (range `[0, 1]`) feeds the existing chain interval-scheduling
selector. Same DP as today; only the input scores change shape.

### 4.4 Chain construction

Reuse current code unchanged:
- `_build_chain_candidates` builds contiguous same-episode chains.
- `_select_non_overlapping_chains` picks the highest-scoring non-overlapping
  set via weighted interval scheduling.

### 4.5 Removed

- `_stitch_adjacent_chains` — drop entirely.
- `MIN_EPISODE_SUPPORT = 2`, `MIN_ALT_CONFIDENCE`, `MIN_RAW_CANDIDATE_CONFIDENCE`
  — drop. Alternatives and raw top-K no longer feed continuity scoring.
- Middle-frame projection in `_get_end_candidates` / `_get_start_candidates`
  for `was_no_match` scenes — drop. Tier 1 already requires a committed
  primary on both sides.

### 4.6 Cost

Per adjacent pair: 2–6 SSCD embeds (with ε-retries) + 2–6 FAISS lookups,
all episode-filtered. ~50 pairs per project → ~150–300 embeds total.
Sub-second on GPU. Negligible.

---

## 5. Pass 2 — Re-match merged scenes

Same machinery as Pass 1 — RANSAC trajectory fit on densified probes —
with two priors that improve precision:

1. **Episode prior.** Pre-select `series=anime_name` *and* post-filter
   per-probe candidates to `episode == merged_episode`. The merged
   episode is the common Pass 1 primary episode across all
   `merged_from` indices (same-episode is a hard requirement of Tier 1,
   so this is unambiguous). Wrong-episode hits are pruned before the
   fit. Cost: free.

2. **Source-window prior.** Compute the merged source range from Pass 1
   per-scene endpoints (read from the pre-merge backup, since
   `match_scenes` is called with `existing_matches` containing the
   merged placeholders):
   ```
   source_lo = min(pre_merge_match_i.start_time for i in merged_from)
   source_hi = max(pre_merge_match_i.end_time   for i in merged_from)
   ```
   Filter trajectory-fit candidates to lie within
   `[source_lo − SOURCE_WINDOW_PRIOR_PAD_SECONDS,
   source_hi + SOURCE_WINDOW_PRIOR_PAD_SECONDS]` (default 2.0s). Removes
   spurious far-away same-episode hits.

The merged scene's longer duration usually triggers 5 or 7 probes in §3.1,
so trajectory fit gets stronger evidence than Pass 1 had on the constituent
short scenes.

If a merged scene fails to find any fit (all probes filtered out), fall
back to the per-scene Pass 1 endpoints union: `start = min(...start_time)`,
`end = max(...end_time)` from the merged-from primaries. This is the only
remaining no-match-recovery path.

---

## 6. Commitment policy

- Pass 1 commits a primary on every scene where at least one probe returned
  any candidate. Trajectory fit always yields a best-scoring fit; we never
  fall through to no-match because the score is "low."
- Pass 2 commits the best fit found within the source-window prior; on
  empty fit, it commits the union-endpoints fallback.
- `confidence` reflects the trajectory score (range `[0, 1]`). The frontend
  retains its existing low-confidence styling and review affordances —
  no API changes required.
- `was_no_match=True` only on hard-failure cases (frame extraction failed,
  no candidate at all on any probe). Strictly less common than today.

---

## 7. Code organization

### 7.1 `backend/app/services/anime_matcher.py`

**Add:**
- `_extract_probe_times(scene_duration, scene_start) -> list[float]`
  — implements §3.1 piecewise rule.
- `_fit_trajectory(probe_times, per_probe_candidates) -> TrajectoryFit | None`
  — RANSAC + LSQ refinement (§3.3).
- `_apply_slope_penalty(speed_ratio) -> float` — §3.5.
- `_static_shot_fit(...)` — §3.6 freeze-frame helper.
- `TrajectoryFit` dataclass.

**Modify:**
- `match_scenes`: replace probe extraction (3 frames) with
  `_extract_probe_times`, replace `_find_temporal_match` call with
  `_fit_trajectory` + slope penalty + static-shot detection.
- `match_scenes`: add a new optional parameter
  `pass2_priors: dict[int, Pass2Prior] | None = None` keyed by
  scene_index, where `Pass2Prior` carries
  `{episode: str, source_lo: float, source_hi: float}`. When present,
  apply the episode + source-window filters from §5 before the fit.
  The matching route in `api/routes/matching.py` builds this dict from
  Pass 1 results and the chains, then passes it on the Pass 2 call.

**Remove:**
- `_find_temporal_match`. (Kept logic ideas: speed gating moves into the
  slope penalty; same-episode constraint becomes the per-episode group
  in trajectory fit.)

**Keep unchanged:**
- `_refine_boundaries`, `_collect_frames_in_window`, `extract_frames`,
  `_search_image_batch`, `_compute_alternatives` (the alternatives surface
  on the UI is preserved unchanged), `_init_searcher`.

### 7.2 `backend/app/services/scene_merger.py`

**Add:**
- `_verify_cut_pair(video_path, scene_n, scene_n1, episode, embedder,
  query_processor, source_fps) -> CutPairResult` — §4.2.
- `CutPairResult` dataclass: `passed: bool, sim_before: float,
  sim_after: float, src_before: float, src_after: float, epsilon_used:
  float`.
- `_pair_score(...)` — §4.3.

**Modify:**
- `detect_continuous_pairs`: implement §4.1 + §4.2 directly. Drop the
  candidate-aggregation logic (`_get_end_candidates`,
  `_get_start_candidates`, `_get_best_pair_continuity`).
- `build_merge_chains`: keep the chain-construction body, change the
  pair-scoring source to `_pair_score`. Drop `_stitch_adjacent_chains`.

**Remove:**
- `_stitch_adjacent_chains`.
- `_get_chain_bridge_continuity`.
- `_get_end_candidates`, `_get_start_candidates`,
  `_get_best_pair_continuity`, `_dedupe_candidates`.
- Constants: `MIN_EPISODE_SUPPORT`, `MIN_ALT_CONFIDENCE`,
  `MIN_RAW_CANDIDATE_CONFIDENCE`, `CHAIN_BRIDGE_*`.

**Keep unchanged:**
- `merge_scenes_and_matches`, all `prepare_manual_merge_with_previous`
  / undo / backup logic (~400 lines), `_continuity_gap_tolerance`.

### 7.3 `backend/app/api/routes/matching.py`

Minimal change: the Pass 2 call site builds a `pass2_priors` dict from
the Pass 1 matches + chains and passes it through. Other call sites
(`detect_continuous_pairs`, `build_merge_chains`,
`prepare_manual_merge_with_previous`) keep their signatures.

### 7.4 New constants module (optional)

If we end up tuning many constants, add `backend/app/services/matcher_config.py`
holding all the §3/§4 constants as module-level values. Otherwise leave
them as class attributes on the existing services.

---

## 8. Tunables

| Constant                            | Value         | Where        |
|-------------------------------------|---------------|--------------|
| `PROBE_COUNT_RULES`                 | §3.1 table    | matcher      |
| `RANSAC_ITERATIONS`                 | 30            | matcher      |
| `RANSAC_INLIER_TOL_SECONDS`         | 0.6           | matcher      |
| `SLOPE_PENALTY_FREE_RANGE`          | (0.65, 1.60)  | matcher      |
| `SLOPE_HARD_BOUNDS`                 | (0.30, 2.50)  | matcher      |
| `STATIC_SHOT_TOL_FRAMES`            | 1.0           | matcher      |
| `SOURCE_WINDOW_PRIOR_PAD_SECONDS`   | 2.0           | matcher pass 2 |
| `CUT_PAIR_EPSILONS`                 | (0.05, 0.10, 0.20) | merger  |
| `CUT_PAIR_MIN_SIMILARITY`           | 0.40          | merger       |
| `CUT_PAIR_MAX_FRAME_GAP_FACTOR`     | 1.0           | merger       |
| `CONTINUITY_GAP_TOLERANCE`          | 0.30          | merger (kept)|
| `SLOPE_CONSISTENCY_TOL`             | 0.35          | merger       |

---

## 9. Backwards compatibility

- Public Pydantic models (`SceneMatch`, `MatchCandidate`, `AlternativeMatch`,
  `MatchList`, `Scene`, `SceneList`) — schema unchanged.
- `confidence` semantics shift: still in `[0, 1]`, but now reflects
  trajectory fit rather than triple similarity. Call sites that compare
  to specific thresholds (e.g. UI styling) keep working without changes;
  threshold values are still meaningful in the same range.
- `merged_from`, `pre_merge_backup`, undo, manual merge — fully unchanged.
- API streaming format unchanged.
- `/scenes` detector untouched (no need to change PySceneDetect or the
  tiny-scene merge — both interact correctly with the new matcher).

---

## 10. Validation plan

### 10.1 Ground truth comparison

Run the new pipeline on fresh project ids reproducing the inputs of
`dcd74148c7ec` and `85de83ca6323`:

- Same `tiktok.mp4` (copy from ground-truth project dir).
- Same `series_id`, `anime_name`, `library_type`.
- Run `/scenes` (existing detector, no change) → `/matches` (new pipeline).
- Compare resulting `scenes.json` and `matches.json` against the ground
  truth's final files (not the `_raw_backup` ones).

### 10.2 Metrics

Per scene (using ground-truth final scene list as the reference timeline):

- **Episode accuracy:** `match.episode == ground_truth.episode`.
- **TikTok scene boundary accuracy:** the TikTok scene boundaries
  themselves come from PySceneDetect — these usually align trivially
  unless the scene was merged. Counted as correct iff the candidate
  scene overlaps the GT scene by ≥75% of `min(durations)`.
- **Source-side accuracy (the user's ±0.3s rule):**
  `|match.start_time − gt.start_time| ≤ 0.3s` AND
  `|match.end_time   − gt.end_time|   ≤ 0.3s`.

Per project:

- **Chain composition F1:** treat each merged group as a set of original
  scene indices; compute precision/recall against ground truth's `merged_from`
  groups, then F1.
- **Over-merge severity:** count ground-truth-pairs `(i, j)` where
  `i, j` are NOT in the same GT chain but ARE in the same algorithm chain.
  Reported as a count and as a fraction of chains.

### 10.3 Targets

- ≥80% of scenes with correct episode.
- ≥75% of scenes with source-side boundaries within ±0.3s of GT.
- ≥70% of chains correctly composed (F1 ≥ 0.7 over chains).
- 0 over-merges spanning ≥4 originally-distinct scenes.
- `was_no_match` rate ≤ today's rate (should drop with §3.7's denser
  evidence; must not rise).

We won't hit 100% — humans pick scenes from arbitrary episode positions for
visual reasons. Targets are calibrated against the observed ground-truth
divergence.

### 10.4 Test inputs beyond the two ground-truth projects

Optional: pick 3–5 additional already-shipped projects from
`backend/data/projects/*/matches.json` where the user manually edited the
matcher output, run the new pipeline against the same TikToks, and report
the same metrics. Acts as a regression suite without overfitting to the
two named ground truths.

---

## 11. Out of scope

- **anime_searcher submodule changes.** Frozen per the brainstorming
  constraints. If the trajectory fit reveals a fundamental retrieval-side
  issue (e.g. SSCD misses on certain animation styles) we surface it as
  a follow-up question for the user, not silently work around it.
- **Source-side cut detection.** Approach C (in brainstorming) and
  Approach B (separate doc) explore this; the main design does not.
- **Frontend changes.** Confidence semantics are preserved; review UI
  works unchanged.
- **PySceneDetect tuning.** Out of scope here. The new matcher / merger
  is robust to the current scene detector's level of over-segmentation.

---

## 12. Risks and mitigations

| Risk                                                | Likelihood | Mitigation                                                                                  |
|-----------------------------------------------------|-----------|---------------------------------------------------------------------------------------------|
| Trajectory fit too strict on talking-head / static scenes | medium    | §3.6 freeze-frame fallback; `RANSAC_INLIER_TOL_SECONDS = 0.6` is generous.                  |
| Cut-pair verification fails on hard fades / flashes | medium    | ε-retry across 0.05/0.10/0.20s; on persistent failure, scenes don't merge — acceptable.     |
| RANSAC variance with small probe counts             | low       | Refinement on inliers via LSQ; 30 iterations is overkill for ≤7 probes.                     |
| Episode-filter on cut-pair retrieval misses cases where Tier 1 picks wrong episode | low | Tier 1 requires same-episode primaries on both sides — by construction the episode is correct or the pair fails Tier 1. |
| Pass 2 source-window prior excludes the right candidate | low       | ±2s pad is wide enough to absorb Pass 1 endpoint drift; on empty fit, fall back to union-endpoints. |
| Confidence numeric values shift slightly, frontend thresholds need tuning | low | Same `[0, 1]` range; spot-check UI styling on representative projects.                      |

---

## 13. Implementation milestones

1. Trajectory fit infrastructure (`_extract_probe_times`, `_fit_trajectory`,
   `_apply_slope_penalty`, `TrajectoryFit`, `_static_shot_fit`).
2. Wire trajectory fit into `match_scenes` for Pass 1 (replacing
   `_find_temporal_match`).
3. Pass 2 priors (episode + source-window).
4. Cut-pair verifier (`_verify_cut_pair`, `CutPairResult`).
5. Merger refactor (drop alternatives/raw-top-K evidence and stitching).
6. Validation run on ground-truth projects, metric report.
7. Tune constants if metrics are below targets; otherwise ship.

Each milestone is independently committable and testable.
