# Global Scene Aligner Journal (restarted 2026-07-10)

Previous journal archived at `GOAL_JOURNAL_ARCHIVE_2026-07-04_to_2026-07-07.md` — all its
numbers are void (v1-v31: corrupted index; v32-v56: pre-fix ground truth for dcd74148c7ec
and 85de83ca6323). Version numbering continues from v57 to keep references unambiguous.

## 2026-07-10 - v57 baseline (clean index + corrected GT, code = v56 / commit 33ea0ed)

- Hypothesis: none (measurement only). Owner corrected the dcd74148c7ec GT (2026-07-09) and
  the 85de83ca6323 GT (2026-07-10, 55→54 scenes); all prior measurements are void.
- Metric (fresh detection; scene E/L/F, source E/L/WP/F, elapsed):
  - dcd74148c7ec (20 GT): 17/1/2, 11/7/0/2, 59.5s
  - 85de83ca6323 (54 GT): 45/6/3, 28/6/15/5, 107.6s
  - 411f73d26c1d (52 GT): 48/0/4, 31/9/8/4, 199.4s
  - 5e85164d9ff8 (46 GT): 41/4/1, 21/15/9/1, 85.9s
- Oracle (`--gt-scenes`) source axis: 11/7/1/1, 31/7/14/2, 31/9/11/1, 24/13/9/0 — within
  noise of fresh: source-axis errors are intrinsic, not segmentation-induced. Oracle also
  degraded given-true boundaries (85de: 49/54 exact only) → oracle guard added to GOAL §7.
