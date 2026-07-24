# FAST MODE JOURNAL

Owner-gated GPU-oriented matching variant. Branch `feat/fast-gpu-matching`,
runtime switch `ATR_FAST_MATCHING` (default ON in this branch; `0`/`off`/`false`
= exact mainline cv2/fp32 path). Precision is REPORTED, not gated (per
`GOAL_FAST.md`). This journal is separate from `docs/GOAL_JOURNAL.md` (the
validated v57→v169 record, which remains binding as FACTS).

## Frozen reference (mainline v5ref, journal v169 state)

`ref_hash.py` canonical (scenes+matches decision fields) hashes — the flag-OFF
byte-identity target:

| project | scenes/matches | v5ref hash |
|---|---|---|
| dcd74148c7ec | 41/41 | 892d36602d2b8d5944e376934dcaa0e3520408b5fcd7984592f64ca04a192087 |
| 5e85164d9ff8 | 55/55 | 0c29f1865a095f109652c6d57aa7aa9cf6ea8e7c766cf4553a1bb44c44e0e218 |
| 85de83ca6323 | 59/59 | b423cda02caadcdad02ec80701c8d427396bbcf1b3e56c6a0f69377d3fa36581 |
| 411f73d26c1d | 78/78 | 9df22c807ea6895d29165685a18cc7d80319a0752506da679dc94ec405f6297d |

Mainline reference timing/quality (v5ref logs, single run):

| project | elapsed | scene_detection | aligner | Scene timing | Source timing |
|---|---|---|---|---|---|
| dcd74148c7ec | 111.9s | 6.5s | 105.4s | exact=20/20 | exact=20/20 |
| (others captured in scoreboard below) | | | | | |

Environment: i9-14900HX, RTX 4070 Laptop 8GB (7.6GB free idle), 32GB RAM.
torch 2.8.0+cu128, PyNvVideoCodec 2.1.0, CUDA available.

---

## vF0 — orientation (2026-07-16)

Read GOAL_FAST.md + the v169 journal state + the unwired PyNv recipe
(`backend/scripts/diagnostics/pynv_decode.py`). Confirmed GPU stack live.
Mapped the decode/embed levers:

- **F1 GPU decode**: window-decode primitive is
  `AnimeMatcherService._collect_frames_in_window_from_capture`. It already
  dispatches on capture type. Source-episode captures that feed it exclusively
  open at 5 sites: aligner `EpisodeFrameCache.get_cap` (l.256, covers all 6 deep
  callers via the frame cache), `prefetch_probe` worker (l.321), `prefetch`
  worker (l.368); matcher `_collect_frames_in_window` (l.918) and
  `_refine_boundaries` shared cap (l.1037). Swapping these 5 opens to emit a
  `PyNvCap` routes every source window decode onto NVDEC.
- **F3 numeric**: embedder built once at matcher l.324 with `precision="fp32"`.
  `SSCDEmbedder` already supports `precision="fp16"` + GPU-resident
  `preprocess_decoded_batch`/`embed_preprocessed_batch`. TF32 is a backend-side
  global (`torch.backends.cuda.matmul.allow_tf32`) — no submodule edit needed.
- **F4 CPU bound**: aligner uses `ThreadPoolExecutor(8)` (l.202) and
  `_prefetch_pool = ThreadPoolExecutor(max_workers=4)` (l.243). Already bounded;
  will confirm no full-core pools appear under fast mode.

Reference generation harness: `evaluate_matching_against_ground_truth.py
<pid> --matcher aligner --save-generated-json <path>` → `ref_hash.py`.
4 GT projects: dcd74148c7ec 5e85164d9ff8 85de83ca6323 411f73d26c1d.

## vF1 — v5ref is STALE; re-froze reference on current main HEAD (2026-07-16)

Commit **863cb42** "feat(frames): enhance frame extraction using presentation
timestamps for variable frame rates" (2026-07-16 03:36) landed AFTER the v5ref
freeze (07-14 05:08) and after v6-closure (9819c60, 01:58). It rewrote 178 lines
of anime_matcher.py + 90 of scene_aligner.py + scene_merger. Current main HEAD
(the base of this branch) therefore no longer reproduces v5ref, e.g. dcd:
v5ref 41 scenes / `892d366…` → main HEAD 42 scenes / `c1aac14…`.

