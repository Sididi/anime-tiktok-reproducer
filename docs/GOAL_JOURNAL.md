# Global Scene Aligner Journal

## 2026-07-04 - Baseline reproduction

- Hypothesis: Current `main` reproduces the measured dcd74148c7ec aligner failure before any code changes.
- Metric before: Section-2 audit reports dcd74148c7ec generated/GT scenes 50/20, Stage-3 evidence recall 19/20, elapsed 96s, strict FAIL.
- Change: None; baseline run only.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 50/20, Stage-3 evidence recall 19/20, elapsed 89.2s, strict FAIL. All-four metrics deferred until the first Stage-4 change.
- Keep/revert + why: Kept as baseline; no code changed.

## 2026-07-04 - Stage 4 joint-fit segmentation v1

- Hypothesis: Replacing the no-op remap with DP continuity reward plus joint-fit fragment grouping will reduce over-segmentation without reducing Stage-3 evidence recall.
- Metric before: dcd74148c7ec generated/GT scenes 50/20, Stage-3 evidence recall 19/20, elapsed 89.2s, strict FAIL.
- Change: Wire `_segment_decoded_continuities`, add pooled affine refits and BIC-style changepoint rejection over decoded same-episode runs, and reuse frame-diff boundary snapping for final aligner scenes.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 24/20, Stage-3 evidence recall 19/20, elapsed 73.2s, strict FAIL. Full all-four run deferred because M1 still fails on the sanity project.
- Keep/revert + why: Kept temporarily as a clear Stage-4 improvement over 50/20 and lower runtime; needs all-four validation once M1 is plausible.

## 2026-07-04 - Stage 4 continuity reward v2

- Hypothesis: A stronger same-episode continuity reward will let weak true fragment hypotheses beat high-emission discontinuous offsets inside long clips.
- Metric before: dcd74148c7ec generated/GT scenes 24/20, Stage-3 evidence recall 19/20, elapsed 73.2s, strict FAIL.
- Change: Increase DP continuity reward from 1x to 2x the episode-switch penalty when the source gap is within the index-grid continuity window.
- Metric after on all four projects: dcd74148c7ec unchanged at generated/GT scenes 24/20, Stage-3 evidence recall 19/20, elapsed 72.1s, strict FAIL.
- Keep/revert + why: Keep only if the next all-four run shows no held-out degradation; by itself it did not move the sanity project.

## 2026-07-04 - Stage 4 same-episode discontinuity penalty v3

- Hypothesis: Penalizing large same-episode source gaps in both directions will stop high-emission but discontinuous detector fragments from splitting true continuous clips.
- Metric before: dcd74148c7ec generated/GT scenes 24/20, Stage-3 evidence recall 19/20, elapsed 72.1s, strict FAIL.
- Change: Apply the existing backward-jump penalty to any same-episode transition outside the continuity window, not only negative gaps.
- Metric after on all four projects: dcd74148c7ec unchanged at generated/GT scenes 24/20, elapsed 71.3s, strict FAIL.
- Keep/revert + why: Marked suspect; it had no measurable effect on the sanity project and should be reverted unless all-four validation later shows value.

## 2026-07-04 - Stage 4 absorb weak decoded fragments v4

- Hypothesis: The segmentation pass is over-splitting because a weak wrong decoded fragment blocks a pooled fit for the surrounding episode.
- Metric before: dcd74148c7ec generated/GT scenes 24/20, elapsed 71.3s, strict FAIL.
- Change: Let pooled group fitting keep the starting episode while testing additional fragments, even when the fragment's decoded primary is no-match, another episode, or another offset.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 20/20, elapsed 69.6s, strict FAIL because the first fragment remains split and later true boundaries are offset/mis-grouped.
- Keep/revert + why: Keep temporarily as it fixes count on the sanity project; next change must fix index-aligned segmentation, not just count.

## 2026-07-04 - Stage 4 interval partition DP v5

- Hypothesis: A true interval partition over detector fragments will choose aligned scene groups better than greedy extension because it can trade one group cost against pooled affine support globally.
- Metric before: dcd74148c7ec generated/GT scenes 20/20 but index-misaligned, elapsed 69.6s, strict FAIL.
- Change: Replace greedy grouping with a DP over candidate fragment intervals scored by pooled affine fit support, residuals, coverage, and a per-scene model cost.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 13/20, elapsed 65.6s, strict FAIL; under-segmented because the per-scene model cost was too strong.
- Keep/revert + why: Keep the interval-DP structure temporarily, tune the model cost once, then evaluate all four before keeping.

## 2026-07-04 - Stage 4 interval DP model cost v6

- Hypothesis: Reducing the interval-DP per-scene model cost will restore true source-change splits while preserving weak-fragment absorption.
- Metric before: dcd74148c7ec generated/GT scenes 13/20, elapsed 65.6s, strict FAIL.
- Change: Lower partition model cost from 2.0 to 1.0 score units by using half the minimum-inlier count.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 40/20, elapsed 86.6s, strict FAIL; over-segmented badly.
- Keep/revert + why: Reject 1.0 cost; too sensitive and slower.

## 2026-07-04 - Stage 4 interval DP model cost midpoint v7

- Hypothesis: The midpoint model cost can balance v5 under-segmentation and v6 over-segmentation; if not, the score form is wrong.
- Metric before: dcd74148c7ec was 13/20 at cost 2.0 and 40/20 at cost 1.0.
- Change: Set interval-DP model cost to 1.5 score units by using 0.75x the minimum-inlier count.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 13/20, elapsed 66.6s, strict FAIL.
- Keep/revert + why: Rejected; the interval-DP score form is not reliable enough. Active path reverted to v4 greedy pooled-fit grouping before the next experiment.

## 2026-07-04 - Stage 4 boundary split/merge post-pass v8

- Hypothesis: The v4 greedy groups need local boundary repair: split only when two pooled sub-fits both cover the boundary-edge fragments and disagree in source time, then merge adjacent groups whose source intervals are continuous.
- Metric before: Best active baseline was v4: dcd74148c7ec generated/GT scenes 20/20, elapsed 69.6s, strict FAIL due index-misaligned groups.
- Change: Add recursive edge-covered discontinuity splitting and adjacent continuity merging after greedy pooled-fit grouping.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 17/20, elapsed 67.4s, strict FAIL.
- Keep/revert + why: Rejected; the local split/merge repair over-merged and worsened count/alignment. Active path reverted to v4 grouping.

## 2026-07-04 - Active v4 all-four measurement

- Hypothesis: Before further Stage 4 changes, the best active v4 path should be measured across all four projects to avoid one-project tuning.
- Metric before: dcd74148c7ec v4 generated/GT scenes 20/20, elapsed 69.6s, strict FAIL due index-misaligned groups.
- Change: No code change; all-four measurement of active v4 path.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 66.7s FAIL; 85de83ca6323 42/55 elapsed 67.4s FAIL; 411f73d26c1d 48/52 elapsed 124.0s FAIL; 5e85164d9ff8 35/46 elapsed 57.8s FAIL.
- Keep/revert + why: Keep v4 only as a temporary baseline. The zoomed project under-recall/under-segmentation matches P2, so the next change is prescribed always-on `wide_pad`.

