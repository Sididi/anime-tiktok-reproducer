Finalize the scene-matching algorithm (`backend/app/services/scene_aligner.py`) down to the LAST owner-confirmed failures, then execute M5 (200s target, legacy deletion, constants audit). This document (v4.2) was updated on 2026-07-13 after the v101→v136 execution of the v4 milestones (journal: `docs/GOAL_JOURNAL.md` — its negative results are load-bearing, never re-derive them) and SEVEN owner review rounds whose verdicts live in `backend/data/eval_waivers.json` (115 entries: 6 fails = the §2 target, 6 skips, the rest owner-passes; zero stale). Fourteen of the original 18 hard fails are machine-fixed, dcd#6 fell to the v136 motion-conditioned cut detector, all four projects run under 300s on a quiet machine. What remains: FOUR hard scenes plus TWO start-precision fixes (§2), each with fresh owner intelligence (§3), then M5.

# 0. Permissions and hard limits

You may rework anything in `scene_aligner.py`, `scene_detector.py`, `scene_merger.py`, `anime_matcher.py`, and the evaluator internals, including segmentation. Where the measured stack works, extend it; when three tunings in a row chase one project, rework the owning stage instead.

Hard limits, absolute:
- Ground truth is FINAL. The owner declined all further GT corrections (2026-07-11), including the sub-second proposals in `docs/review_2026-07-10/GT_CORRECTION_PROPOSALS.md` — that file is now historical. The 411f #7/#8 boundary repair (journal, round 5) was a one-off owner-authorized exception, already applied. Never write into `backend/data/projects/{dcd74148c7ec,85de83ca6323,411f73d26c1d,5e85164d9ff8}`; verify with `git diff` + untracked check before claiming completion. A suspected GT error is flagged for owner arbitration in the review page, never edited, never tuned toward.
- Never modify the `anime_searcher` submodule; never reindex. Own caches outside the submodule/GT folders are fine.
- Evaluator tolerances never loosen (±0.3s exact / ±1.0s loose). Existing equivalence rules stay; extensions only for pixel-proven cases, journaled, reversible.
- Owner-passed scenes are untouchable: the waiver stale guard (interval moved >0.35s ⇒ waiver void, re-review) is a hard regression gate. A change that stales an owner-passed scene without fixing a hard fail is a regression, whatever the aggregate says.
- Time: ≤300s per project during this phase (owner decision 2026-07-11); the final deliverable returns to ≤200s in M5. Measure elapsed on a quiet machine before any cap claim (journal v96: +23% machine-drift regime observed).

# 1. State: what exists and what it measures (2026-07-11)

Pipeline: Stage 1-2 dense 8fps sampling + diff curve + FAISS retrieval → Stage 3 per-fragment line hypotheses → Stage 4 segmentation DP (span fits, boundary priors, beam) → Stage 5 native arbitration (per-end anchoring on true edge frames, zoom-SSCD duplicate arbitration + native identity certificate + global assignment, native boundary tug, interior pull-back snap, registered pan localizer) → SceneMatch build with `doubt_reasons`. `/matches` runs the aligner (M5 of v3 shipped); ~1200 lines of legacy deleted.

