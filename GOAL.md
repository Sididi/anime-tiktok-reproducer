/goal Finish, harden, and productionize the global scene aligner (`backend/app/services/scene_aligner.py`). This document was produced on 2026-07-04 after a full computer-vision re-audit: code reading of the aligner/detector/merger, fresh evaluation runs of the aligner on all four ground-truth projects, and targeted experiments (merge-stage re-enable, query-crop-variant retrieval probe). Every number below is measured on this machine, not estimated. Read section 2 before writing any code: the previous session stopped halfway — most of the pipeline already works, and the dominant failures are known, ranked, and attributable. Your job is to finish the build according to section 3, not to re-diagnose from scratch and not to patch fixtures.

You have explicit permission to rework or delete anything in `scene_aligner.py`, `scene_detector.py`, `scene_merger.py`, `anime_matcher.py`, and the evaluator internals. Scene cutting is inside your perimeter: the final scene segmentation is allowed to differ from the raw detector output (it must — see Stage 4). Hard limits: never modify the anime_searcher submodule or reindex the library (crop invariance lives on the query side only, by explicit owner decision), and never write into the four ground-truth folders.

# 1. Problem statement and domain priors

A project is a short vertical TikTok edit (60-180s) cut from anime episodes. Recover, for every scene of the edit, the source episode and source start/end timestamps precisely enough that a human never scrubs the episode manually.

In the (t_tiktok, t_source) plane each real clip is a near-straight segment whose slope is the playback speed. Matching is global alignment: find those segments and decode a coherent path through them.

Domain priors (measured on the four ground-truth projects; treat as model priors, never as hardcoded constants):

- One dominant source episode per edit, rare intruder scenes from other episodes of the same series.
- Scene order is quasi-chronological (86-100% monotonic adjacent pairs); non-monotonic jumps exist and must survive decoding (one project opens 200→193→196→199→118s).
- Playback speed per scene is near-constant, centered on 1.0x, commonly 0.5-1.7x, with at least one genuine 4.07x scene: evidence collection must never clamp at a "common" speed.
- Scenes are short (median 1.1-2.6s, min ~0.5s); dense edits have 46-55 scenes in ~60s.
- Edits are 9:16 from 16:9 sources; zoomed/cropped edits are common (one full project is zoomed).
- The index stores SSCD embeddings at 2.0 fps (0.5s grid). Retrieval is essentially free (measured 0.09s for 514 queries, top-60). Sub-grid precision only comes from decoding the matched episode locally at native fps.
- SSCD embed throughput ~124 img/s (RTX 4070 Laptop 8GB); a 60-180s edit at 8 fps costs ~4-25s to embed. Anime repeats near-identical frames; local ambiguity is normal and must be resolved globally, not per-sample.

# 2. Measured state, 2026-07-04 (fresh runs, current main)

Aligner (`--matcher aligner`), fresh scene detection, strict validator:

| Project | Scenes gen/GT | Stage-3 evidence recall | Elapsed | Verdict |
|---|---|---|---|---|
| dcd74148c7ec | 50/20 | 19/20 (95%) | 96s | FAIL |
| 85de83ca6323 (zoomed, dense) | 59/55 | 35/55 (64%) | 92s | FAIL |
| 411f73d26c1d | 87/52 | 48/52 (92%) | 174s | FAIL |
| 5e85164d9ff8 (fast montage) | 65/46 | 42/46 (91%) | 97s | FAIL |

Ranked root causes — this ordering is the work plan:

P1 — Scene fusion is disabled. `_merge_decoded_continuities` exists but `_remap_decoded_without_merge` (a no-op) is wired in. Every project fails primarily by scene-count mismatch cascading through the index-aligned validator. Re-enabling the existing pairwise merge (measured by experiment) yields 42/20, 58/55, 78/52, 64/46: the pairwise criterion (gap ≤ 0.25s AND |Δspeed| ≤ 0.20 between two per-fragment fits) is structurally insufficient, because a slope fitted on a 0.5-1s fragment is noise. Fusion must refit a joint model on the union of fragments (Stage 4).

