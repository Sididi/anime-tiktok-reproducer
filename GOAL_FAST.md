Build FAST MODE: an owner-gated, GPU-oriented variant of the matching pipeline that trades strict numeric fidelity for speed and CPU freedom. This document (2026-07-16) deliberately RELAXES the contract that governed v57→v169: bit-identity and evaluation-equivalence are NOT gates here. The owner will run this branch on a REAL project, judge the precision impact visually, and keep or discard the variant on that basis. Your job is to make the trade-off as favorable and as MEASURED as possible — not to hide it.

Everything learned in `docs/GOAL_JOURNAL.md` (v57→v169) is your notebook and remains binding as FACTS (measured numbers, dead ends, recipes) even though the old GATES no longer apply. Do not re-derive: extend.

# 0. Ground rules

- Work on a dedicated branch: `feat/fast-gpu-matching` (mainline stays the validated cv2 path, untouched). Runtime switch `ATR_FAST_MATCHING` (env or config), default ON in this branch so the owner tests by simply running it; OFF must fall back to the exact mainline path (this is the keep-or-discard mechanism — keep it trivially reversible).
- Still absolute: GT project folders read-only; `anime_searcher` submodule untouched; no reindexing; `backend/data/eval_waivers.json` untouched; the scene DETECTOR keeps cv2/byte-identical inputs (boundary stability is cheap to keep and losing it would confuse the owner's visual judgement — its decode cost is minor).
- Precision is REPORTED, not gated: after each significant change, run the strict evaluator on all four GT projects and record the per-project scene/source line DELTAS versus the frozen reference (`~/.cache/atr-eval/v5ref_*`, journal v169 state) in the scoreboard. Decision flips are EXPECTED (margins live at 0.02; the journal's v169 measured a faithful GPU path flipping lines) — the owner decides if they matter, you make them visible.
- Journal: new file `docs/FAST_MODE_JOURNAL.md` (entries vF1, vF2, …) — do NOT write into GOAL_JOURNAL.md (that is the validated record).

# 1. Why GPU-oriented is the prime directive (owner requirement)

The owner runs Chrome + Discord + VSCode alongside matching on this laptop (i9-14900HX, RTX 4070 8GB, 32GB RAM). Today's cv2 path burns ~630% CPU in decode alone (measured, journal 2026-07-15) and thermally throttles the whole machine. Fast mode must move the pipeline onto the GPU so the desktop stays usable: success is measured BOTH in elapsed seconds AND in host-CPU footprint (mean process CPU% during a run — target well under ~200% vs today's 600%+; leave thread pools bounded, ~8-12 workers max, never pin all 32 threads).

# 2. What is already proven and sitting ready (use it, don't rebuild)

- **PyNvVideoCodec persistent NVDEC decode** — the complete working recipe from GOAL v5.3 lives UNWIRED in `backend/scripts/diagnostics/pynv_decode.py` + `probe_pynv_calibration.py`, deps already durable in pixi.toml (`pynvvideocodec==2.1.0`, `nvidia-npp-cu12`). Measured: 0.076s/scattered-window (~3.3× cv2 wall), 37% of ONE core vs cv2's 630%, CPU un-throttles to ~3.8GHz, GPU decoder engages (peak 98%). Known sharp edges, all solved in the recipe: buggy P016 dlpack descriptor (reconstruct via `as_strided`, codes = value/64), BT.601-limited conversion (never PyNv's built-in RGB — its matrix is wrong by Δ2.9), per-stream frame-index↔cv2 mapping, decoder-session LRU (~412MiB VRAM each, keep 2-3).
- **The measured precision cost of that path** (v169, end-to-end): worst-case SSCD 1-cos ~0.0125 → real decision flips (dcd source 20/20→15/20 in the strict eval; 85de −1/−1). That is the baseline trade fast mode STARTS from — your engineering may reduce it (better rounding/debias) but must primarily buy SPEED on top.
- **Un-throttle physics** (v167): ORB 2.01×, embed 1.46× hot/cool — realized once decode leaves the CPU.
- **Dead ends that stay dead** (do not retry): subprocess NVDEC (spawn cost, v168), torchcodec-CUDA (no compatible FFmpeg exists on this system, 2026-07-15), RAM frame caches (redecode×≈1.0, v167), CPU-side micro-trims that trade correctness for nothing (v111/v115).

# 3. The levers, now unlocked (in order)

- F1 Wire the PyNv decoder as the window-decode primitive (flag-gated): sessions LRU, GPU-resident P016→RGB conversion, output tensors fed STRAIGHT into SSCD preprocessing on GPU (resize/normalize on GPU — kill the PIL/CPU round-trip). Watch SM contention (decode-conversion + embed share the 4070): use CUDA streams / interleave batches; v169's 14%-only gain suggests naive wiring leaves throughput on the table.
- F2 Stage-1 TikTok sampling + diff curve via PyNv (8-bit NV12 path) — detector excluded (§0).
- F3 Numeric modes on the embedder, now allowed: TF32 first, then fp16/bf16 (fp16 was ~124→expect 2×+ img/s on Ada; the old "forbidden" was an index-consistency worry under strict gates — HERE measure the eval deltas and report). torch.compile / channels_last / batch consolidation to fill the 64-frame chunks (the ≤64 OOM chunk limit stands on 8GB).
- F4 Bound the remaining CPU: parallel ORB capped, no full-core pools; measure desktop responsiveness informally (owner criterion).
- Aspirational targets: ≤150-200s per project AND host CPU <200% mean. Report whatever is reached — the owner judges the trade, not a gate.

# 4. Concurrency (owner requirement — specify and verify here too)

The shared GPU queue shipped in v169 stays LAW in fast mode: `/matches` and indexation draw from ONE `MAX_CONCURRENT = 2` budget (`indexation_queue.gpu_semaphore()`), so at most two heavy tasks total run at once — the setting chosen for this machine and the owner's simultaneous desktop use. Fast mode makes each matching GPU-heavy, so RE-VERIFY the worst case on this branch: two concurrent FAST matchings = 2× (SSCD model + activations) + 2× decoder-session LRU (~0.8-1.2GB) on the 8GB card — measure VRAM at peak, confirm the OOM cache-clear retry still functions under contention, and record the concurrent per-project elapsed. Bonus expected and worth measuring: under fast mode, two concurrent matchings should leave the CPU mostly idle — the desktop stays usable even at full matching load, which is the whole point.

# 5. Owner test protocol (how this gets judged)

Deliver on the branch: the flag, the scoreboard, and a one-paragraph "how to try it" note (checkout branch → run a real project through /matches as usual). The owner will run a real project and visually inspect the matches (the frontend flags doubtful scenes via `doubt_reasons` — those and any changed-vs-mainline scenes are what they'll check). Based on that observation the branch is merged (flag default OFF on main, ON opt-in) or deleted. Your scoreboard makes that decision informed:

| project | elapsed (was) | host CPU% (was) | scene line Δ vs ref | source line Δ vs ref | flips listed |
|---|---|---|---|---|---|

# 6. Final report

Scoreboard complete on all four GT projects (3-run quiet medians for elapsed; CPU% traces), per-lever gains (F1-F4, each with its own eval-delta measurement so the owner can also cherry-pick — e.g. keep GPU decode but not fp16), concurrency worst-case numbers (§4), the branch + flag + how-to-try note, `docs/FAST_MODE_JOURNAL.md` complete, GT/submodule/ledger untouched (`git status` shown), mainline byte-identical (the flag OFF path re-validated once against `v5ref` hashes).
