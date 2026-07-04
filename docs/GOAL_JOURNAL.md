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