So "flag-OFF byte-identical to v5ref" is literally unreachable without reverting
863cb42 (forbidden — mainline untouched). Honest reading of §0 ("OFF falls back
to the EXACT mainline path"): flag-OFF must reproduce **current main HEAD**. I
re-froze the reference on a clean `main` checkout ("mainref", flag absent = pure
mainline) and validate flag-OFF + fast-ON deltas against mainref; v5ref hashes
kept above for provenance.

**mainref — current main HEAD (863cb42), single run each:**

| project | sc/mt | elapsed | scene_det | aligner | host CPU% | mainref hash |
|---|---|---|---|---|---|---|
| dcd74148c7ec | 42/42 | 113.5s | 6.0s | 107.5s | 428% | c1aac14c5a0f19ff332bc70c474b6f3c842ca28ada0be962f963f1034e5bd6c9 |
| 5e85164d9ff8 | 56/56 | 303.7s | 5.4s | 298.3s | 399% | 4e5cf3799dea9585c37ac5340a8ab5e14089046188d18dba9332295ecd16df03 |
| 85de83ca6323 | 59/59 | 394.4s | 21.9s | 372.5s | 458% | 7863880ac5855bf9ee5d663e4a6e3afa7d75ababf6a8991f763070c15d86bf53 |
| 411f73d26c1d | 78/78 | 420.1s | 23.0s | 397.2s | 454% | 2313218980c2ce97efe907bf59e4169f6121844b1255d6e8c60abe56b55af33f |

Host CPU% = (Σ child utime+stime)/wall×100 via getrusage(RUSAGE_CHILDREN) —
GNU `time` is not installed on this Arch box. Baseline sits ~400-460% mean
(decode bursts to ~630% as GOAL_FAST notes, embed/DP phases pull the mean down).

## vF2 — flag-OFF byte-identity CONFIRMED (2026-07-16)

`ATR_FAST_MATCHING=0` on this branch reproduces mainref byte-for-byte on all 4:

| project | flag-OFF hash | == mainref |
|---|---|---|
| dcd74148c7ec | c1aac14c5a0f… | ✓ |
| 5e85164d9ff8 | 4e5cf3799dea… | ✓ |
| 85de83ca6323 | 7863880ac585… | ✓ |
| 411f73d26c1d | 2313218980c2… | ✓ |

The keep-or-discard switch is proven trivially reversible: flag off = exact
mainline (added imports + an untaken PyNvCap dispatch branch, no behaviour
change). Lever isolation env matrix (unit-checked):
`ATR_FAST_MATCHING=1` FULL; `+ATR_FAST_NUMERICS=0` = F1-only;
`+ATR_FAST_DECODE=0` = F3-only; `ATR_FAST_MATCHING=0` = mainline.

## vF3 — fp16 is DEAD; fast mode = fp32 + TF32 + GPU decode (2026-07-16)

First FULL fast run (initial design: fp16 embedder + TF32 + GPU decode) on dcd
came back **functionally broken**: Scene 8/20 failed=10, Source **0/20 all
no-match**, scenes 42→53 (merger can't collapse unmatched scenes). Direct probe:

```
cos(fp32, fp16)      = 0.079    <- orthogonal garbage; .half() SSCD collapses
cos(fp32, fp32+TF32) = 1.000000 <- TF32 is bit-safe
bf16                 = unsupported by SSCDEmbedder (auto/fp32/fp16 only)
```

So **fp16 destroys matching** (query embeddings can't hit the fp32 index) —
confirming the journal's "fp16 forbidden (cos 0.02)". bf16 can't be tried
without editing the submodule (forbidden). The only usable F3 numeric lever is
**TF32** (fp32 model, TF32 matmul), which is bit-exact on the embedding and
still accelerates the ResNet forward on Ada.

Fast mode redefined: **GPU decode (F1) + fp32 + TF32 (F3)**. fp16 retained only
behind `ATR_FAST_PRECISION=fp16` so the owner can reproduce the broken delta.
Corrected FULL fast on dcd (single run):

| metric | mainref | fast (fp32+TF32+GPU) | Δ |
|---|---|---|---|
| wall | 113.5s | **98.8s** | −13% |
| host CPU% | 428% | **201%** | −53% (decode off CPU) |
| Scene timing | 20/20 | 20/20 | scene_line_delta=0 |
| Source timing | 19/20 exact | 17/20 exact, 3 loose | 2 exact→loose |
| scenes/matches | 42/42 | 42/42 | count identical |
| source_line_delta | — | 35 (23 material >1 src-frame) | boundary shifts only, **no episode flips** |

The precision cost is exactly the documented GPU-decode source-boundary drift
(BT.601 vs swscale, ~0.04–0.9s sub-second boundary shifts) — same source
episodes chosen, refined boundaries moved by ≤~1s. This is the "reported not
gated" trade the owner judges visually.

## vF4 — SCOREBOARD: full fast, 3-run quiet medians (2026-07-16)

`ATR_FAST_MATCHING=1` (GPU decode + fp32 + TF32) vs mainref (main HEAD).
Elapsed = median of 3 quiet runs (cooled to ≤76 °C between runs); host CPU% =
median of the 3 getrusage(children) means; scene/source line Δ from `diff_vs_ref`.

| project | elapsed (was) | host CPU% (was) | scene Δ | source line Δ | Scene / Source timing (fast) |
|---|---|---|---|---|---|
| dcd74148c7ec | **101s** (113.5) −11% | **202%** (428) | 0 | 35 (23 material) | 20/20 · 17/20 exact,3 loose |
| 5e85164d9ff8 | **261s** (303.7) −14% | **156%** (399) | 0 | 53 (17 material) | 46/46 · 40/46 exact,3 loose,3 wrong-prim |
| 85de83ca6323 | **378s** (394.4) −4% | **168%** (458) | 0 | 55 (23 material) | 52/54 · 52/54 exact,1 loose,1 fail |
| 411f73d26c1d | **368s** (420.1) −13% | **183%** (454) | 0 | 70 (23 material) | 52/52 · 50/52 exact,1 loose,1 wrong-prim |

3-run wall stability (very tight): dcd [100.5,100.6,101.1]; 5e85
[262.6,260.3,261.3]; 85de [378.0,373.3,377.5]; 411f [370.4,368.2,367.8].
Hashes reproduce across all 3 runs per project (deterministic).

**scene line Δ = 0 everywhere — scene boundaries byte-identical to mainline**
(detector kept on cv2 per §0). All deltas are on the source axis. Every flip is
a boundary shift, NOT an episode change, except the handful of material
decisions below (the ones `doubt_reasons` will surface for the owner):
- 5e85 scene 12: episode → no-match (lost a match)
- 85de scene 27: source jumped +76.5s (wrong location within the right episode)
- 5e85 scene 25: +1.24s source-start shift
- the rest are ≤~0.9s sub-second boundary drift (cosmetic).

Headline: **−4 to −14% wall AND host CPU roughly a third to a half of mainline
(400–460% → 156–202%)** — the desktop-usability prime directive, met on all 4.
The mean CPU sits at/under the ~200% target; the machine no longer thermally
throttles under matching (decode left the CPU).

## vF5 — PER-LEVER: F1 is the whole win, F3/TF32 is droppable (2026-07-16)

Single run each vs mainref. F1-only = `ATR_FAST_NUMERICS=0` (GPU decode, fp32,
no TF32). F3-only = `ATR_FAST_DECODE=0` (cv2 decode, fp32 + TF32).

| lever | dcd | 5e85 | 85de | 411f |
|---|---|---|---|---|
| **F1** GPU decode wall | 101s −12% | 264s −14% | 379s −4% | 370s −12% |
| **F1** host CPU% | 202% | 155% | 169% | 182% |
| **F1** Source exact | 18/20 | 40/46 | 52/54 | 50/52 |
| **F3** TF32 wall | 118s +3% | 305s +0% | 386s −2% | 407s −3% |
| **F3** host CPU% | 433% | 400% | 454% | 456% |
| **F3** Source exact | 19/20 | 43/46 | 53/54 | 52/52 |

Verdict:
- **F1 (GPU decode) delivers 100% of the win** — the whole CPU drop
  (400–460%→155–202%) and the whole wall gain (−4…−14%). Its cost is the
  source-boundary drift (BT.601). Keep it: this is fast mode.
- **F3 (TF32) buys ~nothing here**: wall within ±3% of mainline (embed is not
  the wall bottleneck once decode is on GPU; DP/ORB dominate), CPU unchanged
  (cv2 decode still on CPU). TF32 is bit-safe on the model (cos 1.0) but at
  margin 0.02 still perturbs a couple decisions — FULL(+TF32) dcd 17/20 vs
  F1-only 18/20. **Droppable with `ATR_FAST_NUMERICS=0`** for identical speed
  and equal-or-slightly-better precision. Kept ON by default only because a
  more embed-heavy real project could benefit; owner cherry-picks.
- **F2 (Stage-1 TikTok sampling via PyNv)**: NOT wired — the mainline Stage-1
  dense sampler already runs one sequential cv2 pass over the short TikTok
  source (seconds), overlapped with embed; it is a negligible slice of wall and
  not a scattered-access decode, so PyNv offers no meaningful gain there and
  the §0 detector-input stability is easier to keep on cv2. Left as cv2.
- **F4 (CPU bounding)**: mainline pools were already bounded — aligner
  `ThreadPoolExecutor(8)` + prefetch `ThreadPoolExecutor(4)`; fast mode adds no
  new pools and pins nothing. Confirmed: host CPU% dropping to 156–202% (from
  400–460%) is the direct evidence the CPU is no longer saturated — the F4 goal
  ("well under ~200%, never pin all 32 threads") is met by F1 offloading decode,
  with the existing bounds intact.

## vF6 — §4 concurrency: found a fast-mode OOM crash, fixed it (2026-07-16)

First 2-concurrent-fast run (85de + 411f, the 2 heaviest, sharing the 8 GB card)
exposed a **fast-mode-introduced crash**: peak VRAM 7756/7834 MiB (99%), and
411f died with a hard `torch.OutOfMemoryError` (tried 1.38 GiB, 759 MiB free) at
`pynv_decode.decode_window` — my **batched `torch.stack(window).cpu()`** was
stacking a wide (~222-frame) zoom window as >1 GiB of RGB on the GPU, unguarded
by the embed OOM retry. 85de survived (528.9s under contention, CPU 159%).

Two fixes:
1. **Per-frame host transfer** in `decode_window` (revert the batched-stack
   optimization to the proven recipe behaviour): each frame → `.cpu()`
   immediately, bounding decode VRAM to a single frame's intermediates. Bit-
   identical output (same values), so hashes are unchanged.
2. **OOM guard + cv2 fallback** in `_collect_frames_in_window_from_capture`: a
   CUDA-OOM from the GPU decode clears the cache and decodes THAT window on a
   transient cv2 capture — transparent, per-window, no crash. Stat
   `fast_decode_oom_cv2_fallback` counts it.
3. `_MAX_SESSIONS` 3 → 2: two concurrent processes now hold ≤4 decoders
   (~1.6 GB) instead of 6, keeping the peak out of the embed's OOM margin.

**Post-fix re-verification (2 concurrent FAST matchings, 85de + 411f):**

| metric | value |
|---|---|
| both complete without crash | ✓ (was: 411f OOM-crashed) |
| peak VRAM | 7767 / 7834 MiB (99% — full but non-fatal) |
| peak GPU util | 100% (both saturate the SM) |
| 85de elapsed (concurrent) | 520.9s (solo fast 378s → ~1.4× under contention) |
| 411f elapsed (concurrent) | 524.1s (solo fast 368s → ~1.4×) |
| 85de host CPU% | 156% · 411f host CPU% | 166% |
| combined host CPU% | ~322% of 3200% (32 threads) → **CPU ~90% idle** |
| output hashes | match each project's solo fast hash (deterministic) |
| natural OOMs this run | 0 (per-frame transfer + 2-session cap keep the peak just under the wall) |
| OOM→cv2 fallback mechanism | verified functional by fault injection: a simulated CUDA-OOM in `decode_window` falls back to cv2 and returns the correct window (stat `fast_decode_oom_cv2_fallback`=1) |
| embed adaptive OOM retry (pre-existing, cache-clear + batch split) | unchanged, still in place |

The shared 2-slot GPU queue (`indexation_queue.gpu_semaphore()`,
`MAX_CONCURRENT=2`) is untouched — its semantics still cap the machine at two
heavy GPU tasks total. Fast mode's key §4 win: under 2 concurrent matchings the
host CPU stays ~90% idle (combined ~322% of 3200%), so the desktop remains
usable at full matching load — vs mainline where two concurrent matchings
saturate CPU and pressure the 32 GB RAM wall (GOAL_JOURNAL v170). The cost moves
to VRAM (peak ~99%), held below the crash line by the fixes above.

## vF7 — final validation summary (2026-07-16)

- Flag-OFF byte-identical to current main HEAD on all 4 GT (vF2); re-confirmed
  on dcd after the OOM fix (`c1aac14…`).
- Per-frame decode transfer bit-identical to the batched version (dcd fast
  `17705f6c…` unchanged pre/post fix).
- GT folders, `anime_searcher` submodule, `eval_waivers.json` untouched
  (`git status` clean; no diff vs main on data/ledger; submodule pointer
  unchanged).
- Scene detector kept on cv2: scene_line_delta = 0 on every project.

## vF8 — post-merge RAM-launcher investigation (2026-07-16)

A real-project regression check on `85de83ca6323` showed that the new native
thread caps were not the source of the slowdown:

| configuration | elapsed | window decode | SSCD embed |
|---|---:|---:|---:|
| RAM-safe launcher, 4 threads | 505.2s | 273.5s | 97.3s |
| same launcher, 8 threads | 486.9s | 258.0s | 95.1s |
| 4 threads + preselected RGB conversion | **370.7s** | **150.4s** | 92.4s |

The regression came from the vF6 OOM fix converting every native source frame
to a full-resolution host RGB image before the existing 12-fps linspace
subsample discarded roughly half. The corrected decoder still visits every
native index in the original order (required for stateful GOP output identity),
but performs GPU RGB conversion and device-to-host copying only for indices the
sampler will return. It retains the one-frame-at-a-time VRAM bound.

The final 4-thread output is byte-identical to the original 505.2s run
(`scenes` + `matches` SHA-256 `43bab278ea483e151c2e2c37454803f94b48b809bf3948d9b768aaa9a9a69dbf`).
Peak process RSS during active refinement remained high at about 15.3 GiB, but
the heavy-job phase cleanup returns it after matching; the optimization targets
the conversion/copy churn without weakening the two-job queue or allocator
limits.

## vF9 — Fast Matching Round 2, Task 1: branch, R2 flags, frozen reference (2026-07-23/24)

New branch `feat/fast-matching-r2` off main HEAD (this journal's vF8 state plus
whatever landed since — see below), starting the R2 wall-time push
(`docs/superpowers/specs/2026-07-23-fast-matching-r2-design.md`). Added the
master switch and per-lever helper to `fast_matching.py` (TDD: 4 failing tests
→ 4 passing):

```python
_R2_FLAG = "ATR_FAST_R2"

def fast_r2_enabled() -> bool:
    if not fast_enabled():
        return False
    return not _off(os.environ.get(_R2_FLAG))

def r2_lever(name: str, default: bool = True) -> bool:
    if not fast_r2_enabled():
        return False
    val = os.environ.get(name)
    if val is None:
        return default
    return not _off(val)
```

`ATR_FAST_R2` rides on `ATR_FAST_MATCHING` — master OFF kills R2 too, so the
proven `ATR_FAST_MATCHING=0` mainline escape hatch stays intact. Individual
levers (`ATR_R2_COARSE`, `ATR_R2_FP16_WIN`, `ATR_R2_THIN`, wired in Tasks 3-9)
default ON but are dead the instant the master is off. No lever is consulted
by any code path yet on this commit — this task only lands the flag plumbing
and the reference numbers Tasks 3-10 compare against.

**Reference freeze protocol**: same invocation as vF1/vF4 —
`evaluate_matching_against_ground_truth.py <pid> --matcher aligner
[--save-generated-json ...]`, fast mode ON (`ATR_FAST_MATCHING`/`ATR_FAST_R2`
unset ⇒ both default ON, no lever wired yet so this is plain F1+F3 fast mode),
machine quiet (GPU idle 74MiB/8188MiB, load avg ~0.9 before the first run).
Decision hash = `sha256` over the canonical scenes+matches projection (drops
`thumbnail`, keeps every other field, sorted keys) via the same
`~/.cache/atr-eval/ref_hash.py` helper used for vF2/vF8. 3-run medians on both
heavies (85de83ca6323, 411f73d26c1d) plus dcd74148c7ec (ran 3 for extra
margin, cheap at ~85s/run); 2 runs on 5e85164d9ff8 per the practicality
allowance (a full 3×4 matrix would have added ~4 more minutes for a project
whose 2-run spread was already 2.2s).

**vF9 frozen reference — r2ref (fast mode ON, no R2 lever wired):**

| project | sc/mt | elapsed runs (s) | median | r2ref hash | scenes=/matches= | Scene timing | Source timing |
|---|---|---|---:|---|---|---|---|
| dcd74148c7ec | 42/42 | 85.3, 83.4, 85.4 | **85.3s** | `27acedd58ea169fb3eb2d9f7eab55e67c01216a7292fbecee58adf39c0ab9e46` | 42/42 | exact=20/20 | exact=17/20, loose=3 |
| 5e85164d9ff8 | 56/56 | 234.8, 232.6 (2-run) | **233.7s** | `e2916b5e807cca98a92cd62fee209a7102360e11f23873e09b5cb7739724e9b6` | 56/56 | exact=46/46 | exact=40/46, loose=3, wrong_primary=3 |
| 85de83ca6323 | 59/59 | 300.6, 302.5, 323.8 | **302.5s** | `b7034eaf7a249ff257d9a4156e80b67d27aef4f618b5f830ded990a5a5865f86` | 59/59 | exact=52/54, loose=2 | exact=52/54, loose=1, failed=1 |
| 411f73d26c1d | 78/78 | 334.5, 345.5, 324.6 | **334.5s** | `fe9393966d95e02d849e9e7ef65300654022f68fc7771ded8b2e0f63e5a3bd2b` | 78/78 | exact=52/52 | exact=50/52, loose=1, wrong_primary=1 |

Every project reproduced the identical scene/source line counts and evaluator
verdict (`PASS-WITH-LEDGER`, pre-existing waiver-ceiling notices, untouched
`eval_waivers.json`) across all runs — decision hashes are only computed from
the first (saved) run per project but the printed scene/source stats matched
byte-for-byte run to run, consistent with vF4's "hashes reproduce across all 3
runs" finding.

**These numbers are markedly faster than vF4/vF8** (dcd 101s→85s, 5e85
261s→234s, 85de 378s→302s, 411f 368s→335s) despite being the *same* fast-mode
code path (F1 GPU decode + fp32/TF32, no R2 lever wired) — main HEAD has moved
since 2026-07-16 (vF6 OOM fix, vF8 RGB-preselection fix, and whatever else
landed on main in the interim). This is exactly the vF1 lesson repeated: never
compare a new change against a stale journal number — always refreeze on
current HEAD before measuring a delta. **vF9, not vF4/vF8, is the baseline
Tasks 3-10 diff against.**

Generated JSON saved at `~/.cache/atr-eval/r2ref_<project>.json` for all 4
projects (first run each). Environment unchanged from vF1: i9-14900HX, RTX
4070 Laptop 8GB, 32GB RAM; torch 2.8.0+cu128, PyNvVideoCodec 2.1.0.

GT project folders, `anime_searcher` submodule, and `backend/data/
eval_waivers.json` untouched (evaluations are read-only against them; no
diffs). Evaluations run strictly sequentially, one GT project at a time.

## vF10 — Fast Matching Round 2, Task 2: profiling baseline (2026-07-24)

**Step 1 — `_prof` dump**: already present. `_stage5_refine`'s `finally`
block (`scene_aligner.py:5934-5938`) already prints the stage-5 `_prof` dict
(`rect`/`cur`/`cand`/`recall`, seconds) under `ATR_RERANK_DEBUG`, tagged
`[prof]` (not `[s5prof]` as the brief's illustrative snippet used — same
dict, same gate, pre-existing code, no functional gap). No code change
needed; `git status` on this task's start and end is otherwise clean besides
the journal edit.

**Step 2/3 — profiled the two heavies**, `ATR_RERANK_DEBUG=1`, same
`--matcher aligner [--save-generated-json ...]` invocation as vF9, run
strictly sequentially in the foreground (one `pixi run python
scripts/evaluate_matching_against_ground_truth.py <pid> --matcher aligner`
per heavy, 600s Bash timeout, nothing else heavy running — GPU idle 74MiB/
8188MiB before starting). Logs teed to
`/tmp/claude-1000/-home-sid-Projects-anime-tiktok-reproducer/277f86be-e242-4349-a808-205fb701a97f/scratchpad/vf10_prof_{85de,411f}.log`.

**Hash-inertness re-confirmed**: `ref_hash.py` over each debug run's
`--save-generated-json` output reproduces the exact vF9 r2ref hash for both
heavies —

| project | vF10 hash (debug run) | vF9 r2ref hash | match |
|---|---|---|---|
| 85de83ca6323 | `b7034eaf7a249ff257d9a4156e80b67d27aef4f618b5f830ded990a5a5865f86` | same | identical |
| 411f73d26c1d | `fe9393966d95e02d849e9e7ef65300654022f68fc7771ded8b2e0f63e5a3bd2b` | same | identical |

`ATR_RERANK_DEBUG=1` stays decision-inert, per the GOAL v5 M0 precedent.

**vF10 phase-timing table (single debug run per heavy, elapsed within vF9's
run-to-run spread):**

| project | elapsed | scene/source verdict | stage-5 `[prof]` (s) | `[winprof]` (s) | `aligner_refine_build_seconds` |
|---|---:|---|---|---|---:|
| 85de83ca6323 | 292.6s | Scene exact=52/54,loose=2; Source exact=52/54,loose=1,failed=1 | rect=26.9, cur=19.9, cand=144.3, recall=0.2 | decode=75.1, embed=99.9 | 234.44s |
| 411f73d26c1d | 324.8s | Scene exact=52/52; Source exact=50/52,loose=1,wrong_primary=1 | rect=29.1, cur=25.2, cand=97.8, recall=0.8 | decode=64.9, embed=96.9 | 219.88s |

(Scene/source verdicts and generated/GT scene counts match vF9 exactly for
both projects — 59/54 and 78/52 — consistent with the hash match above; not
re-derived here, see vF9 table.)

Full `aligner_*_seconds` breakdown (both heavies), for completeness:

| phase | 85de83ca6323 | 411f73d26c1d |
|---|---:|---:|
| aligner_segment_seconds | 1.99s | 10.27s |
| aligner_sample_seconds | 20.91s | 34.20s |
| aligner_variant_retrieve_seconds | — | 16.51s |
| aligner_retrieve_seconds | 0.14s | 0.69s |
| aligner_refine_build_seconds | 234.44s | 219.88s |
| aligner_interior_split_seconds | 12.58s | 18.03s |
| aligner_merge_seconds | 1.11s | 1.55s |
| aligner_presnap_seconds | 0.00s | 0.00s |

`aligner_refine_build_seconds` (the `_stage5_refine` call) dominates total
aligner time on both heavies (234.44s of the 273.1s aligner phase = ~86% for
85de; 219.88s of the 302.8s aligner phase = ~73% for 411f) and is exactly
the phase the `[prof]` rect/cur/cand/recall split decomposes.

**Derived decision 1 — Task 6 (A4) go/no-go**: rule is *go only if
`rect + cur ≥ 20s` on either heavy*.

- 85de83ca6323: rect(26.9) + cur(19.9) = **46.8s** ≥ 20s
- 411f73d26c1d: rect(29.1) + cur(25.2) = **54.3s** ≥ 20s

Both heavies clear the 20s bar by more than 2×, so **Task 6 (A4): GO.**

**Derived decision 2 — Task 7 (B1) sizing note**: the `[winprof] embed=`
total (99.9s / 96.9s) is a cumulative counter over the whole `_stage5_refine`
call, not scoped per `_prof` phase, so it cannot be split exactly by phase;
the operational estimate here is `embed / (cand + cur)` — the fraction of
the two wide-sweep, window-scoring phases' wall time that is upper-bounded
by embed compute (cand does exhaustive candidate-window scoring, cur does
current-window scoring; both call the embed cache):

- 85de83ca6323: cand+cur = 144.3+19.9 = 164.2s; embed=99.9s → **~61%** of
  wide-sweep wall time is embed-bound.
- 411f73d26c1d: cand+cur = 97.8+25.2 = 123.0s; embed=96.9s → **~79%** of
  wide-sweep wall time is embed-bound.

**B1 payoff estimate**: wide-sweep (`cand`+`cur`) phases are 164.2s/292.6s
(~56%) of 85de's total run and 123.0s/324.8s (~38%) of 411f's. The `[winprof]
embed=` totals (99.9s / 96.9s) are themselves entirely attributable to those
wide-sweep phases (nothing else in `_stage5_refine` touches the embed
cache), so a B1 optimization that drove embed cost to zero in the
wide-sweep path has a theoretical ceiling of roughly **~100s (85de) / ~97s
(411f)** of the current per-project wall time — i.e. B1 is sized as the
largest single lever surfaced by this profiling pass (bigger than the A4
rect+cur target, ~47-54s). This is an upper bound (embed cannot realistically
reach zero); actual achievable savings depend on how much of `cand`/`cur`
non-embed overhead (scoring math, candidate enumeration) is irreducible —
the 61%/79% embed-share figures above bound how much of the wide-sweep wall
time is even addressable by an embed-side fix.

Environment unchanged from vF9: i9-14900HX, RTX 4070 Laptop 8GB, 32GB RAM;
torch 2.8.0+cu128, PyNvVideoCodec 2.1.0. GT project folders,
`anime_searcher` submodule, and `backend/data/eval_waivers.json` untouched.
Evaluations run strictly sequentially, one GT project at a time, nothing
else heavy running concurrently.

## vF11 — Fast Matching Round 2, Task 3 (A1): byte-budgeted decoded-frames LRU — NULL RESULT (2026-07-24)

**Implemented exactly as specified.** `_WindowEmbedCache._frames_lru_budget`
(bytes; `ATR_R2_FRAMES_LRU_MB`, default 4096, 0 under `fast_r2_enabled()==False`
⇒ legacy 6-window behaviour preserved), `_frames_nbytes(frames)`,
`_trim_frames_lru()`; `window()`'s insertion now tracks
`_frames_lru_bytes` and calls `_trim_frames_lru()` instead of the fixed
`while len() > 6: popitem()`. TDD: 3 tests written first
(`backend/tests/test_window_embed_cache_lru.py`), confirmed failing
(`AttributeError: ... has no attribute '_frames_nbytes'`), then 3 passed
after implementation.

**Hash-identity: CONFIRMED on every run.** `ref_hash.py` reproduces the vF9
r2ref hash exactly on dcd74148c7ec (`27acedd5…`) and on all 5 measured runs
of 85de83ca6323 (`b7034eaf…`) — 3 quiet runs, 1 `ATR_RERANK_DEBUG=1` run, 1
RSS-wrapped run. Byte-identical by construction, verified.

**Wall-clock / decode-time: NO measurable improvement — root cause found.**
Added a temporary `ATR_LRU_DEBUG=1` counter (hits/misses/evictions/peak_len/
peak_bytes, gated the same way as `ATR_RERANK_DEBUG`/`ATR_TUG_DEBUG`,
zero-cost when unset) to check whether the larger budget was actually being
used:

| project | budget | hits | misses | evictions | peak_len | peak_bytes |
|---|---|---:|---:|---:|---:|---:|
| dcd74148c7ec | 4096MB | **0** | 111 | 74 | 39 | 4.00GiB |
| 85de83ca6323 | 4096MB | **13** | 417 | 401 | 31 | 4.00GiB |

The budget is real (peak_len rose from the old hard cap of 6 to 31-39
entries, filling to the byte budget as designed) but **cache hits are
near-zero regardless of capacity** (0/111 on dcd, 13/430 on 85de = ~3%).
`window()`'s cache key is the literal `(episode, r0, r1)` decode-run
boundary; a raw window is only reused when a *later* request's `missing`
gap resolves to the exact same `(r0, r1)` pair under a different geometry.
In the current codebase this coincidence is rare — most geometry variants
compute different `missing` boundaries against their own `slots` dict
(which caches embeddings permanently per-geometry and never evicts), so the
frames LRU mostly serves single-use decode results no matter how large it
is. Raising the cap from 6 to "however many fit in 4GB" therefore holds far
more RAM without intercepting more redecodes.

Direct before/after on the `[winprof]` scoped counter (apples-to-apples
with vF10's methodology, `ATR_RERANK_DEBUG=1` single run, decision hash
confirmed `b7034eaf…` == vF9/vF10):

| project | decode (vF10, pre-A1) | decode (vF11, post-A1) | Δ | embed (vF10) | embed (vF11) |
|---|---:|---:|---:|---:|---:|
| 85de83ca6323 | 75.1s | **74.2s** | −0.9s (noise) | 99.9s | 102.3s |

3-run quiet elapsed median on 85de83ca6323 (post-A1): 295.1, 291.6, 298.8s →
**median 295.1s** — statistically indistinguishable from vF9's 302.5s / vF10's
292.6s single run (run-to-run spread on this project has historically been
±10-20s, e.g. vF9's 300.6/302.5/323.8).

**RSS**: measured via a `getrusage(RUSAGE_CHILDREN)` subprocess wrapper
(GNU `time -v` unavailable, per vF1 precedent) on one 85de run:
**peak_rss = 21.25GiB** (rc=1 is the pre-existing waiver-ceiling exit
convention, not a crash — hash still confirmed identical on this run). vF8's
prior baseline (pre-R2, plain fast mode) was 15.3GiB — this task added
**~+6GiB** for the ~0s speed delta above. 21.25GiB is comfortably under the
32GB wall (66%), so the stop-rule ("halve the knob if RSS approaches the
wall") does not trigger, but the trade as measured is net-negative
(RAM cost, no speed benefit) on both GT projects tested.

**Conclusion**: the A1 premise — "the fixed 6-window LRU evicts regions that
later geometries re-request, costing redecode ×2.28-2.68" (GOAL v5 M0) — does
not hold against the *current* codebase's request pattern. That estimate
predates several architecture changes that landed since (863cb42's frame-
timestamp rewrite, F1 GPU decode, the prefetch/staged-frame threading layer,
and the permanent per-geometry `slots` cache), any of which could have
already eliminated the redecode churn A1 targeted. Implementation is correct,
tested, and shipped behind `ATR_R2_FRAMES_LRU_MB` exactly as specified (default
4096MB, 0 = legacy under `fast_r2_enabled()==False`) — trivially reversible —
but delivers no measured wall-time win on either GT project exercised here.
Recommend the owner treat A1 as **not worth its RAM cost** at the default
budget; lowering `ATR_R2_FRAMES_LRU_MB` (e.g. to 512 or lower) would recover
most of the RSS delta at no further decode cost, since hits are already rare
at 4096MB.

Environment unchanged from vF9/vF10: i9-14900HX, RTX 4070 Laptop 8GB, 32GB
RAM; torch 2.8.0+cu128, PyNvVideoCodec 2.1.0. GT project folders,
`anime_searcher` submodule, and `backend/data/eval_waivers.json` untouched.
Evaluations run strictly sequentially, one GT project at a time in the
foreground, nothing else heavy running concurrently.

**Post-review decision**: vF11 measured null wall-time benefit (−0.9s decode noise
on 85de, +2.4s embed, net ~0s) but +6GiB peak RSS against the vF8 fast-mode
baseline (15.3GB→21.25GB). The A1 premise—"fixed 6-window evicts regions that
later geometries re-request"—does not hold in the current codebase; cache hits
are ~0–3% regardless of capacity, because current geometry variants compute
different missing-run boundaries and permanently cache results per-geometry (no
LRU churn to address). Trade is net-negative (RAM cost, no speed gain). **Default
`ATR_R2_FRAMES_LRU_MB` flipped to `0`** (opt-in only, legacy 6-window reserved).
Re-enable with `ATR_R2_FRAMES_LRU_MB=<MB>` if future changes unlock frame-reuse
patterns; current choice balances simplicity + honesty with future flexibility.

## How to try it (owner test protocol)

```bash
git checkout feat/fast-gpu-matching
# fast mode is ON by default on this branch — just run a project through
# /matches as usual (backend picks it up automatically).
#   default : GPU NVDEC decode + fp32 + TF32   (ATR_FAST_MATCHING unset/1)
#   compare : ATR_FAST_MATCHING=0  -> exact mainline (byte-identical) for A/B
# Optional lever toggles (all default to the fast setting):
#   ATR_FAST_NUMERICS=0  -> drop TF32 (identical speed, slightly better precision)
#   ATR_FAST_DECODE=0    -> keep cv2 decode, TF32 only (isolate F3)
```

What to look at: the frontend flags doubtful scenes via `doubt_reasons`; those
plus any scene whose source differs from a mainline (`ATR_FAST_MATCHING=0`) run
are what to inspect. Expect same scene boundaries (identical), same source
episodes, and source in/out points shifted by ≤~1s on some scenes, with a few
material flips per project (listed in vF4). Judge those visually; keep the flag
(merge, default OFF on main, opt-in ON) or discard the branch.

Keep-or-discard is trivially reversible: `ATR_FAST_MATCHING=0` is proven
byte-identical to current mainline on all 4 GT projects (vF2).
