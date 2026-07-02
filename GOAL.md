/goal Rebuild this repository's anime scene matching around a global sequence-alignment core, then validate it strictly on the four curated ground-truth projects. This document was produced after a full computer-vision audit of the current pipeline (code reading + fresh baseline runs on all four projects). It prescribes an architecture, not just a target: the previous iteration loop failed because it kept the per-scene architecture and accumulated ground-truth-specific patches. Do not repeat that. You have explicit permission to rework or delete any part of the matching pipeline.

# 1. Problem statement (computer vision framing)

A project is a short vertical TikTok edit (60-180s) cut from anime episodes. The task: recover, for every scene of the edit, the source episode and the source start/end timestamps, precisely enough that a human never has to scrub the episode manually.

Measured invariants of the domain (from the four ground-truth projects — treat these as priors of the model, not as constants to hardcode):

- One dominant source episode per edit (47/47 to 55/55 scenes), with occasionally a few intruder scenes from other episodes of the same series (up to 5/52).
- Scene order is quasi-chronological in the source: 86-100% of adjacent scene pairs are monotonic. Non-monotonic jumps exist and must be representable (one project opens with an intro montage jumping 200→193→196→199→118s in the source).
- Playback speed per scene is roughly constant, centered on 1.0x, commonly 0.5x-1.7x. Ground truth contains at least one 4.07x scene — note the current MAX_SPEED=1.60 makes that scene structurally unfindable; the new core must not have such a hard ceiling on evidence collection.
- Scenes are short: median 1.1-2.6s, min ~0.5s, max ~15s. Dense edits have 46-55 scenes in ~60s.
- Edits are vertical (9:16); sources are 16:9. Crops/zooms on the query side are common (one full project is a zoomed edit).
- The library index (anime_searcher, DO NOT MODIFY) stores SSCD embeddings of episode frames at 2.0 fps in FAISS. Search is essentially free (0.57s total per project measured). Index temporal resolution is 0.5s: sub-grid precision can only come from decoding the matched episode locally at native fps.

In this structure, matching is a global alignment problem: in the (t_tiktok, t_source) plane, each real clip is a near-straight line segment whose slope is the playback speed. The correct algorithm finds those segments globally. The current algorithm instead matches each scene independently from 3 probe frames and then tries to restore global coherence with 13 sequential correction passes — that is the root cause of both the precision plateau and the overfitting spiral.

# 2. Honest diagnosis of the current pipeline (measured 2026-07-02, fresh runs)

Baseline, fresh scene detection, current main branch:

| Project | Scenes (gen/GT) | Scene timing exact/loose/fail | Source exact/loose/wrongPrimary/fail | Elapsed | Verdict |
|---|---|---|---|---|---|
| dcd74148c7ec | 20/20 | 18/2/0 | 13/5/2/0 | 268s | FAIL (5 loose > 3) |
| 85de83ca6323 | 55/55 | 53/2/0 | 30/16/9/0 | 134s | FAIL (16 loose, 9 wrong primary) |
| 411f73d26c1d | 52/52 | 16/1/35 | 8/4/8/32 | 412s | FAIL (~2-4 real cut/fusion mistakes cascading into index-shifted failures) |
| 5e85164d9ff8 | 46/46 | 5/3/38 | 3/1/3/39 | 331s | FAIL (fast-montage under-segmentation; truth often absent from candidates) |

Read the failure modes carefully — each project exposes a different one:

- dcd74148c7ec: sub-grid precision exists (refinement works) but is discarded by the bonus ranking; outputs sit on the 0.5s grid.
- 85de83ca6323 (dense zoomed edit): 3-probe per-scene evidence is too weak on ~1s zoomed scenes; 16 loose + 9 wrong primaries despite the very correction passes that were tuned for it.
- 411f73d26c1d: a handful of wrong cut/fusion decisions cascade into 35 index-shifted scene failures.
- 5e85164d9ff8 (fast montage, non-monotonic intro jumping 200→193→196→199→118s in the source): the detector under-segments fast cuts, and for many scenes the correct source interval is absent from every exposed candidate — an evidence-recall failure that no ranking or correction pass can fix downstream.

What works and must be kept (or kept in spirit):

