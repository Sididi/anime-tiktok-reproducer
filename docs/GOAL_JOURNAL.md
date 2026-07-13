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

## 2026-07-10 - owner review round 5 integrated + owner-authorized 411f GT fix

- Round-5 verdicts (on the video-clip review5 pages): identical to round 2 except
  411f #8 pass→FAIL. Owner diagnosis confirmed frame-precise by direct inspection:
  the TikTok cut (woman→landscape) sits at frame 443/30fps = 14.767, so GT#7's range
  held the first frame of #8's shot. Owner offered "fix GT automatically or skip
  #7+#8"; fix chosen (well-determined: cut measured, E02 shot continues past the new
  #7 source end, E01 landscape shot starts 1079.70-1079.80 < src start). GT edit
  (owner-authorized exception to the read-only rule, backup in
  ~/.cache/atr-eval/gt_backup_411f_2026-07-10/): scenes.json #7/#8 boundary
  14.80→14.766667; matches.json #7 end 1107.95→1107.917, #8 start 1079.91→1079.89
  (each at its own scene rate; speed_ratio recomputed).
- Waivers: 54 review5 entries upserted (unmentioned = pass, with generated intervals
  for the stale guard), (8,scene)+(8,source) → fail, stale (25,source) fail dropped
  (machine-fixed in v94, owner exhaustive list = valid). 113 entries total.
- Metric (v99 outputs re-scored, GT fix + round-5 waivers, zero stale):
  dcd 19/0/1 + 19/0/0/1 (7 waivers); 85de 49/2/3 + 43/0/6/5 (14);
  411f 50/0/2 + 49/0/0/3 (13); 5e85 44/2/0 + 40/0/6/0 (12). All CEILING-REPORT
  (>3 waivers); the strict-PASS path remains the owner GT-corrections
  (GT_CORRECTION_PROPOSALS.md). 411f source exact 48→49 (#25 now counts on its own).
- Also this round: review generator now embeds real video clips (ffmpeg veryfast,
  360p, frame-accurate seek, data URIs; global 1x/0.5x/0.25x speed control +
  per-entry synchronized playback); review5_*.html regenerated from the v99 JSONs
  with identical entry sets.

## 2026-07-11 - GOAL.md v4: finalization phase opened

- Owner decisions (2026-07-11): GT corrections DECLINED (GT_CORRECTION_PROPOSALS.md is
  historical; GT final as-is, 411f #7/#8 repair was the one exception); hard set =
  duplicates + structural (18 scenes), quasi-static trims + 411f #7/#8 tolerable
  (waivable after honest attempt); dev cap 300s/project, back to 200s at M5; the
  <=3-waiver ceiling rule is RETIRED — done = owner-fail set empty + zero stale
  waivers + strict budgets on non-waived scenes (the verdict ledger is the
  acceptance record).
- GOAL.md rewritten (v4): targets the owner-labeled 24 scenes (18 hard: 14 wrong-
  instance H1, 4 structural H2; 6 tolerable), prescribes D1 motion/temporal-signature
  instrument (pan-localizer generalization) + D2 segmentation repair, and makes the
  offline labeled bench MANDATORY before wiring any scorer (v61 lesson codified).
  Milestones M0 reproduce -> M1 bench go/no-go -> M2 H1 -> M3 H2 -> M4 tolerable+
  review -> M5 re-hardening (200s, anime_matcher legacy deletion, AUTO_DENSE
  experiment, constants <=15).

## 2026-07-11 - v101: M0 reproduction (fresh + oracle, unmodified code)

- Fresh (round-5 waivers + 411f GT fix, JSONs ~/.cache/atr-eval/v101_fresh_*.json):
  dcd 19/0/1 + 19/0/0/1 (78.5s, 9 waiver entries), 85de 49/2/3 + 43/0/6/5 (167.2s,
  17), 411f 50/0/2 + 49/0/0/3 (233.6s, 14), 5e85 44/2/0 + 40/0/6/0 (144.8s, 13).
  Identical to the GOAL v4 §1 standings table on every axis; zero stale waivers;
  all four under the 300s dev cap (411f back under its drift-regime 242s).
- Oracle (--gt-scenes): scene 19/20, 51/54, 51/52, 46/46 — identical to the §1
  guard line; source 18/1/0/1, 43/1/8/2, 45/0/6/1, 38/1/7/0 = v99 oracle. The 9
  STALE lines in oracle output are expected mode noise (waivers certify the
  FRESH reviewed intervals; given-GT boundaries shift them) — fresh stale = 0.
- GT folders: git diff + untracked check clean. M0 done.

## 2026-07-11 - M1: labeled bench built + D1 measured; go/no-go per class

- Bench (`build_motion_bench.py` -> ~/.cache/atr-eval/bench/, manifest.json + 360p
  clips): 24 target scenes + 24 owner-passed controls; per scene the TikTok clip,
  the GT-truth window and the machine's claimed window (max-overlap generated
  line evaluated over the GT TikTok span — merging folded intervals is incoherent
  across broken chains) at native fps, +-1.25s pad. Probe:
  `probe_motion_signature.py` (motion + optional --sscd, signature caching).
- D1 as prescribed (motion/temporal-signature: 12Hz frame-diff energy global+3x3
  cells + phase-correlation velocity, Pearson under the candidate line, +-0.6s
  sweep): NO-GO as a standalone arbiter. Best variant (v4/v6 files): H1/distant
  7/9 positive but min -0.144; H1/near 0/2; controls 13/13 positive. Variants
  measured and rejected: 60fps densification (compression noise: truth r
  0.71->0.26), +-1.0s sweep (max-over-trials bias, 2 control flips) — the v61
  lesson reproduced offline, exactly what the bench is for.
- THE bench discovery — registered-footprint SSCD: registering the query frame
  onto each candidate's frame (ORB+RANSAC, the pan-localizer's first stage)
  shows the edit is a FULL-HEIGHT vertical crop (y-span 1.0, x-span 1/2.1-1/4.3
  of source width) whose x-center varies per scene (0.22-0.65 measured) — the
  production center `_zoom_crop` at z=1.45 is the wrong geometry model for
  off-center-framed scenes. SSCD scored on per-candidate registered footprints
  (production-faithful: each candidate registered independently, truth never
  consulted): ALL NINE rival-bearing 85de H1 duplicates separate, margins
  +0.196..+0.422 (incl. the 0.73s near-shift #10 at +0.278); 13/13 controls
  positive margin (distant min +0.143); ZERO false switches at threshold 0.10.
  GO for the H1 rerank class.
- Dead classes (both signals |margin| <= 0.03): 411f #28 (sscd -0.009, both
  absolutes ~0.80 — visually identical instances, only chronology/assignment can
  decide) and #51 (+0.008); quasi-static trims 85de #13 (+0.004), 5e85 #32/#34
  (+0.091/+0.026) — the round-1-2 verdict reconfirmed at registered geometry.
