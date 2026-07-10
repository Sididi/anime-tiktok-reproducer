Bring the global scene aligner (`backend/app/services/scene_aligner.py`) to a strict PASS on all four ground-truth projects and ship it to production. This document was produced on 2026-07-10 after a full computer-vision re-audit on the CLEAN index and the CORRECTED ground truths: fresh evaluation runs (fresh detection AND oracle GT-boundary runs on all four projects), per-failure attribution, and a native-frame pixel-separation experiment on the dominant failure class. Every number below was measured on this machine on 2026-07-10. Everything measured before 2026-07-10 is void: v1-v31 ran on a corrupted index, v32-v56 ran against pre-fix ground truth for dcd74148c7ec and 85de83ca6323 (see the archived journal). Do not import conclusions from the archive; re-derive.

The headline of the audit: **segmentation is essentially solved; the source axis is the game, and the index has no more signal to give there.** An oracle run that feeds the aligner the exact GT scene boundaries produces the same source-axis numbers as fresh detection (±3 scenes). The three remaining failure families — duplicate-instance picks, per-end sub-second precision, slope collapse on lookalike content — are all index-blind and all yield to the same instrument: comparing actual TikTok frames against natively decoded source frames at pixel level. The plan below is therefore not "tune the DP harder"; it is "build the native arbitration layer the DP has been missing, then clean up".

# 0. Permissions and hard limits

You have explicit permission to rework or delete ANYTHING in `scene_aligner.py`, `scene_detector.py`, `scene_merger.py`, `anime_matcher.py`, and the evaluator internals — including the scene-cutting phase (the final segmentation may differ from the raw detector output) and including wholesale replacement of any stage whose contract you preserve. A rework that simplifies is preferred over a compensation that patches: when three tunings in a row chase the same project, rework the stage instead. The current architecture survived this audit on measurement, not on sentiment — where it works (§2 "what works") do not rebuild it; where it is dead code (§6) delete it.

Hard limits, unchanged and absolute:
- Never modify the `anime_searcher` submodule; never reindex or run the indexer (owner decision 2026-07-06). Auxiliary caches you build yourself (decoded-frame caches, etc.) are fine as long as they live outside the submodule and outside the GT folders.
- Ground-truth folders are read-only: `backend/data/projects/{dcd74148c7ec,85de83ca6323,411f73d26c1d,5e85164d9ff8}`. Verify with `git diff` + untracked-file check on those paths before claiming completion.
- Evaluator tolerances never loosen (±0.3s exact / ±1.0s loose, budgets in §7). Equivalence rules: keep the existing ones (folding, lookalike, static-duration waiver); extensions are allowed only for cases proven pixel-identical, one journal entry each, reversible (owner decision 2026-07-10).
- 200s end-to-end hard cap per project on this machine. 411f73d26c1d is at 199.4s today: performance work is part of the job, not an afterthought (§5).

# 1. Problem statement and domain priors (re-measured 2026-07-10)

A project is a short vertical TikTok edit (60-180s) cut from anime episodes. Recover, for every scene of the edit, the source episode and start/end timestamps precisely enough that a human never scrubs the episode manually. In the (t_tiktok, t_source) plane each clip is a near-straight segment; matching is global alignment plus, now, native verification.

Priors measured on the four corrected GTs (model priors, never hardcoded constants):
- Playback rate (source seconds per TikTok second): **median 1.00 on every project, full range [0.59, 1.54], 88-98% of scenes in [0.8, 1.25]**. The 4.07x scene cited by the previous GOAL was an artifact of the broken GT — it does not exist. Wide evidence bounds (~[0.4, 2.5]) remain cheap insurance for future projects, but the working prior is: playback is real-time unless the evidence is loud, and a fitted rate outside ~[0.6, 1.6] is more likely a phantom fit than a real retime.
- Scene durations 0.47-14.5s, medians 1.1-2.6s; 46-54 scenes in dense ~60s edits.
- Source order is mostly forward-monotonic (79-100% of adjacent same-episode pairs) with real backward jumps. Edits DO re-use source clips: 85de replays 3 clips at distant scene indices — soft chronology/assignment priors yes, hard injectivity no.
- One dominant episode per edit (411f is the exception: 5 episodes).
- Edits are 9:16 crops/zooms of 16:9 sources (85de is fully zoomed). Index: SSCD embeddings at 2 fps (0.5s grid), retrieval essentially free. Native decode + SSCD embed of episode windows costs ~17-32s/project today at 12 fps — the precision budget lives here.
- GT precision (owner, 2026-07-10): dcd/85de re-verified visually to <0.2-0.3s with at most 1-2 residual hard cases; 5e85/411f numerically precise. 411f scene #5 (tiktok 11.13-13.03 → E04 668.00-669.90) is a knowingly-approximate GT ("really similar" scene, exact source never found): if it resists, waive it via the §8 protocol instead of chasing it. Suspected GT errors are reported for visual arbitration (§8), never edited and never tuned toward.