- Attribution (scripts in `backend/scripts/diagnostics/`):
  - Scene axis: 3 true detector misses total (dcd@16.03, 85de@12.15, 411f@14.80), ~4
    placement offsets ~0.3s (85de@59.98/63.03), 2 DP over-merges (85de@21.22, 5e85@32.5);
    fold-no-chain fails trace to wrong source lines on pieces, not to cuts.
  - Source axis: duplicates WP 15/8/9 on 85de/411f/5e85 (index cos gap 0.02-0.05 between
    instances = index-blind); 37 looses, median end error 0.37s, all <1.0s, 30/37 one side
    already exact; slope collapses (411f#50 rate 0.52 vs GT 1.0; 411f#46 phantom skips).
  - Pixel probe on 85de WP cases: native SSCD prefers the WRONG instance (-0.06/-0.09),
    gray pixel NCC + zoom search prefers truth (+0.05/+0.10); OP/ED repeats pixel-identical
    (margins ~0) → content-undecidable, need chronology/assignment or equivalence/waiver.
  - Priors re-measured: playback rate median 1.00, range [0.59,1.54], 88-98% in [0.8,1.25];
    the previously-claimed 4.07x scene does not exist in the corrected GT.
- Keep/revert + why: baseline recorded; artifacts at `~/.cache/atr-eval/v57_newgt_*.json`
  and `v57_oracle*.log`. Next: M1 (native per-end anchoring + rate arbitration).

## 2026-07-10 - v58 M0 reproduction (same code as v57, fresh session)

- Hypothesis: none (environment sanity check; GOAL M0).
- Metric (fresh detection; scene E/L/F, source E/L/WP/F, elapsed):
  - dcd74148c7ec: 17/1/2, 11/7/0/2, 57.3s
  - 85de83ca6323: 45/6/3, 28/6/15/5, 87.7s
  - 411f73d26c1d: 48/0/4, 31/9/8/4, 151.1s
  - 5e85164d9ff8: 41/4/1, 21/15/9/1, 69.3s
- All four match the §2 table exactly; elapsed is lower across the board (151s max vs
  199s) — same machine, less load. Oracle reproduction: see below (run follows).
- Keep/revert + why: environment confirmed sound. Artifacts `~/.cache/atr-eval/v58_m0_*`.
- Oracle (`--gt-scenes`) reproduction: source 11/7/1/1, 31/7/14/2, 31/9/11/1, 24/13/9/0 —
  identical to v57 oracle. Scene-axis oracle degradation confirmed (85de 49/54 exact on
  given-true boundaries; dcd 18/20; 5e85 44/46) — the §7 oracle guard target.

## 2026-07-10 - v59 M1: Stage-5 per-end anchoring + generalized rate arbitration

- Hypothesis (R2/R3): anchoring per-end offsets on the TRUE TikTok edge frames (decoded
  natively, 3 insets/end) and argmaxing against native source frames along the chain line
  beats the old mean-of-8fps-samples delta-lock; source-cut snapping only for temporally
  ambiguous (plateau) ends; rate arbitration widened from |rate-1|>0.2,dur<=4 to >0.1, no
  dur cap. One change: `_build_matches` delta-lock block -> `_stage5_refine` (+ SceneMatch
  gains `doubt_reasons`; synthetic toolkit tests added).
- Metric before (v58, source E/L/WP/F): 11/7/0/2, 28/6/15/5, 31/9/8/4, 21/15/9/1.
- Metric after (fresh): dcd 12/6/0/2 (61.3s), 85de 30/5/14/5 (102.6s),
  411f 35/4/9/4 (164.7s), 5e85 27/9/9/1 (80.1s). Scene axis unchanged everywhere.
- Aggregate: exact +13 (91->104), loose -13 (37->24), WP 32->32, failed unchanged. Every
  project improved or held; elapsed within cap (max 164.7s).
- Churn observed: a few exact->loose rigid shifts (dcd#11 -0.45s, 85de#43 +0.33s) where a
  confident lock anchored onto contaminated edge frames (boundary slightly off the true
  cut -> edge frame shows the neighbouring scene). Next: edge-contamination guard.
- Keep/revert + why: KEEP — aggregate and per-project wins, no scene-axis effect.
  Artifacts `~/.cache/atr-eval/v59_m1_fresh*`.

## 2026-07-10 - v60 edge-contamination guard (REVERTED, negative result)

- Hypothesis: v59's exact->loose churn (dcd#11 rigid -0.45s, 85de#43 +0.33s) is caused by
  the outermost edge frame showing the neighbouring scene when the generated boundary sits
  slightly off the true cut; dropping an edge frame that disagrees with both deeper insets
  (cos<0.60) while they agree (cos>=0.70) should fix it.
- Change (one line): `_drop_contaminated_edge` filter on both per-end query sets.
- Metric after: byte-identical intervals to v59 on ALL FOUR projects (12/6/0/2, 30/5/14/5,
  35/4/9/4, 27/9/9/1). The filter never fired where it mattered.
- Keep/revert + why: REVERT — no aggregate improvement (guardrail), avoids drift. The v59
  churn cases are content-level (lookalike lock or GT hard case, dcd#11 is a §8 review
  candidate), not boundary contamination. Evaluator gained §8 waiver support + `--review`
  HTML mode in the same window (no metric effect; row format for source lines changed).

## 2026-07-10 - v61 M2 duplicate pixel rerank, first attempt (REVERTED after 85de)

- Hypothesis (R1): switching a chain's primary to the duplicate-instance candidate whose
  pixel NCC (geometry-swept, mid-frame queries) beats the current line by >0.04 fixes the
  WP class (pixel separates what index cosine cannot; probe evidence +0.05/+0.10).
- Change: `_duplicate_candidates` (distant segment alts + unit-rate correspondence
  clusters within 0.10 index sim) + `_pixel_score_line` (NCC over geometry variants,
  +-0.6s sweep) + switch in `_stage5_refine`; mid-frame grays added to the edge decode.
- Metric: dcd 12/6/0/2 (unchanged, +24s elapsed). 85de: 30/5/14/5 -> 23/2/22/7 and scene
  F 3->4 — CATASTROPHIC: correct primaries switched onto distant duplicates and even
  ep-02 OP/ED lookalikes (#14: near-exact 599.2 -> 759.9; #42: 922.8 -> 1141.0). Run
  killed at 411f; numbers decisive.
- Diagnosis: max-over-many-trials bias — 4 candidates x 15 offsets x 20 geometries each
  get a "best" NCC while the current line gets one; a 0.04 margin on noisy ~0.2-0.5 NCC
  values (overlay text, zoomed content) flips freely. Cluster lines (2s-bucket medians)
  can also misalign temporally, deflating the truth's own score.
- Keep/revert + why: REVERT the switch (keep the toolkit + tests). Next: measure NCC
  margin distributions offline (truth vs wrong instance on all 85de scenes) and redesign
  gate/margin from data instead of guessing thresholds.

## 2026-07-10 - v62-v64 M2 rework attempts on 85de (all superseded, journal batch)

- v62 (pixel switch 0.045 + local-neighbour chronology): 85de 27/2/18/7 — chronology
  anchored on IMMEDIATE neighbours propagates wrong instances (615/774 clusters pulled
  #4/#13/#14/#18/#32 onto their neighbours' wrong positions).
- v63 (trusted-anchor chronology + identity certificate): 85de 25/2/21/6 — candidate
  window 0.10 flags nearly every chain, so no trusted anchors exist; pixel margins alone
  decide and 15 greedy switches fire, many wrong. Offline margin probe (54 scenes,
  `probe_rerank_margins.py` + `probe_scorer_variants.py`): NO pixel scorer variant (gray
  NCC, Sobel-gradient NCC, center-masked gradient, 96-128px, 20 geometries) separates —
  wrong candidates reach +0.11 while 6/14 true margins are negative. Pixel NCC is dead
  as a duplicate discriminator on zoomed edits.
- v64 (global assignment DP + identical-or-pixel veto): 85de 28/5/16/5 — the DP proposes
  the right OP/ED switches (#33/#35/#42-class) but the veto rejects them (identical
  content has pixel margin ~0; index-grid identity certificate at 0.90 both accepts
  non-identical pairs and rejects identical OPs).
- Diagnosis kept: duplicate WPs on 85de split into (a) dialogue-type instances that need
  a CONTENT scorer, (b) identical repeats that need chronology + an identity certificate.
  The missing instrument was geometry: probe `probe_sscd_zoom.py` shows SSCD on
  zoom-cropped native frames (z=1.45 on the fully-zoomed 85de) yields 12/14 POSITIVE
  GT margins (up to +0.39) with the two negatives being the pixel-identical repeats —
  where plain native SSCD (z=1.0) is mixed/negative. Zoom-aligned SSCD is the scorer.

## 2026-07-10 - v65/v66 M2: zoom-SSCD arbitration + global assignment (KEEP v66)

- Change: per-project zoom estimated once from confident chains (§4 geometric matcher,
  `_estimate_project_zoom`, picks 1.45 on 85de); `_zoom_sscd_score_line` replaces the
  NCC scorer (returns matched native embeddings for the identity certificate); decision:
  greedy switch at margin >=0.07, assignment-proposed at >=0.02, chronology switch for
  certified-identical (native cross-window cos >=0.95) at margin >=-0.02.
- v65 (greedy 0.05 + index-grid certificate): 85de 30/5/14/5 — net zero: fixed #40/#42,
  broke #2 (weak certificate) and #50 (false +0.064 greedy margin).
- v66 (greedy 0.07 + native certificate): 85de 33/5/11/5 vs v60 30/5/14/5 — +3 exact,
  -3 WP, nothing lost; scene axis unchanged; OP repeat #35 switched via certificate.
  Elapsed 181.3s (repeated decode cost — M4 target). Leave-one-out on the other three
  projects: dcd 12/6/0/2 unchanged (97.3s), 411f 35/4/9/4 unchanged (235.5s — OVER the
  200s cap), 5e85 26/9/10/1 (-1 exact: #16 correct 68.0 switched to its genuine repeat
  at 398.0 by a chronology_assign whose identity certificate rested on ONE mid frame).
- Keep/revert + why: KEEP the mechanism (85de +3, others unchanged except one 5e85 flip
  caused by a 1-frame certificate); the two follow-ups land immediately (below).

## 2026-07-10 - v67/v68: M3 native boundary tug + certificate hardening + M4 perf

- Changes (measured together in v68, individually motivated):
  1. `_native_tug_boundaries` (M3/R4): boundaries between different-line scenes are
     re-placed by 24fps TikTok decode scored against BOTH lines' native frames; the
     diff curve cannot place these (measured: GT cuts at 85de@12.15/dcd@16.03 are
     invisible in the diff curve while a stronger flash peak sits 0.3-0.5s away).
     Move requires split-score gain >= 0.10 (oracle guard). 5e85 scene axis 41/4/1 ->
     43/2/1 in the v67 probe run.
  2. Single-piece chains decode THREE mid queries (0.3/0.5/0.7) — a 1-frame identity
     certificate mistook a repeated still for whole-scene identity (5e85#16 flip).
  3. M4: `_WindowEmbedCache` — per-run (episode, zoom) embedding cache on the 12fps
     decode grid shared by native tug, duplicate arbitration and per-end refinement
     (R5: 85de hits the same neighbourhoods repeatedly); `_embed_pil_batch` chunks
     batches >64 (a growing one-shot batch ballooned the CUDA allocator reserve and
     OOM'd the 8GB card mid-run — twice) and the batch-1 OOM fallback retries once
     after an empty_cache.
- Metric v68 (native tug 0.04 floor + 3-mid + cache; scene / source / elapsed):
  - dcd: 18/1/1 (was 17/1/2 — tug recovered the dcd@16.03 region), 12/6/1/1 (a source
    FAIL downgraded to WP), 124.9s
  - 85de: 45/6/3 (=), 30/7/11/6 (v66 33/5/11/5 — sub-0.1s tug nudges jitter the source
    mapping, -3 exact), 244.6s
  - 411f: 47/0/5 (was 48/0/4 — one tug move broke a boundary), 34/5/8/5, 308.4s
  - 5e85: 43/2/1 (was 41/4/1 — two placement fixes), 28/6/11/1 (+2 exact vs v66), 221.4s
- Diagnosis: the native tug's REAL fixes all move >=0.3s; its sub-0.12s moves are pure
  jitter inside both tolerances that shifts source mappings. Floor raised 0.04 -> 0.12.
  Elapsed exploded (embedding is CPU-resize-bound: 12ms/img single-thread, measured);
  fix: parallel pre-resize with the embedder's own transform (bit-identical on
  re-application, verified; ~6x). v69 measures both.
- Also: M5 started — /matches route now runs the aligner (find_matches rewired, legacy
  two-pass+merger flow deleted); §6 condemned scene_aligner subsystems deleted (25
  functions, ~1200 lines; `_emission_score`/`_speed_prior_penalty` restored — they are
  LIVE via extract_scene_segments ranking, §6 list was over-broad); decode-DP tests
  rewritten against `_segment_timeline_dp`. Full pytest (dev env): 389 passed, 10
  failed — the same 10 fail on unmodified HEAD (LAN-transfer/upload-readiness env
  dependencies), not aligner-related.

## 2026-07-10 - v69-v77: M4 cap recovery on 411f (iterated on 411f only, then all-four)

- v69 (tug floor 0.12 + parallel presize): metrics identical to v68 on all four; elapsed
  96/197/252/154 — presize works but 411f still 252s > 200s cap.
- v70 (tug 24->16fps + candidate cap 3 + weak-cluster floor 0.35): 411f 214.6s, same.
- v71 (tug gated to SAME-EPISODE boundaries — episode switches are hard content changes
  every instrument sees; the invisible-cut pathology is intra-episode): 411f scene axis
  RESTORED 48/0/4 (the v68 break was an episode-switch tug move) and source 35/5/8/4.
- v72 (frame-store cache variant): 252s — the 1080p->640 PIL downscale on 10k frames
  costs more than the cross-zoom decode reuse saves on a zoom-1.0 project. REVERTED.
- v73 (tug 12fps, +-0.55 window): 209.5s, metrics stable.
- v74-v76 (score only assignment-proposed chains; then + index-suspect gate at -0.05):
  411f 167-201s; the -0.05 gate keeps the #6 greedy fix (970->964).
- v77 (query-variant path disabled — §6 evaluation): 411f identical metrics at 191.1s
  UNDER CAP, but leave-one-out FAILS elsewhere: dcd 12->11 exact, 85de 30->29 (+1 WP),
  5e85 28->25 exact (+3 loose). The variant path is NOT covered by the zoom-SSCD
  geometry estimate — it feeds retrieval-level evidence for weak scenes that arbitration
  cannot reconstruct. VARIANTS STAY (§6's conditional deletion is answered: keep).
  Elapsed savings were real (74/169/188/105) but not worth -5 exact.
- v78 (variants restored + variant embeds presized — the last single-threaded resize
  path): 5e85 scene axis REGRESSED to 41/4/1 (the tug's 6.85 fix lost). Bisection
  (v79-v83, 5e85 runs; result deterministic across repeats): NOT the tug window/floor,
  NOT the same-episode gate, NOT candidate floor/cap, NOT the scoring gates — the
  culprit was the v70 tug query rate 24->16 fps, whose 411f-only validation was a
  leave-one-out violation (lesson re-learned). Root cause: the move gain >= 0.10 is a
  SUM over moved frames — at 16 fps the 0.38s move has ~6 terms and cannot reach it.
- v84: 16 fps kept, gain floor scaled by rate (0.10 * 16/24 = 0.0667): 5e85 restored
  43/2/1 + 28/6/11/1 at 112.9s. v85 = final all-four fresh run.

## 2026-07-10 - v85 FINAL fresh state (frozen) + v86 rejected trim

- v85 (fresh, scene E/L/F, source E/L/WP/F, elapsed):
  - dcd74148c7ec: 18/1/1, 12/6/1/1, 81.8s
  - 85de83ca6323: 45/6/3, 29/7/12/6, 174.1s
  - 411f73d26c1d: 48/0/4, 35/5/8/4, 209.6s (~10s over the 200s cap; run variance ±8s)
  - 5e85164d9ff8: 43/2/1, 28/6/11/1, 111.8s
- vs v57 baseline (17/1/2 11/7/0/2; 45/6/3 28/6/15/5; 48/0/4 31/9/8/4; 41/4/1
  21/15/9/1): source exact 91->104 (+13), source loose 37->24 (-13), WP 32->32,
  scene exact 151->154, scene failed 10->9. dcd's source F 2->1 (downgraded to WP).
- v86 (tug window +-0.55 for the cap): REJECTED — re-broke the 5e85/dcd tug fixes and
  saved nothing (411f 210.5s: the window wasn't the cost). Reverted; v85 is frozen.
- 411f cap breach ~5-10s is reported honestly in the ceiling report; further savings
  require pipelining the sample decode (~40s serial floor), out of session scope.

## 2026-07-10 - v87-v90: oracle-guard hardening

- v85 oracle: dcd 18/20 (= baseline), 85de 47/54 (baseline 49 — WORSE), 411f 49/52
  (baseline 51 — worse), 5e85 46/46 (baseline 44 — the tug fixed the oracle too).
- v87 (tug local-optimum guard): no effect — the degraded oracle boundary (given-true
  85de@12.15 moved to 11.70) sits between WRONG-LINE (WP) scenes; under a wrong line
  the split evidence is meaningless and no self-referential guard can see it.
- v88 (tug skips duplicate-suspect sides — distant correspondence cluster within 0.05
  of the line's own support): oracle restored to baseline 85de 49/54, 411f 51/52.
- v89 (presnap guard: never move a boundary already sitting on a cut-grade diff peak
  — editors DO cut next to in-scene flashes, a stronger neighbour peak is not evidence
  of misplacement): 85de oracle 49 -> 51/54. The remaining oracle non-exacts are the
  arbitration-era fold-no-chain (#0) and two placements.
- v90 (both guards): oracle clean (18/20, 51/54, 51/52, 46/46) but FRESH paid -6 source
  exact — the presnap guard also blocks the moves that fix real detector offsets fresh.
- v91 = v85 + v88 tug-suspect-gate only (presnap guard reverted): FINAL configuration.
  Fresh: dcd 17/1/2 12/6/0/2 (74.3s), 85de 45/6/3 29/7/13/5 (136.3s), 411f 48/0/4
  36/4/8/4 (183.9s), 5e85 43/2/1 27/7/11/1 (103.3s). Aggregate source exact 104 (par
  with v85), 411f +1 exact, ALL FOUR UNDER THE 200s CAP, oracle guard baseline-or-
  better. Trade: dcd's 16.03 tug fix is blocked by the suspect gate (dcd scene back to
  17/1/2). Keep/why: only configuration meeting cap + oracle + best-known source axis.
- v91 oracle: 18/1/1 12/6/1/1 (58.4s), 49/4/1 32/8/12/2 (133.6s), 51/0/1 36/4/11/1
  (149.6s), 46/0/0 28/7/11/0 (89.6s) — every project at or above the v57/v58 oracle
  baseline; 5e85 returns given boundaries untouched.

## 2026-07-10 - session close: deliverables & remaining work

- Review HTMLs generated for all four projects: `docs/review_2026-07-10/review_*.html`
  (frame strips embedded); ceiling report + §8 waiver candidates:
  `docs/review_2026-07-10/CEILING_REPORT.md`. `backend/data/eval_waivers.json` left
  absent — verdicts are the owner's.
- Per-scene confidence + doubt tags ship on SceneMatch (`doubt_reasons`): 46/58 scenes
  tagged on 85de (duplicate_tie / static_start / static_end / rate_arbitrated /
  duplicate_rerank / chronology_assign).
- Tests: `pixi run -e dev pytest backend/tests/` = 389 passed, 10 failed — the same 10
  fail on unmodified HEAD (LAN-transfer 503s + upload-readiness fixtures; env-dependent,
  outside this goal). Excluding those two files: 385 passed, 0 failed. The aligner suite
  (10 tests incl. synthetic Stage-5 toolkit tests) passes.
- GT folders: `git diff` empty, zero untracked files.
- M5 status: /matches route runs the aligner; condemned scene_aligner subsystems deleted
  (25 functions ~1200 lines + 10 dead constants; `_emission_score`/`_speed_prior_penalty`
  proved LIVE and restored). REMAINING (documented, not done): anime_matcher's 13
  correction passes + crop-index subsystem (still serve the manual merge/rematch API;
  deletion needs dedicated regression coverage), scene_detector AUTO_DENSE_* gates
  (their removal changes fresh detection = a measured leave-one-out experiment),
  constants budget 32 vs <=15 (dead ones removed; consolidation not attempted).
- Strict PASS not reached on any project; binding constraints are index-blind duplicate
  repeats (85de WP 13, 5e85 WP 11 vs budget 2+3 waivers) and sub-second looses. Session
  outcome vs v57 baseline: source exact +13, source loose -13, all under 200s cap,
  oracle guard restored+, production route shipped.

## 2026-07-10 - v92-v94: owner review round 1 integrated

- Owner delivered exhaustive verdicts on all round-1 review HTMLs (unmentioned = pass).
  Recorded in `backend/data/eval_waivers.json` (109 entries: 75 pass / 34 fail across
  axes). Evaluator gained a STALE-waiver guard: a pass-waiver applies only while the
  generated interval stays within 0.35s/end of the reviewed one; moved intervals are
  re-flagged for review (owner explicitly allows microadaptation + re-validation).
- Post-review standings (v91 outputs + waivers): dcd 18/0/1/1, 85de 41/2/6/5,
  411f 47/1/0/4, 5e85 38/1/7/0 — remaining failures = exactly the owner-confirmed set.
- Failure-class attribution from the verdicts:
  (a) "too soon first frame"/"too late last frame": probed 85de#13 — NO source cut at
      the GT edge and SSCD edge margins are +-0.001 at both zooms: mid-shot trims on
      quasi-static shots pinned only by subtle motion. Machine-undecidable with current
      instruments (needs a motion-level comparator; future work).
  (b) 411f#25-class: chain-INTERIOR boundary smeared past a real source cut (cut at
      159.28, boundary at 159.70) — invisible to per-end anchoring (interior =
      continuity-locked) and to the tug (skips continuous same-line boundaries).
- v92 (containment clamp on chain-end edges, 0.5s reach): no effect — offsets exceed
  reach and/or no discriminating cut. v93 (reach 1.2 + 2-corr suspect clusters): still
  no effect on the target class (margins fail on lookalikes). One collateral: 5e85#1
  end moved -0.88 (stale-flagged).
- v94 (interior chain boundaries snap to a source cut within 0.55s): FIXED 411f#25
  (159.70 -> 159.24 vs GT 159.00) and improved dcd#9 (end lands exactly on GT 652.50);
  perturbed two previously-passed scenes within re-review scope (85de#9 +0.36 end,
  5e85#1). All v91->v94 metric deltas are stale-waiver accounting, no new failures.
  KEEP. Round-2 review HTMLs generated (`docs/review_2026-07-10/review2_*.html`:
  6/29/20/14 entries).
- Elapsed drifted up across v92-v94 runs (411f 184 -> 247s incl. run variance);
  needs one clean re-measure before the next cap claim.

## 2026-07-10 - v95/v96: post-review oracle + clean re-measure, ceiling report v2

- v94 oracle (--gt-scenes, waivers applied): scene 19/20, 50/54, 51/52, 46/46 — oracle
  guard HOLDS at or above the v57/v58 given-boundary baseline everywhere.
- v95 fresh (all four): reproduces v94 exactly (deterministic). v96 (411f, after
  reverting the metric-neutral 2-corr suspect softening): 411f source improved to
  48/0/0/4. Elapsed 240.5s vs v91's 183.9s — profiled: identical phases cost +23%
  (decode 50.4->72.1s for +5% frames, sampling +10s on unchanged code) = machine
  drift; real added work since v91 ~= +10-15s (interior snap + clamp). Normalized
  estimate ~195-200s; borderline vs the 200s cap, re-measure when the machine is
  quiet.
- Ceiling report updated (`docs/review_2026-07-10/CEILING_REPORT.md`): every project
  is a CEILING-REPORT by the §8 rule (owner waivers 6/13/12/11 > 3 each). Remaining
  failure classes: (1) near-identical duplicate repeats, (2) quasi-static mid-shot
  trims (measured SSCD-undecidable at any zoom -> needs a motion-level comparator),
  (3) the dcd#6/#7 missed-cut fold, (4) GT-side items for the owner (411f #7/#8 cut
  anomaly, #35, #4). Round-2 owner re-validation pending on review2_*.html
  (dcd#9 improved to exact-end, 85de#9 / 5e85#1 perturbed within microadaptation).

## 2026-07-10 - v97: pull-back-only interior snap (final state this round)

- Change: the interior chain-boundary snap only PULLS a boundary back behind a crossed
  cut (containment violation = the owner-confirmed defect); pushing boundaries OUT to a
  later cut extended owner-validated intervals (85de#9 round-2 perturbation) and is
  disallowed.
- Metric: 85de restored to 41/2/6/5 with ALL 15 waivers valid (zero stale); 411f keeps
  both round-1 fixes (48/0/0/4, scene 49/0/3); dcd 17/1/1/1; 5e85 37/1/8/0. Remaining
  stale re-reviews: exactly two, both evidence-backed pull-backs listed as doubtful
  scenes in the round-3 review pages — dcd#9 (end now exactly on GT 652.50) and
  5e85#1 (end 195.95 -> 195.07, edge-frame-confirmed).
- Round-3 review pages: docs/review_2026-07-10/review3_*.html (6/27/19/14 entries).
  KEEP.

## 2026-07-10 - v98 FINAL: containment clamp reverted (negative result), zero gating staleness

- Journal-protocol verdict on the chain-end containment clamp (v92/v93): it moved NONE
  of its target scenes (owner's "too soon/too late frame" class — measured, intervals
  byte-identical) and its only real effects were perturbing two owner-passed scenes
  (dcd#9 -0.41 end, 5e85#1 -0.88 end). REVERTED (block + synthetic test deleted). The
  interior-boundary pull-back snap (the change that actually fixed 411f#25) stays.
- v98 fresh (owner waivers applied): dcd 19/0/1 + 17/1/1/1 (7 waivers, 92.7s),
  85de 48/3/3 + 41/2/6/5 (15, 174.9s), 411f 49/0/3 + 48/0/0/4 (12, 238.6s*),
  5e85 44/2/0 + 38/1/7/0 (12, 129.0s). Zero stale waivers on 85de/411f/5e85; dcd#9's
  interval remains at the interior-snap position (end exactly on GT 652.50) — its
  waiver is stale but NOT NEEDED (loose-within-budget); it re-enters the §8 doubtful
  list as an ordinary loose entry. *411f elapsed carries the measured +23% machine
  drift (journal v96); normalized ~195-200s.
- v98 oracle: scene 19/20, 50/54, 51/52, 46/46 — guard holds at/above the
  given-boundary baseline everywhere.
- Round-4 review pages generated from v98: docs/review_2026-07-10/review4_*.html.
  State frozen pending owner §8 verdicts.

## 2026-07-10 - owner review round 2 integrated (v98 outputs)

- Owner verdicts on the round-4 pages upserted into `backend/data/eval_waivers.json`
  (112 entries). Round-2 newly VALIDATED: 411f #8 (explicit pass on the tricky
  first-frame case), #9, #31, #45; 5e85 #10; 85de #51; dcd #9 (the interior-snap
  improvement is now owner-approved at its current interval). Zero stale waivers.
- Post-round-2 standings (fresh v98 + waivers): dcd 19/0/1 + 19/0/0/1 (9 waivers) —
  only #6/#7 (missed-cut fold) remains; 85de 49/2/3 + 42/1/6/5 (17); 411f 51/0/1 +
  48/0/2/2 (15); 5e85 44/2/0 + 40/0/6/0 (14).
- Owner-confirmed residual fails: duplicates (85de #3 #10 #11 #17 #19 #20 #22 #24 #40
  #53, 5e85 #11 #25 #26 #45, 411f #28 #51, dcd #6), static trims (85de #13 #49,
  5e85 #32 #34, 85de #0). New owner hint: 5e85 #25 is a zoomed very fast linear
  right-to-left swoosh (motion signature).
- Static-trim probe (`probe_static_trim_localization.py`): high-res (256px) pixel NCC
  time-localization of the query edge frame within the static shot FAILS — argmax
  0.3-1.6s off or zero prominence; absolute NCC 0.13-0.28 (center-crop zoom search
  cannot reach pixel registration on zoomed+translated edits). NEGATIVE RESULT: the
  class needs true geometric registration (feature-based alignment / motion
  comparator) — proposed next instrument, out of this round's scope.

## 2026-07-10 - v99: registered pan localizer (the §4 geometric matcher, feature level)

- Instrument built after the probe validated it (`probe_registered_localization.py`):
  ORB+RANSAC partial-affine registration of the outermost edge frame onto the source
  plane, then phase-correlation shift-vs-time ZERO CROSSING — for panning shots the
  moment the pan passes the query's position localizes the edge (measured +0.026s on
  the owner-flagged 5e85#25 swoosh end where SSCD and NCC both fail). Wired into the
  ambiguous-end fallback, gated on in-scene motion (edge-vs-inset cos < 0.85), pan
  trajectory range >= 8px, response >= 0.4, |t0 - pred| <= 1.2s. Synthetic
  translating-texture test added (11 aligner tests green).
- The same probe TERMINALLY measures the quasi-static trim class: even at perfect
  registration (455 inliers, NCC 0.996) prominence is 0.001-0.002 — no exploitable
  signal at 360px. 85de#13-class stays undecidable; only sub-pixel/optical-flow
  research could revisit it.
- v99 fresh (round-2 waivers applied): dcd 19/0/1 + 19/0/0/1 (86.8s), 85de 49/2/3 +
  43/0/6/5 (168.4s, +1 exact, loose 0 — the pan localizer expanded the collapsed
  85de#49 interval to (1264.78,1265.56) vs GT (1264.91,1265.58)), 411f 51/0/1 +
  48/0/2/2 (242.0s*), 5e85 44/2/0 + 40/0/6/0 (160.4s). No regressions. KEEP.
  *Machine-drift regime (journal v96).
- 5e85#25/#26 remain: their defect is structural (generated interval 2.0s shorter
  than GT — a segmentation/extent error beyond edge repair), reclassified out of the
  edge-precision class.

## 2026-07-10 - strict-PASS path clarified: GT-noise corrections proposal

- Correction of an earlier overstatement: strict PASS with <=3 waivers is NOT
  arithmetically impossible — §8 classifies owner-passed sub-second disagreements as
  GT-noise, and the owner correcting GT for those (owner-only action; GT folders are
  agent-read-only) turns them exact WITHOUT waivers. Generated
  `docs/review_2026-07-10/GT_CORRECTION_PROPOSALS.md` from the v99-applied waivers:
  sub-second corrections dcd 6, 85de 7, 411f 3, 5e85 7; duplicate-instance decisions
  (repoint GT or keep as one of <=3 waivers) dcd 1, 85de 6, 411f 8, 5e85 5.
- Post-GT-correction arithmetic: dcd would sit at ~1-3 waivers + 1 fix needed (#6);
  the other three still require fixing the owner-confirmed fails (411f 2, 5e85 6,
  85de 12) whose instruments are at measured limits (duplicates / static trims /
  extent errors).
- Pending owner: round-5 verdicts (review5_*.html), GT corrections decision, 411f
  #7/#8 GT anomaly.

## 2026-07-10 - v99 oracle (final-state guard evidence)

- Oracle (--gt-scenes) on the final v99 code: scene axis 19/20, 51/54, 51/52, 46/46
  — at or above the v57/v58 given-boundary baseline (18/49/51/44) on every project;
  85de improved to 51 (the pan localizer helps the oracle too), 5e85 perfect. The
  oracle guard holds in the final state. Source axis 18/1/0/1, 43/1/8/2, 45/0/6/1,
  38/1/7/0 — consistent with fresh (segmentation-independence maintained).

## 2026-07-10 - v100 tug reach/suspect-floor experiment (REVERTED, negative result)

- Hypothesis: reach +-0.65 -> +-0.95 catches the 5e85@32.5 over-merge boundary (0.83
  away) and an evidence-scaled floor (2.5x for duplicate-suspect sides) unblocks the
  dcd@16.03 fix without the binary skip.
- Metric: 5e85 scene axis UNCHANGED (44/2/0 — the 32.5 boundary did not move) and dcd
  REGRESSED 19/0/0/1 -> 16/2/1/1 (owner-passed #11/#19 intervals perturbed, waivers
  stale). REVERTED; v99 state restored and verified (dcd 19/0/0/1 again, 9 waivers,
  zero stale).
- Conclusion: the three structural residuals (5e85#25/#26 over-merge, dcd#6/#7 fold,
  85de#0) are not reachable by tug parameter widening; they need segmentation-level
  treatment (DP boundary priors around loud cuts), which interacts with the whole
  measured stack — deferred with evidence rather than risked blind.