- Recovery class (was_no_match, no rival to outscore): 85de #19 truth certifies
  at 0.751 registered-SSCD (GO); 5e85 #11/#45 and 411f #8 defeat registration on
  every frame pair (fast action / 0.59x slow-mo; relaxed ORB yields degenerate
  rects) — recovery needs a fallback geometry, attempt due in M2 before any
  ceiling claim.
- H2: 5e85 #26 separates (+0.384 registered-SSCD); #25 dead (+0.010); dcd #6 and
  85de #0 are segmentation-shaped (D2/M3), bench margins irrelevant there.

## 2026-07-11 - v102-v104: M2 registered-footprint arbitration (iterated on 85de/dcd, then all four)

Instrument wired into Stage-5 R1, converging over ~10 debug iterations (logs
~/.cache/atr-eval/v102*_debug_*.log, v103*, v104*):
- `_footprint_rect` (ORB+RANSAC corners->fractional rect) + rect-crop support in
  `_zoom_crop`/`_WindowEmbedCache` (geom keys quantized 0.05); per-chain rect from
  the chain's own mid frame; candidates score under the chain rect as a cheap
  LOWER BOUND and re-register themselves only near the decision boundary
  (margin in [-0.10, +0.09) — a wrong current instance's framing understates the
  truth: 85de #17/#24 lost under shared-rect-only).
- Candidate recall: `_index_duplicate_recall` (source self-similarity >=0.80,
  >=2 distinct query times, neighbouring-cluster merge — the quantized key
  boundary otherwise splits one instance into two 1-hit clusters) +
  `_query_deep_recall` (query embeddings incl. edge insets, floor 0.45 — true
  instances measured at cos 0.51-0.54, rank 2-3, while sitting OUTSIDE the
  decode top-K) + chronology proposals (neighbours' unit-rate continuations;
  proposals REPLACE colliding candidates — their offset is exact where cluster
  offsets drift ~2s, dcd#19's fix needed exactly this).
- Decision tiers: switch at margin >=0.07 both-registered / >=0.12 one-sided
  (bench: identical repeats <=0.03, near controls <=0.047, true duplicates
  >=0.104); assignment-proposed >=0.02; certificate path unchanged; NEW
  fold-continuity tier: on forced revisits a proposal with margin >=-0.02 joins
  the switched neighbour's line. Best-margin wins over ALL candidates
  (first-past-post picked an inferior instance once recall widened the set).
- Revisit queue: a switch enqueues both neighbour chains (forced); revisits that
  already agree with the switched line propagate onward (dcd#19: 3-piece fold
  converged 773.9->777.0 across two hops, now strictly EXACT, waiver unused).
- Sweep: candidates +-1.2s (production offsets err by up to ~1s; +-0.8 lost
  switches), current line +-0.3 (its own fit); rescores +-0.3 at the first
  pass's alignment.
- Perf gates (v102h hit 504s on 85de): trust gate — chains whose current line
  scores >= trusted_floor registered with no suspicion skip arbitration
  entirely; recall only for doubtful chains; proposals only for deeply doubtful
  (< floor-0.05); assignment candidates filtered to index near-ties (junk
  measured -0.13..-0.50); scored set capped at 5.
- v104 all-four fresh: dcd 19/0/1 + 19/0/0/1 (108.8s, #19 EXACT beyond its
  waiver), 85de 50/2/2 + 48/1/2/3 (316.0s), 411f 50/0/2 + 49/0/0/3 (316.5s),
  5e85 44/2/0 + 40/0/6/0 (234.2s). ZERO stale. Aggregate source exact +5 vs
  v101; machine-fixed 85de H1: #17 #20(source) #22 #24 #40 #53 + #0(H2, now
  loose) + dcd#19. 85de/411f ~16s over the 300s cap -> v105 adds a per-project
  trust-floor calibration (registered scores run 0.72-0.93 on one style,
  0.64-0.79 on another; floor = max(probe scores) - 0.12 clipped [0.60, 0.75]
  on confident >=1.5s chains, windows cached for reuse).

## 2026-07-11 - v105: M2 rerank phase validated (fresh + oracle, all four)

- Fresh (waivers applied, JSONs v105_fresh_*.json): dcd 19/0/1 + 19/0/0/1
  (105.4s), 85de 50/2/2 + 48/1/2/3 (304.5s), 411f 50/0/2 + 49/0/0/3 (292.7s),
  5e85 44/2/0 + 40/0/6/0 (238.7s). Zero stale. 85de 4.5s over the cap
  (drift-noise level; the real cut is M5 work).
- Oracle guard HOLDS and improves: scene 19/20, 52/54 (was 51 — the registered
  arbitration helps the oracle too), 51/52, 46/46; source 18/1/0/1, 48/2/4/0,
  45/0/6/1, 38/1/7/0.
- H1 burndown after v105: fixed 85de #17 #22 #24 #40 #53 (+#20 source axis,
  +#0 H2 source loose, +dcd#19 exact-beyond-waiver). Remaining: 85de #3 (no
  instrument reaches truth: not in stage-3/recall/proposals), #10 (scene
  no-coverage inside a 3-piece chain — segmentation-shaped), #11 (piece-level
  wrong instance INSIDE a chained run — whole-chain switching can't see it),
  #19 + 5e85 #11 #45 (no-match recovery class, bench-certified reachable:
  truth 0.37-0.58 vs junk <=0.16 at grid geometry), #20 scene axis
  (fold-no-chain), 411f #28 #51 (both-signals-dead, chronology class).

## 2026-07-11 - v106: no-match recovery (wired, honest-abstain)

- R6 `_recover_no_match`: no-match scenes score candidate lines (neighbour
  continuations, own Stage-3 hypotheses, raw correspondence clusters, deep
  recall) under registered rects (bar max(0.55, floor-0.15) — registration
  success is itself >=15-inlier evidence) or a full-height grid of aspect
  footprints (bar 0.32, bench-derived); the SAME win-margin discipline as R1
  (best >= second + 0.07) or ABSTAIN — first cut recovered 5e85#11 to a
  lookalike (the neighbour continuation certified too; a wrong recovery stales
  waivers where a no-match stays harmless).
- Outcome: all three recovery targets ABSTAIN legitimately — 5e85#11: truth
  registers at 0.627 but the 251-lookalike scores 0.387-0.56 within 0.07 (loop
  content); 5e85#45: truth@790 enters via corr-clusters but scores 0.289 grid
  (< 0.32); 85de#19: its no-match piece spans GT#19+#20 (segmentation), the
  #20-half certifies (0.677) and wins — recovered content is #20's, #19 stays
  WP. No metric change on any project, zero stale; kept for the honest-attempt
  record and future reach.

## 2026-07-11 - v107/v108: dcd#6 instruments (both NEGATIVE, reverted)

- v107 certified tug (registered-SSCD certification overriding the v88
  duplicate-suspect gate): moved owner-passed dcd#11 (+0.36s end, waiver
  STALE) without touching #6 — #6 has NO boundary to move ("no generated
  coverage": the detector+DP timeline spans [15.33,17.73] across the 16.03
  cut). REVERTED per the §0 regression rule.
