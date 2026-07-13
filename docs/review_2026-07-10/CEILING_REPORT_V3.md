# Ceiling Report v3 — GOAL v4 finalization (2026-07-11, journal v101–v115)

State evaluated: v114 fresh outputs (`~/.cache/atr-eval/v114_fresh_*.json`), round-5
waiver ledger, GT untouched (git diff + untracked clean). Review pages for this
round: `review6_<pid>.html` (video-embedded, regenerate from v114 if verdicts are
given later than 2026-07-11).

## 1. Standings: v101 (session open) → v114 (final)

| Project | Scene v101 → v114 | Source v101 → v114 | Elapsed v114 | Stale |
|---|---|---|---|---|
| dcd74148c7ec | 19/0/1 → 19/0/1 | 19/0/0/1 → 19/0/0/1 (¹) | 113.2s | 0 |
| 85de83ca6323 | 49/2/3 → 50/2/2 | 43/0/6/5 → 49/1/1/3 | 327.6s | 0 |
| 411f73d26c1d | 50/0/2 → 50/0/2 | 49/0/0/3 → 49/0/0/3 | 347.4s | 0 |
| 5e85164d9ff8 | 44/2/0 → 45/0/1 | 40/0/6/0 → 41/0/4/1 | 261.0s | 0 |

(¹) dcd #19 moved from pass-by-waiver to strict EXACT (777.0–781.1 vs GT 777.0–781.33).

Aggregate: scene exact 162→164, source exact 151→158, wrong-primary 12→5,
source fails 9→8, zero stale waivers throughout. Oracle guard (--gt-scenes)
holds and improves: scene 19/20, **52**/54, 51/52, 46/46 (baseline 19/51/51/46).

## 2. Hard-set burndown (GOAL §2, 18 scenes)

**Machine-fixed this session (9):**
85de #11 (piece-outlier arbitration, winner 198.1 vs GT 198.59), #17, #22, #24,
#40, #53 (registered-footprint rerank + recall/proposals), #0 H2 (scene folds,
source loose), #20 source axis; 5e85 #26 H2 (hard-cut boundary-prior floor);
411f #28 now renders equivalently (source lookalike-equivalent PASS).
Plus dcd #19 (outside the §2 list) to strict exact.

**Still failing (9), by class, with bench margins and the attempts made:**

| Scene | Class | Bench margins | Honest attempts (journal) | Missing instrument |
|---|---|---|---|---|
| dcd #6 (+#7 fold) | static missed cut | reg-SSCD **+0.007** (dead); motion +0.03..+0.18 unstable | certified tug (v107, REVERTED: staled #11), residual-step split (v108, NO-GO: steps 0.5–1.0 in owner-passed scenes too), diff probe (cut pixel-invisible, 0.33 vs noise) | detector-level: the 16.20 flash-burst peak is merged at detection; the M5 base-threshold/AUTO_DENSE experiment (AUTO_DENSE inactive on dcd, ≤70 scenes) |
| 85de #3 | montage-hidden instance | reg-SSCD **+0.196** IF candidate present | deep recall k=40 floor 0.45 ×8 queries, index self-sim (montage↔truth cos 0.71 < 0.80), chronology proposals (truth not neighbour-adjacent) — truth never enters the candidate set in production | exhaustive/montage-aware index scan (cross-scene recap linker) |
| 85de #10 | fold/no-coverage | n/a (segmentation) | piece-outlier pass fixed sibling #11; #10's fold stays empty (missed boundary 12.15→ detector) | detector boundary near 12.15/12.65 |
| 85de #19 | no-match piece spans #19+#20 | recovery cert 0.677 = #20's content | R6 recovery certifies the #20-half and wins; the piece needs a SPLIT at 21.22 first | detector/DP boundary inside a no-evidence hole |
| 85de #20 | fold-no-chain (scene axis) | source axis passes | same piece as #19 | same |
| 411f #51 | identical-instance chronology | reg-SSCD **+0.008**, motion −0.25 (both dead; absolutes 0.61 both) | recall+proposals scored; certificate path needs an assignment proposal that never comes (no correspondences on the true instance) | chronology at assignment level for correspondence-less candidates |
| 5e85 #11 | lookalike-loop recovery | truth reg 0.627 vs loop rival 0.387–0.56: win-margin < 0.07 at times | R6 recovery with win-margin discipline honestly ABSTAINS (a wrong recovery stales waivers) | motion/temporal comparator for loop instances (D1 measured NO-GO at this granularity) |
| 5e85 #25 | looping pan | reg-SSCD **+0.018** (dead); pan localizer places the query at the MACHINE's instance (479.39, best response), not GT's 481.0 | hard-cut floor fixed the 32.5 boundary (sibling #26 exact); the 4 pieces sit on the 1.6s-early loop instance, inside the 3s dedupe radius | **owner arbitration requested**: GT-vs-instrument tension flagged per §0 |
| 5e85 #45 | recovery below certification | truth@790 reaches the candidate set via corr-clusters but scores 0.289 grid (bar 0.32, junk ≤0.16) | R6 with corr-cluster candidates + grid fallback; registration defeated by fast content (bench: degenerate rects on every frame pair) | sub-pixel/flow-level registration for blurred content |

