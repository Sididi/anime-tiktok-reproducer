Finalize the scene-matching algorithm (`backend/app/services/scene_aligner.py`) against the owner's confirmed failure list, then re-harden performance and finish the production cleanup. This document (v4) was produced on 2026-07-11 after the full v57→v100 execution of GOAL v3 (journal: `docs/GOAL_JOURNAL.md`) and FIVE owner review rounds whose verdicts live in `backend/data/eval_waivers.json` (113 entries, zero stale). The Stage-5 native arbitration layer exists and works; the production route runs the aligner; the remaining work is a precisely enumerated, owner-labeled set of 18 hard-fail scenes plus a tolerable set, with the instruments' limits already measured. Do not re-derive what the journal already settles — its negative results are load-bearing.

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

Standings (v99 outputs + round-5 verdicts + the 411f GT fix; scene E/L/F then source E/L/WP/F; waivers):

| Project | Scene | Source | Waivers | Elapsed |
|---|---|---|---|---|
| dcd74148c7ec | 19/0/1 | 19/0/0/1 | 7 | 86.8s |
| 85de83ca6323 | 49/2/3 | 43/0/6/5 | 14 | 168.4s |
| 411f73d26c1d | 50/0/2 | 49/0/0/3 | 13 | 242.0s (drift regime; ~195-200s normalized) |
| 5e85164d9ff8 | 44/2/0 | 40/0/6/0 | 12 | 160.4s |

Oracle guard (`--gt-scenes` must return given boundaries at/above baseline): holds (19/20, 51/54, 51/52, 46/46).

Measured instrument limits — treat as settled, do not re-attempt blind:
- Index-side signals (SSCD 2fps cosine) cannot separate duplicate instances (gaps 0.02-0.05) nor rates on lookalike montage. (v33, v53)
- Pixel NCC — gray, gradient, masked, any resolution/geometry — has NO separating threshold for duplicates on zoomed edits; max-over-trials bias makes greedy pixel switches catastrophic (85de 30→23 exact). (v61-v63, probes `probe_rerank_margins.py`, `probe_scorer_variants.py`)
- Zoom-aligned SSCD (per-project zoom from confident chains; 85de z=1.45) is the working duplicate scorer: 12/14 positive GT margins; its two failures are pixel-identical repeats — those need the native identity certificate (cross-window cos ≥0.95 on ≥3 mid frames) + chronology/assignment. (v64-v66, `probe_sscd_zoom.py`)
- Quasi-static mid-shot trims are undecidable by SSCD at any zoom (edge margins ±0.001) AND by pixel NCC even at perfect feature-level registration (prominence 0.001-0.002 at NCC 0.996). Only a motion-level comparator could revisit them. (rounds 1-2 probes, `probe_static_trim_localization.py`, `probe_registered_localization.py`)
- The registered pan localizer (ORB+RANSAC partial-affine + phase-correlation shift-vs-time zero crossing) localizes edges in PANNING shots where everything else fails (+0.026s on the 5e85 swoosh end); it is the seed of the motion instrument H-classes below need. (v99)
- Local-neighbour chronology propagates wrong duplicate instances; assignment must stay global and switches certified. (v62-v63)
- Tug parameter widening does not reach the structural residuals; they are segmentation-level. (v100)
- Never validate any change on one project — leave-one-out on all four, fresh AND oracle. (v70/v78-v84 lesson, twice)

# 2. The target: owner-labeled failure set (round 5, exhaustive — unlisted scenes are owner-valid)

HARD (must be machine-fixed; 18 scenes):
- H1 wrong instance / duplicates (14): 85de #3 #10 #11 #17 #19 #20 #22 #24 #40 #53 (10 — #20 "start way too early"); 5e85 #11 #45; 411f #28 ("first frame too soon") #51. Each has the owner-validated truth in GT and the wrong machine pick recorded — a fully labeled bench.
- H2 structural segmentation (4): dcd #6 (+#7 fold — the 16.03 missed cut inside static content; the tug fix exists but is blocked by the duplicate-suspect gate, journal v91); 85de #0 (fold-no-chain); 5e85 #25 #26 (generated interval 2.0s shorter than GT — extent/over-merge; owner hint: #25 is a zoomed, very fast, linear right-to-left swoosh — a motion signature).

