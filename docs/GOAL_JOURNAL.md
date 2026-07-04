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