## 2026-07-04 - Stage 1 always-on wide_pad v9

- Hypothesis: Always embedding `plain` plus `wide_pad` query variants will restore zoomed-project evidence and give Stage 4 enough correct support without lazy weak-scene gating.
- Metric before: 85de83ca6323 active v4 generated/GT scenes 42/55, elapsed 67.4s, strict FAIL; Section-2 probe measured wide_pad recall improvement from 48/55 to 52/55.
- Change: Sample every dense frame as `plain` and `wide_pad`, remove the lazy weak-scene variant retrieval pass, and merge variant retrievals by `(episode, t_tiktok, t_source)`.
- Metric after on all four projects: 85de83ca6323 generated/GT scenes 40/55, Stage-3 evidence recall 38/55, elapsed 228.1s, strict FAIL.
- Keep/revert + why: Not sufficient as implemented; keep temporarily only to test whether Stage-3 seed truncation is masking the retrieved wide-pad hits.

## 2026-07-04 - Stage 3 top-60 seed budget v10

- Hypothesis: The always-on wide-pad hits are present in top-60 retrieval but lost because Stage 3 seeds from only the top ~10 hits per sample time.
- Metric before: 85de83ca6323 with always-on wide_pad had Stage-3 evidence recall 38/55 and elapsed 228.1s.
- Change: Use the retrieval top-k budget for per-time line seeds and edge-pair seeds.
- Metric after on all four projects: 85de83ca6323 generated/GT scenes 39/55, Stage-3 evidence recall 37/55, elapsed 228.7s, strict FAIL.
- Keep/revert + why: Rejected; wider seeding did not recover zoom evidence and stayed far over budget. Active path reverted to the v4 lazy-variant baseline.

## 2026-07-04 - Stage 4 discontinuous changepoint v11

- Hypothesis: The v4 grouper cuts continuous clips because BIC favors two low-residual local lines even when their source positions are continuous across the detector boundary.
- Metric before: Active v4 all-four measurement: dcd74148c7ec 20/20 but index-misaligned; 85de83ca6323 42/55; 411f73d26c1d 48/52; 5e85164d9ff8 35/46.
- Change: Make `_group_has_clear_changepoint` reject a pooled group only when the best two-line split improves BIC and the two fitted source positions are discontinuous by more than the index-grid continuity window.
- Metric after on all four projects: dcd74148c7ec generated/GT scenes 18/20, elapsed 64.5s, strict FAIL.
- Keep/revert + why: Rejected; it still split the first true clip and under-segmented later clips. Active changepoint logic reverted to v4.

## 2026-07-04 - Stage 4 pooled refit correspondence seeds v12

- Hypothesis: Pooled group fits are missing the correct line because they seed only from the decoded fragment primaries; when those primaries are wrong, the truth in the correspondence cloud is never refit.
- Metric before: With GT scene boundaries on dcd74148c7ec, active v4 still merged to 16/20 and failed, proving Stage 4 refit/decode ownership beyond raw detector placement.
- Change: Add slope-1 and bounded pairwise seeds from the candidate group's own correspondences before the pooled inlier refit.
- Metric after on all four projects: GT-boundary dcd improved only to 18/20; fresh dcd regressed to 32/20, elapsed 87.2s, strict FAIL.
- Keep/revert + why: Rejected; extra seeds let spurious local lines dominate pooled groups and badly over-segmented fresh detection. Active path reverted to v4.

## 2026-07-04 - Stage 4 transition prior rollback v13

- Hypothesis: Penalizing all same-episode forward source gaps makes the DP choose wrong continuous alternatives across true cuts; the domain prior only calls for backward-jump penalties plus a continuity reward.
- Metric before: Active v4 dcd has correct scene count but wrong index alignment; hypothesis diagnostics show correct fragment candidates are often present but decoded primaries prefer source-continuous wrong alternatives.
- Change: Revert same-episode forward-gap penalty and reduce the continuity reward from 2x to 1x `EPISODE_SWITCH_PENALTY`.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 66.4s FAIL; 85de83ca6323 44/55 elapsed 67.9s FAIL; 411f73d26c1d 50/52 elapsed 124.8s FAIL; 5e85164d9ff8 35/46 elapsed 57.5s FAIL.
- Keep/revert + why: Kept; it restores the prescribed transition semantics and improves scene count on two projects without count regression on the others.

## 2026-07-04 - Stage 4 ungated final boundary snap v14

- Hypothesis: Final aligner boundaries are not being snapped on most projects because `_snap_final_boundaries` calls the legacy dense-gated snap candidate helper.
- Metric before: v13 all-four scene timing remains mostly failed even when counts are close; P3 measured systematic 0.2-0.4s boundary placement offsets outside dense-only cases.
- Change: Replace the dense-gated candidate call with an aligner-local frame-diff snap candidate finder that considers every final boundary while preserving the existing window/diff/ratio/duration safeguards.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 66.6s FAIL; 85de83ca6323 44/55 elapsed 71.1s FAIL; 411f73d26c1d 50/52 elapsed 126.5s FAIL; 5e85164d9ff8 35/46 elapsed 59.8s FAIL.
- Keep/revert + why: Kept for now; it is count-neutral and moves boundary snapping into the aligner as prescribed, but M1 remains owned by Stage 4 segmentation rather than snap placement.

## 2026-07-04 - Stage 4 interval segmentation DP v15

- Hypothesis: Greedy grouping makes irreversible early cuts before later pooled evidence is visible; scoring bounded candidate groups with a DP will preserve jointly coherent fused scenes while cutting at source-discontinuous changepoints.
- Metric before: v14 all-four counts: dcd74148c7ec 20/20 FAIL; 85de83ca6323 44/55 FAIL; 411f73d26c1d 50/52 FAIL; 5e85164d9ff8 35/46 FAIL.
- Change: Replaced greedy `_decoded_fragment_groups` with interval DP over pooled group fits, made changepoint rejection require both BIC improvement and source discontinuity, and penalized DP cuts between source-continuous adjacent groups.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 13/20, elapsed 66.2s, strict FAIL.
- Keep/revert + why: Rejected before all-four evaluation; it under-segmented the sanity project badly, so the interval score over-rewarded long pooled fits. Active path reverted to v14.

## 2026-07-04 - Stage 4 source-discontinuous changepoint v16