- Scene detection + tiny-scene sanitizing + continuity fusion produce the correct scene counts on all four projects, and ≥90% exact scene timings on the two projects without fusion mistakes. PySceneDetect ContentDetector at threshold 16 is a sound cut detector; the fragile part is the match-driven fusion/split decisions, which Stage 4 below replaces.
- SSCD embeddings discriminate well; FAISS retrieval is fast and mostly puts the right neighborhood in top-k.
- `_refine_boundaries` (native-fps local re-embedding around a boundary) is the right precision mechanism. It is currently wasted: refined proposals carry a +0.02 selection bonus and routinely lose the ranking to coarse "projected" proposals carrying +0.25, which is why final outputs often sit exactly on the 0.5s index grid, one grid step off ground truth (e.g. generated 636.00-637.00 vs GT 635.50-636.50).
- The output data contract (SceneMatch with alternatives, start/middle/end candidates, merged_from) — the frontend and manual-override UI depend on it.

What is condemned and must be deleted, not adapted:

- The 13 post-hoc correction passes called at the end of `match_scenes` (`_stabilize_short_scene_sequence`, `_stabilize_monotonic_source_sequence`, `_snap_short_scene_reset_edges`, `_stabilize_monotonic_tail_pair`, `_promote_dense_short_alternatives`, `_promote_dense_local_source_alternatives`, `_promote_dense_boundary_supported_alternatives`, `_promote_duration_consistent_weighted_alternatives`, `_extend_underfilled_source_end_candidates`, `_extend_end_to_next_start_candidates`, `_extend_monotonic_speed_floor_alternatives`, `_promote_short_end_projection_start_anchors`, `_promote_dense_source_cut_aligned_alternatives`, `_promote_supported_local_bracket_refinements`). They encode fixture-specific behavior behind pseudo-general gates: `smooth_start_index = 26`, `len(scenes) < 30`, `median_duration > 2.2`, `scene.index < 33` (in the main loop!). Even with all of them, the dense project fails 16 loose + 9 wrong-primary.
- The additive selection-bonus ranking (+0.25 projected, +0.10 merged seed, +0.02 direct/refined, -0.25/-0.20/-0.05 algorithm penalties). Hand-tuned score soup; it discards the refined (precise) result by construction.
- The per-episode crop-index subsystem (`_load_or_build_crop_index`, `_search_crop_index_batch`, `_search_local_crop_windows_batch` and friends): 140s of the 268s baseline on the smallest project (52 crop-recovery calls for 50 scenes). Crop invariance belongs on the query side (a TikTok frame is the cropped one — search several un-crop/pad variants of the query frame), not on N precomputed crop indexes of every episode.
- Per-scene 3-probe evidence (start/middle/end frames only). On 1s zoomed scenes this is why the dense project collapses.

# 3. Prescribed architecture: dense correspondences + robust segment fit + global decode

Build a new alignment core (new module, e.g. `backend/app/services/scene_aligner.py`) with five stages. Stage contracts are fixed; implementation inside each stage is yours. If you want to deviate from a stage design, you must first show a measured leave-one-out comparison justifying it.

Stage 1 — Dense query sampling and embedding.
Decode the TikTok once, sequentially (single VideoCapture pass, no per-scene seeking). Sample at a uniform rate (target 4-8 fps; keep it a named constant). Embed all frames in large adaptive GPU batches (existing `_embed_pil_batch` already handles OOM backoff; measured throughput ~124 img/s on the RTX 4070 Laptop, so a 60-180s edit costs ~4-25s). For each sampled frame also embed a small set of query-side crop variants (center-crop widescreen reconstruction, top/bottom pad removal) — only when the plain query yields weak evidence in Stage 3, lazily.
Output: `QuerySample[] = (t_tiktok, embedding, variant_id)`.

Stage 2 — Batched retrieval.
One batched FAISS search over all samples (top-k per sample, k around 10-20, series-scoped as today).
Output: correspondence cloud `C = {(t_tiktok, t_source, episode, similarity)}`.

