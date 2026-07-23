# Fast Matching Round 2 — wall-time push with budgeted quality trades

**Date:** 2026-07-23
**Status:** Approved (owner, 2026-07-23)
**Branch:** `feat/fast-matching-r2`
**Predecessors:** GOAL.md (v57→v170, strict-fidelity record), GOAL_FAST.md +
`docs/FAST_MODE_JOURNAL.md` vF0–vF8 (fast mode, merged, `ATR_FAST_MATCHING`
default ON).

## 1. Goal

Cut heavy-project matching wall time from ~370s (current fast mode, 85de-class
projects) toward **150–220s**, on the same hardware (i9-14900HX, RTX 4070 8GB,
32GB RAM, desktop apps running alongside). Precision losses are allowed but
bounded by an explicit per-lever quality budget (§4).

Non-goals: reindexing (still forbidden), `anime_searcher` submodule changes,
scene-detector changes (boundary stability stays on the byte-identical cv2
path), concurrency-model changes (the shared `MAX_CONCURRENT=2` GPU queue is
law).

## 2. Measured starting point (why these levers)

From GOAL_JOURNAL v166 + FAST_MODE_JOURNAL vF5/vF8, the 85de fast-mode budget
(~370s) decomposes as:

| Phase | ~Time | Fact that makes it attackable |
|---|---:|---|
| Window decode (NVDEC) | 150s | Redecode factor ×2.3–2.7: `_WindowEmbedCache._frames_lru` holds only 6 windows; later geometries/retries re-decode evicted regions. NVDEC session cap 2 (vF6) churns sessions across episodes. |
| SSCD window embed (fp32+TF32) | 92s | Mostly fresh-vs-fresh comparisons (window embeds vs fresh query embeds) — not index-facing. |
| Registration/ORB + DP + stage-5 CPU | 80–100s | Runs largely sequentially on a CPU that fast mode left ~90% idle (combined 156–202% of 3200%). |
| Query sampling + scene detect + retrieve | 40–50s | Scene detect (~18s) locked by contract; sampling/retrieve small. |

Two prior verdicts are explicitly superseded in scope:
- v111/v115 "any trim loses correctness" and the ≤200s impossibility proof
  (v166) were **byte-identity-gated**; this round has a quality budget.
- The fp16 ban (vF3, cos 0.079) remains law **only for index-facing
  embeddings**; fresh-vs-fresh window scoring in fp16 is untested and is a
  lever here.

## 3. Flags and reversibility

- Master switch `ATR_FAST_R2` — default ON on the branch; `ATR_FAST_R2=0`
  reproduces current main behavior (keep-or-discard mechanism, GOAL_FAST
  pattern).
- Each Workstream-B lever gets its own sub-toggle (`ATR_R2_COARSE`,
  `ATR_R2_FP16_WIN`, `ATR_R2_THIN`) so the owner can cherry-pick.
- Journal: continue `docs/FAST_MODE_JOURNAL.md` (entries vF9+). GOAL_JOURNAL.md
  stays closed.

## 4. Quality budget (owner decision, 2026-07-23: "Moderate")

Per GT project, each lever (and the final combined default set) is compared to
the frozen fast-mode reference (§6). A lever stays **default-ON** only if it
causes:

- ≤1 episode-identity flip per project, AND
- ≤4 source-line exactness losses per project (strict evaluator), AND
- zero scene-line changes. The detector is untouched, but stage-5 boundary
  tug consumes window embeds, so B levers can in principle move scene lines —
  this is gated, not assumed.

Levers exceeding the budget ship **default-OFF** with their measured deltas
recorded, available as cherry-picks. All changed scenes (vs reference) are
listed per lever for the owner's visual pass; the frontend `doubt_reasons`
flagging is the entry point.

## 5. Workstreams

### Workstream A — lossless plumbing (lands first, hash-verified)