- v108 residual-step interior split (a missed cut inside static content
  prints a signed-residual step where the line smooths across the source
  skip 643.3->644.2): detector measured top-steps 0.5-1.0 in MANY owner-
  passed scenes (2fps grid + lookalikes) — the spread gate that protects
  them also blocks the target. NO-GO with n=1 labeled instance; REVERTED.
- dcd#6 diff-curve probe: the 16.03 cut itself is pixel-invisible (diff 0.33
  vs noise 0.2-0.5); a strong peak sits at 16.20 (8.7) inside a flash burst
  the detector merged. The reachable path is detector-level (the M5
  AUTO_DENSE/base-threshold experiment — AUTO_DENSE itself is inactive on
  dcd, <=70 scenes); until then #6 is a ceiling candidate: the missing
  instrument is a static-content cut detector.

## 2026-07-11 - v109: D2 hard-cut boundary-prior floor (M3) — 5e85 #26 fixed

- Root cause of the 5e85 #25/#26 fold: fragments split exactly at tt 32.50
  (tiktok_cos 0.067 — as hard as pixel cuts get) but the DP's dynamic-regime
  extrapolation prior measured 0.668 (blur/lookalike swoosh content) and
  leaned merge (-0.17). Fix: HARD_CUT_TIKTOK_COS=0.30 floor — at a certified
  hard pixel cut the prior never goes below +0.2 (over-keeping folds back for
  free under scene equivalence; a wrong merge is unrecoverable — the design's
  own stated asymmetry).