## 3. Tolerable set (GOAL §2, 6 scenes)

- 85de #13, #49: **FIXED** (now lookalike-equivalent-pass / exact).
- 411f #7: pass (owner waiver). 411f #8: scene fail — owner pre-approved waivable
  (0.59x slow-mo evidence hole; recovery abstains: nothing certifies ≥ bar).
- 5e85 #32, #34: source WP — quasi-static trims, terminally measured (round 1-2
  probes + this session's registered-geometry re-check: margins +0.004..+0.091).
  Waiver requested.

## 4. Perf ceiling (M4 cap ≤300s)

dcd 113s ✓, 5e85 261s ✓, 85de 305–328s ✗, 411f 332–347s ✗ (quiet machine,
run-to-run spread shown). Six measured reduction attempts each traded
owner-passed scenes or ran slower:
fps 12→10 (−8 source exact, 9 stale), candidate sweep 1.2→1.0/0.8 (−1..−3
exact + stale), shared-rect-only scoring (−2 exact), chunked decode/embed
pipelining (slower: chunk seeks; results shifted), candidate filter −0.05
(−1 on 411f), candidate cap 5→4 (−1 on 411f). Serial cost split measured:
decode 119.5s / embed 79.7s on 85de. The remaining instruments are
structural (M5): batch/NVDEC decode, pixel-retaining multi-geometry embeds,
per-episode sequential window planning. Priority per §0 was owner scenes
over the cap ("a change that stales an owner-passed scene without fixing a
hard fail is a regression, whatever the aggregate says").

## 5. Owner asks for review round 6

1. Verdicts on `review6_*.html` entries (all loose/WP/fail scenes shown with clips).
2. 5e85 #25: arbitration of the GT-vs-pan-localizer tension (§2 above).
3. Waivers (post-honest-attempt): 411f #8, 5e85 #32, #34; plus the still-failing
   hard scenes in §2 if their classes are accepted as instrument-ceiling.

---

## ADDENDUM — Owner review ROUND 6 integrated (2026-07-11, journal v124-v129)

Verdicts on `review6_*.html` (exhaustive): all PASS except **dcd #6, 85de #10,
85de #20, 411f #51, 5e85 #11** (machine failures stand — the acceptance-record
fail set). **SKIP** (permanent owner-approved ignore, new evaluator verdict, no
stale guard): 411f #7/#8 (GT region buggy — evidence-hole slow-mo burst) and
5e85 #45 (a non-anime scene appended at the edit end contaminates matching;
truth timings verified present among primary/secondary candidates).

**Start-side containment shipped** (owner-endorsed spec): per render-segment,
the locked interval must not cross a native source cut the TikTok start frame
sits after; the start pulls onto the first clean post-cut frame. Fixed the
round-6 precision asks: 85de #13 (491.72→491.78 — the single pre-cut frame
removed; the GT-position "cut" at 492.75 measures emb-diff 0.003, i.e. static,
renders identically) and #12 (256.21→256.55). Leave-one-out: dcd/411f
unchanged, 5e85 improved (WP 4→3), zero stale, oracle guard holds.