- **A1. Kill the redecode tax.** Replace the fixed 6-window `_frames_lru` with
  a byte-budgeted LRU (~4–6GB decoded frames, configurable), sized against
  measured peak RSS (15.3GiB, vF8) under the 32GB wall. Expected redecode
  ×2.3–2.7 → ~×1.1; decode ~150s → ~65–80s. Frames identical ⇒ hashes
  unchanged.
- **A2. NVDEC session management.** 3 decoder sessions when running solo, 2
  under concurrent load (queried from `indexation_queue.gpu_semaphore()`
  occupancy). Cuts session churn when candidate episodes alternate.
- **A3. Deeper decode↔embed overlap.** Widen the prefetch pipeline (queue
  depth + scheduling from known upcoming candidate/DP order) so embed never
  waits on a cold window. Staged frames keep the same decode-call shape as
  synchronous decode (existing identity guarantee).
- **A4. Parallelize stage-5 CPU work.** Fan independent ORB/registration and
  per-candidate scoring out on the idle CPU (bounded pool ~8 workers, never
  all 32 threads), with deterministic merge order. Verification: byte-identical
  output, or if ordering effects are unavoidable, decision-identical on all 4
  GT with the difference reported.

### Workstream B — budgeted levers (each measured solo, then combined)

- **B1. Coarse-to-fine window sweep** (`ATR_R2_COARSE`). Score windows at
  every 2nd decode slot; densify to the full grid only around the argmax
  region (margin sized from diff-curve stats). Attacks decode+embed volume on
  the dominant wide windows.
- **B2. fp16 window scoring** (`ATR_R2_FP16_WIN`). fp16/bf16 autocast only
  where both sides of a similarity are freshly embedded (window vs query).
  Index-facing paths (`_index_cos_across`, `_index_embedding_at`, FAISS
  queries) stay fp32 — vF3's ban holds there. Expected embed ~92s → ~50–60s.
- **B3. Variant/sample thinning on low-doubt scenes** (`ATR_R2_THIN`). Skip
  query-variant retrieval / dense probing where stage-1 evidence is
  unambiguous (targets 411f's variant_retrieve 14s + interior_split 19.7s
  tail).

## 6. Measurement protocol

1. Freeze a fresh reference on current main HEAD, fast mode ON: 3-run quiet
   medians for elapsed + decision hashes + strict-evaluator lines, all 4 GT
   projects (85de, 411f, 5e85, dcd). (vF1 precedent: never trust a stale ref.)
2. Per lever: wall Δ per project, phase timings (`aligner_*_seconds`,
   `frame_decode_window_seconds`, `sscd_embedding_seconds`), evaluator
   scene/source deltas, flips listed by scene index.
3. Budget gate (§4) applied per lever and to the final combined default set.
4. Concurrency re-check: 2 concurrent fast matchings on the two heaviest;
   both complete, peak VRAM recorded, OOM→cv2 fallback still functional.
5. Final: `ATR_FAST_R2=0` re-verified byte-identical to main on all 4 GT.

## 7. Risks

- **VRAM (8GB wall):** B2 halves window-embed activation memory (helps); A2's
  third session costs ~412MiB and is solo-only; vF6's OOM→cv2 fallback and
  the 2-session concurrent cap stay in place.
- **RAM (32GB wall):** A1's cache is byte-budgeted and configurable; peak RSS
  monitored against the vF8 15.3GiB baseline. Two concurrent matchings must
  not breach the wall (concurrency re-check).
- **Determinism (A4):** explicit re-verification step; if parallel ordering
  changes results, either fix the merge order or report decision-identity
  instead and flag it.
- **B1 missing narrow peaks:** densify margin sized from measured diff-curve
  autocorrelation; the budget gate catches residual damage.

## 8. Deliverables

Branch `feat/fast-matching-r2` with flags; `docs/FAST_MODE_JOURNAL.md` vF9+
per-lever scoreboard; changed-scenes list for the owner's visual pass;
one-paragraph how-to-try note; flag-OFF byte-identity re-verification; GT
folders / submodule / `eval_waivers.json` untouched.