- 5e85: scene 44/2/0 -> 45/0/1, source 40/0/6/0 -> 41/0/4/1, zero stale.
  #26 EXACT (scene+source); two additional WPs fixed. #25 becomes the sole
  scene fail (fold-no-chain: the swoosh pieces sit on a 1.6s-early loop
  instance; the pan localizer GEOMETRICALLY places the query at the machine's
  position (479.39, response-best zero crossing) not GT's 481.0 — a looping
  pan whose instances sit INSIDE the 3s dedupe radius; margins dead
  (bench +0.018). Flag for owner arbitration in the next review round.

## 2026-07-11 - v110-v112: leave-one-out of the M3 floor + perf experiments

- v110 all-four exposed a 411f regression from the raw hard-cut floor: fast
  action has low tcos WITHIN shots too (73->79 generated scenes, two new
  fold-no-chain fails, +145s) — over-splitting an evidence hole does NOT fold
  back for free. Fix: contrast gate (floor only when intra - tcos >= 0.35 —
  content coheres on each side yet craters across the boundary). 411f
  restored, 5e85 gains kept.
- v111 perf experiment VERIFY_DECODE_FPS 12->10: REVERTED — the 0.1s grid
  cost 8 source exacts and staled 9 waivers across the four projects; 12 fps
  is load-bearing for R2 per-end precision. Recovery trims kept (grid 5->3
  x-centers, grid budget 3, candidates [:5]; 411f 386->338s).
- v112 FINAL M2+M3 state (fresh, zero stale; JSONs v112_fresh_*.json):
  dcd 19/0/1 + 19/0/0/1 (121.5s), 85de 50/2/2 + 48/1/2/3 (321.9s),
  411f 50/0/2 + 49/0/0/3 (338.1s), 5e85 45/0/1 + 41/0/4/1 (260.2s).
  Aggregate vs v101: scene exact 162->164, source exact 151->157, WP 12->6,
  source fails 9->8. Oracle guard HOLDS: scene 19/20, 52/54, 51/52, 46/46
  (at/above the v57/v58 baseline on every project; 85de +1 over the §1 line).
- Elapsed: 85de 322 / 411f 338 exceed the 300s M4 cap by 7-13% — inside the
  v96 drift-regime band but not claimable as met; the M5 re-hardening
  (pixel-retaining cache, batched multi-geometry embeds, decode reuse) is the
  real fix and is due regardless for the 200s target. Flagged as an M5
  dependency rather than papered over with micro-trims that kept costing
  correctness (v111).

## 2026-07-11 - v113/v114: piece-outlier arbitration — 85de #11 fixed

- R5b: a multi-piece chain can hide ONE wrong piece (the edit jumps away and
  back, 256.0 -> 198.6 -> 257.0, while a lookalike keeps the lines
  continuous). Per-piece registered scores on the chain's own cached window
  expose the outlier (chain 11-13 measured 0.87 / 0.57 / 0.76); the outlier
  piece decodes its own mids (the recall agreement gate needs >=2 distinct
  query times — one chain-mid is structurally insufficient) and arbitrates
  alone. 85de #11: winner @198.1 (margin +0.145) vs GT 198.59 -> EXACT.
- v114 all-four fresh + oracle: dcd 19/0/1 + 19/0/0/1 (113.2s), 85de 50/2/2 +
  49/1/1/3 (327.6s), 411f 50/0/2 + 49/0/0/3 (347.4s), 5e85 45/0/1 + 41/0/4/1
  (261.0s); ZERO stale; oracle 19/20, 52/54, 51/52, 46/46 — guard holds.
  Hard-set per-scene audit: 9 of 18 pass (85de #0 #11 #17 #22 #24 #40 #53,
  5e85 #26, 411f #28 via lookalike-equivalence); TOL 85de #13 #49 FIXED,
  411f #7 waived. review6_*.html regenerated from v114.

## 2026-07-11 - v115: perf ceiling measured (six attempts, all traded correctness)

- Attempts and their measured cost: candidate sweep 1.2->1.0 (85de -1 exact,
  411f -1 + 2 stale), sweep 0.8 (v103, -3), fps 12->10 (v111, -8 + 9 stale),
  shared-rect-only (v103, -2), chunked decode/embed pipelining (decode 119.5
  -> 202.8s: per-chunk seeks; results shifted), assignment-candidate filter
  -0.05 (411f -1), candidate cap 5->4 (411f -1). Serial split measured:
  decode 119.5s / embed 79.7s on 85de. Config restored to v114 exactly and
  re-verified (411f 49/0/0/3, zero stale).
- Verdict: at the current architecture the M4 300s cap conflicts with the §0
  regression rule on 85de/411f (305-347s); dcd 113s and 5e85 261s comply.
  The remaining levers are structural (M5): batch/NVDEC decode,
  pixel-retaining multi-geometry embeds, per-episode window planning.
- Ceiling report v3 written: docs/review_2026-07-10/CEILING_REPORT_V3.md
  (hard-set burndown, per-scene bench margins, missing instruments, owner
  asks for review round 6 incl. the 5e85#25 GT-vs-pan-localizer arbitration).

## 2026-07-11 - v116-v119: byte-identical pipelining for the M4 cap

After the correctness-trading trims were all rejected, the cap work moved to
output-preserving overlap only (validated per change: metrics + zero stale
identical on every run):
- Window prefetch: a decode worker (2 threads, per-thread captures) stages
  upcoming windows keyed by the EXACT slot run — a staged run is produced by
  the same decode call with the same parameters, so embeddings are
  byte-identical; partial-cache runs simply fall through to normal decode.
  Prefetch issue points: next-2-chains trust windows + R2 window specs
  (the R2 spec computation factored into `_r2_specs`, shared between the
  pass and the prefetcher for exact key match) + the current chain's
  candidate windows + registration probes (`prefetch_probe`/`probe_frames`).
- Stage-1 sampling producer/consumer: the worker owns the sequential decode
  + diff curve, the main thread embeds each 96-frame batch (unchanged batch
  composition). 411f sampling 48.8 -> 31.4s.
- R6 recovery invocation DISABLED (kept as documented experiment): every
  owner-labeled target legitimately abstains, so it cost ~40s/project for
  zero output change.
- Failed variants this phase (reverted): chunked decode+embed pipelining
  inside window() (per-chunk seeks ballooned decode 119->203s AND changed
  the sampling grid -> different results); cv2 FFmpeg threads option
  (already threaded, no change, hashes identical).
- Measured after: dcd 113-117s, 5e85 261-264s (comfortably under); 85de and
  411f oscillate 294-326s run-to-run with IDENTICAL code and outputs — the
  v96 drift regime straddles the cap on the two heavy projects; a 3-run
  median series decides the cap claim (v120).

## 2026-07-11 - v120-v123: cap measurement series + final M2/M3 validation

- v120 3-run series on the heavies: 85de 316/327/331, 411f 322/310/329 —
  consistently over after ~6h of continuous load. Thermal check: package
  83C, cores throttled to 800-950MHz — the drift is the machine, not the
  code. Cooled (72C): 85de 299.8 (v119a) and 411f 288.6 (v121) with the
  exact final code — the quiet-machine cap numbers; a last lookahead-4
  prefetch experiment showed no gain and was reverted to the measured
  state. Production runs one project per process, so per-invocation
  measurement is the production-faithful unit.
- v123 final M2+M3 validation (per-project fresh + all-four oracle):
  metrics identical to v114 on every axis, zero fresh stale, oracle
  19/20, 52/54, 51/52, 46/46 — guard holds.

## 2026-07-11 - owner review ROUND 6 integrated + start-side containment

- Round-6 verdicts (review6_*.html, exhaustive): all PASS except dcd #6,
  85de #10 #20, 411f #51, 5e85 #11 (the five machine failures stand);
  411f #7/#8 SKIP (GT region buggy: evidence-hole slow-mo burst) and
  5e85 #45 SKIP (NEW FACT: a non-anime scene is appended at the edit end,
  contaminating matching there; truth timings verified present among
  primary/secondary candidates). Evaluator gained a "skip" verdict:
  owner-approved permanent ignore, no stale-interval guard.
- Start-side containment (owner-endorsed spec, v124-v128): once the line
  is locked, the interval must not CROSS a native source cut the TikTok
  start frame sits after. Runs as a POST-pass (after R5b piece switches —
  85de#12's rendered start only becomes a render-segment start once piece
  12 moved), per render-segment (chain starts + intra-chain line
  discontinuities), scanning [s0, s0+1.25] on the shared window cache; a
  cut pair counts when its POST-cut frame lands inside the interval (the
  pair can straddle s0 itself — the dcd-grid places the 85de#13 cut mid at
  s0-0.02), and the start pulls onto the first clean frame when the start
  edge frame matches the post side by >=0.05. FIXED per the owner's
  complaint: 85de #13 491.72->491.78 (the single pre-cut frame removed;
  the GT 492.75 "cut" measures diff 0.003 — invisible static, renders
  identically), #12 256.21->256.55. Leave-one-out v128: dcd/411f
  unchanged, 5e85 improved (WP 4->3), zero stale, oracle guard holds
  (19/20, 52/54, 51/52, 46/46).
- Round-6 ledger upsert (`upsert_round6_waivers.py`, 121 entries; ran
  twice — a fold-no-chain scene failure short-circuits its source review
  entry, so second-order entries only emerge once the scene-axis waiver
  lands). FINAL round-6 standings (v128 outputs, zero stale):
    dcd  19/0/1 + 19/0/0/1   (#6 the sole failure)
    85de 52/0/2 + 52/0/0/2   (#10, #20)
    411f 51/0/1 + 51/0/0/1   (#51)
    5e85 46/0/0 + 45/0/1/0   (#11, WP)
  Non-waived budgets: loose 0 on every axis (<=3), WP 1 (<=2), and the
  only source fails are the owner-confirmed five. Every remaining failure
  IS the owner acceptance-record fail set; each carries its bench margins,
  honest integration attempts, and named missing instrument
  (CEILING_REPORT_V3).

## 2026-07-11 - v129/v130: round-6 fresh validation + cap evidence closed

- Fresh detection under the round-6 ledger (per-project invocations):
  dcd 19/0/1 + 19/0/0/1 (104.0s), 85de 52/0/2 + 52/0/0/2, 411f 51/0/1 +
  51/0/0/1, 5e85 46/0/0 + 45/0/1/0 (233.1s). Zero stale on every run; the
  #12/#13 containment intervals (256.55 / 491.78) hold from fresh detection.
  The only remaining non-waived failures are the owner's five acceptance-
  record fails.
- Containment reach trimmed 1.25 -> 0.85s: the scan is now a pure cache hit
  on the R2 window (both owner cases needed <=0.35); metrics identical.
- Cap evidence: the afternoon "drift" was identified as a REAL competing
  workload (Discord call at ~105% CPU, machine idle floor 72C) — not code.
  Quiet-machine per-invocation measurements (production-faithful: /matches
  serves one project per process): dcd 104-117s, 5e85 233-267s, 411f 288.6s
  (v121), 85de 299.8s (v119a); the code deltas since those runs (start-side
  containment after the 0.85 trim, scoring-side skip verdicts) measured ~0
  marginal cost on same-day comparisons (v129 vs v121-code both 313-314
  warm). Busy-machine numbers run +10-20% and are documented, not claimed.

## 2026-07-12 - v133: quiet-machine cap measurement — ALL FOUR under 300s

- The machine finally went quiet (Discord call ended; package back at its
  71C idle floor). Final-code per-invocation fresh runs, round-6 ledger:
  85de 52/0/2 + 52/0/0/2 in 271.8s; 411f 51/0/1 + 51/0/0/1 in 250.5s;
  zero stale. With dcd 104.0s and 5e85 233.1s (v130), every project runs
  the full fresh pipeline within the M4 300s cap on a quiet machine —
  the entire 314-359s band measured earlier was workload/thermal
  contention (journal v129), not code.
- M4 is complete: round-6 verdicts integrated, review pages delivered,
  tolerable set fixed-or-waived, budgets met, the five owner-confirmed
  fails carried by the ceiling report. M5 (200s target, legacy deletion —
  rematch contract test already in place at
  test_anime_matcher_partial_rematch.py — detector experiment, constants
  audit) is the next phase.

## 2026-07-12 - v134/v135: the named detector-level experiment (RUN, negative, REVERTED)

- The ceiling report named a detector-level path for dcd#6 (and possibly
  85de#10/#20) that had never been attempted: the M5 AUTO_DENSE/base-
  threshold experiment. Probe: threshold 8 emits boundaries 15.47 + 16.20
  around the invisible dcd cut (16.20 = 0.17 from GT — exact range).
  Implemented as an unconditional sensitive pass reusing the existing
  AUTO_DENSE reinjection (`_refine_dense_ranges_with_sensitive_boundaries`
  with threshold-8 boundaries injected into the base-16 skeleton).
- dcd alone: #6 CONVERTED (scene fail -> loose (15.46,16.57) vs GT
  (15.33,16.03); source fail -> WP-with-candidate; source fails 0) at the
  cost of owner-passed #7 slipping exact -> loose + 1 stale (its interval
  actually moved CLOSER to GT: 644.50/645.78 vs 644.20/645.90).
- Leave-one-out KILLED it: 5e85 collapsed 46/0/0 -> 41/2/3 scene with
  FIVE stale waivers and 2 new source fails (threshold-8 over-cuts fast
  action into no-evidence pieces -> fold-no-chain + interval churn);
  411f -1 source exact + 1 stale; 85de -1 source exact and #10/#20
  unmoved. A dcd-only gate would be fixture-keying (forbidden). REVERTED;
  revert verified (dcd 19/0/1 + 19/0/0/1 in 97.0s, 5e85 46/0/0 +
  45/0/1/0 in 213.6s, zero stale).
- The dcd#6 exception record is now complete at every identified layer:
  three aligner-level instruments (certified tug v107, residual-step
  split v108, containment reach) plus the detector-level experiment, all
  measured. The missing instrument stands: a cut detector that can emit
  static-content boundaries WITHOUT over-cutting action content — a
  motion-conditioned sensitivity the current ContentDetector cannot
  express. Same verdict transfers to 85de#10/#20 (their boundaries did
  not materialize even under threshold 8's global over-cutting).

## 2026-07-12 - v136: the MOTION-CONDITIONED cut detector — dcd#6 CONVERTED (KEEP)

- The v134 negative result refined the missing instrument to "a cut
  detector that emits static-content boundaries without over-cutting
  action". Measured signature: the true static cut's sides run 0.09-0.27
  median 64px frame-diff while every v134-damaging action boundary has a
  side >=14 (min-side 0.49 but max-side 27) — a physical near-zero-motion
  gate, not a tuned threshold. Implemented:
  `_reinject_static_sensitive_cuts` (threshold-8 boundaries novel vs the
  base skeleton, kept only when BOTH sides are static <=1.0, reinjected
  via the existing AUTO_DENSE refine; STATIC_CUT_MOTION_CEILING=1.0).
- dcd: #6 scene FAIL -> LOOSE (15.33,16.57 vs GT 15.33,16.03 — start
  exact) AND source FAIL -> LOOSE (642.72,643.96 vs 642.60,643.30). The
  owner-labeled static missed cut is machine-fixed within §6.
- Leave-one-out: 85de 52/0/2 + 52/0/0/2 and 5e85 46/0/0 + 45/0/1/0 —
  byte-stable, ZERO stale (the gate fully prevents the v134 collapse);
  411f 51/0/1 + 50/1/0/1 (#12 exact -> loose, 1 stale). Oracle guard
  HOLDS: 19/20, 52/54, 51/52, 46/46.
- Cost, per §0 permitted (a hard fail is fixed): three owner re-reviews —
  dcd #7 (643.96,645.12 — start toward GT, end 0.78 off, still loose),
  dcd #18 (end +1.22), 411f #12 (43.17,46.14, loose). All within the §6
  budgets (dcd source loose 3<=3, 411f loose 1). review7_*.html generated
  for the round.
- Hard-fail set after v136: FOUR — 85de #10 #20 (detector boundaries did
  not materialize even at threshold 8: no pixel-level cut exists at
  12.15/21.22 — evidence-hole class), 411f #51, 5e85 #11 (both
  instrument-dead with bench margins <=0.01). Elapsed (quiet machine,
  same-session): dcd 98.5s, 5e85 225.4s, 85de 283.1s, 411f 298.5s — all
  four within the 300s cap WITH the new detector pass.

## 2026-07-13 - owner review ROUND 7 integrated + GOAL v4.2 (final-six phase)

- Round-7 verdicts (review7_*.html, exhaustive): all PASS — including dcd #6/#7
  (the v136 motion-conditioned detector conversion is owner-validated) and 411f
  #12 — except: 411f #28 "first frame too soon" (owner: the chosen instance is an
  exact duplicate of GT's and APPROVED — the GT instance is distinguishable only
  by having NO progressive zoom-out; fix the start only), 411f #51, 5e85 #11,
  85de #10 #20 (hard, confirmed), dcd #18 "first frame too soon" (v136
  collateral).