P2 — Zoom recall. On the zoomed project the lazy query-variant path never triggered (weak_variant_sample_count = 0): spurious segments satisfy the "enough inliers" gate. Measured probe (3 frames/GT scene, top-60, ±1.0s): plain query recalls 48/55 scenes; the `wide_pad` variant alone recalls 52/55; any-variant 53/55 (96%). `trim_bars` and `center_portrait` recall 0/55 — dead weight. Always-on `wide_pad` (plus plain) at Stage 1 closes most of the gap for ~+8-15s embedding; the lazy gate and dead variants get deleted.

P3 — Boundary placement, not boundary detection. Comparing fresh detector boundaries to GT boundaries across all four projects: only ONE true missing cut (411f73d26c1d @ 14.80s, nearest detected 0.77s away). All other "missing" GT boundaries are 0.2-0.4s placement offsets from a detected boundary, and there are 10-37 excess detector boundaries per project (fusion work). So: the detector finds the cuts; the pipeline must merge the excess, snap placements at sub-second scale, and handle the rare interior split.

P4 — Internal drift already present. The aligner has 26 module constants (budget: ≤15), four overlapping segment-extraction mechanisms (per-scene line seeds, edge pairs, a second sample-level Viterbi in `_add_global_path_segments`, and a dual top-60/top-20 segment set), and the scene-level DP has no continuity reward (only penalties), contrary to the original design. Scene-count/density gates survive outside the aligner: `AUTO_DENSE_MIN_SCENES = 70` and the 45-70 accept window in `scene_detector.py`, `DENSE_SCENE_COUNT` + median-duration gate in `scene_merger.py`. These are the same overfitting pattern the aligner was built to eliminate.

What already works — do not rebuild, do not regress:

- Stages 1-2 (dense 8 fps sampling, batched FAISS retrieval): sound and cheap.
- Stage 3 evidence recall is 91-95% on three projects before any of this work: the truth is almost always in the correspondence cloud.
- Stage 5 native-fps refinement (`_refine_boundaries` mechanics): works (55/59 successful refinements on dcd74148c7ec) and is the only sub-grid precision source.
- PySceneDetect ContentDetector threshold 16 + tiny-scene sanitizing: near-complete cut coverage (see P3).
- The frame-diff boundary snap mechanic in `snap_dense_visual_boundaries` is the right placement primitive; only its density gate is wrong.
- The SceneMatch/MatchList output contract, and the manual merge/undo service APIs (frontend depends on them).

# 3. Prescribed architecture (revision, not rebuild)

Five stages, same skeleton as today. Stage contracts are fixed; internals are yours. Deviating from a stage design requires a measured leave-one-out comparison first.

Stage 1 — Dense query sampling, variants always on.
Single sequential decode of the TikTok, uniform sampling (8 fps today, keep it a named constant). For EVERY sample embed the plain frame and the `wide_pad` 16:9 reconstruction (blurred-background pad — measured as the variant that matters). Delete the lazy weak-scene variant path and the dead variants (`trim_bars`, `center_portrait`; keep `center_landscape` only if a measurement on all four projects justifies its cost). Output: `QuerySample[] = (t_tiktok, embedding, variant_id)`.

Stage 2 — One batched FAISS retrieval over all samples and variants, top-k per sample, series-scoped. Merge correspondences across variants of the same sample (keep max similarity per (episode, t_source) cell). Output: correspondence cloud.

Stage 3 — ONE robust segment-hypothesis mechanism per (detector fragment, episode). Seeds: slope-1 offsets from each correspondence + bounded pairwise slopes across distinct sample times; weighted least-squares refit over inliers within the grid-derived residual tolerance; dedupe in (slope, offset-grid) space; score = inlier support + mean similarity − residual penalty. Wide speed bounds for evidence (0.25-5x). Delete `_add_global_path_segments`, `_decode_sample_path`, the dual top-60/top-20 segment sets, and `include_low_rank_common_seeds`. One hypothesis set per fragment, one constant for its size. Keep the edge-pair fallback only if removing it measurably hurts a project under leave-one-out.

