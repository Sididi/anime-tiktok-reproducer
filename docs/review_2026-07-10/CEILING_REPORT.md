# Per-project ceiling report — post owner-review round 5 (v99 outputs, 2026-07-10)

Strict evaluator, tolerances unchanged. Owner verdicts (rounds 1-5, 113 entries)
applied from `backend/data/eval_waivers.json`; a pass-waiver is voided automatically
if the generated interval later moves >0.35s (stale guard). Zero stale waivers.

Round 5 (2026-07-10 evening): verdicts identical to round 2 except 411f #8
pass→FAIL — its GT cut was defective (the first frame of #8's shot sat at the end
of #7's range); owner authorized an automatic GT fix, applied frame-precise:
#7/#8 boundary 14.80 → 14.767 (measured true cut), source ends adjusted at each
scene's rate (backup: `~/.cache/atr-eval/gt_backup_411f_2026-07-10/`). The stale
411f #25 fail entry was dropped (machine-fixed in v94; owner round-5 exhaustive
list = valid). Review pages carry embedded video clips since round 5.

## Fresh detection (v99 outputs + round-5 verdicts + 411f GT fix)

| Project | Scene E/L/F | Source E/L/WP/F | Waivers | Elapsed | Verdict |
|---|---|---|---|---|---|
| dcd74148c7ec | 19/0/1 | 19/0/0/1 | 7 | 86.8s | CEILING-REPORT |
| 85de83ca6323 | 49/2/3 | 43/0/6/5 | 14 | 168.4s | CEILING-REPORT |
| 411f73d26c1d | 50/0/2 | 49/0/0/3 | 13 | 242.0s* | CEILING-REPORT |
| 5e85164d9ff8 | 44/2/0 | 40/0/6/0 | 12 | 160.4s | CEILING-REPORT |

Current review pages: `docs/review_2026-07-10/review5_*.html` (4/23/19/10 entries).

*Includes measured +23% machine drift on unchanged phases (journal v96); normalized
≈195-200s. All four are CEILING-REPORTs by the §8 rule (owner passed >3 scenes per
project). Baseline (v57) → now: source exact 91 → 149-with-verdicts (104
machine-only), 411f wrong-primaries 8 → 2, dcd down to a single failure.

## Oracle (`--gt-scenes`, v98)

Scene axis 19/20, 50/54, 51/52, 46/46 — at or above the given-boundary baseline
everywhere (guard holds).

## Owner-confirmed residual failures (round 2)

1. **Duplicates / wrong instances**: 85de #3 #10 #11 #17 #19 #20 #22 #24 #40 #53,
   5e85 #11 #25 #26 #45, 411f #28 #51, dcd #6(+#7 fold). Index-blind; zoom-SSCD
   arbitration fixed the separable ones, the rest are near-identical repeats.
   Owner hint: 5e85 #25/#26 are a zoomed fast right-to-left swoosh (motion
   signature — same future instrument as below).
2. **Quasi-static mid-shot trims**: 85de #13 #49 #0(end), 5e85 #32 #34.
   Measured undecidable: SSCD margins ±0.001 at all zooms; high-res pixel NCC
   time-localization probe NEGATIVE (argmax 0.3-1.6s off, scores 0.13-0.28 —
   center-crop zoom search cannot reach pixel registration on zoomed+translated
   edits). Next instrument: feature-based geometric registration + motion-level
   differencing.
3. **GT-side (owner action)**: 411f #7/#8 GT cut anomaly noted in round 1 (411f #8
   itself now owner-passed), 411f #35 GT last frame late, 411f #4 exact source
   absent from GT (skippable, recorded).

## Review pages

Current: `docs/review_2026-07-10/review4_*.html` (owner round-2 verdicts given
against these). History: review_*, review2_*, review3_*.