- Hypothesis: The v14 grouper shifts boundaries because BIC alone allows one pooled line to absorb a detector fragment across a real source jump; pooled split fits should force a cut when their predicted source positions disagree by more than the index-grid continuity window.
- Metric before: Active v14 dcd74148c7ec generated/GT scenes 20/20, elapsed 65.8s in the saved `/tmp` diagnostic, but 19/20 scene timings failed because the first false split shifted later indices and several true cuts were absorbed into neighboring groups.
- Change: Extended `_group_has_clear_changepoint` so a candidate group is rejected when any pooled two-side split has a source discontinuity beyond the continuity window, even if BIC alone does not select that split.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 23/20, elapsed 68.5s, strict FAIL.
- Keep/revert + why: Rejected before all-four evaluation; pooled source-gap cuts are too aggressive on short/noisy fragments and recreate the pairwise-oversegmentation failure mode. Active path reverted to v14.

## 2026-07-04 - Stage 4 speed-prior changepoint BIC v17

- Hypothesis: False pooled groups often span real source jumps by fitting an implausibly fast single line; changepoint model selection should include the existing speed prior so one high-speed line is compared fairly against two ordinary-speed lines.
- Metric before: Active v14 dcd diagnostic shows false groups such as 8.50-10.63s decoded as a ~4.4x line across source 618.92-628.33, while adjacent GT clips are separate lower-speed intervals.
- Change: Added per-inlier speed-prior cost to the one-line and two-line BIC scores inside `_group_has_clear_changepoint`.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 18/20, elapsed 65.0s, strict FAIL.
- Keep/revert + why: Rejected before all-four evaluation; the prior changed which false groups survived but under-segmented the sanity project, so speed prior is not enough without a stronger segmentation formulation. Active path reverted to v14.

## 2026-07-04 - Stage 4 group diagnostics v18

- Hypothesis: Further dcd-only threshold changes are overfitting; the next Stage 4 change needs explicit group-fit diagnostics saved by the evaluator so failures can be attributed across projects.
- Metric before: Active v14 all-four counts remain dcd74148c7ec 20/20, 85de83ca6323 44/55, 411f73d26c1d 50/52, 5e85164d9ff8 35/46, all strict FAIL.
- Change: Add Stage 4 group records to `AlignmentDiagnostics` and persist them in evaluator-generated JSON without changing matching output.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 20/20, elapsed 67.1s, strict FAIL; output pattern unchanged and `/tmp/dcd_v18_generated.json` now contains `aligner_debug.stage4_groups`.
- Keep/revert + why: Kept; behavior-neutral diagnostics make the next Stage 4 change attributable without touching fixtures.

## 2026-07-04 - Stage 4 decoded-fragment diagnostics v19

- Hypothesis: Selected group records alone do not explain whether bad fusion comes from decoded fragment continuity or pooled changepoint selection; save decoded fragment intervals and per-group best split summaries before another Stage 4 change.
- Metric before: v18 dcd diagnostics show the first generated group is fragment `[0]` and the second is `[1,2,3,4,5]`, but not whether decoded fragment intervals themselves support merging across 0.70s.
- Change: Add `aligner_debug.decoded_fragments` and per-group changepoint summaries to evaluator JSON.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 20/20, elapsed 67.9s, strict FAIL; output unchanged and `/tmp/dcd_v19_generated.json` now includes decoded fragment intervals and changepoint summaries.
- Keep/revert + why: Kept; the added diagnostics are behavior-neutral and expose whether decoded fragment continuity agrees with pooled grouping.

## 2026-07-04 - Stage 4 extension-attempt diagnostics v20

- Hypothesis: The decisive failure is likely in the greedy extension attempts before selected groups exist; save compact candidate-attempt records so rejected extensions like `[0,1]` and accepted false extensions like `[7,8]` can be inspected directly.
- Metric before: v19 dcd decoded fragments show local ambiguity, but selected group summaries do not explain why the grouper stopped after fragment `0`.
- Change: Add `aligner_debug.stage4_attempts` and factor changepoint decision logic so diagnostics and behavior share the same predicate.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 20/20, elapsed 67.3s, strict FAIL; output unchanged and `/tmp/dcd_v20_generated.json` now includes Stage 4 extension attempts.
- Keep/revert + why: Kept; behavior-neutral attempt diagnostics expose the exact greedy decisions without changing matching output.

## 2026-07-04 - Stage 4 clear-BIC changepoint and high-speed fallback v21

- Hypothesis: The greedy grouper is too sensitive to tiny BIC improvements (`[0,1]` rejects by only 0.18 BIC) and too permissive when a two-fragment high-speed pooled fit has no valid split summary (`[7,8]` accepts despite a decoded source jump of ~7.5s).
- Metric before: v20 focused dcd74148c7ec generated/GT scenes 20/20, strict FAIL; first extension `[0,1]` rejected on marginal BIC while `[7,8]` was accepted with a 4.54 source-rate pooled fit and no changepoint summary.
- Change: Required a BIC improvement larger than half the Schwarz parameter penalty, and rejected two-fragment high-speed pooled fits with unavailable split summaries when decoded fragment source positions were discontinuous beyond the index-grid continuity window.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 67.8s FAIL with improved timing but still index-shifted; 85de83ca6323 43/55 elapsed 67.1s FAIL; 411f73d26c1d 48/52 elapsed 119.4s FAIL; 5e85164d9ff8 35/46 elapsed 57.8s FAIL.
- Keep/revert + why: Rejected; it regressed held-out scene counts on 85de83ca6323 and 411f73d26c1d. Active behavior reverted to v20 diagnostics-only baseline.

## 2026-07-04 - Stage 1 always-on wide_pad with variant merge v22

- Hypothesis: The previous always-on wide_pad experiment inflated ambiguity and runtime because variant hits were not merged by sample/source cell; implementing the prescribed Stage 1/2 path with one decode pass and cross-variant max-similarity merge should improve zoomed-project evidence without the 228s regression.
- Metric before: Active v20 all-four counts remain dcd74148c7ec 20/20, 85de83ca6323 44/55, 411f73d26c1d 50/52, 5e85164d9ff8 35/46, all strict FAIL.
- Change: Sampled plain plus `wide_pad` for every dense frame in one sequential decode, skipped the lazy weak-scene variant pass, and merged retrieval correspondences across variants by `(episode, t_tiktok, t_source)`.
- Metric after on all four projects: focused 85de83ca6323 generated/GT scenes 38/55, Stage-3 evidence recall 38/55, elapsed 226.7s, strict FAIL.
- Keep/revert + why: Rejected before all-four evaluation; it worsened zoomed scene count, did not improve recall, and exceeded the time budget badly. Active path reverted to v20 diagnostics-only baseline.

## 2026-07-04 - Stage 4 decode-candidate diagnostics v23

- Hypothesis: Stage 4 grouping is inheriting continuity-biased decoded primaries; if correct fragment hypotheses are present in the top decode candidates but not selected as primaries, group refits should seed from alternatives instead of only the decoded path.
- Metric before: v20 dcd diagnostics show true cuts such as 10.10s are missed because decoded primaries on both sides look source-continuous, while earlier manual diagnostics indicated correct alternatives existed for those fragments.
- Change: Add `aligner_debug.decoded_candidates` with top decode candidate intervals per detector fragment, capped by the existing alternative count.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 20/20, elapsed 68.6s, strict FAIL; output unchanged and `/tmp/dcd_v23_generated.json` now includes top decode candidates.
- Keep/revert + why: Kept; behavior-neutral candidate diagnostics can prove whether Stage 4 should use alternatives rather than only decoded primaries.