Standings after v136 + the round-6 ledger (scene E/L/F then source E/L/WP/F; quiet-machine elapsed; round-7 re-score due at M0 — dcd's line carries the v136 collaterals now resolved by round-7 verdicts):

| Project | Scene | Source | Elapsed |
|---|---|---|---|
| dcd74148c7ec | #6 → loose (was the last scene fail) | 3 looses + #18 WP (round-7: fix #18 start) | 98.5s |
| 85de83ca6323 | 52/0/2 | 52/0/0/2 | 283.1s |
| 411f73d26c1d | 51/0/1 | 50/1/0/1 | 298.5s |
| 5e85164d9ff8 | 46/0/0 | 45/0/1/0 | 225.4s |

Oracle guard (`--gt-scenes` must return given boundaries at/above baseline): holds (19/20, 52/54, 51/52, 46/46).

Measured instrument limits — treat as settled, do not re-attempt blind:
- Index-side signals (SSCD 2fps cosine) cannot separate duplicate instances (gaps 0.02-0.05) nor rates on lookalike montage. (v33, v53)
- Pixel NCC — gray, gradient, masked, any resolution/geometry — has NO separating threshold for duplicates on zoomed edits; max-over-trials bias makes greedy pixel switches catastrophic (85de 30→23 exact). (v61-v63, probes `probe_rerank_margins.py`, `probe_scorer_variants.py`)
- Zoom-aligned SSCD (per-project zoom from confident chains; 85de z=1.45) is the working duplicate scorer: 12/14 positive GT margins; its two failures are pixel-identical repeats — those need the native identity certificate (cross-window cos ≥0.95 on ≥3 mid frames) + chronology/assignment. (v64-v66, `probe_sscd_zoom.py`)
- Quasi-static mid-shot trims are undecidable by SSCD at any zoom (edge margins ±0.001) AND by pixel NCC even at perfect feature-level registration (prominence 0.001-0.002 at NCC 0.996). Only a motion-level comparator could revisit them. (rounds 1-2 probes, `probe_static_trim_localization.py`, `probe_registered_localization.py`)
- The registered pan localizer (ORB+RANSAC partial-affine + phase-correlation shift-vs-time zero crossing) localizes edges in PANNING shots where everything else fails (+0.026s on the 5e85 swoosh end); it is the seed of the motion instrument H-classes below need. (v99)
- Local-neighbour chronology propagates wrong duplicate instances; assignment must stay global and switches certified. (v62-v63)
- Tug parameter widening does not reach the structural residuals; they are segmentation-level. (v100)
- Never validate any change on one project — leave-one-out on all four, fresh AND oracle. (v70/v78-v84 lesson, twice)

# 2. The target: owner round-7 failure set (2026-07-13, exhaustive — unlisted scenes are owner-valid)

HARD (4 scenes — the survivors of every instrument built so far; per-scene diagnoses in the journal v105-v136):
- 85de #10 (scene no-coverage inside a chain) and #20 (fold-no-chain): evidence-hole cuts — NO pixel-level cut exists at the GT boundaries even at detector threshold 8 (v134), and both sides are lookalike content. The cut must be INSERTED from source-side reasoning, not found on the TikTok side (§3-D4).
- 411f #51 (fold-no-chain): duplicate instances with both static signals dead (bench margins ≤0.01).
- 5e85 #11 (wrong primary): loop content — truth registers at 0.627 but a loop-instance lookalike sits within the 0.07 switch margin; the honest-abstain is correct under current instruments (§3-D3).

START-PRECISION (2 scenes, owner round-7 — the chosen instance/content is approved, only the interval start is wrong):
- 411f #28: "first frame too soon". Owner intelligence: the machine's instance and GT's are exact duplicates that render identically; the GT one is distinguishable ONLY by having no progressive zoom-out — i.e. instance pairs can differ purely in scale-velocity (§3-D3). The chosen instance stays; fix the start.
- dcd #18: "first frame too soon" (v136 collateral). Fix the start without losing the v136 dcd#6 conversion.

SKIPPED (owner-approved permanent ignores, already in the ledger): 411f #7 #8 (buggy GT region, evidence-hole slow-mo burst), 5e85 #45 (a non-anime scene is appended at the edit's very end — also a production reality: guard trailing no-evidence content as honest no-match, never force a match there).

# 3. Prescribed direction (v4.2 — each item keyed to §2 scenes, bench-gated per §4)

D3 — Registered SCALE-VELOCITY and LOOP-PHASE signatures (411f #51, 5e85 #11; the owner's #28 remark is the design hint). The registration stack already estimates per-frame geometry (ORB+RANSAC partial-affine returns SCALE, the phase-correlation localizer returns SHIFT) but no instrument compares their TIME-DERIVATIVES: a progressive zoom-out is a monotone scale(t) slope; loop instances differ in motion PHASE (shift(t) trajectory alignment — the pan localizer's zero-crossing generalized to a trajectory correlation). Measure both signatures on the #51 and #11 candidate pairs offline FIRST (§4); wire only what separates. These reuse cached windows — marginal decode cost ~0.

D4 — Source-side cut INSERTION for evidence-hole boundaries (85de #10 #20). No TikTok-side signal exists (v134 measured), but the machine already knows both neighbouring lines: score the TikTok samples over t under line A and line B on the cached registered windows and insert the cut at the CROSSOVER (where B starts out-explaining A), snapping to the sample grid. This extends the native tug to boundary-less regions and plugs into the per-piece outlier arbitration (v113) as its entry point: a chain whose interior piece scores as an outlier under its own line but well under a neighbour-instance line is the #10/#20 pattern. Guard: insertion only when the crossover margin is clean on ≥3 consecutive samples (over-cutting folds back, but a phantom insertion inside an owner-passed chain is a stale — bench it on the two labeled cases + controls first.

D5 — Start-precision (411f #28, dcd #18): the start-side containment pass (v124-v128) exists and fixed 85de #12/#13; diagnose why it does not fire on these two starts (reach 0.85s? no native cut at the true start? edge-frame match threshold?) and extend the mechanism generally (never a per-scene branch). dcd #18's fix must keep dcd #6's v136 conversion intact (they share the region — regression-check the pair together).

Do NOT build: new index-side scorers, new pixel-NCC variants, audio matching, threshold-8 global reinjection without the motion gate (v134), or any per-scene special case.

# 4. Methodology: labeled bench first (binding)

The v61 catastrophe (a scorer wired on probe vibes, −7 exact on 85de) must not repeat. Before wiring ANY new scorer or gate:
1. Build the offline bench from the labels: for each of the 24 target scenes (18 hard + 6 tolerable), extract the TikTok clip and BOTH source windows (GT truth; current wrong pick where applicable) at native fps; add a control set of ≥20 owner-passed scenes across all four projects.
2. Run the candidate instrument offline on the whole bench; report per-scene margins (truth vs wrong) and control-set false-switch rate. Wire it only if the hard-class margins separate with zero control-set flips at the chosen threshold; journal the distribution.
3. Integrate behind the existing decision framework (greedy margin / assignment-proposed / certificate tiers), then leave-one-out fresh+oracle on all four.
Bench artifacts live in `~/.cache/atr-eval/bench/` (survives reboots); probe scripts join `backend/scripts/diagnostics/`.

# 5. Milestones — gated, in order

- M0 Verify the round-7 ledger (115 entries, 6 fails, 6 skips — already upserted 2026-07-13) by re-scoring the latest saved outputs, then reproduce fresh standings + oracle guard on current (v136) code. Journal it.
- M1 Bench the §3 signatures offline on the labeled pairs: D3 scale-velocity + loop-phase on 411f#51 / 5e85#11, D4 crossover insertion on 85de#10/#20, plus ≥20 owner-passed controls. Go/no-go per scene with margins; a no-go is stated with numbers and closes that scene as a final owner call.
- M2 Start-precision pair (D5): 411f #28 and dcd #18 fixed, zero stale (dcd #6/#7/#18 checked together), oracle guard holds.
- M3 Hard burndown (D3/D4 where the bench said GO): target scenes → exact/loose, zero stale, oracle guard, ≤300s.
- M4 Final review round (video pages, review8) on fresh outputs; integrate verdicts; the goal's matching phase closes when the owner's exhaustive list returns zero non-skip fails OR every remaining fail carries a bench-measured no-go and the owner's explicit final call.
- M5 Re-hardening (starts immediately after M4, same session): elapsed ≤200s/project on a quiet machine (levers: NVDEC/batch decode, window planning — the serial split measured decode 119.5s / embed 79.7s on 85de); `anime_matcher` legacy correction passes + crop-index deletion behind the existing rematch contract test (`test_anime_matcher_partial_rematch.py`); `scene_detector` AUTO_DENSE_* removal experiment — CAREFUL: the v136 static-cut reinject rides the AUTO_DENSE refine machinery, preserve it; aligner constants consolidated ≤15 with one-line justifications; `pixi run -e dev pytest backend/tests/` green modulo the pre-existing env failures documented in the journal.

# 6. Definition of done (replaces the ≤3-waiver rule — owner decision 2026-07-11)

The owner's verdict ledger is the acceptance record; GT corrections were declined, so owner-passed sub-second disagreements legitimately live as waivers with no cap. Done means, per project, from fresh detection:
- Zero scenes in the owner-fail set: every §2 hard/start-precision scene scores exact/loose against GT, or carries a bench-measured no-go plus the owner's explicit final call from the M4 review round; skips stay skipped.
- No NEW failures: every previously owner-passed scene still passes or its waiver is intact (zero stale waivers).
- Non-waived scenes respect the strict budgets (loose ≤3 per axis, WP ≤2, source fails 0).
- Oracle guard holds; elapsed ≤300s (M4) then ≤200s (M5); GT folders byte-identical; SceneMatch/MatchList contract and manual APIs unchanged.
Final acceptance is an owner review round on fresh outputs (video pages): the goal is complete when the owner's exhaustive list comes back empty for the hard classes and all remaining entries are explicit waivers.

# 7. Guardrails (binding, extended)

All GOAL v3 guardrails stand (no fixture-keyed constants or per-scene branches; leave-one-out on all four for every threshold change; synthetic tests for every new instrument; journal every iteration, re-read the last three before a new change). Added from measured experience:
- Offline bench before wiring (§4) — no scorer goes live on probe anecdotes.
- Fresh AND oracle on every all-four measurement; the stale-waiver count is part of every metric line.
- Perf trims are threshold changes: leave-one-out applies (v70).
- Batch owner reviews at milestone boundaries; never block mid-iteration. Review pages embed video clips (the `--review` mode as of round 5); continue numbering (`review6_*.html`, …) in `docs/review_2026-07-10/` or a dated sibling folder.

# 8. Work loop

- `pixi run python backend/scripts/evaluate_matching_against_ground_truth.py <pids…> --matcher aligner [--gt-scenes] [--save-generated-json …] [--review …]` — per-project JSON suffixes when multiple pids; waivers auto-applied from `backend/data/eval_waivers.json`.
- `pixi run -e dev pytest backend/tests/`
- Diagnostics probes: `backend/scripts/diagnostics/` (attribution, zoom-SSCD, rerank margins, static-trim, registered localization, eval-log diff).
- Artifacts in `~/.cache/atr-eval/`; journal `docs/GOAL_JOURNAL.md` (continue version numbering).
- GT untouched check: `git diff` + `git status --short` on the four project folders.

# 9. Final report

- §1 table vs final table (fresh + oracle + stale count + elapsed on a quiet machine), the hard-fail burndown per class with per-scene outcomes, bench margin distributions for every instrument decision (adopted AND rejected), review round outcomes, deleted-code and constants inventories (M5), explicit GT-untouched confirmation.
- If a hard class proves unreachable after the bench verdict and an honest integration attempt, stop and produce a ceiling report naming the missing instrument — do not soften the evaluator, do not spend the loose budgets to fake a pass.