Stage 3 — Robust segment extraction (the new heart).
In the (t_tiktok, t_source) plane, find line segments: clusters of correspondences that agree on (episode, offset, slope) with slope in a wide speed range (do not clamp at 1.6x; use a generous bound like 0.25x-5x for evidence collection). Hough-style voting on (episode, quantized offset) with slope refinement by least squares on inliers, or RANSAC per episode — your choice. A correct segment typically has many inliers per second of scene at 4-8 fps sampling; isolated correspondences are noise. Anime repeats near-identical frames (stills, backgrounds), so ambiguity is normal — keep all plausible segments with their inlier support, do not decide locally.
Output: `SegmentHypothesis[] = (episode, t_tiktok_range, affine map t_source = a*t_tiktok + b, inlier count, mean similarity)`.

Stage 4 — Global decode on the timeline (Viterbi/DP).
Given detector scene cuts (keep the current PySceneDetect + tiny-scene sanitizing) and segment hypotheses, run a dynamic program over the scene sequence: state = segment hypothesis covering the scene (or "no match"), emission = inlier support + similarity within the scene span, transition = reward source-continuity across the cut (predicted end of scene N vs start of N+1 within a grid step), mild penalty for episode switches and backward jumps, zero reward for staying — never a hard constraint, the 4.07x scene and non-monotonic jumps must survive decoding. This single DP replaces all 13 correction passes and the continuity second match pass. Scene fusion falls out of it: adjacent detector scenes decoded to the same continuous mapping merge (this replaces `detect_continuous_pairs`/chain building for the automatic phase; keep the manual-merge service API working).
The decode must be able to repair the detector in both directions: merge adjacent detector scenes decoded to one continuous mapping, and split a detector scene whose interior shows a clear affine-map break in the correspondence cloud (change-point in the residuals), snapping the inserted cut to the strongest local visual discontinuity (the frame-diff snap idea already in `snap_dense_visual_boundaries`). The measured 411f73d26c1d baseline shows why: only ~2-4 real cut/fusion mistakes (one missed cut, two spurious ones, one bad fusion) cascade into 35 index-shifted scene failures under strict validation.
Output: per final scene, the chosen (episode, source interval) plus ranked alternative hypotheses.

Stage 5 — Native-fps boundary refinement, authoritative.
For each final scene boundary, refine with the existing `_refine_boundaries` mechanism (decode the episode locally at native fps, argmax cosine against the actual first/last TikTok frame of the scene). The refined result is authoritative when its similarity clears a sanity check against the unrefined one — it must not compete in a bonus ranking. This is what converts 0.5s-grid outputs into ±0.3s outputs.

Populate the existing SceneMatch contract from these stages: primary = decoded choice; alternatives = other surviving segment hypotheses mapped through the scene span; start/middle/end candidates = raw correspondences near the scene edges (the UI uses them for manual override). Preserving this contract is part of the acceptance criteria.

Performance envelope (measured, not aspirational): decode+embed ~5-25s, FAISS ~1-3s, segment extraction and DP are milliseconds of numpy, refinement is the only remaining per-boundary video decoding — bound it to final boundaries only (~40-60 per project). Total target below.

# 4. Anti-overfitting guardrails (hard rules)

The previous loop died by fitting the four fixtures. These rules are as binding as the acceptance criteria:

- Leave-one-out protocol: when tuning any threshold or prior, evaluate on all four projects but treat each in turn as held-out: a change is kept only if it does not degrade the held-out project for any rotation and improves the aggregate. Never keep a change that helps exactly one project and is justified by nothing but that project's numbers.
- Banned code patterns, enforced by review of your own diff before every commit: no gate or constant keyed on scene index, scene count, scene-count ranges, median durations tuned to a fixture, project id, episode name, or timestamp literals from the fixtures. `smooth_start_index = 26` and `scene.index < 33` are the canonical crimes.
- Every numeric constant in the new core must be (a) derived from a domain fact stated in section 1 (index grid, speed range, sampling rate, similarity floor), (b) named and centralized at module top with a one-line justification, and (c) counted: if the new core needs more than ~15 such constants, the model is wrong — stop and rethink instead of adding the 16th.
- No per-scene special-case branches added to fix a named failing scene. If a scene fails, diagnose which stage lost the truth (was the correspondence absent in Stage 2? outvoted in Stage 3? mis-decoded in Stage 4? unrefined in Stage 5?) and fix that stage's model. The stage structure exists precisely so failures are attributable.
- Ground-truth folders are read-only: never modify, save into, normalize, or rewrite `backend/data/projects/{dcd74148c7ec,85de83ca6323,411f73d26c1d,5e85164d9ff8}`. Validate with `git diff` at the end. All experiment outputs go to temp/scratch paths.
- Do not edit the anime_searcher submodule or reindex the library.