Stage 4 — Global decode AND final scene segmentation (the milestone-1 work).
Run the DP over detector fragments with: emission = inlier support + similarity (as today), transition = episode-switch penalty + backward-jump penalty PLUS an explicit continuity reward when the predicted source position is continuous across the cut within ~one grid step (the current code has penalties only — restoring the reward is required, it is what lets weak fragments ride on strong neighbors).
Then produce the final scene list as a segmentation problem, not pairwise gluing: group consecutive fragments assigned to the same episode by refitting ONE line on the pooled inlier correspondences of the candidate group; accept a group while the pooled fit's residuals stay within the grid tolerance and reject it when a two-line model is clearly better (a changepoint/model-selection criterion — BIC-style or penalized residual, your choice, but it must be a joint-fit decision, never a comparison of two per-fragment slopes; section 2 P1 shows the pairwise version fails 42-vs-20). The same residual-changepoint test, applied inside a single fragment, yields the rare interior split (one known case across all four projects) — snap any inserted cut to the strongest local frame-diff peak.
Boundary placement: after segmentation, snap every final boundary to the strongest frame-diff peak within ±0.5s (reuse the `snap_dense_visual_boundaries` mechanic with its density gate removed — P3 shows 0.2-0.4s placement offsets are a systematic error mode on all projects, not a dense-edit special case).
Output per final scene: chosen (episode, affine map) + ranked alternative hypotheses. Populate SceneMatch exactly as today (primary, alternatives, start/middle/end candidates, merged_from).

Stage 5 — Native-fps refinement, authoritative.
As today: decode the episode locally around each final boundary, argmax cosine against the actual first/last TikTok frame. Refined output wins when it passes the sanity window; it never competes in a score ranking. Watch the budget: refinement is the main per-boundary cost (38s for 59 boundaries measured); fusion reduces boundary count, which is your margin. If a project exceeds the time budget, reduce refinement frame counts before touching anything else.

# 4. Milestones — gated, in order

Work strictly in this order; do not advance while a previous milestone regresses. Every failure is attributed to its owning stage (was the truth absent from the cloud? outvoted in hypotheses? mis-grouped in segmentation? mis-decoded? unrefined?) before any code change.

