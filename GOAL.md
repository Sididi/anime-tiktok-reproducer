> **CLOSED 2026-07-16 (v170).** This goal is complete and the matching workstream is
> FROZEN: shipped cv2 state validated (fresh decisions byte-identical to `v5ref` on all
> four GT projects; pytest at its 11-fail baseline), W5 concurrency wiring proven E2E
> through the real `/matches` route, the throughput datum recorded, the evaluator verdict
> label fixed, and the official production cap set at **~320s quiet per heavy project**.
> See journal entry v170. Any future speed work belongs ONLY to the owner-gated experiment
> in `GOAL_FAST.md` — do not reopen the v166–v169 speed territory on the validated path.

Close the matching workstream (v57→v169): final validation of the shipped state, end-to-end verification of the W5 concurrency wiring, one throughput measurement, logical commits, and the official production cap — NO algorithm or performance changes of any kind. This document (v6-closure, 2026-07-16) follows four convergent terminal speed verdicts (journal v166/v167/v168/v169): the safe-optimization surface is proven empty, the shipped decode path is cv2, and the speed case is CLOSED at ~300-320s quiet per heavy project. The separate, permissive fast-mode experiment lives in `GOAL_FAST.md` and is NOT part of this goal.

# 0. Scope guard

Allowed: validation runs, the E2E queue check, one concurrency throughput measurement, the evaluator label fix (C2), documentation/journal updates, and git commits. Forbidden: any change to `scene_aligner.py` / `anime_matcher.py` / `scene_detector.py` decode or decision logic, any new perf experiment (the case is closed — do not reopen v166-v169 territory), any GT/ledger/submodule modification.

# 1. Milestones

- C0 Final validation of the current working tree exactly as it stands: fresh + oracle (`--gt-scenes`) on all four GT projects — expected: the v169 reverted-state results (dcd 20/20+20/20; decisions identical to `v5ref`; oracle 19/20, 52/54, 51/52, 46/46; zero stale; ledger 110 pass + 6 skip + 0 fail); `pixi run -e dev pytest backend/tests/` at its documented baseline (11 env/order failures, zero new); GT folders + submodule byte-identical (`git status` empty on both).
- C1 W5 end-to-end + the throughput datum:
  - E2E through the REAL route (the unit test `test_gpu_semaphore_caps_concurrent_heavy_tasks` already proves the semaphore; this proves the wiring): start the backend, launch `/matches` on two projects simultaneously plus a third — the third must emit the "Waiting for a GPU slot" SSE frame and start only after a slot frees. One run, logged output in the journal.
  - Throughput measurement (informational, one run, quiet machine): two heavy projects matched CONCURRENTLY (two evaluator processes are an acceptable proxy for machine contention); record per-project elapsed and the effective seconds-per-project versus sequential. Rationale: the 200s target was a throughput proxy; 2×~300-450s concurrent may already beat 200s/project effective. Report the number, draw no new work from it.
- C2 Evaluator verdict label: `CEILING-REPORT (waivers > 3)` still prints the RETIRED ≤3-waiver rule (superseded 2026-07-11 by the ledger-based acceptance). Reword the verdict line to reflect the ledger semantics (e.g. PASS-WITH-LEDGER / fails listed). Label/reporting only — tolerances, buckets, folding, equivalence rules untouched; re-run one project to show the new output.
- C3 Commits — the working tree carries finished, tested work; commit it in logical units with descriptive messages (not "refactor code structure"): (1) W5 queue wiring + tests (`matching.py`, `indexation_queue.py`, `test_indexation_queue.py`); (2) pixi deps (`pynvvideocodec`, `nvidia-npp-cu12` + lock); (3) diagnostics scripts (`pynv_decode.py`, `probe_pynv_calibration.py`, any untracked probes); (4) docs: journal, GOAL files, `review9_*.html`, assets if related. Nothing force-pushed; branch = main.
- C4 Freeze the record: append the closing journal entry (v170) — official production cap **~320s quiet per project** (dcd ~110s), the four-verdict trail, the C1 throughput datum — and add a short "CLOSED" note at the top of this GOAL.md pointing future work to `GOAL_FAST.md` (owner-gated experiment) only.

# 2. Final report

C0 outputs verbatim (fresh + oracle + pytest + git status), the C1 SSE/throughput evidence, the C2 before/after verdict line, the commit list (`git log --oneline`), and the closing journal entry.