# 5. Acceptance criteria

For each ground-truth project, run fresh scene detection, the new alignment core, and automatic fusion (never seed from ground-truth scenes), via `backend/scripts/evaluate_matching_against_ground_truth.py` (keep its strict checks; adapt its internals only to call the new core, not to loosen tolerances).

A project passes only if:

- Generated scene list has the correct cuts/fusions relative to ground truth (equal counts, aligned indices).
- Scene start/end within ±0.3s for every scene, except at most 3 scenes within ±1.0s.
- Primary match has correct episode and source timing within the same tolerance rules, except at most 2 scenes with a wrong primary; for those, the correct source timing must be present in the exposed candidate data (alternatives or start/end candidates) so the user can pick it directly.
- No scene requires manually searching the episode.

Additionally:

- End-to-end elapsed time ≤ 120s per project on this machine (RTX 4070 Laptop 8GB, assume 2-3 concurrent matching jobs elsewhere: keep GPU memory adaptive and conservative; the existing OOM-backoff embedding path is the pattern to follow).
- The SceneMatch/MatchList output contract and the manual merge/undo service APIs keep working (frontend depends on them); `pixi run pytest backend/tests/` regressions must pass, updating tests whose behavior legitimately changed with the rework.
- The four ground-truth folders unchanged (`git diff` clean on those paths).

# 6. Work loop

1. The section 2 baseline is already measured — do not spend time re-running it except to sanity-check your environment. Build the new core alongside the old one (`scene_aligner.py`), behind a switch in the evaluator so both can be compared on the same fresh scenes.
2. Develop stages 1-3 first and validate them standalone: for each ground-truth project, dump the correspondence cloud and check that for every GT scene, some segment hypothesis within ±0.5s of GT truth exists with healthy inlier support. This recall-of-evidence metric is your leading indicator: if the truth is not in the hypotheses, no decoder can recover it — fix sampling/crop-variants/retrieval before touching the DP.
3. Add the DP decode (Stage 4) and measure primary accuracy. Then wire Stage 5 refinement as authoritative and measure the ±0.3s exact rate.
4. Unit-test each stage with synthetic data (fabricated correspondence clouds with known segments, noise, speed changes, non-monotonic jumps, an intruder episode) — these tests encode the domain model, not the fixtures. Keep the existing merge/cache tests passing or update them for the new flow.
5. Iterate under the leave-one-out protocol of section 4 until all four projects pass the strict validator from fresh detection. When a project shows many consecutive index-shifted scene failures, diagnose it as a cut/fusion error (compare boundary sets, not per-index deltas) before touching anything in matching.
6. Delete the condemned code (13 passes, bonus ranking, per-episode crop indexes) once the new core wins on all four projects; keep git history as the archive. Re-run the full validation after deletion.
7. Run regression tests; verify ground-truth folders with git diff.

Suggested commands:

- `pixi run python backend/scripts/evaluate_matching_against_ground_truth.py dcd74148c7ec 85de83ca6323 411f73d26c1d 5e85164d9ff8`
- `pixi run pytest backend/tests/`
- `git diff -- backend/data/projects/dcd74148c7ec backend/data/projects/85de83ca6323 backend/data/projects/411f73d26c1d backend/data/projects/5e85164d9ff8`

# 7. Final report

- Baseline (section 2 table) vs final: per-project strict metrics and elapsed time.
- Evidence-recall metric per project after Stage 3 (from work-loop step 2).
- Leave-one-out summary: which tunings were tested, which were rejected for degrading a held-out project.
- Inventory of every numeric constant in the new core with its one-line justification (the ≤15 budget of section 4).
- Code deleted (functions/subsystems) and tests added/updated.
- Any new dependency and why.
- Explicit confirmation the four ground-truth folders are unchanged.
- If blocked by missing videos, model files, indexes, CUDA/FAISS issues, or reproducibility problems: report the exact path/error and stop instead of claiming success.