- Ledger: 44 review7 entries upserted; 8 stale fail entries purged for scenes
  machine-fixed in v105-v136 (5e85#26, 85de#0/#11/#17/#24/#40/#49) per the
  exhaustive convention. Final: 115 entries = 6 fails (the target), 6 skips
  (411f#7/#8, 5e85#45), rest passes; zero stale.
- GOAL.md updated to v4.2: target = 4 hard (85de #10 #20 evidence-hole cut
  insertion, 411f #51 + 5e85 #11 signature-dead duplicates) + 2 start-precision
  (411f #28, dcd #18); new prescribed instruments D3 (registered scale-velocity
  + loop-phase signatures — the owner's zoom-out remark is the design hint),
  D4 (source-side crossover cut insertion for boundary-less regions), D5
  (start-containment extension); M5 unchanged (200s, legacy deletion behind the
  rematch contract test, AUTO_DENSE experiment preserving the v136 reinject,
  constants <=15).

## 2026-07-13 - v138: M0 verification of the round-7 ledger (v4.2 phase opened)

- The v137 fresh runs (05:43, made to generate review7 pages) PREDATE the
  final round-7 ledger upsert (08:37) — their STALE lines were pre-upsert
  noise; timestamps + ledger inspection confirmed the ledger holds the
  reviewed (review7-page) intervals for dcd#7 (643.96,645.12), 411f#12
  (43.17,46.14) and dcd#18 (761.58,771.71). 115 entries, no duplicate
  (project,scene,axis) keys, 6 fails + 6 skips as §2.
- Re-score of the saved v137 outputs under the final ledger
  (--load-generated-json, all four): ZERO stale; dcd 20/0/0 + 19/0/1(WP
  #18), 85de 52/0/2 + 52/0/0/2 (#10 #20), 411f 51/0/1 + 51/0/0/1 (#51;
  #28 ledger-failed on the source axis), 5e85 46/0/0 + 45/0/1/0 (#11).
  The only non-waived failures are exactly the six §2 targets.
- Fresh standings = the v137 runs themselves (current code, quiet machine):
  dcd 100.7s, 85de 293.7s, 411f 295.6s, 5e85 224.6s — all under the 300s
  M4 cap. Oracle guard (v138_oracle.log): scene 19/20, 52/54, 51/52,
  46/46 — exactly the §1 baseline; oracle STALE lines remain expected
  mode noise (v101). GT folders: git diff + untracked clean. M0 done.

## 2026-07-13 - v139: M1 bench — D3/D4 measured, go/no-go per scene

- New probes (`probe_geometry_trajectory.py`, `probe_crossover_insertion.py`),
  artifacts geomtraj_ctrl.json + crossover_v1.json in the bench dir.
- D3 geometry-trajectory (ORB+RANSAC scale/shift per 12Hz sample, coherence =
  reg-persistence minus trajectory roughness, offset sweep ±0.25):
  - 5e85#11 GO (abstain-breaker): truth coherence 0.360 / reg-rate 0.41 vs
    BOTH loop lookalikes (250.4 + 260.5 windows) coherence 0.0 / reg-rate
    0.00. Rule shape: registration-persistence certificate (best reg>=0.4,
    second reg<=0.1). Controls 13/13 positive margin (min +0.030); no
    wrong-direction fire anywhere (H1 12/12 positive too).
  - 411f#51 NO-GO, terminal: loop phases register equally (0.86/0.86),
    coherence margin +0.002, slopes/roughness identical; adds to the binding
    negatives (SSCD +0.008 v66, NCC dead v61-63, D1 energy corr NEGATIVE
    toward truth). A ~1s loop-phase shift on near-static content has no
    measurable signature in any built instrument → owner final call.
  - 411f#28: the owner's zoom-out intelligence is REAL and measurable:
    truth scale-slope −0.0010/s vs pick −0.0664/s (progressive zoom-out).
    #28's fix stays D5 (instance approved; start only).
- D4 crossover insertion (per-sample registered SSCD under line A vs B,
  per-line offset calibrated on its home region):
  - 85de#10: crossover 12.248 vs GT 12.15 (err +0.098s, exact range);
    run margins [0.024, 0.159, 0.146, 0.147].
  - 85de#20: crossover 21.182 vs GT 21.217 (err −0.035s); margins
    0.337-0.488.
  - Controls 23: raw >0 fires 2 (mean margins 0.0023 / 0.0165); with a
    per-sample margin floor 0.05 and >=3-run: ZERO control fires, gap
    [0.017, 0.146] ~9x. GO for both scenes at floor 0.05.

## 2026-07-13 - v140-v143: M2 start-precision pair — BOTH targets machine-fixed

- dcd#18 diagnosis: the start (761.58) is NOT cut-crossing — the native cut
  sits at ~761.56 (post side) and GT 761.73 is 0.19s INTO the shot: the
  start was already within exact tolerance. The real defect was the END:
  v136's reinjected 57.80 boundary created 1-piece chain g37 whose line
  landed on a +0.86s static lookalike (771.34-771.71, registered 0.751 —
  confident, margins dead) while g36's continuation is the truth. Root
  cause: `_known`'s 3.0s dedupe radius dropped every neighbour-continuation
  proposal within 3s of the current line — the sub-3s duplicate class
  (same radius v109 measured on 5e85#25) was structurally unreachable.
- Fix 1 (near-continuation certificate): proposals survive injection
  unless truly same-line (<0.15s); a proposal within 3s of the current
  line may win the fold-continuity tier ONLY under the native identity
  certificate (cross-window cos >=0.95) — pre-hardening against the
  v62-63 wrong-propagation trap. dcd: #18 source WP -> EXACT
  (761.58,770.72 vs GT 761.73,770.50), scene 20/20 + source 20/20, zero
  stale, 113.7s.
- Fix 2 tried and REVERTED (all-pieces start containment): +85s window
  decode on dcd for zero effect on the targets (dcd#18's cut straddles
  its start and is correctly excluded; 411f#28's containment ALREADY
  fires — its start sits on the exact first post-cut native frame).
  Collateral measured before revert: dcd#8 start pulled 645.12->645.92
  (toward GT 646.00) but stales the owner-pass; not needed by any target.
- 411f#28 diagnosis: "first frame too soon" is SHOT-RELATIVE: the truth
  shot is fully static and starts >1.25s before GT's start (no native cut
  at 196.5); the pick instance progressively zooms (bench slope -0.0664/s
  vs truth -0.0010/s) and its zoom state never matches the static query
  at any offset inside the interval (target scale 1.495 reached ~+2.9s,
  outside all candidates). NO start on the zooming instance renders
  correctly -> the honest fix is the owner's own hint: scale-velocity.
- Fix 3 (D3 scale-velocity certificate, new tier in R1): on duplicate
  near-ties (|margin| <= 0.06 — SSCD prefers wrong duplicates by up to
  0.06), measure zoom rates (ORB scale of registrations, lstsq log-scale
  slope): query self-rate from the chain's interior query frames,
  line rates from two-three probe frames along each line. A candidate
  qualifies when |sv_cur - sv_q| >= 0.03 AND |sv_cand - sv_q| <= 0.015
  AND cur-mismatch >= 2.5x cand-mismatch; multiple qualifiers (loop
  repeats) prefer the neighbours' episode then best margin. New helper
  `_zoom_rate`, per-chain `mid_gray_seq`. 411f: chain 42 switched off the
  zooming instance; after a forced revisit the chronology tier settled
  (197.23,198.23) — the STATIC instance, lookalike-equivalent to GT
  (196.50,197.50): source EXACT, render artifact gone. Measured control
  behaviour: chain 11 (q +0.027/cand -0.017) and chain 40 (q +0.028/
  cand 0.0 at ratio 1.66) both correctly rejected by the match bound.
- 411f collateral: source#19 stale (111.67,115.06 -> 110.81,115.44,
  START toward GT 110.36: 1.31 -> 0.45) from a certificate chronology
  switch; scores loose non-waived; goes to review8 (v136-precedent
  shape). #12 intact. 411f standings: 51/0/1 scene + 50/1/1 source
  (#51 = the bench-measured no-go).

## 2026-07-13 - v144-v155: M3 hard burndown — 85de#10 #20 and 5e85#11 machine-fixed

- R5c crossover insertion (D4) shipped after three trigger iterations:
  - Hypothesis-triggered version measured DEAD: stage-3 hypotheses all
    span the full scene (no sub-span localization), and retrieval cannot
    attribute supporters between lines <1s apart (corr tolerance 0.35) —
    the correspondence-support pre-gate never fires for near-shifts.
  - Shipped trigger: chains where R5b moved an interior piece (the
    measured jump-away-and-back pattern); line B = SHIFT GRID of the
    piece's own line (±0.3..±1.2 step 0.15). Shift-sweep bench
    (probe_shift_sweep): positive fires at delta 0.60/0.75/0.90 for the
    true +0.73, crossover 12.33 err +0.18s, suffix margins 0.13-0.14;
    controls 0/24 phantom fires across the whole grid. All deltas reuse
    ONE window's embeddings (pure numpy per delta).
  - The split is applied in _build_matches (a new Scene + SceneMatch,
    renumbered; one-match-per-scene contract kept). 85de#10: [xover]
    piece 11 split@12.33 delta +0.75 -> scene#10 EXACT, source#10 EXACT
    (256.20,256.52 vs GT 256.0,256.5); #9 self-passes via equivalence.
- 85de#20 was a candidate-SOURCING hole, not a scorer hole: truth scores
  0.823 at g21's span vs 0.549 current (+0.274!) but nothing proposed it.
  Fixes: (1) recall-cluster candidates sweep 2.0 (their offsets drift
  ~2s, measured v102-104) and self-register; (2) low-margin retry: a
  recall/proposal candidate losing beyond the rescore band gets ONE
  self-registered wide-sweep retry (85de#20: 0.424 chain-rect -> 0.855
  self-registered at −1.9s). g21 switched (+0.216), g22 joined via the
  near-continuation certificate -> scene#20 LOOSE (22.07,23.80) +
  source#20 LOOSE (716.84,719.33 vs 716.35,719.00). FAIL -> LOOSE both
  axes.
- Retry guardrails, each measured before adoption: (a) index
  alternatives EXCLUDED from the retry (unrestricted retry switched
  85de#34 to the OP repeat at 21.7s, staling an owner pass + 8 collateral
  switches); (b) RETRIED candidates may only win via a real margin
  (best_switch), never the continuity tiers — the certificate verified
  alignment 20.3 but the max-over-trials sweep applied 21.7 (v61 bias in
  new clothes). Final 85de: 53/1/0 + 53/1/0, ZERO stale, zero fails,
  #34 kept at 789.9.
- 5e85#11 (D3): R6 recovery RE-ENABLED behind the registration-
  persistence certificate. First attempt exposed two recovery defects,
  both fixed: a lone grid-path lookalike won by default (the 251.1 loop
  instance — grid-only candidates now NEVER win alone), and the truth
  hypothesis line is rate-corrupted (maps 1.9s early; the grid's best
  alignment now seeds a registration retry, exactly the #20 trick).
  Result: candidate @235.3 registers persistently (reg_n=2, 0.627 — the
  v106 truth score) -> primary (234.31,236.01) vs GT (234.57,236.43):
  source LOOSE, WP 1 -> 0. 5e85: 46/0/0 + 45/1/0/0, zero stale.
- Target ledger after M2+M3: dcd#18 EXACT, 411f#28 EXACT(equivalent,
  static instance), 85de#10 EXACT, 85de#20 LOOSE, 5e85#11 LOOSE;
  411f#51 = the one bench-measured NO-GO (terminal: loop phases register
  identically 0.86/0.86, coherence margin +0.002, SSCD +0.008, NCC dead,
  D1 energy corr negative — no instrument separates a ~1s loop-phase
  shift on near-static content). v156 all-four leave-one-out running.

## 2026-07-13 - M5 part 1: anime_matcher legacy correction passes + crop-index DELETED

- anime_matcher.py 6572 -> ~3500 lines. Deleted: the 15-pass correction
  stack (_stabilize_*, _snap_short_scene_reset_edges, _promote_dense_*,
  _promote_duration_consistent_*, _extend_*, _promote_short_end_*,
  _promote_supported_local_bracket_*, _promote_dense_visual_aligned_*)
  and the whole crop subsystem (_load_or_build_crop_index,
  _search_crop_index_batch, _search_local_crop_windows_batch,
  _source_crop_variants, _refine_crop_projected_start,
  _should_try_crop_search, crop LRU cache + CROP_*/LOCAL_CROP_*
  constants, the boundary-refine crop fallback). match_scenes now:
  probe search -> temporal/projected proposals -> merged-seed ->
  refine -> _validate_and_repair_matches -> partial-rematch
  preservation. _load_dense_source_cuts kept (contract test patches it).
- Rematch contract test GREEN standalone
  (test_anime_matcher_partial_rematch). 25 tests OF deleted passes
  removed from test_anime_matcher_cache.py; 5 aligner tests updated for
  the _stage5_refine 3-tuple return; new synthetic test
  test_zoom_rate_measures_progressive_zoom_and_static (D3 instrument).
- PRE-EXISTING pollution documented: test_anime_matcher_cache leaves
  "'local' is not a valid LibraryType" state that fails the rematch
  contract test when run AFTER it in the same session (reproduced with
  the pre-deletion test file too — not introduced by the deletion;
  rematch test passes standalone).
- Two collateral attribute-block losses during deletion caught by tests
  and restored: _runtime_stats and the video-frame embedding LRU
  (_video_frame_embedding_cache / VIDEO_FRAME_EMBEDDING_CACHE_MAX).

## 2026-07-13 - v157/v158: frozen-code validation exposed a recovery trap; M5 part 2

- M5 part 2 shipped before the freeze: aligner constants 38 -> 15
  module-level (23 single/double-use tunables inlined at their use sites
  with values unchanged; the 15 keepers each carry their measured
  justification), AUTO_DENSE removal experiment gated behind
  ATR_NO_AUTO_DENSE (default behaviour unchanged), full pytest measured:
  405 passed / 11 failed = the 10 journal-documented env failures
  (LAN-transfer 503s + upload-readiness fixtures) + 1 PRE-EXISTING
  test-order pollution (test_anime_matcher_cache leaves state that fails
  the rematch contract test in-suite; REPRODUCED on unmodified HEAD via
  stash — not ours).
- v157 (fresh, quiet): dcd 20/0/0 + 20/0/0/0 in 110.0s ✓; 411f 51/0/1 +
  50/1/0/1 (#51 + the known #19 toward-GT stale) BUT 425.9s; 5e85 46/0/0
  + 45/1/0/0 zero stale BUT 320.8s; 85de REGRESSED: the re-enabled
  recovery matched no-match piece g20 (tt 20.35-22.07, spans GT#19+#20 —
  the v106-documented trap) onto #20's back-extended line at a 2-of-3
  persistence certificate -> scene#19 stale + WP. The new instruments
  also cost +90-130s/project (411f window calls 467 -> 877; recovery
  windows were the largest un-prefetched decode source).
- v158 (5e85, 3-of-3 certificate only): #11 STILL recovers — by pure
  margin now (0.627 truth vs 0.387 lookalike, +0.24) — proving the
  certificate gate alone cannot stop g20-class margin wins either.
- Hardened rule shipped: ANY recovery win (margin or certificate) needs
  registration at head, mid AND tail query frames; each outer frame gets
  up to 3 probe-frame attempts (fast action defeats single-frame ORB at
  the bench-measured 0.41 reg-rate). Also: trailing no-match pieces are
  never recovered (GOAL §2: production edits append non-anime outros);
  recovery rect-path sweep back to 1.2 (truth enters via the grid->reg
  retry); recovery candidate windows + retry-eligible proposals staged
  on the prefetch worker. v159 validating.

## 2026-07-13 - v159-v161: the recovery trap resolved structurally; perf ceiling measured

- v159 negative result (3-of-3 registration persistence): registration
  measures REGISTRABILITY, not identity — 85de g20's WRONG winner (the
  #20 back-extension over lookalike shots) registers 3-of-3 while
  5e85#11's TRUE winner registers only 2-of-3 (fast action defeats the
  head frame at the bench-measured 0.41 reg-rate, even with 3
  probe-frame attempts). The per-frame gate is backwards for this shape.
- Shipped rule (v160): a recovery winner that is merely a NEIGHBOUR
  chain's line continued into the no-match span (boundary gap <0.5s)
  is never applied — either the content truly continues (the chain
  machinery would have joined it) or the piece hides a boundary and the
  line explains one side only. Novel-line winners still need mid-frame
  registration + margin/certificate. v160: 85de 53/1/0 + 53/1/0 ZERO
  stale (#19 intact, #10 exact, #20 loose); 5e85 46/0/0 + 45/1/0/0
  zero stale (#11 recovers loose at 234.28,235.98).
- Perf (quiet, sequential): dcd 110.0 (v157), 5e85 312.1, 85de 389.4,
  411f ~426. The M2/M3 instruments cost ~+30% over the v137 baseline
  (294-296s on the heavies — already above the 200s M5 target BEFORE
  any new instrument). Profile: candidate scoring 210s of 330s refine
  on 85de (224 scorings; window decode 210-222s). Measured
  non-levers: prefetch workers 2->4 changed nothing (candidates are
  requested immediately after staging — no pipeline gap); NVDEC (the
  submodule's proven path) is built for sequential full-episode
  decode, not the aligner's dynamically-discovered seeked windows.
  The honest M5 conclusion: <=200s needs the named window-planning
  redesign (plan-ahead window batches per episode + sequential NVDEC
  sweeps) — a structural project, documented as the remaining gap.

## 2026-07-13 - v162: final-code dcd/411f validation + the AUTO_DENSE experiment (RUN, inert)

- Final code, fresh: dcd 20/0/0 + 20/0/0/0 zero stale (113.4s);
  411f 51/0/1 + 50/1/0/1 with only the known #19 toward-GT stale
  (416.1s). Full final-state fresh set: v162_dcd, v161_85de (53/1/0 +
  53/1/0, zero stale), v162_411f, v160_5e85 (46/0/0 + 45/1/0/0, zero
  stale).
- AUTO_DENSE_* removal experiment (ATR_NO_AUTO_DENSE=1 on 411f, the
  only GT project above the 70-scene trigger): metrics and scene count
  BYTE-EQUAL to baseline (78 scenes, same standings, same stale). The
  pass triggers but its threshold-40 re-detection lands outside the
  [45,70] accept band and is rejected — AUTO_DENSE is inert on the
  whole GT set. Verdict: keep the code as a production safety net for
  >70-scene edits whose dense counts land in the accept band; the v136
  static-cut reinject (independent machinery) preserved and verified
  (dcd#6 still loose/waived-pass, dcd 20/20).

## 2026-07-13 - v163: FINAL frozen-code validation — M2/M3/M5 closed, M4 review-8 ready

- Final fresh standings (frozen code, per-project invocations; scene
  E/L/F + source E/L/WP/F, quiet-machine elapsed, stale):
    dcd  20/0/0 + 20/0/0/0  113.4s  stale 0
    85de 53/1/0 + 53/1/0/0  389-400s stale 0
    411f 51/0/1 + 50/1/0/1  416.1s  stale 1 (#19, toward-GT, re-review)
    5e85 46/0/0 + 45/1/0/0  312.1s  stale 0
  The only non-waived scene/source FAILURE anywhere is 411f#51 (the
  bench-measured terminal no-go). Non-waived budgets: loose <=3/axis ✓,
  WP 0 <=2 ✓, source fails: only #51 ✓.
- Oracle guard (v163, frozen code): scene 19/20, 52/54, 51/52, 46/46 —
  exactly the §1 baseline on every project. GT folders byte-identical
  (git diff empty, zero untracked). Ledger unchanged: 115 entries
  (6 fails / 6 skips / 103 passes) — verdict updates are the owner's
  round-8 act.
- pytest final: 405 passed, 11 failed = the 10 journal-documented env
  failures (LAN-transfer 503s + upload-readiness fixtures) + the
  pre-existing test-order pollution (rematch contract test after
  test_anime_matcher_cache; reproduced on unmodified HEAD).
- review8_*.html generated in docs/review_2026-07-10/ from the final
  outputs (2/15/19/5 entries, embedded clips). Round-8 owner items:
  (1) 411f#51 — final call on the terminal no-go; (2) 411f#19 stale —
  start moved TOWARD GT (err 1.31 -> 0.45, loose); (3) the five fixed
  targets at their new intervals (dcd#18 exact, 411f#28 static-instance
  exact-equivalent, 85de#10 exact via crossover split, 85de#20 loose,
  5e85#11 recovered loose); (4) collateral interval movements inside
  tolerance are listed on the pages.
- M5 ledger: legacy passes + crop-index DELETED (contract test green);
  AUTO_DENSE experiment run (inert on the GT set, code retained);
  aligner constants 15 with justifications; <=200s NOT met — measured
  110/389/416/312 quiet; the remaining gap needs the window-planning +
  sequential-NVDEC decode redesign (named, scoped, out of this
  session's reach); the M4 300s cap holds only for dcd — the heavies
  run 312-416s with the full M2/M3 instrument set (the owner's 300s was
  set before D3/D4/D5 existed; flagged for the round-8 conversation).

## 2026-07-13 - ROUND 8 verdicts + v164: 411f#51 FIXED — the owner-fail set is EMPTY

- Owner round-8 (exhaustive on the review8 pages): ALL PASS — the five
  machine-fixed targets at their new intervals, the 411f#19 toward-GT
  stale, every collateral. Ledger upserted (upsert_round8_waivers.py,
  10 entries refreshed; 2 stale round-7 fail records purged per the
  exhaustive convention).
- NEW owner intelligence on the last failure, 411f#51: the tail is a
  MANUALLY ADDED FADEOUT-TO-BLACK (the edit's closing transition), and
  the machine's 587.03 start is explicitly confirmed good.
- Bench (probe_fadeout): the 411f tail (175.97-176.40) measures pure
  black (luminance 0.0 on every sample — the fade completes inside the
  matched g76); the 5e85#45 control tail measures bright real content
  (80->137). The discriminator is structural, not a threshold.
- Instrument (fadeout-tail continuation, replaces the blanket tail
  skip in _recover_no_match): a TRAILING no-match piece <=2.5s whose
  luminance is monotone non-increasing with the final samples <12/255,
  following a matched chain, joins that chain's line at unit rate
  (doubt tag fadeout_tail). Bright/non-fading tails keep the honest
  no-match (5e85#45 unchanged). Synthetic test added
  (test_fadeout_tail_joins_previous_line_and_bright_tail_stays).
- v164 411f fresh: scene 52/52 exact, source 52/52 exact, ZERO stale,
  ZERO fails — scene#51 folds (g76+g77 chained), source#51
  (587.03,589.96) lookalike-equivalent to GT (588,591) with the
  owner-confirmed start. Ledger final: 115 entries = 109 pass + 6 skip,
  ZERO fail — the owner-fail set is EMPTY. Leave-one-out + oracle
  running (v164).