**Final round-6 standings** (v128 outputs + round-6 ledger, zero stale):

| Project | Scene E/L/F | Source E/L/WP/F | Remaining failure |
|---|---|---|---|
| dcd74148c7ec | 19/0/1 | 19/0/0/1 | #6 |
| 85de83ca6323 | 52/0/2 | 52/0/0/2 | #10, #20 |
| 411f73d26c1d | 51/0/1 | 51/0/0/1 | #51 |
| 5e85164d9ff8 | 46/0/0 | 45/0/1/0 | #11 (WP) |

Non-waived strict budgets: loose 0 on every axis (≤3), WP 1 (≤2); the only
source fails are the five owner-confirmed scenes. Each of the five carries its
bench margins, honest integration attempts, and named missing instrument in §2
above (dcd#6: static-content cut detector / M5 detector experiment; 85de#10 &
#20: detector boundary inside a no-evidence hole at tt 12.15 / 21.22;
411f#51: chronology for correspondence-less identical instances; 5e85#11:
motion-level comparator for loop instances at sub-3s spacing).

---

## ADDENDUM 2 — detector-level experiment completed (2026-07-12, v134/v135)

The §2 "missing instrument" for dcd#6 / 85de#10 / 85de#20 (detector-level
boundary emission) has now been RUN and measured: an unconditional
threshold-8 sensitive pass reinjected into the base-16 skeleton (the exact
AUTO_DENSE mechanism, ungated). It converts dcd#6 to loose/WP — and
collapses 5e85 (46/0/0 → 41/2/3 scene, five stale owner waivers, two new
source fails) while leaving 85de #10/#20 unmoved. REVERTED per §0. The
exception classes are therefore proven unreachable at every identified
layer with the current instruments; the missing instrument is refined to:
**a motion-conditioned cut detector** (emit static-content boundaries
without over-cutting action shots). Final standings remain the round-6
table above, re-verified post-revert (zero stale; quiet-machine elapsed:
dcd 97.0s, 5e85 213.6s, 411f 250.5s, 85de 271.8s — all ≤300s).

---

## ADDENDUM 3 — the missing instrument BUILT: motion-conditioned cut detector (2026-07-12, v136/v137)

Addendum 2's refined missing instrument was implemented and validated:
`_reinject_static_sensitive_cuts` keeps a threshold-8 boundary only when
BOTH sides are near-static (measured: true static cut 0.09–0.27 median
64px frame-diff vs ≥14 max-side on every v134-damaging action boundary —
a physical gate, ceiling 1.0). **dcd #6 is machine-fixed**: scene FAIL →
LOOSE (15.33–16.57 vs GT 15.33–16.03, start exact) and source FAIL →
LOOSE (642.72–643.96 vs 642.60–643.30). Leave-one-out: 85de and 5e85
byte-stable (zero stale), 411f #12 exact→loose; oracle guard holds
(19/20, 52/54, 51/52, 46/46).

**Final standings (v137 fresh, round-6 ledger, review7 pages generated):**

| Project | Scene E/L/F | Source E/L/WP/F | Elapsed | Remaining owner-fail |
|---|---|---|---|---|
| dcd74148c7ec | 18/2/0 | 16/3/1/0 | 100.7s | — (none) |
| 85de83ca6323 | 52/0/2 | 52/0/0/2 | 293.7s | #10, #20 |
| 411f73d26c1d | 51/0/1 | 50/1/0/1 | 295.6s | #51 |
| 5e85164d9ff8 | 46/0/0 | 45/0/1/0 | 224.6s | #11 |

Budgets: loose ≤3 per axis everywhere (dcd source 3/3), WP ≤2, source
fails only the owner-confirmed scenes. Pending owner round 7: re-review
of the three §0-permitted stales (dcd #7 — 643.96/645.12, dcd #18 —
end +1.22, 411f #12 — 43.17/46.14, all loose) plus the standing four.
The four remaining hard fails are terminally instrumented: 85de #10/#20
have NO pixel-level cut even at threshold 8 (evidence-hole class);
411f #51 and 5e85 #11 are dead on both bench signals (margins ≤0.01).