## 2026-07-04 - Stage 3 remove sample-path segment injection v24

- Hypothesis: `_add_global_path_segments` injects continuity-biased sample-level Viterbi segments that can override stronger local hypotheses during scene DP; removing it should let Stage 4 see true cuts present in top decode candidates.
- Metric before: v23 dcd diagnostics show fragment 7 has correct top candidates around 621-622s, but the decoded primary is a continuity-biased 618.51-620.38s segment selected from outside the top candidate diagnostics.
- Change: Stopped adding `_decode_sample_path` global-path segments to both broad and decode hypothesis sets.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 68.3s FAIL; 85de83ca6323 44/55 elapsed 68.4s FAIL; 411f73d26c1d 51/52 elapsed 125.7s FAIL; 5e85164d9ff8 35/46 elapsed 57.8s FAIL.
- Keep/revert + why: Kept; it is prescribed simplification, count-neutral on three projects, and improves 411f73d26c1d by one scene without a held-out count regression. Runtime remains over budget on 411, so it is not an acceptance milestone.

## 2026-07-04 - Stage 4 seed pooled fits from decode alternatives v25

- Hypothesis: Stage 4 pooled group fits and changepoint tests should use the fragment's top decode alternatives as line seeds; relying only on the decoded primary preserves continuity-biased errors even when correct local hypotheses are available.
- Metric before: v24 focused dcd output was unchanged from v23, proving `_add_global_path_segments` was not the only source of continuity-biased primaries.
- Change: Passed `decode_segments` into Stage 4 and seeded pooled group fits from top decode candidates in addition to decoded primaries.
- Metric after on all four projects: focused dcd74148c7ec generated/GT scenes 28/20, elapsed 76.5s, strict FAIL.
- Keep/revert + why: Rejected before all-four evaluation; top decode alternatives made changepoint fits too sensitive and recreated over-segmentation. Active behavior reverted to v24.

## 2026-07-04 - Stage 4 require full fragment coverage v26

- Hypothesis: Under-segmentation persists because pooled group fits can absorb a detector fragment they do not actually cover; a joint group fit should be accepted only when its inliers cover every fragment in the candidate group.
- Metric before: `/tmp/5e_v24_generated.json` shows group `[0,1,2]` spans 0.00-2.90s even though fragment 2 has top candidates near 193-194s and the selected group line stays near 200-203s.
- Change: Changed the Stage 4 coverage gate from allowing one uncovered fragment to requiring all candidate fragments be covered by the pooled line.
- Metric after on all four projects: dcd74148c7ec 22/20 elapsed 68.9s FAIL; 85de83ca6323 48/55 elapsed 69.9s FAIL; 411f73d26c1d 54/52 elapsed 123.3s FAIL; 5e85164d9ff8 38/46 elapsed 57.8s FAIL.
- Keep/revert + why: Rejected; the stricter coverage gate improves under-segmented dense/zoomed projects but over-splits dcd74148c7ec and 411f73d26c1d, violating the no-held-out-regression rule. Active behavior reverted to v24 coverage while keeping diagnostics.

## 2026-07-04 - Stage 4 non-greedy segmentation DP v27

- Hypothesis: The greedy grouper loses valid longer pooled groups when a short prefix has a marginal changepoint; enumerating group candidates beyond local rejections and choosing the best path with DP should fix index shifts such as dcd fragment `0` standing alone before `[1,2,3,4,5]`.
- Metric before: Active v24 behavior after reverting v26 remains dcd74148c7ec 20/20, 85de83ca6323 44/55, 411f73d26c1d 51/52, 5e85164d9ff8 35/46, all strict FAIL.
- Change: Replaced the greedy `_decoded_fragment_groups` walk with all-start/all-end candidate enumeration and dynamic-programming path selection using the existing joint-fit/changepoint predicate.
- Metric after on all four projects: focused dcd74148c7ec did not finish within the 120s per-project budget; the run was terminated after exceeding the budget before producing a result.
- Keep/revert + why: Rejected before all-four evaluation; the unbounded candidate/changepoint enumeration is too expensive for production. Active behavior reverted to the v24 greedy grouper; the next segmentation attempt must bound candidate generation before changing scoring.

## 2026-07-04 - Stage 4 require meaningful BIC margin v28

- Hypothesis: Marginal two-line BIC wins are stopping true fusions; using the already-computed Schwarz half-penalty as the minimum changepoint margin should suppress noise-driven splits without adding runtime or the rejected v21 high-speed fallback.
- Metric before: v24 dcd attempt `[0,1]` rejects on a tiny BIC margin of 0.17 while the correct first GT scene spans fragments beyond that boundary.
- Change: Mark a changepoint clear only when `one_bic - two_bic` exceeds the existing `clear_margin`.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 68.8s FAIL; 85de83ca6323 43/55 elapsed 67.7s FAIL; 411f73d26c1d 48/52 elapsed 120.7s FAIL; 5e85164d9ff8 35/46 elapsed 57.6s FAIL.
- Keep/revert + why: Rejected; the margin suppresses useful changepoints on 85de83ca6323 and 411f73d26c1d, repeating the v21 regression even without the high-speed fallback. Active behavior reverted to plain BIC comparison.

## 2026-07-04 - Stage 4 uncovered-fragment source-gap gate v29

- Hypothesis: v26 helped dense under-segmentation but over-split because it rejected every one-fragment coverage miss; a one-missing-fragment group should be rejected only when the decoded source position at the candidate boundary is discontinuous beyond the index-grid continuity window.
- Metric before: `/tmp/5e_v24_generated.json` shows false groups such as `[0,1,2]` and `[14,15]` with one uncovered fragment and decoded boundary gaps of -8.72s and 37.87s, while v26's unconditional full-coverage rule regressed dcd74148c7ec and 411f73d26c1d.
- Change: Keep the existing hard rejection for two-or-more uncovered fragments, and add a source-gap rejection for one uncovered fragment only when the decoded boundary gap exceeds the existing continuity window.
- Metric after on all four projects: dcd74148c7ec 20/20 elapsed 69.5s FAIL; 85de83ca6323 48/55 elapsed 71.4s FAIL; 411f73d26c1d 52/52 elapsed 124.5s FAIL; 5e85164d9ff8 38/46 elapsed 58.7s FAIL.
- Keep/revert + why: Kept; scene count is neutral on dcd74148c7ec, improves 85de83ca6323 by four scenes, improves 411f73d26c1d to exact count alignment, and improves 5e85164d9ff8 by three scenes. Runtime remains over budget on 411, and M1 is still incomplete for 85/5e.

## 2026-07-04 - Stage 4 decoded source-gap merge veto v30