- M1 Segmentation: fusion + snap produce the correct scene count and index alignment on 4/4 projects (validator's scene section). This is the single highest-leverage change; nothing else is measurable until it holds.
- M2 Evidence recall ≥ 95% per project (evaluator's `aligner_stage3_evidence_recall`), including the zoomed project via always-on wide_pad.
- M3 Decode: primary episode+interval correct (within loose tolerance) for all but a handful of scenes per project; failures analyzed per stage.
- M4 Precision: Stage 5 refinement drives ±0.3s exact rates to the acceptance thresholds.
- M5 Strict PASS on all four projects from fresh detection, within the time budget.
- M6 Production: route `/matches` (3 call sites in `backend/app/api/routes/matching.py`) runs the aligner; manual merge/undo APIs and the frontend contract keep working; the condemned legacy is deleted (the 13 correction passes, the additive bonus ranking, the per-episode crop-index subsystem in `anime_matcher.py`; keep `_refine_boundaries`, frame/embedding utilities, and anything the aligner reuses); `pixi run pytest backend/tests/` green with tests updated for the new flow. Deletion happens only after M5.

# 5. Anti-overfitting guardrails (as binding as the acceptance criteria)

- Scope: these rules now cover `scene_detector.py` and `scene_merger.py` too — that is where scene-count gates survived last time. No gate or constant keyed on scene index, scene count or count ranges, median durations tuned to a fixture, project id, episode name, or fixture timestamp literals. The known offenders to remove as their responsibilities migrate into Stage 4: `AUTO_DENSE_*` in the detector, `DENSE_SCENE_COUNT`/`_is_dense_short_scene_list` gating in the merger.
- Every numeric constant in the aligner: derived from a stated domain fact, named at module top with a one-line justification, total ≤ 15. The file has 26 today — the Stage-3/Stage-4 simplification must shrink it, and if you find yourself adding a 16th, the model is wrong; stop and rethink.
- Leave-one-out protocol for any threshold/prior change: evaluate all four projects, treat each in turn as held out; keep a change only if no rotation degrades its held-out project and the aggregate improves. Never keep a change justified by exactly one project's numbers.
- No per-scene special-case branches to fix a named failing scene, ever. Diagnose the owning stage instead (the stage structure exists so failures are attributable).
- Synthetic unit tests encode the domain model (fabricated clouds with known segments, noise, speed changes, non-monotonic jumps, an intruder episode, fragment groups needing fusion, one fragment needing a split) — never fixture timestamps. Keep `backend/tests/test_scene_aligner.py` in this spirit.
- Ground-truth folders are read-only: `backend/data/projects/{dcd74148c7ec,85de83ca6323,411f73d26c1d,5e85164d9ff8}`. Verify with `git diff` on those paths before claiming completion. All experiment outputs go to scratch paths.
- Do not modify the anime_searcher submodule; do not reindex the library.

# 6. Work loop and experiment journal

Maintain `docs/GOAL_JOURNAL.md`, appended every iteration: hypothesis → metric before → change (one line) → metric after on ALL FOUR projects → keep/revert + why. No tuning without a journal entry. If an idea cannot be phrased as a testable hypothesis with a metric, it is not ready to be code. Re-read your last three entries before each new change: three consecutive entries chasing the same project's numbers is the overfitting spiral — stop, re-diagnose at stage level, and prefer reworking the stage over adding a compensation.

1. Sanity-check the environment by reproducing one line of the section-2 table (dcd74148c7ec), then go straight to M1.
2. Suggested commands:
   - `pixi run python backend/scripts/evaluate_matching_against_ground_truth.py dcd74148c7ec 85de83ca6323 411f73d26c1d 5e85164d9ff8 --matcher aligner`
   - `pixi run pytest backend/tests/`
   - `git diff -- backend/data/projects/dcd74148c7ec backend/data/projects/85de83ca6323 backend/data/projects/411f73d26c1d backend/data/projects/5e85164d9ff8`
3. The evaluator is yours to extend with diagnostics (per-stage metrics, boundary-coverage reports), never to loosen: strict tolerances and index-aligned comparison stay.
4. Keep the aligner/legacy switch until M6, then delete the legacy path.

# 7. Acceptance criteria

Per project, from fresh scene detection, via the strict evaluator (unchanged tolerances):

- Correct cuts/fusions relative to ground truth (equal counts, aligned indices).
- Scene start/end within ±0.3s for every scene, except at most 3 scenes within ±1.0s.
- Primary match correct (episode + timing, same tolerance rules), except at most 2 scenes with wrong primary whose correct source timing is present in the exposed candidate data.
- No scene requires manually searching the episode.
- End-to-end ≤ 120s per project on this machine (411f73d26c1d is at 174s today; the fusion-reduced boundary count and the single-pass Stage 1 are your budget levers). GPU memory stays adaptive (existing OOM-backoff embedding path).
- SceneMatch/MatchList contract and manual merge/undo APIs unchanged for consumers; regression tests pass; ground-truth folders untouched.

# 8. Final report

- Section-2 table vs final table (all metrics + elapsed).
- Evidence recall per project after M2.
- The journal: which tunings were tried, which were rejected by leave-one-out and why.
- Inventory of every numeric constant in the aligner with justification (≤15).
- Code deleted (functions/subsystems, line counts) and tests added/updated.
- Explicit `git diff` confirmation the ground-truth folders are unchanged.
- If blocked (missing videos/models/indexes, CUDA/FAISS failures): report the exact path/error and stop rather than claiming success.