# 2. Measured state, 2026-07-10 (aligner v56 code, clean index, corrected GT)

Fresh detection, strict validator, scene axis exact/loose/failed then source axis exact/loose/wrong-primary-with-candidate/failed:

| Project | GT scenes | Scene E/L/F | Source E/L/WP/F | Elapsed |
|---|---|---|---|---|
| dcd74148c7ec | 20 | 17/1/2 | 11/7/0/2 | 59.5s |
| 85de83ca6323 (zoomed, dense) | 54 | 45/6/3 | 28/6/15/5 | 107.6s |
| 411f73d26c1d (5 episodes) | 52 | 48/0/4 | 31/9/8/4 | 199.4s |
| 5e85164d9ff8 (fast montage) | 46 | 41/4/1 | 21/15/9/1 | 85.9s |

Oracle runs (`--gt-scenes`, perfect boundaries given) yield source-axis 11/7/1/1, 31/7/14/2, 31/9/11/1, 24/13/9/0 — **within noise of fresh detection**. Two conclusions: (a) fixing segmentation further cannot fix the source axis; (b) the pipeline slightly DEGRADES perfect input boundaries (85de oracle: 5 given-true boundaries moved by presnap/tug; the aligner also merged 1 given-true pair on dcd and split 1 on 85de/411f) — any snapping/tugging you keep must be evidence-gated enough to leave a correct boundary alone.

What works — measured, do not rebuild:
- Stages 1-2 (single-decode 8 fps sampling + diff curve, batched FAISS retrieval): sound, cheap. Stage-3 evidence recall 93-95% (43/46 on 5e85).
- Detector + presnap boundary coverage: only 3 GT cuts missing across all four projects (dcd@16.03s nearest 0.7s, 85de@12.15s nearest 0.5s, 411f@14.80s nearest 0.77s); everything else within 0.3s. The scene axis needs a scalpel, not a rework.
- Stage-4 segmentation DP (span fits + boundary priors + beam): produces the right structure; its errors are inherited from wrong source lines, not from the DP itself (fold-no-chain scene failures trace to per-piece line errors).
- Chain detection + one-pooled-line-per-chain + delta-lock sweep mechanics in `_build_matches`: the right skeleton for precision; its per-end decisions are what §3-R2 replaces.
- The SceneMatch/MatchList contract, manual merge/undo APIs, and the evaluator's folding/equivalence machinery (owner-approved).

# 3. Root causes, ranked — this ordering is the work plan

R1 — Duplicate-instance primaries (WP 15/8/9 on 85de/411f/5e85; budget 2). Recurring dialogue shots and OP/ED repeats: index cosine between the true and wrong instance differs by 0.02-0.05 — no index-side ranking can decide this. Measured on 85de WP cases (probe script: `backend/scripts/diagnostics/probe_duplicate_separation.py`): SSCD embeddings of NATIVE decoded frames actively prefer the WRONG instance (margins -0.06, -0.09) while plain gray pixel NCC with a center-crop zoom search prefers the truth (+0.05, +0.10). True OP/ED repeats are pixel-identical at every candidate (margins ~0.00) and are content-undecidable — those need the chronology/assignment prior and the (owner-approved) lookalike-equivalence acceptance, and the irreducible remainder goes to the §8 review. So: pixel-level arbitration for dialogue-type duplicates, soft global assignment (forward-monotonic runs, reuse allowed) for identical repeats, in that order of trust.

R2 — Per-end sub-second precision (source loose 7/6/9/15; budget 3). 37 loose scenes measured: median end error 0.37s, all <1.0s, 30/37 with one side already exact. Structural cause: the delta-lock sweeps MEAN similarity of 8 fps samples near chain ends — it never looks at the actual first/last TikTok frame of the scene, and a mean over 4 samples blurs the very boundary it estimates. The end-snap then prefers source frame-change peaks, which is right when the editor cut on a source cut and wrong when they trimmed mid-shot. The fix direction: per-end refinement anchored on the true edge frames (decode the TikTok edge frame, argmax cosine/NCC against native source frames along the chain line, constrained near the line prediction), falling back to snap-to-source-cut only when the edge frame is temporally ambiguous (static plateau). This is the same native instrument as R1 — build it once.

