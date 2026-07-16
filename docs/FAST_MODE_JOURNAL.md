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
