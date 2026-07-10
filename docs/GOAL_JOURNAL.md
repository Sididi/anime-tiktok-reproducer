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