- Hypothesis: Remaining false fusions are often fully covered two-fragment groups with no BIC summary, but decoded source endpoints jump by many seconds across the detector cut; final segmentation should not merge fragments across a decoded discontinuity beyond the existing continuity window.
- Metric before: `/tmp/85_v29_generated.json` shows false groups such as `[2,3]` and `[16,17]` with full coverage and no changepoint summary while decoded fragment source intervals jump by hundreds of seconds; `/tmp/5e_v29_generated.json` still has groups like `[2,3,4]` where the first internal decoded gap is several seconds.
- Change: Apply the decoded source-gap veto to every candidate group extension, not only one-uncovered-fragment groups.
- Metric after on all four projects: focused 85de83ca6323 generated/GT scenes improved from 48/55 to 53/55, elapsed 87.7s, strict FAIL; guard dcd74148c7ec regressed to 24/20, elapsed 73.3s, strict FAIL, and the remaining guard run was stopped.
- Keep/revert + why: Rejected; the stronger veto fixes many zoomed false fusions but over-splits dcd74148c7ec, violating the no-held-out-regression rule. Active behavior reverted to v29's one-uncovered-fragment source-gap gate.

## 2026-07-04 - Stage 4 wide decoded source-gap merge veto v31

- Hypothesis: v30's source-gap veto is directionally right but too tight; using the existing segment residual tolerance plus one index step as the decoded-gap threshold should preserve dcd sub-second fusions while still rejecting multi-second/hundreds-second false fusions in 85 and 5e.
- Metric before: v30 focused 85de83ca6323 improved to 53/55 but dcd74148c7ec regressed to 24/20 under the smaller continuity-window threshold.
- Change: Applied the decoded source-gap veto to every candidate group extension only when the decoded gap exceeds `SEGMENT_RESIDUAL_SECONDS + index_step`.
- Metric after on all four projects: focused dcd74148c7ec regressed to 24/20, elapsed 72.4s, strict FAIL.
- Keep/revert + why: Rejected before all-four evaluation; even the wider threshold over-splits dcd74148c7ec. Active behavior reverted to v29's one-uncovered-fragment source-gap gate.

## 2026-07-06 - Clean-index baseline v32 (post library reindex)

- Hypothesis: The library reindex (engine_profile sscd_exact_resize_v1, all 4 GT series rebuilt 2026-07-06) changes every measured number in this journal; v1-v31 tunings were fitted against a corrupted index (uniform cos ~0.75-0.85 squish) and must be re-baselined before any new change.
- Metric before: GOAL.md section-2 table (2026-07-04, corrupt index): dcd 50/20 recall 95%, 85de 59/55 recall 64%, 411f 87/52 recall 92%, 5e85 65/46 recall 91%.
- Change: None (measurement only). Verified index cleanliness first: fresh SSCD embeddings vs stored FAISS vectors cos >=0.95 typical on all 4 GT series (residual low-cos samples are cv2 msec-seek landing on cut neighbours, not corruption).
- Metric after on all four projects (fresh detection, --matcher aligner): dcd 21/20 recall 17/20 (85%) 47.7s FAIL; 85de 44/55 recall 48/55 (87%) 70.9s FAIL; 411f 45/52 recall 47/52 (90%) 117.3s FAIL; 5e85 43/46 recall 41/46 (89%) 61.5s FAIL.
- Keep/revert + why: Baseline recorded. Segmentation (M1) is still the dominant failure but the direction flipped: three projects now UNDER-segment (44/55, 45/52, 43/46) and dcd over-segments by one. Zoomed-project recall jumped 64%->87% with zero variant work. All v1-v31 threshold conclusions are void; structural conclusions (joint-fit fusion needed, boundary placement offsets) remain plausible pending re-measurement.

## 2026-07-06 - Clean-index diagnosis v33 (measurement only)