TOLERABLE (fix if the instrument reaches them, else owner-waived after honest attempt; 6 scenes):
- Quasi-static trims: 85de #13 #49 ("first frame too soon"), 5e85 #32 ("too late") #34 ("a bit too early").
- 411f #7 #8: extreme evidence hole (multi-shot action burst played at 0.59x slow-mo; retrieval sees nothing). GT is now correct there; waivable per owner (2026-07-11).

# 3. Prescribed direction

One new instrument, one repair, in this order of leverage:

D1 — Motion/temporal-signature matching (serves H1, the 5e85 #25/#26 pair, and possibly the tolerable statics). Frames lie; trajectories don't: duplicate still-shots and lookalike instances differ in WHEN and HOW things move (mouth flaps, pans, the swoosh direction/speed). Generalize the pan localizer into a scorer: per-frame registered shift (phase correlation on the source plane) and/or coarse flow direction/magnitude time-series over the scene, correlated between the TikTok clip and each candidate source window at native fps. The zoom estimate and `_WindowEmbedCache` already exist; the bench (§4) decides go/no-go before any wiring.

D2 — Segmentation-level repair for H2 (deferred with evidence in v100, now due): the DP must be able to cut at a loud native cut even when index evidence is ambiguous — revisit boundary priors/tug gating around owner-confirmed misses (dcd 16.03: the fix exists, find a gate that admits it without re-breaking the v88 oracle guard; 85de #0: fold-no-chain from the arbitration era; 5e85 #25/#26: the extent error is upstream of arbitration). Full regression guard: fresh + oracle on all four, stale-waiver check zero.

Do NOT build: new index-side scorers, new pixel-NCC variants, audio matching (edits replace audio), or any per-scene special case.

# 4. Methodology: labeled bench first (binding)

The v61 catastrophe (a scorer wired on probe vibes, −7 exact on 85de) must not repeat. Before wiring ANY new scorer or gate:
1. Build the offline bench from the labels: for each of the 24 target scenes (18 hard + 6 tolerable), extract the TikTok clip and BOTH source windows (GT truth; current wrong pick where applicable) at native fps; add a control set of ≥20 owner-passed scenes across all four projects.
2. Run the candidate instrument offline on the whole bench; report per-scene margins (truth vs wrong) and control-set false-switch rate. Wire it only if the hard-class margins separate with zero control-set flips at the chosen threshold; journal the distribution.
3. Integrate behind the existing decision framework (greedy margin / assignment-proposed / certificate tiers), then leave-one-out fresh+oracle on all four.
Bench artifacts live in `~/.cache/atr-eval/bench/` (survives reboots); probe scripts join `backend/scripts/diagnostics/`.

# 5. Milestones — gated, in order

- M0 Reproduce the §1 standings table (fresh, waivers applied) + oracle guard on current code. Journal as v101.
- M1 Bench + instrument validation: the §4 bench built; D1 measured on it; go/no-go per failure class with numbers. A no-go on a class ⇒ that class's scenes are candidates for the §6 waiver path, stated explicitly.
- M2 H1 burndown: hard duplicate fails → 0 across all four projects, zero stale waivers, oracle guard holds.
- M3 H2 burndown: the four structural scenes fixed (or reclassified with owner sign-off), same guards.
- M4 Tolerable set: fixed where D1 reaches them; the remainder goes to a review round (video pages) for owner waivers. Full fresh runs ≤300s/project.
- M5 Re-hardening: elapsed back ≤200s/project on a quiet machine; production cleanup leftovers — `anime_matcher` legacy correction passes + crop-index deletion (with regression coverage for the manual merge/rematch APIs they still serve), `scene_detector` AUTO_DENSE_* gates removal (measured leave-one-out experiment), aligner constants consolidated ≤15 with one-line justifications; `pixi run -e dev pytest backend/tests/` green modulo the pre-existing env failures documented in the journal.

# 6. Definition of done (replaces the ≤3-waiver rule — owner decision 2026-07-11)

The owner's verdict ledger is the acceptance record; GT corrections were declined, so owner-passed sub-second disagreements legitimately live as waivers with no cap. Done means, per project, from fresh detection:
- Zero scenes in the owner-fail set: every H1/H2 scene scores exact/loose against GT, and every tolerable scene either scores or carries an owner pass-waiver from a review round.
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