R3 — Slope collapse on lookalike content (part of WP + loose on 411f/5e85: e.g. 411f#50 rate 0.52 vs GT 1.0 doubling the source span; 411f#46 phantom skips inside a 0.7x slow-mo scene). Phantom inliers hold plausible support at bogus slopes; index-level parsimony (v51-v53 archive) cannot fix it because the phantom genuinely outscores truth on index embeddings. With the corrected priors (§1) the unit-rate prior can be much more assertive, and the native layer arbitrates the residual: a scene's rate is trusted only if the native sweep at that rate beats the unit-rate alternative (the v54 mechanism, currently gated to isolated |rate-1|>0.2 scenes only, generalizes).

R4 — Scene-axis residuals (scene F 2/3/4/1; budget 0). Exactly three true detector misses (listed in §2) plus ~4 placement offsets of ~0.3s (85de@59.98/63.03) plus two DP over-merges (85de@21.22, 5e85@32.5) plus fold-no-chain fails that R1-R3 fix for free (pieces chain once their lines are right). The missed cuts are invisible to the TikTok diff curve at detection thresholds (5e85@6.85: real-cut diff bump ranked 7th among local peaks) but LOUD in source space: the fitted line jumps discontinuously mid-scene. Drive interior splits from source-side discontinuity of the evidence (dead sample-run + alternative line already half-implemented in `_interior_splits`) and use the diff curve only to place the cut, not to find it. Do not add detector sensitivity (excess boundaries are free under folding; missing GT coverage is not).

R5 — Time budget. 411f at 199.4s vs 200s cap. Costs: `sample` 19-65s (sequential full decode + SSCD embed of the TikTok), `refine_build` 25-82s (native window decode + embed + sweeps), everything else <15s. The native layer (R1-R3) ADDS decode work, so this is a real engineering constraint: batch and cache decoded episode windows (85de hits the same source neighborhoods repeatedly), dedupe overlapping windows, embed in larger batches, consider decode downscaling for the pixel-NCC path (NCC works at 96px), and only arbitrate scenes that are actually doubtful (tie margins, |rate-1| large, duplicate-flagged) rather than everything. Target ≤120s typical, ≤200s hard.

R6 — Drift. The file carries ~1500 lines of dead subsystems (§6) and ~40 module constants against a budget of 15 (aligner) — the previous sessions' abandoned experiments. Deletion is gated behind M5 (as before) but nothing stops you deleting UNREACHABLE code earlier; less haystack, faster needles.

# 4. Prescribed architecture (revision, not rebuild)

Stages 1-4 keep their contracts (sampling+diff curve → retrieval → per-fragment hypotheses → segmentation DP + chains). The new work is a **Stage 5: native arbitration & precision layer** replacing today's delta-lock/end-snap internals in `_build_matches`, with one shared toolkit:

- A `NativeWindow` primitive: decode episode window [lo, hi] once (cached, deduped across scenes/chains), holding native timestamps, SSCD embeddings (batch), and small grayscale frames for NCC.
- A geometric matcher for query↔source frame pairs: NCC over a small zoom/translation search (the edit may be zoomed — plain center-crop NCC failed on one probe case; include translation), returning both score and best geometry. Geometry is roughly constant per project — estimate it once from high-confidence scenes, then reuse (cheap, and it hardens the zoomed-project path far beyond what query variants did).
- Arbitration passes, in trust order: (1) per-end anchoring on true edge frames (R2); (2) rate arbitration free-slope vs unit-rate (R3); (3) duplicate re-ranking of primary vs its near-tie candidates by pixel score at aligned geometry (R1); (4) chronology/assignment pass for pixel-identical ties (soft forward-monotonic preference within the DP's existing CONTINUITY_REWARD framework or as a post-pass over near-tie sets — your choice, but it must remain soft: reuse exists).
- Every arbitration emits a per-scene confidence + doubt reasons (`duplicate_tie`, `static_end`, `equivalence_accepted`, `low_margin`, ...) — this feeds §8 and ships to production as part of SceneMatch confidence.

Stage 6 (unchanged contract): SceneMatch/MatchList population, alternatives, candidates. Alternatives must expose the losing duplicate instances (they are the safety net that makes WP the mildest failure class — 411f#8 shows what happens when evidence holes leave no candidates at all: three no-match pieces).

Scene axis: keep detector+presnap; add the source-discontinuity interior split (R4); ensure presnap/tug cannot move a boundary that already has strong bilateral line support (the oracle-degradation guard, §7).

# 5. Milestones — gated, in order

Attribute every failure to its owning stage before changing code (is the truth absent from the correspondences? outvoted in hypotheses? mis-segmented? mis-arbitrated? unrefined?). Do not advance while a previous milestone regresses.

- M0 Reproduce the §2 table (fresh + oracle) to sanity-check the environment. Journal it.
- M1 Native precision: source loose ≤3 on every project via per-end anchoring + rate arbitration (R2, R3), without losing source exacts anywhere (leave-one-out). This is the highest-leverage change and builds the toolkit everything else uses.
- M2 Duplicates: WP ≤2 on every project via pixel re-ranking + soft assignment (R1). Pixel-identical residuals may consume the equivalence extension path (owner-approved, journaled) or a §8 waiver.
- M3 Scene axis: scene failed = 0, scene loose ≤3 everywhere (R4: 3 interior splits, ~4 placements, 2 over-merges), plus the oracle guard: a `--gt-scenes` run must return the given boundaries untouched (≥49→54 exact on 85de means the guard currently fails).
- M4 Strict PASS on all four projects from fresh detection within 200s each (R5 perf work lands here), where PASS may include §8 waivers for at most 3 scenes/project after owner review.
- M5 Production: `/matches` route (3 call sites in `backend/app/api/routes/matching.py`) runs the aligner; manual merge/undo APIs and the frontend contract keep working; condemned legacy deleted (§6); `pixi run pytest backend/tests/` green with tests updated; constants audit ≤15 in the aligner with one-line justifications.

# 6. Condemned code (delete at M5; delete earlier if provably unreachable)

In `scene_aligner.py`: the legacy Stage-4 grouper chain (`_segment_decoded_continuities`, `_decoded_fragment_groups`, `_fit_decoded_group`, `_measure_group_fit`, `_group_fit_score`, `_group_has_clear_changepoint*`, `_group_has_uncovered_source_gap`, `_decoded_boundary_gap`, `_group_changepoint_summary`, `_bic`, `_merge_decoded_continuities`, `_should_merge_pair`), the dead native verifier (`_verify_continuations` + `PIXEL_GAP_*`/`VERIFY_CONTINUATION_RATIO` constants — superseded by the Stage-5 toolkit, mine it for the pixel-alignment code before deleting), `_snap_final_boundaries`, `_refine_boundaries_with_query_embeddings`, the unused decode-DP path (`decode_scene_sequence`, `_decode_sample_path`, `_add_global_path_segments`, `_sample_transition_score`, `_crosses_scene_boundary`, `_transition_score`, `_emission_score`, `_speed_prior_penalty`) and the `_query_boundary_embeddings` call whose result nothing consumes. Evaluate the lazy query-variant path (`sample_query_variants`, `_weak_scene_sample_indices`, 7-20s/project) against the Stage-5 geometry estimate: if geometry-aware native arbitration covers the zoomed project, the variant path dies too. In `scene_detector.py`/`scene_merger.py`: the `AUTO_DENSE_*` scene-count gates and `DENSE_SCENE_COUNT` machinery remain the known overfitting pattern — remove them as the aligner takes ownership of their responsibilities, with leave-one-out proof. In `anime_matcher.py`: the 13 correction passes, additive bonus ranking, and per-episode crop-index subsystem (keep `_refine_boundaries`, frame/embedding utilities, and everything the aligner calls).

# 7. Anti-overfitting guardrails (binding)

- No gate or constant keyed on scene index, scene count/ranges, fixture-tuned medians, project id, episode name, or fixture timestamps. No per-scene special-case branches to fix a named failing scene, ever.
- Leave-one-out for any threshold/prior change: all four projects, each held out in turn; keep only if no rotation degrades its held-out project and the aggregate improves. Never keep a change justified by one project's numbers.
- Constants budget: ≤15 in the aligner at M5, each derived from a stated domain fact, named at module top with one-line justification. If you need a 16th, the model is wrong — stop and rethink.
- Synthetic unit tests encode the domain model (fabricated clouds: noise, duplicates, speed changes, non-monotonic jumps, an intruder episode, groups needing fusion, one interior split) — never fixture timestamps. Keep `backend/tests/test_scene_aligner.py` in this spirit; add the same for the native-arbitration toolkit (fabricated frame sequences with known offsets/zooms).
- The oracle guard (new): every all-four measurement runs fresh AND `--gt-scenes`; the oracle run must never return worse boundaries than it was given.
- The evaluator may gain diagnostics, never looser tolerances. Equivalence extensions: pixel-proven, journaled, reversible, one at a time.

# 8. Doubt review & waiver protocol (owner-approved 2026-07-10)

When you believe a residual failure is content-undecidable, GT-noise, or visually equivalent, do not tune toward it — surface it:
- The evaluator gains a `--review out.html` mode: for every doubtful scene (failed/loose/WP + every equivalence acceptance + every waiver candidate), render side-by-side frame strips (TikTok scene frames | generated source interval | GT source interval, a few frames each, embedded as data URIs) with the numbers and the doubt reason. One self-contained HTML per project.
- The owner's verdicts go into `backend/data/eval_waivers.json` (repo-tracked, OUTSIDE the GT folders): `{project_id, gt_scene_index, axis: "scene"|"source", generated: [s,e], verdict: "pass"|"fail", note, date}`. The evaluator counts owner-passed waivers as exact but reports them in their own column — a PASS built on more than 3 waivers/project is a ceiling report, not a PASS.
- Pre-authorized: 411f scene #5 may be waived without review if it resists (known-approximate GT). dcd/85de may contain 1-2 hard-case GT timestamps (owner statement): if the machine result disagrees sub-second AND the pixel evidence supports the machine, submit it for review rather than absorbing it into the loose budget.
- Batch reviews at milestone boundaries; never block mid-iteration on a review.

# 9. Work loop and experiment journal

Maintain `docs/GOAL_JOURNAL.md` (fresh file; the old journal is archived at `docs/GOAL_JOURNAL_ARCHIVE_2026-07-04_to_2026-07-07.md` and its numbers are void). Every iteration: hypothesis → metric before → change (one line) → metric after on ALL FOUR projects (fresh + oracle) → keep/revert + why. No tuning without a journal entry. Re-read your last three entries before each change: three consecutive entries chasing one project is the overfitting spiral — stop and re-diagnose at stage level.

Commands:
- `pixi run python backend/scripts/evaluate_matching_against_ground_truth.py dcd74148c7ec 85de83ca6323 411f73d26c1d 5e85164d9ff8 --matcher aligner [--gt-scenes] [--save-generated-json ~/.cache/atr-eval/<tag>.json]`
- `pixi run pytest backend/tests/`
- `git diff -- backend/data/projects/dcd74148c7ec backend/data/projects/85de83ca6323 backend/data/projects/411f73d26c1d backend/data/projects/5e85164d9ff8`
- Diagnostics: `backend/scripts/diagnostics/attrib_boundaries.py` (scene-axis attribution vs a saved run JSON), `backend/scripts/diagnostics/probe_duplicate_separation.py` (native SSCD-vs-pixel separation probe). Keep eval artifacts in `~/.cache/atr-eval/` (survives reboots; the scratchpad does not).

# 10. Acceptance criteria

Per project, from fresh scene detection, strict evaluator (tolerances unchanged):
- Scene axis: correct cuts/fusions relative to GT under the folding rules (failed = 0), start/end within ±0.3s for every scene except ≤3 within ±1.0s.
- Source axis: primary episode+interval within ±0.3s except ≤3 within ±1.0s; ≤2 wrong-primary scenes whose correct source timing is present in the exposed candidate data; zero scenes whose truth is absent from primary AND candidates.
- Owner waivers (§8): ≤3/project, each reviewed.
- End-to-end ≤200s per project on this machine; GPU memory stays adaptive (existing OOM-backoff paths).
- Oracle guard holds; SceneMatch/MatchList contract and manual merge/undo APIs unchanged; regression tests pass; GT folders untouched.

# 11. Final report

- §2 table vs final table (fresh + oracle, all metrics + elapsed), per-milestone journal summary including rejected tunings and their leave-one-out evidence.
- Per-scene confidence/doubt output demonstrated on one project; review HTML generated at least once; waivers file state.
- Constants inventory (≤15) with justifications; deleted code inventory (subsystems + line counts); tests added/updated.
- Explicit `git diff` confirmation the GT folders are unchanged.
- If blocked (missing videos/models/index, CUDA/FAISS failures): report the exact path/error and stop rather than claiming success.