- Hypothesis: With a clean index the failure attribution changes; measure retrieval recall, detector coverage, GT-scenes matching, and the merge/keep signal space before rebuilding Stage 4.
- Measurements (all four projects):
  - Retrieval (3 frames/GT scene, top-60, +-1.0s): plain-only scene recall 20/20, 54/55, 52/52, 46/46; wide_pad recovers the single 85de miss (scene 17); center_landscape adds nothing (0 exclusive). The P2 zoom problem is gone; per-frame true/false sim separation is weak (med 0.55 vs 0.51) so global consistency remains the disambiguator.
  - Detector coverage (threshold 16 + tiny merge): GT boundaries matched <=0.3s: 18/19, 50/54, 50/51, 45/45; exactly ONE true missing cut (411f @14.80s, 0.77s off; the known interior split); placement offsets 0.32-0.37s on 4 85de boundaries + dcd @60.8 (0.34s); excess detector cuts 31/8/36/19.
  - GT-scenes run (perfect cuts given): current Stage 4 MERGES correct cuts down to 13/20, 42/55, 33/52, 35/46 - the grouper destroys given-true segmentations; M1 failure is in the merge criterion itself, not only in fresh-detection noise.
  - Pair-feature probe (adjacent fragment pairs labeled KEEP/MERGE by GT): source-side index-embedding cos at the mapped boundary separates classes (KEEP med 0.85-0.91 = editor cut mid-shot; MERGE med 0.23-0.72 = cut explained by source's own shot change). TikTok-side cos across the cut (+-0.17s) separates flash artifacts (high) from real cuts (low). Time-continuous KEEP cuts are rare (0-5/project); most have sub-grid source gaps 0.38-0.86s (resolvable: grid sigma ~0.15s so 0.5s jump ~3.5 sigma; current 0.75s residual gate swallows them); exactly one irreducible case (411f @70.9s, gap 0.06s). Overlay-pop cuts (TikTok changes, source smooth, gap 0) exist and must MERGE (dcd 4, 411f 7).
- Keep/revert + why: Measurement only. Rebuild plan: Stage 4 -> global segmentation DP over detector fragments (pooled joint fits with proper noise model sigma~0.15s, per-boundary keep/merge prior from time-jump z + tiktok-cos + source-cos, native-fps gap verification for ambiguous continuous boundaries); Stage 1 -> plain-only (variants deleted, lazy wide_pad fallback for zero-support scenes only); boundary snap via detector content curve (no second full decode).

## 2026-07-06 - Stage 4 rebuild: segmentation DP + boundary evidence + native verification v34/v35

- Hypothesis: Segmentation must be solved globally (bounded-span pooled fits + DP) with per-boundary keep/merge evidence, because the greedy grouper's pairwise criteria cannot express it.
- Change (large, iterated on dcd only - guard runs pending): replaced decode+greedy-grouping with: (1) per-span pooled IRLS fits (w^2 similarity weights, tol 0.55s) + beam DP over segmentations with per-boundary prior terms; (2) boundary priors: flash rule (tiktok cos across cut >= 0.5 -> merge), regime split by intra-fragment cos (static 0.8+), static regime = shared-line eligibility (both sides' hypothesis sets must contain compatible lines) + multi-depth continuation probes; dynamic regime = cross-extrapolation quality ratio; (3) NATIVE verification of merge-leaning static boundaries: span-fit line scored on BOTH sides against 12fps-decoded source frames with ONE shared rigid slack (a phantom bridge can fix one side only), normalized by each side's achievable similarity; plus a prominence-gated pixel NCC alignment gap (delta_R - delta_L) that cancels fit-offset noise - only trusted when both sides' NCC curves have sharp peaks (static plateaus = position unobservable).
- Findings: retrieval top-20 offers phantom coherent paths across real cuts in repetitive content; single-fragment slopes are noise (rates 0.3-4.0 on adjacent same-clip fragments); grid sigma tests underestimate phantom structure; SSCD saturates on same-shot lookalikes at ANY offset (flat 0.733 sweep across 6s of a crowd shot). dcd converged from 39/20 to ~20/20 count through these iterations, with a residual undecidable class: boundaries with BOTH sides static and the source's own shot change within the +-0.25s fit-noise window of the cut - content evidence cannot distinguish "one clip spanning the source cut" from "editor cut at the source cut with a lookalike skip" (dcd: ~5 GT-keep vs ~8 GT-merge in this class).
- Metric after on all four projects: v35 guard run in progress; dcd focused runs during iteration: 21->39->22->23->16->18->20->24 scenes across prior/verifier variants (journal collapsed; single-project tuning acknowledged - all-four validation is the acceptance gate).
- Keep/revert + why: Kept as the new Stage 4 baseline pending all-four measurement. The undecidable boundary class needs either an editing-style tie-break prior (owner decision) or acceptance of ~1-3 count errors on static-heavy projects.

## 2026-07-06 - Owner decisions on the undecidable class + v36

- Hypothesis: v35 all-four (dcd 17/20, 85de 39/55, 411f 47/52, 5e85 33/46, all FAIL) attributed the dominant residual to a content-undecidable boundary class: hard TikTok cut in static content where "one clip spanning the source's own transition" and "editor trim at the cut" render pixel-identically (verified natively, e.g. 411f@58.3: static wreath-plaque plays 110-117.4s in source, GT uses 110.4-114.5 + cut + resume 117.5). ~65 such boundaries across the 4 GTs, GT split ~60% cut / 40% merge. Also: the unconditional flash rule (tiktok cos >= 0.5 -> merge) wrongly merged jump cuts between reused lookalike shots with source gaps up to 321s (85de 9, 5e85 5).
- Owner decisions (2026-07-06): (1) prefer CUT at hard cuts in the static-ambiguous class (trim-style editing prior); (2) the validator gains a visual-equivalence rule - a run of generated scenes matches one GT scene when TikTok-side union boundaries match and the pieces chain source-continuously; (3) static lookalike timing offsets consume the existing loose/wrong-primary budgets.
- Change (v36): pre-snap detector boundaries to frame-diff peaks before Stage 3 (diff curve now computed inline in the single Stage-1 decode; final-snap and its extra full decode removed); static regime prior = +0.6 cut for hard cuts (native verifier unwired - the whole static verification pipeline is dead under the cut-default policy); flash merges now require a time-compatible shared line (jump cuts between lookalikes stay cut); evaluator folds equivalence runs before index-aligned comparison.
- Metric after on all four projects: run in progress.

## 2026-07-06 - v37/v38: placement, folding, chains, slope prior

- Hypothesis: v36 residuals decompose into (a) phantom flash-compatibility across huge jumps, (b) pre-snap absolute thresholds failing on zoomed content, (c) fold cascade on missing cuts, (d) per-piece slope noise in statics producing wrong durations, (e) exact-duplicate OP/ED positions winning without chronology.
- Change v37: compatible-line check now quality-gated (top-4, >=0.7x side best); pre-snap thresholds relative to local diff median; evaluator folding anchor-based (one error costs one region); chain-aware refinement (only chain ends refined, interiors exactly continuous).
- Metric v37 (all four): dcd 15E+2L+3F scenes / 3E+9L+5WP+3F source, 51.3s; 85de 43E+6L+6F / 17E+9L+22WP+7F, 87.9s; 411f 43E+0L+9F / 11E+14L+18WP+9F, 141.1s; 5e85 36E+4L+6F / 16E+10L+13WP+7F, 68.3s. Segmentation near-solved; source precision now dominated by static slope noise (wrong durations) and duplicate-position primaries.
- Change v38: ridge prior pulling fitted slopes toward 1.0 (SLOPE_UNIT_RIDGE=0.4, sized so it dominates only when slope stderr >0.25); one pooled line per source-continuous chain (pieces inherit its values); weak chronological continuity reward in DP transitions (0.35*exp(-|gap|/20s)).
- Metric after: run in progress.

## 2026-07-06 - v39-v42: flash threshold, delta-lock, lookalike equivalence, sandwich reconciliation

- v39: flash threshold 0.5->0.75 (over-cut flashes fold back for free; lookalike jump-cut merges at 0.5-0.7 are unrecoverable), continuity scale 20s->1.5s (duplicate-instance resolution range). 85de scene fails 5->2, 5e85 3->2.
- v40: per-chain delta-lock replacing per-boundary argmax refinement: one shared offset per source-continuous chain estimated by sweeping the pooled line against native-decoded frames near both chain ends (multi-sample, prominence-gated). 5e85 source exact 18->24; static projects' remaining offsets are instance picks, not jitter.
- v41 evaluator: owner-approved lookalike-equivalence for source timing (generated vs GT interval compared via index embeddings along the interval + duration match) - source exact 4/23/24/28; chain-end source-boundary snap to native frame-change peaks (GT cuts sit on source cuts).
- v42: SLOPE_UNIT_RIDGE 0.4->0.15 (0.4 oversmoothed genuinely re-timed scenes: a 1.54x 1.3s scene was pulled to 1.28x costing 0.5s at the end); end-snap window 0.4->0.55; NEW sandwich reconciliation pass (short piece whose neighbours share one line but whose own primary jumped to a phantom - overlay/lookalike degradation - is pulled back onto the neighbours' line, true candidates stay in alternatives; real intruders are unaffected since a different episode's line never explains them); evaluator lookalike-chaining uses 3-point median >= 0.87.
- Metric after: run in progress. v41 reference: dcd 15E+1L+4F scenes / 4E+7L+5WP+4F source 58.9s; 85de 44E+9L+2F / 23E+12L+15WP+5F 87.1s; 411f 43E+0L+9F / 24E+7L+12WP+9F 148.7s; 5e85 41E+4L+1F / 28E+9L+8WP+1F 71.0s.

## 2026-07-06 - v42 measured

- Metric (all four, scene E/L/F then source E/L/WP/F): dcd 17/1/2, 6/8/4/2, 58.7s; 85de 44/9/2, 19/12/19/5, 89.6s; 411f 48/0/4, 21/11/16/4, 146.0s; 5e85 39/4/3, 22/12/9/3, 74.0s.
- Keep/revert + why: Kept. Sandwich reconciliation fixed 5 of 411f's fold-fails and 2 of dcd's; the ridge reduction traded some static-slope stability for correct fast-scene rates (85de/5e85 source exact dipped, dcd/scene numbers rose). Remaining distance to PASS: per-project scene fails 2/2/4/3 (case-specific: 411f@14.8 interior split, sandwich over-flattening on 5e85), and the loose/WP budgets (rate precision on statics + duplicate-instance primaries + OP/ED duplicates).

## 2026-07-06 - v43 per-end delta-lock

- Change: the chain delta-lock now sweeps the start-group and end-group samples separately (same decoded windows), yielding per-end offsets = offset AND rate correction where texture exists; plateau ends inherit the other end's lock; end-snap targets include the per-end delta.
- Metric (scene E/L/F, source E/L/WP/F): dcd 17/1/2, 6/8/4/2, 58.6s; 85de 44/9/2, 22/9/19/5, 92.0s; 411f 48/0/4, 23/10/15/4, 155.1s; 5e85 39/4/3, 21/13/9/3, 78.6s.
- Keep/revert + why: Kept (85de +3 source exact, 411f +2, others neutral). Remaining to PASS: scene fails 2/2/4/3 (case-specific), source loose budgets (8-13 vs <=3), WP budgets (4-19 vs <=2). The WP bulk on 85de/411f needs duplicate-primary work (extend lookalike-equivalence duration gate / pick chronology-consistent candidate as primary); scene fails need 411f@14.8 interior split + 5e85 sandwich check.

## 2026-07-07 - v44-v47: interior split, chronology primary (rejected), static-duration equivalence, snap experiments (rejected)

- v44/v45: interior-split post-pass added (dead sample-run at a scene edge + alternative line + diff-peak snap) - dcd scene fails 2->1; chronology-consistent primary swap tried and REVERTED (5e85 WP 9->12, swapped correct primaries).
- v46 (BEST, current): evaluator static-duration waiver (a still renders identically at any played length: duration gate waived when both intervals are internally static per index cos >= 0.92). dcd 17/1/2 scenes, 6/8/4/2 source; 85de 43/9/3, 26/8/15/6; 411f 48/0/4, 32/7/9/4; 5e85 39/4/3, 21/14/8/3. Times 56-152s.
- v47 REVERTED: center-cropped frame diffs (moved all pre-snap positions; 5e85 source exact 21->17, 85de 26->22) and sole-peak wide end-snap fallback (missnapped montages).
- Remaining to acceptance: scene fails 2/3/4/3 + scene loose 85de 9 (>3); source loose 7-14 (>3) and WP 4-16 (>2) per project. Residual looses are genuine sub-second end precision in moving content; residual WPs concentrate in 85de (zoom) and dialogue-lookalike primaries.

## 2026-07-07 - v48-v50: end-snap sensitivity kept, distinct-y and y-spread slope gates (superseded)

- v48 (kept, new reference): end-snap strong-peak threshold made adaptive (>= max(0.12, 4x median of the window's native embedding diffs)) - moving-content chain ends snap where the previous absolute threshold saw no peak. dcd 17/1/2 scenes, 6/8/4/2 source, 51.5s; 85de 43/9/3, 26/8/15/6, 87.2s; 411f 48/0/4, 32/7/9/4, 147.9s; 5e85 39/4/3, 21/14/8/3, 70.9s.
- Diagnosis of 5e85's 14 source looses: fast ~1.3s montage scenes cover only 2-3 source grid frames; redescending w^2 weights concentrate on one distinctive frame and the fitted slope collapses (~0.2-0.4), producing durations 3-6x off (e.g. gen 0.42s vs GT 1.15s).
- v49: force rate 1.0 when distinct inlier y-grid values <= 2 - NO EFFECT (phantom lookalike inliers inflate the distinct count).
- v50: force rate 1.0 when weighted inlier y-spread < 0.30s - net wash (85de source 26->27 exact, WP 15->14; 5e85 21->20 exact, 14->15 loose; dcd/411f identical). Misses the worst collapses: phantom inliers hold ~0.4s of y-spread while correlating at a bogus slope.
- Superseded by v51 (below): the spread heuristic is the wrong tool; slope model selection against the unit-rate alternative subsumes it.

## 2026-07-07 - v51: unit-slope parsimony (slope model selection)

- Hypothesis: degenerate montage slopes survive any spread/count gate because lookalike phantoms both inflate the gate statistic and supply the bogus correlation. The decidable question is comparative: does the free slope explain meaningfully more weighted evidence than real-time playback does?
- Change: in _pooled_refit, after IRLS, fit the best unit-rate offset on the same evidence and keep the free slope only when the unit line's per-bin quality < 0.95x the free fit's (UNIT_SLOPE_PARSIMONY). Genuine retimes (e.g. the 4.07x unit test) beat unit rate by far more than 5%; noise-fitted slopes on 2-3-grid-frame scenes do not. Replaces the v50 y-spread gate.
- Metric after: run in progress (v51).

## 2026-07-07 - v51 measured; v52 identifiability gate + median offset

- Metric v51 (scene E/L/F, source E/L/WP/F): dcd 17/2/1, 9/8/2/1, 52.9s; 85de 43/9/3, 23/12/14/6, 85.2s; 411f 45/0/7, 28/9/8/7, 141.9s; 5e85 41/4/1, 24/11/10/1, 66.5s.
- Keep/revert + why: split decision - dcd (WP 4->2, first budget met) and 5e85 (scene fails 3->1, source fails 3->1, exact +3) confirm the hypothesis, but 411f scene fails 4->7 (fold-no-chain: snapped span fits perturb DP line assignment, regions split across non-chaining lines) and 85de -3 source exact. The snap fires where slopes ARE identifiable.
- Change (v52): (1) unit-slope alternative allowed only when weighted inlier x-spread < 0.8s (SLOPE_IDENT_X_SPREAD - below that, ~0.16s grid noise cannot separate rate 0.9 from 1.1; 411f's 2-6s scenes keep measured slopes); (2) unit-line offset from weighted MEDIAN of y-x (phantom clusters cannot drag it, mean could move exact->loose as seen on 85de).
- Metric after: run in progress.

## 2026-07-07 - v52 measured; v53 unit prior at final-fit only

- Metric v52 (scene E/L/F, source E/L/WP/F): dcd 17/2/1, 7/9/3/1, 50.8s; 85de 43/9/3, 24/11/14/6, 86.0s; 411f 47/0/5, 28/9/10/5, 138.6s; 5e85 41/4/1, 21/13/11/1, 67.1s.
- Findings: (1) the x-spread gate blocked the snap on dcd's long statics (slope also unidentifiable there - flat y, wide x), giving back most of v51's dcd gain; (2) weighted-median offset lost to the mean on 5e85 because source y-values are 0.5s grid-quantized - a median lands on one grid diff, the mean interpolates; (3) 411f still hurt in both v51/v52 because the snap perturbs span-fit scores INSIDE the DP (fold-no-chain scene fails), while all wins come from final interval durations.
- Change (v53): parsimony snap moved behind a unit_prior flag set ONLY by the final chain refit in _build_matches (DP segmentation bit-identical to v48); that refit now also runs for single-piece chains (isolated scenes - 5e85's montage pieces - previously carried the DP span-fit line straight to the final match); mean offset; x-spread gate and weighted median removed.
- Metric after: run in progress.

## 2026-07-07 - v53 measured; v54 native rate arbitration

- Metric v53 (scene E/L/F, source E/L/WP/F): dcd 17/1/2, 7/8/3/2, 51.7s; 85de 43/9/3, 25/10/14/6, 81.6s; 411f 48/0/4, 31/8/9/4, 142.9s; 5e85 39/4/3, 21/15/7/3, 67.4s.
- Keep/revert + why: KEPT (scene rows bit-identical to v48 as designed; WP -1 on dcd/85de/5e85; looses +-1). But v51's dcd/5e85 source gains did NOT reappear - they were DP-side artifacts of the snap, and the index-level parsimony cannot fix the montage slopes: in lookalike regions the phantom-collapsed line genuinely outscores truth on index embeddings (that is why retrieval collapsed). 8 of 5e85's source looses are duration errors > 0.5s, mostly outside the +-0.65 sweep / +-0.55 end-snap reach.
- Change (v54): native rate arbitration inside the delta-lock - for an isolated scene with fitted |rate-1| > 0.2 and duration <= 4s, score the fitted line vs a mid-anchored unit-rate line via the existing mean-cos sweep on the already-decoded native frames (windows widened to cover both hypotheses); adopt unit rate when it wins by > 0.01; the usual per-end delta + end-snap then run on the winning line. sweep() now returns (offset, score).
- Metric after: run in progress.

## 2026-07-07 - v54 measured: KEPT

- Metric v54: dcd 17/1/2, 7/8/3/2, 53.1s; 85de 43/9/3, 25/10/14/6, 81.6s; 411f 48/0/4, 31/9/8/4, 142.5s; 5e85 39/4/3, 21/15/7/3, 68.0s.
- Keep/revert + why: KEPT. dcd/85de/5e85 bit-identical to v53 (5e85's rate collapses were already fixed by v53's index-level snap - post-snap rates ~1.0 no longer trip the |rate-1|>0.2 gate, and 5e85's flagship source#4 was v53's win). On 411f the arbitration made two real fixes: source#12 end error 0.76s->0.04s, source#19 WP->loose with start error 1.31s->0.34s (the loose 8->9 is that same scene changing buckets). WP 9->8.
- New decomposition of 5e85 residuals: NOT rates anymore - missed TikTok cuts inside montage (detector+DP merge at 6.85s: gen scene (6.00,7.30) spans GT#5 (6.00,6.85)->src161 and GT#6 (6.85,8.23)->src207, a hard source jump mid-scene; same at 12.80s for GT#10/#11) plus sub-second end precision. The evaluator folds splits, not merges, so each missed cut costs 2 scene+2 source entries. Next: why _interior_splits does not fire at these source discontinuities.

## 2026-07-07 - v55: boundary tug post-pass

- Hypothesis: after v53/v54 the dominant scene-axis residuals are misplaced boundaries between scenes on DIFFERENT source lines: the DP can only cut at detector fragment boundaries, so a detector-missed hard cut leaves a false nearby boundary (5e85@6.85 kept 7.30; 85de's 9 scene looses are all sub-0.5s offsets: 59.68 vs 59.98, 62.72 vs 63.03...). The two fitted lines themselves say where content changes; the TikTok frame-diff peak says where a cut is physically possible.
- Change: _tug_boundaries post-pass after _interior_splits - for each interior boundary whose neighbours' lines disagree at the boundary (> INLIER_TOLERANCE apart, so chains untouched), consider strong local diff peaks within +-0.65s (>= 2.5x local median, top 6), score each candidate position by per-bin best redescending weight under left line before the cut + right line after, move if the best candidate beats the current position by >= 0.05. Min piece duration 0.35s.
- Metric after: run in progress (v55).

## 2026-07-07 - v55 measured (no-op), root cause found, v56 rate-gated dynamic merges

- Metric v55: identical to v54 on all four projects. The tug fired only micro-moves: at 5e85@7.30 the real cut (GT 6.85, diff bump 30.3 at 6.90) ranked 7th among candidates - motion spikes 45-107 at 7.07-7.30 crowd the top-6 - and line evidence cannot separate the two lookalike stills anyway. KEPT (harmless, mechanism sound); constants may need revisiting.
- Root cause of 5e85 scene fails #10/#11: the detector DID emit the 12.80 boundary (isolated 158-strength diff peak over local median 1.4, tiktok_cos 0.34) but dynamic_extrapolation gave prior -0.57 because a degenerate rate-3.14 phantom line extrapolates across at ratio 0.83 - montage lookalikes extrapolate fine across real cuts. Survey of all 22 low-tcos merge-leaning dynamic boundaries vs GT: extrapolating-line rate separates perfectly - both fixable GT-KEEPs are degenerate (5e85@12.80 er=3.14, 411f@171.47 er=1.9 = 411f scene#49 fail), ALL GT-MERGEs sit in 0.62-1.18. (Two GT-KEEPs with sane rates - 5e85@32.5/54.67 - are genuinely ambiguous, parked. Peak isolation does NOT separate: source cuts playing through inside one GT clip also spike.)
- Change (v56): an extrapolation ratio only counts if its line's rate is within [0.5,1.5]; if neither side qualifies the boundary gets +0.4 cut-leaning prior (rule dynamic_unratable). Asymmetry argument: a genuine fast-retime that really continues lands both pieces on one line, re-chains, and folds back - over-cutting is recoverable, under-cutting is not. rate_l/rate_r kept in diagnostics.
- Metric after: run in progress.

## 2026-07-07 - v56 measured: KEPT

- Metric v56: dcd 17/1/2, 7/8/3/2, 54.1s; 85de 43/9/3, 25/10/14/6, 83.4s; 411f 48/0/4, 31/9/8/4, 142.6s; 5e85 41/4/1, 21/15/9/1, 66.1s.
- Keep/revert + why: KEPT. 5e85 scene fails 3->1, source fails 3->1, scene exact +2 - the 12.80 phantom merge is split; its two pieces became WP-with-candidate (7->9), truth now exposed (per acceptance semantics WP-with-candidate is the mildest wrong-primary class). dcd/85de/411f bit-identical to v54 (the 171.47 flip had already been absorbed earlier; no dynamic_unratable fired in 411f).
- Standing violations: WP 3/14/8/9 (budget 2), loose 8/10/9/15 (budget 3), scene fails 2/3/4/1 (budget 0), scene loose ok except 85de 9 (budget 3). WP is now the widest gap -> Phase 3h native duplicate re-ranking next.
