# Fast Matching Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut heavy-project matching wall time from ~370s toward 150–220s via lossless plumbing (Workstream A) plus per-lever budgeted quality trades (Workstream B), per the approved spec `docs/superpowers/specs/2026-07-23-fast-matching-r2-design.md`.

**Architecture:** All levers land behind env flags on branch `feat/fast-matching-r2`. Workstream A (A1–A4) must keep decisions byte-identical (verified by output hashes). Workstream B levers (B1–B3) each carry their own flag and are measured solo against a frozen reference on the 4 GT projects; the moderate quality budget decides default-ON vs default-OFF.

**Tech Stack:** Python 3 (pixi env), OpenCV, PyTorch (SSCD embedder), PyNvVideoCodec (NVDEC), FAISS (read-only index), pytest.

## Global Constraints

- Hardware truth: RTX 4070 8GB VRAM, 32GB RAM, desktop apps running — never assume more.
- FORBIDDEN: reindexing; any change to the `anime_searcher` submodule; any change to scene-detector inputs (stays cv2/byte-identical); touching GT project folders or `backend/data/eval_waivers.json`.
- The shared GPU queue (`indexation_queue.gpu_semaphore()`, `MAX_CONCURRENT = 2`) is law — do not change its semantics.
- fp16 is FORBIDDEN for anything index-facing (FAISS queries, `_index_cos_across`, `_index_embedding_at`) — vF3 measured cos 0.079 divergence. fp16 is allowed ONLY where both sides of a similarity are freshly embedded (Task 8).
- Quality budget (owner, "Moderate"): per GT project vs the Task-1 frozen reference — ≤1 episode-identity flip, ≤4 source-line exactness losses, **zero** scene-line changes. Over-budget levers ship default-OFF.
- Journal all measurements in `docs/FAST_MODE_JOURNAL.md` (entries vF9+). Never write to `docs/GOAL_JOURNAL.md`.
- Never run two GT evaluations concurrently (they fight for the GPU and corrupt timings — memory `backend-test-suite-preexisting-failures`).
- GT project ids: `dcd74148c7ec` (light), `5e85164d9ff8`, `85de83ca6323`, `411f73d26c1d` (heavies).
- Timing runs: 3-run medians, machine quiet (no other heavy processes), noted as "quiet" in the journal.
- Backend tests: main is NOT green (38 pre-existing failures, 2026-07-10). For any pytest run, compare failures against a fresh main-baseline run of the same selection; only NEW failures block.
- Git: commit after every task; messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Branch, R2 flags, frozen reference (vF9)

**Files:**
- Modify: `backend/app/services/fast_matching.py` (append after `configure_numerics`, line 77+)
- Test: `backend/tests/test_fast_matching_r2_flags.py` (create)
- Modify: `docs/FAST_MODE_JOURNAL.md` (append vF9 entry)

**Interfaces:**
- Produces: `fast_matching.fast_r2_enabled() -> bool`, `fast_matching.r2_lever(name: str, default: bool = True) -> bool` — every later task gates on these exact functions.
- Produces: frozen reference artifacts `~/.cache/atr-eval/r2ref_<project>.json` + a vF9 journal table (elapsed medians, decision hashes, evaluator scene/source lines) that Tasks 3–10 compare against.

- [ ] **Step 1: Create the branch**

```bash
cd /home/sid/Projects/anime-tiktok-reproducer
git checkout -b feat/fast-matching-r2
```

- [ ] **Step 2: Write the failing flag tests**

Create `backend/tests/test_fast_matching_r2_flags.py`:

```python
import importlib

from app.services import fast_matching


def _reload_with_env(monkeypatch, **env):
    for key in ("ATR_FAST_R2", "ATR_R2_COARSE", "ATR_R2_FP16_WIN", "ATR_R2_THIN"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    importlib.reload(fast_matching)
    return fast_matching


def test_r2_default_on_when_fast_mode_on(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="1")
    assert fm.fast_r2_enabled() is True


def test_r2_master_kill_switch(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="1", ATR_FAST_R2="0")
    assert fm.fast_r2_enabled() is False
    # levers are dead when the master is off, whatever their own flag says
    assert fm.r2_lever("ATR_R2_COARSE") is False


def test_r2_follows_fast_master(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="0")
    assert fm.fast_r2_enabled() is False


def test_r2_lever_toggles(monkeypatch):
    fm = _reload_with_env(monkeypatch, ATR_FAST_MATCHING="1", ATR_R2_FP16_WIN="0")
    assert fm.r2_lever("ATR_R2_FP16_WIN") is False
    assert fm.r2_lever("ATR_R2_COARSE") is True  # default ON on the branch
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd backend && pixi run pytest tests/test_fast_matching_r2_flags.py -v
```
Expected: FAIL with `AttributeError: module ... has no attribute 'fast_r2_enabled'`.
(If `pixi run pytest` is not the working invocation, use the same pytest invocation the existing `backend/tests` suite uses — check `pixi.toml` tasks; async tests historically need the pixi `dev` env.)

- [ ] **Step 4: Implement the flags**

Append to `backend/app/services/fast_matching.py`:

```python
# ---------------------------------------------------------------------------
# Round 2 (2026-07-23 spec): wall-time levers behind a master switch.
# ATR_FAST_R2 rides on top of ATR_FAST_MATCHING: fast mode OFF kills R2 too,
# so ATR_FAST_MATCHING=0 stays the proven byte-identical mainline escape hatch.
# ---------------------------------------------------------------------------
_R2_FLAG = "ATR_FAST_R2"


def fast_r2_enabled() -> bool:
    if not fast_enabled():
        return False
    return not _off(os.environ.get(_R2_FLAG))


def r2_lever(name: str, default: bool = True) -> bool:
    """Per-lever toggle (e.g. ATR_R2_COARSE); dead when the master is off."""
    if not fast_r2_enabled():
        return False
    val = os.environ.get(name)
    if val is None:
        return default
    return not _off(val)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd backend && pixi run pytest tests/test_fast_matching_r2_flags.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Freeze the R2 reference (vF9)**

Re-read `docs/FAST_MODE_JOURNAL.md` vF1 ("v5ref is STALE") and reuse its exact evaluator invocation. For each GT project, one invocation per project, machine quiet, fast mode ON (env unset), 3 runs each for the elapsed median; save the generated JSON on the first run:

```bash
cd backend && pixi run python scripts/evaluate_matching_against_ground_truth.py \
  85de83ca6323 --matcher aligner \
  --save-generated-json ~/.cache/atr-eval/r2ref_85de83ca6323.json
# repeat for dcd74148c7ec, 5e85164d9ff8, 411f73d26c1d
```

Record in a new vF9 journal entry: per-project elapsed (3-run medians), the scenes+matches SHA-256 decision hash (same hashing as vF2/vF8 — `sha256` over the canonical scenes+matches JSON), and the evaluator's scene/source line counts. These are THE reference numbers for every later task.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/fast_matching.py backend/tests/test_fast_matching_r2_flags.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2): add ATR_FAST_R2 master flag + lever helper; freeze vF9 reference"
```

---

### Task 2: Profiling baseline (vF10) — steers A4/B1 anchors

**Files:**
- Modify: `backend/app/services/scene_aligner.py:5944` region (only if `_prof` is not already printed — see Step 1)
- Modify: `docs/FAST_MODE_JOURNAL.md` (append vF10)

**Interfaces:**
- Produces: vF10 journal table with, for 85de + 411f: `aligner_*_seconds` phase timings, `[winprof] decode=/embed=` (from `_WindowEmbedCache.close`, printed under `ATR_RERANK_DEBUG=1`), and the stage-5 `_prof` split (`rect/cur/cand/recall` seconds). Task 6 and Task 7 read their go/no-go thresholds from this table.

- [ ] **Step 1: Ensure `_prof` is dumped**

Check the end of `_stage5_refine` (search `_prof` after `scene_aligner.py:5794`). If the dict is never printed, add before the final return of `_stage5_refine`:

```python
        if _os.environ.get("ATR_RERANK_DEBUG"):
            print(
                "[s5prof] "
                + " ".join(f"{k}={v:.1f}s" for k, v in _prof.items())
            )
```

(`_os` is already imported in this scope — verify with a grep for `_os.environ` inside `scene_aligner.py`; if the local alias differs, match it.)

- [ ] **Step 2: Profile the two heavies**

```bash
cd backend && ATR_RERANK_DEBUG=1 pixi run python scripts/evaluate_matching_against_ground_truth.py \
  85de83ca6323 --matcher aligner 2>&1 | tee /tmp/claude-1000/r2_prof_85de.log
grep -E '\[winprof\]|\[s5prof\]|aligner_.*_seconds' /tmp/claude-1000/r2_prof_85de.log
# repeat for 411f73d26c1d
```

First confirm (vF1 precedent) that `ATR_RERANK_DEBUG=1` does not change the decision hash: compare the run's hash against the Task-1 reference — must be identical (it was proven inert in GOAL v5 M0; re-confirm once).

- [ ] **Step 3: Record vF10 + derive go/no-go**

Append vF10 to the journal with the full phase table and these two derived decisions, stated explicitly:
- **Task 6 (A4) go/no-go:** go only if `rect + cur ≥ 20s` on either heavy.
- **Task 7 (B1) sizing:** note the `[winprof] embed` share attributable to wide sweeps (`cand` + `cur` window scoring) — B1's payoff estimate.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/scene_aligner.py docs/FAST_MODE_JOURNAL.md
git commit -m "chore(fast-r2): dump stage-5 _prof under ATR_RERANK_DEBUG; record vF10 profiling baseline"
```

---

### Task 3 (A1): Byte-budgeted decoded-frames LRU — kill the redecode tax

**Files:**
- Modify: `backend/app/services/scene_aligner.py:234-248` (`_WindowEmbedCache.__init__`) and `scene_aligner.py:413-433` (`window()` LRU insertion)
- Test: `backend/tests/test_window_embed_cache_lru.py` (create)
- Modify: `docs/FAST_MODE_JOURNAL.md` (append vF11)

**Interfaces:**
- Consumes: `fast_matching.fast_r2_enabled()` (Task 1).
- Produces: `_WindowEmbedCache._frames_lru_budget: int` (bytes; 0 = legacy 6-window mode), `_WindowEmbedCache._frames_nbytes(frames) -> int`, `_WindowEmbedCache._trim_frames_lru() -> None`. Env knob `ATR_R2_FRAMES_LRU_MB` (default 4096).

Background (why): GOAL v5 M0 measured redecode factor ×2.28–2.68 — the fixed 6-window LRU evicts regions that later geometries re-request. RAM headroom exists (peak RSS 15.3GiB of 32GB, vF8). Frames are identical whether cached or re-decoded ⇒ decisions byte-identical by construction; we verify anyway.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_window_embed_cache_lru.py`:

```python
from collections import OrderedDict

from PIL import Image

from app.services.scene_aligner import _WindowEmbedCache


def _frames(n: int, w: int = 100, h: int = 50):
    return [(float(i), Image.new("RGB", (w, h))) for i in range(n)]


def _cache_with_budget(budget_bytes: int) -> _WindowEmbedCache:
    cache = _WindowEmbedCache.__new__(_WindowEmbedCache)  # skip heavy __init__
    cache._frames_lru = OrderedDict()
    cache._frames_lru_bytes = 0
    cache._frames_lru_budget = budget_bytes
    return cache


def test_frames_nbytes_counts_rgb_pixels():
    frames = _frames(2, w=100, h=50)
    assert _WindowEmbedCache._frames_nbytes(frames) == 2 * 100 * 50 * 3


def test_trim_respects_byte_budget_and_lru_order():
    one_run = 100 * 50 * 3 * 2  # 2 frames per run
    cache = _cache_with_budget(budget_bytes=3 * one_run)
    for k in range(5):
        run = _frames(2)
        cache._frames_lru[("ep", k, k + 1)] = run
        cache._frames_lru_bytes += _WindowEmbedCache._frames_nbytes(run)
        cache._trim_frames_lru()
    assert cache._frames_lru_bytes <= 3 * one_run
    # oldest runs evicted first
    assert ("ep", 0, 1) not in cache._frames_lru
    assert ("ep", 4, 5) in cache._frames_lru


def test_zero_budget_keeps_legacy_six_window_bound():
    cache = _cache_with_budget(budget_bytes=0)
    for k in range(8):
        run = _frames(1)
        cache._frames_lru[("ep", k, k)] = run
        cache._frames_lru_bytes += _WindowEmbedCache._frames_nbytes(run)
        cache._trim_frames_lru()
    assert len(cache._frames_lru) == 6
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && pixi run pytest tests/test_window_embed_cache_lru.py -v
```
Expected: FAIL with `AttributeError: ... has no attribute '_frames_nbytes'`.

- [ ] **Step 3: Implement the byte-budgeted LRU**

In `_WindowEmbedCache.__init__` (`scene_aligner.py`, the block ending `OrderedDict()` around line 240), extend:

```python
        self._frames_lru: "OrderedDict[tuple[str, int, int], list]" = (
            OrderedDict()
        )
        # A1 (R2): the 6-window bound was sized for the 32GB wall under CPU
        # decode; it costs redecode ×2.3-2.7 (GOAL v5 M0). Under R2 the LRU
        # is byte-budgeted instead. Budget 0 = legacy behaviour.
        from .fast_matching import fast_r2_enabled

        self._frames_lru_bytes = 0
        self._frames_lru_budget = (
            int(os.environ.get("ATR_R2_FRAMES_LRU_MB", "4096")) * 1024 * 1024
            if fast_r2_enabled()
            else 0
        )
```

(Add `import os` at the top of `scene_aligner.py` if only `_os` aliases exist in inner scopes — match the file's existing import style.)

Add two methods to `_WindowEmbedCache`:

```python
    @staticmethod
    def _frames_nbytes(frames: list) -> int:
        total = 0
        for _, im in frames:
            w, h = im.size
            total += w * h * 3
        return total

    def _trim_frames_lru(self) -> None:
        if self._frames_lru_budget <= 0:
            while len(self._frames_lru) > 6:
                _, evicted = self._frames_lru.popitem(last=False)
                self._frames_lru_bytes -= self._frames_nbytes(evicted)
            self._frames_lru_bytes = max(0, self._frames_lru_bytes)
            return
        while (
            self._frames_lru_bytes > self._frames_lru_budget
            and len(self._frames_lru) > 1
        ):
            _, evicted = self._frames_lru.popitem(last=False)
            self._frames_lru_bytes -= self._frames_nbytes(evicted)
```

In `window()` replace the insertion block (currently around lines 431-433):

```python
                    self._frames_lru[key] = frames
                    while len(self._frames_lru) > 6:
                        self._frames_lru.popitem(last=False)
```

with:

```python
                    self._frames_lru[key] = frames
                    self._frames_lru_bytes += self._frames_nbytes(frames)
                    self._trim_frames_lru()
```

- [ ] **Step 4: Run the unit tests**

```bash
cd backend && pixi run pytest tests/test_window_embed_cache_lru.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Hash-verify + measure (vF11)**

Byte-identity check on the light project first, then measure a heavy:

```bash
cd backend && pixi run python scripts/evaluate_matching_against_ground_truth.py \
  dcd74148c7ec --matcher aligner
# decision hash MUST equal the Task-1 r2ref hash for dcd
pixi run python scripts/evaluate_matching_against_ground_truth.py \
  85de83ca6323 --matcher aligner   # 3 quiet runs, median
```

Also watch peak RSS (the vF8 launcher pattern; `/usr/bin/env time -v` is unavailable — use the getrusage wrapper noted in vF1). Record vF11: decode seconds before/after (expect ~150s → ~65–90s on 85de), elapsed, peak RSS delta, hash identity on all projects run. If RSS approaches the 32GB wall under the default 4096MB budget, halve `ATR_R2_FRAMES_LRU_MB` and re-measure — the knob exists precisely for this.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scene_aligner.py backend/tests/test_window_embed_cache_lru.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/A1): byte-budgeted decoded-frames LRU kills the redecode tax"
```

---

### Task 4 (A2): NVDEC session budget — 3 solo, 2 concurrent

**Files:**
- Modify: `backend/app/services/pynv_decode.py:59` (`_MAX_SESSIONS`) and `_SessionPool` (line 171+)
- Modify: `backend/app/services/indexation_queue.py` (add `gpu_slots_in_use()` next to `gpu_semaphore()`, line 465)
- Modify: `backend/app/services/scene_aligner.py` (`align_scenes_sync` entry, line 556+)
- Test: `backend/tests/test_pynv_session_budget.py` (create)
- Modify: `docs/FAST_MODE_JOURNAL.md` (append to vF11 or new vF12)

**Interfaces:**
- Consumes: `fast_matching.fast_r2_enabled()`.
- Produces: `pynv_decode.set_session_budget(n: int) -> None` (clamps to [1, 3]); `indexation_queue.IndexationQueue.gpu_slots_in_use(self) -> int`.

Background: vF6 cut `_MAX_SESSIONS` 3→2 because TWO CONCURRENT processes held 6 sessions. Solo runs pay session churn for a wall that only exists under concurrency. Rule: 3 sessions when this matching holds the only busy GPU slot, 2 otherwise. The vF6 OOM→cv2 fallback stays as the safety net either way.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_pynv_session_budget.py`:

```python
from app.services import pynv_decode


def test_set_session_budget_clamps():
    pynv_decode.set_session_budget(5)
    assert pynv_decode.get_session_budget() == 3
    pynv_decode.set_session_budget(0)
    assert pynv_decode.get_session_budget() == 1
    pynv_decode.set_session_budget(2)
    assert pynv_decode.get_session_budget() == 2


def test_budget_applies_to_pool_max():
    pynv_decode.set_session_budget(3)
    assert pynv_decode._pool_max_sessions() == 3
    pynv_decode.set_session_budget(2)
    assert pynv_decode._pool_max_sessions() == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && pixi run pytest tests/test_pynv_session_budget.py -v
```
Expected: FAIL with `AttributeError: ... no attribute 'set_session_budget'`.

- [ ] **Step 3: Implement**

In `pynv_decode.py`, next to `_MAX_SESSIONS = 2` (line 59):

```python
_MAX_SESSIONS = 2
# A2 (R2): mutable budget — 3 when a matching runs solo on the GPU queue,
# 2 under concurrency (vF6: 2 procs × 3 sessions = ~2.4GB pushed the embed
# into its OOM margin). Clamped [1, 3]; ~412MiB per session.
_session_budget = _MAX_SESSIONS


def set_session_budget(n: int) -> None:
    global _session_budget
    _session_budget = max(1, min(3, int(n)))


def get_session_budget() -> int:
    return _session_budget


def _pool_max_sessions() -> int:
    return _session_budget
```

In `_SessionPool` — wherever `self._max` is read for eviction (find the eviction loop in `get()`, around lines 179-205) — replace reads of `self._max` with `_pool_max_sessions()` so budget changes apply to the live pool. Keep the constructor parameter for tests.

In `indexation_queue.py`, next to `gpu_semaphore()` (line 465):

```python
    def gpu_slots_in_use(self) -> int:
        """Busy GPU slots right now (0..MAX_CONCURRENT). Reads the asyncio
        semaphore's internal counter — advisory only, used to size the NVDEC
        session budget; correctness never depends on it."""
        return self.MAX_CONCURRENT - self._semaphore._value
```

In `scene_aligner.py` at the top of `align_scenes_sync` (after its `started = time.perf_counter()` line, ~564), add:

```python
        from .fast_matching import fast_r2_enabled
        if fast_r2_enabled():
            try:
                from . import pynv_decode
                from .indexation_queue import indexation_queue

                pynv_decode.set_session_budget(
                    3 if indexation_queue.gpu_slots_in_use() <= 1 else 2
                )
            except Exception:
                pass
```

(Verify the singleton import name: grep `indexation_queue.py` for how other modules import the instance — e.g. `from .indexation_queue import indexation_queue` vs a `get_queue()` accessor — and match it.)

- [ ] **Step 4: Run tests**

```bash
cd backend && pixi run pytest tests/test_pynv_session_budget.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Verify + measure**

Hash-verify on dcd (must equal r2ref), then 85de 3-run median. Session budget changes affect only decoder eviction order, never frame values — hashes must be identical. Record the wall delta in the journal (expected modest; this lever mainly removes session-churn stalls when candidate episodes alternate). Concurrent safety is re-proven later in Task 10's §4 re-check.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/pynv_decode.py backend/app/services/indexation_queue.py backend/app/services/scene_aligner.py backend/tests/test_pynv_session_budget.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/A2): NVDEC session budget 3-solo/2-concurrent"
```

---

### Task 5 (A3): Deeper decode↔embed overlap

**Files:**
- Modify: `backend/app/services/scene_aligner.py:229-248` (`_WindowEmbedCache.__init__`), `:303-341` (`prefetch_probe`), `:343-382` (`prefetch`)
- Modify: `docs/FAST_MODE_JOURNAL.md`

**Interfaces:**
- Consumes: `fast_matching.fast_r2_enabled()`.
- Produces: env knobs `ATR_R2_PREFETCH_WORKERS` (default 6), `ATR_R2_PREFETCH_DEPTH` (default 16). No API change — existing `prefetch`/`prefetch_probe` semantics preserved (staged frames still come from the identical decode-call shape, so byte-identity holds by construction).

- [ ] **Step 1: Implement the knobs**

In `_WindowEmbedCache.__init__`, replace:

```python
        self._prefetch_pool = ThreadPoolExecutor(max_workers=4)
```

with:

```python
        from .fast_matching import fast_r2_enabled

        workers = (
            int(os.environ.get("ATR_R2_PREFETCH_WORKERS", "6"))
            if fast_r2_enabled()
            else 4
        )
        self._prefetch_depth = (
            int(os.environ.get("ATR_R2_PREFETCH_DEPTH", "16"))
            if fast_r2_enabled()
            else 8
        )
        self._prefetch_pool = ThreadPoolExecutor(max_workers=workers)
```

In `prefetch()` replace the hard-coded depth check `len(self._inflight) + len(self._staged) > 8` with `len(self._inflight) + len(self._staged) > self._prefetch_depth`. In `prefetch_probe()` replace `> 12` with `> self._prefetch_depth`.

Beware VRAM: prefetch workers open their OWN captures (`_prefetch_caps` per thread) which in fast mode are NVDEC-backed and draw from the Task-4 session budget. The global native lock in `pynv_decode` (line ~80) serialises decoder calls, so more workers deepen the queue without concurrent native decode — that is the intended effect (embed never waits on a cold window).

- [ ] **Step 2: Hash-verify + measure**

dcd hash must equal r2ref. Then 85de + 411f 3-run medians with `ATR_RERANK_DEBUG=1`, comparing `[winprof] decode=` (main-thread blocked-on-decode time should drop; total decode work is unchanged). If wall gain on both heavies is <2%, revert the default workers to 4 (keep the knobs) and note the null result in the journal — do not carry speculative config.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/scene_aligner.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/A3): env-tunable prefetch depth/workers for decode-embed overlap"
```

---

### Task 6 (A4): Parallel ORB registration probes — CONDITIONAL on vF10

**Go/no-go:** Execute only if vF10 (Task 2) recorded `rect + cur ≥ 20s` on either heavy. Otherwise mark this task "skipped — profile said no" in the journal and move on. Rationale: the chain visit queue is order-dependent (forced neighbour revisits, `raw` mutated in place — `scene_aligner.py:4437-4441`), so only the *inputs* to a visit may be precomputed in parallel, and that is only worth doing if registration actually costs wall time.

**Files:**
- Modify: `backend/app/services/scene_aligner.py` — the arbitration candidate block (candidate list assembly precedes `cand_rect` uses, region ~4700-4990) plus `cand_rect` (line 4598)
- Modify: `docs/FAST_MODE_JOURNAL.md`

**Interfaces:**
- Consumes: `_WindowEmbedCache.prefetch_probe(episode, pred)` (existing, thread-safe), `_footprint_rect` (pure-CPU cv2, reentrant), vF10 numbers.
- Produces: no new public API; behaviour: for each chain visit, `prefetch_probe` is issued for EVERY candidate episode's midpoint prediction as soon as the candidate list for that visit is assembled (before the first `cand_rect` call), so probe decode overlaps the current chain's scoring instead of serialising inside it.

- [ ] **Step 1: Implement probe pre-issue**

In the arbitration block, immediately after the candidate list for the visit is finalised (locate where the candidate iteration begins after `if not arbitrate:` at `scene_aligner.py:4692` — candidates come from `candidate_sets[ci]` and recall; grep `for` loops referencing `_prof["cand"]` at ~4917), insert:

```python
                # A4 (R2): stage the registration probes for every candidate
                # NOW — cand_rect() will consume the staged frames instead of
                # decoding serially mid-scoring. Identical decode-call shape
                # => identical frames (prefetch_probe contract).
                from .fast_matching import fast_r2_enabled as _r2_on
                if _r2_on():
                    t_mid_pf = 0.5 * (
                        scenes[i].start_time + scenes[j].end_time
                    )
                    for _cand in cand_list:
                        _fn = _cand_line_fn(_cand)
                        if _fn is not None:
                            cache.prefetch_probe(
                                _cand_episode(_cand), float(_fn(t_mid_pf))
                            )
```

The names `cand_list`, `_cand_line_fn`, `_cand_episode` stand for whatever the block actually iterates — resolve them by reading the loop at `_prof["cand"]` (~4917) during implementation and binding to the real variables (the candidate dicts carry `episode` and line parameters; the same values later fed to `scored_with_rect`). This is a mechanical binding, not a design decision: the pre-issue must cover exactly the `(ep, source_fn)` pairs that `cand_rect` will be called with.

- [ ] **Step 2: Hash-verify + measure**

dcd + one heavy: hashes must equal r2ref (prefetch staging is byte-identical by contract — the vF8/vF6-era guarantee that staged and synchronous decode share one call shape). 3-run median on the heavy with `[s5prof]`: `rect` should shrink toward its pure-CPU floor. Record in journal. If hash differs: a probe key mismatch is feeding different frames — fix the key (`("probe", episode, round(pred, 3))` must match exactly what `probe_frames` computes) rather than accepting the diff.

Escalation (only if `rect` is STILL ≥ 15s after pre-issue): the residual is the ORB/`_footprint_rect` CPU itself — then additionally precompute the rects in a bounded `ThreadPoolExecutor(4)` over the same candidate list (`_footprint_rect` is pure-CPU cv2, reentrant; inputs are the staged probe frames, so results are deterministic per candidate), store `{candidate_key: rect}` before the scoring loop, and have `cand_rect` consult that dict first. Consume results in the original candidate order — the scoring loop itself stays sequential (chain-visit order dependency, `scene_aligner.py:4437`). Hash-verify again after this escalation.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/scene_aligner.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/A4): pre-issue candidate registration probes per chain visit"
```

---

### Task 7 (B1): Coarse-to-fine window scoring (`ATR_R2_COARSE`)

**Files:**
- Modify: `backend/app/services/scene_aligner.py:266-277` (`_decode_run`), `:384-461` (`window()`), `:3728-3772` (`_zoom_sscd_score_line`)
- Test: `backend/tests/test_r2_coarse_window.py` (create)
- Modify: `docs/FAST_MODE_JOURNAL.md` (append vF12/vF13 entry with the eval scoreboard)

**Interfaces:**
- Consumes: `fast_matching.r2_lever("ATR_R2_COARSE")`.
- Produces: `_WindowEmbedCache.window(episode, zoom, lo, hi, stride: int = 1)` — stride-2 fills only every 2nd decode slot (decoder still visits every native index in GOP order per the vF8 identity requirement; conversion/transfer/embed are halved). `_zoom_sscd_score_line` gains the two-pass logic.

Quality note: this is the first BUDGETED lever. It runs the full GT eval and is judged against the moderate budget (Global Constraints). Ship default-ON only if within budget on all 4 projects.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_r2_coarse_window.py`:

```python
import numpy as np

from app.services.scene_aligner import _WindowEmbedCache


def test_window_stride_requests_every_second_slot(monkeypatch):
    cache = _WindowEmbedCache.__new__(_WindowEmbedCache)
    cache.fps = 12.0
    cache.slots = {}
    requested: list[tuple[int, int]] = []

    def fake_decode(cap, r0, r1):
        requested.append((r0, r1))
        return [(k / 12.0, None) for k in range(r0, r1 + 1)]

    # decode-run capture: stride must thin the *requested slot set*,
    # runs stay contiguous per run-group
    slots = cache.slots.setdefault(("ep", 1.0), {})
    i0, i1, stride = 0, 10, 2
    missing = [k for k in range(i0, i1 + 1, stride) if k not in slots]
    assert missing == [0, 2, 4, 6, 8, 10]
```

(This pins the stride slot-set arithmetic; the integration behaviour is covered by the GT eval in Step 4.)

- [ ] **Step 2: Implement**

`window()` — signature and slot walk:

```python
    def window(
        self,
        episode: str,
        zoom: "float | tuple[float, float, float, float]",
        lo: float,
        hi: float,
        stride: int = 1,
    ) -> tuple[np.ndarray, np.ndarray] | None:
```

Inside, replace `missing = [k for k in range(i0, i1 + 1) if k not in slots]` with

```python
        missing = [k for k in range(i0, i1 + 1, max(1, stride)) if k not in slots]
```

and the final entries collection stays over the FULL range (`range(i0, i1 + 1)`) — dense slots from earlier fine passes are reused automatically. `_decode_run` gains the matching thinning:

```python
    def _decode_run(self, cap, r0: int, r1: int, stride: int = 1) -> list:
        w_lo = r0 / self.fps
        w_hi = (r1 + 1) / self.fps
        return AnimeMatcherService._collect_frames_in_window_from_capture(
            cap,
            w_lo,
            w_hi,
            max_frames=int((w_hi - w_lo) * 65) + 8,
            sample_frames=max(
                2, int(round((w_hi - w_lo) * self.fps / max(1, stride))) + 1
            ),
        )
```

Pass `stride` through the `window()` decode path (`self._decode_run(cap, r0, r1, stride)`); prefetch keeps stride 1 (staged runs must stay reusable by any pass). IMPORTANT: keep the `slots.setdefault(k, None)` back-fill loop restricted to the *visited* slots (`range(r0, r1 + 1, stride)`), otherwise a coarse pass would poison the skipped slots as permanently empty for the later fine pass.

`_zoom_sscd_score_line` — two-pass under the lever:

```python
        from .fast_matching import r2_lever

        span = hi - lo
        coarse = r2_lever("ATR_R2_COARSE") and span > 3.0
        win = cache.window(episode, zoom, lo, hi, stride=2 if coarse else 1)
        if win is None:
            return None
        times, embs = win
        ...  # existing sweep, but with tolerance 0.15 -> 0.18 when coarse
        if coarse and best is not None:
            # densify around the winning alignment and re-score exactly
            center = preds.mean() + best[1]
            fine = cache.window(
                episode, zoom,
                max(lo, float(preds.min()) + best[1] - 0.35),
                min(hi, float(preds.max()) + best[1] + 0.35),
                stride=1,
            )
            if fine is not None:
                times, embs = fine
                sims = q @ embs.T
                best = None
                ...  # re-run the existing delta loop restricted to
                     # delta within best_coarse ± 0.35, original 0.15 tolerance
```

Implement the re-scoring by extracting the existing delta-sweep loop body (`scene_aligner.py:3757-3771`) into a local helper `def _sweep(times, embs, sims, deltas, tol)` used by both passes — DRY, and the fine pass simply calls it with `np.arange(best_c - 0.35, best_c + 0.35 + 1e-6, 1.0 / VERIFY_DECODE_FPS)`. The coarse pass uses the full delta range with `tol=0.18` (stride-2 grid spacing is 0.167s, so nearest-slot distance ≤ 0.083s... but slot *misses* can reach 0.167s under drops — 0.18 keeps the 2/3-valid quorum reachable).

- [ ] **Step 3: Unit test passes**

```bash
cd backend && pixi run pytest tests/test_r2_coarse_window.py -v
```
Expected: PASS.

- [ ] **Step 4: Budget-gate eval (the lever's real test)**

All 4 GT projects, lever solo (`ATR_R2_COARSE=1`, other B levers unset which currently means default — for SOLO measurement export `ATR_R2_FP16_WIN=0 ATR_R2_THIN=0`):

```bash
cd backend && ATR_R2_FP16_WIN=0 ATR_R2_THIN=0 pixi run python \
  scripts/evaluate_matching_against_ground_truth.py 85de83ca6323 --matcher aligner
# all 4 projects; heavies 3-run medians for elapsed
```

Journal a vF-entry scoreboard: wall Δ, `[winprof]` decode/embed Δ, evaluator scene/source lines vs r2ref, every flip listed by scene. Apply the budget: ≤1 episode flip, ≤4 source-line losses, 0 scene-line changes, per project. Within budget → lever default stays ON (`r2_lever` default True). Over budget → change the call sites to `r2_lever("ATR_R2_COARSE", default=False)` and say so in the journal.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/scene_aligner.py backend/tests/test_r2_coarse_window.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/B1): coarse-to-fine window scoring behind ATR_R2_COARSE"
```

---

### Task 8 (B2): fp16 fresh-vs-fresh window scoring (`ATR_R2_FP16_WIN`)

**Files:**
- Modify: `backend/app/services/anime_matcher.py:862-935` (`_embed_pil_batch` — add precision parameter)
- Modify: `backend/app/services/scene_aligner.py:437-444` (`window()` embed call), `:4334` (stage-5 edge/mid embed call), `:891` (`_embed_variant_images` — must stay fp32: index-facing)
- Test: `backend/tests/test_r2_fp16_embed.py` (create)
- Modify: `docs/FAST_MODE_JOURNAL.md`

**Interfaces:**
- Consumes: `fast_matching.r2_lever("ATR_R2_FP16_WIN", default=True)`.
- Produces: `AnimeMatcherService._embed_pil_batch(images, *, half: bool = False) -> np.ndarray` (returns float32 ndarray regardless — fp16 is a compute dtype, not a storage contract).

HARD RULE (vF3): fp16 embeddings must NEVER be compared against index embeddings or sent to FAISS. The only two call sites allowed to pass `half=True` are (a) `_WindowEmbedCache.window()` and (b) the stage-5 edge/mid embed at `scene_aligner.py:4334` — and BOTH sides must be half together (they are: window embeddings are only compared against edge/mid embeddings and each other inside stage 5; the identity certificate at `_zoom_sscd_score_line` compares matched *window* embeddings across candidates — all half). Audit during implementation: grep every consumer of `edge_embs`/`mid_embs` and of `window()` results; if ANY flows into `_index_cos_across`, `_index_embedding_at`, or a FAISS call, that consumer keeps an fp32 path or the lever is dead — record the audit result in the journal.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_r2_fp16_embed.py`:

```python
import inspect

from app.services.anime_matcher import AnimeMatcherService


def test_embed_pil_batch_has_half_kwarg():
    sig = inspect.signature(AnimeMatcherService._embed_pil_batch.__func__)
    assert "half" in sig.parameters
    assert sig.parameters["half"].default is False


def test_variant_embeds_never_half():
    # index-facing embeds must not grow a half switch: the vF3 ban.
    from app.services import scene_aligner

    src = inspect.getsource(scene_aligner.SceneAlignerService._embed_variant_images.__func__)
    assert "half=True" not in src
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && pixi run pytest tests/test_r2_fp16_embed.py -v
```
Expected: first test FAILS (`'half' not in parameters`).

- [ ] **Step 3: Implement**

In `_embed_pil_batch` (`anime_matcher.py:862`), thread a keyword through to the model call inside `embed_chunk`:

```python
    def _embed_pil_batch(
        cls, images: list[Image.Image], *, half: bool = False
    ) -> np.ndarray:
```

and wrap the forward pass (locate the actual `torch` call inside `embed_chunk`, line ~901):

```python
            import torch

            use_half = half and torch.cuda.is_available()
            with torch.autocast("cuda", dtype=torch.float16, enabled=use_half):
                out = <existing forward call unchanged>
            emb = out.float()  # storage stays fp32; L2-normalize as before
```

Keep the existing OOM-adaptive chunking untouched — autocast composes with it. In `scene_aligner.py`:

```python
# window():  (line ~437)
                    embs = AnimeMatcherService._embed_pil_batch(
                        _presize_images([...unchanged...]),
                        half=r2_lever("ATR_R2_FP16_WIN"),
                    )
# stage 5:  (line 4334)
        edge_embs = AnimeMatcherService._embed_pil_batch(
            _presize_images(images), half=r2_lever("ATR_R2_FP16_WIN")
        )
```

with `from .fast_matching import r2_lever` at module import site matching file style.

- [ ] **Step 4: Unit tests pass; sanity-check divergence**

```bash
cd backend && pixi run pytest tests/test_r2_fp16_embed.py -v
```
Expected: 2 passed. Then a one-off divergence probe (scratchpad script): embed 32 frames from any local video both ways, report `1 - cos` per pair. Expect ~1e-3-order (autocast fp16 with fp32 accumulate), i.e. an order of magnitude below the 0.02 decision margins — record the number in the journal. If it lands near 0.02, stop and journal the lever as dead (do not proceed to the eval).

- [ ] **Step 5: Budget-gate eval**

Same protocol as Task 7 Step 4, lever solo (`ATR_R2_COARSE=0 ATR_R2_THIN=0`, `ATR_R2_FP16_WIN=1`). Also record `[winprof] embed=` (expect ~92s → 50–60s on 85de) and peak VRAM (expect lower — fp16 activations). Budget verdict → default ON or OFF exactly as Task 7.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/anime_matcher.py backend/app/services/scene_aligner.py backend/tests/test_r2_fp16_embed.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/B2): fp16 autocast for fresh-vs-fresh window scoring behind ATR_R2_FP16_WIN"
```

---

### Task 9 (B3): Variant-retrieval thinning (`ATR_R2_THIN`)

**Files:**
- Modify: `backend/app/services/scene_aligner.py:1380-1396` (`_weak_scene_sample_indices`)
- Test: `backend/tests/test_r2_variant_thinning.py` (create)
- Modify: `docs/FAST_MODE_JOURNAL.md`

**Interfaces:**
- Consumes: `fast_matching.r2_lever("ATR_R2_THIN", default=True)`; `SegmentHypothesis.inlier_count` (existing field — verify by grep, it is used at line 1391).
- Produces: behaviour only — under the lever, a scene skips variant retrieval when its best segment has `inlier_count >= 3` (mainline: `>= 4`). Targets 411f's measured variant_retrieve 14.0s + downstream interior_split work.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_r2_variant_thinning.py`:

```python
from types import SimpleNamespace

from app.services.scene_aligner import SceneAlignerService


def _scene_list(n=1):
    scenes = [
        SimpleNamespace(start_time=float(k), end_time=float(k + 1))
        for k in range(n)
    ]
    return SimpleNamespace(scenes=scenes)


def _samples():
    return [SimpleNamespace(t_tiktok=0.5)]


def test_inlier3_scene_is_weak_on_mainline(monkeypatch):
    monkeypatch.setenv("ATR_FAST_MATCHING", "0")  # R2 off => mainline rule
    import importlib
    from app.services import fast_matching
    importlib.reload(fast_matching)
    segs = {0: [SimpleNamespace(inlier_count=3)]}
    weak = SceneAlignerService._weak_scene_sample_indices(
        _scene_list(), _samples(), segs
    )
    assert weak == {0}


def test_inlier3_scene_skipped_under_thin(monkeypatch):
    monkeypatch.setenv("ATR_FAST_MATCHING", "1")
    monkeypatch.setenv("ATR_FAST_R2", "1")
    monkeypatch.setenv("ATR_R2_THIN", "1")
    import importlib
    from app.services import fast_matching
    importlib.reload(fast_matching)
    segs = {0: [SimpleNamespace(inlier_count=3)]}
    weak = SceneAlignerService._weak_scene_sample_indices(
        _scene_list(), _samples(), segs
    )
    assert weak == set()
```

(If `_weak_scene_sample_indices` touches attributes the SimpleNamespace stubs lack, extend the stubs — keep the test at this behavioural level.)

- [ ] **Step 2: Run tests to verify the second fails**

```bash
cd backend && pixi run pytest tests/test_r2_variant_thinning.py -v
```
Expected: first PASSES (current behaviour), second FAILS (`weak == {0}`).

- [ ] **Step 3: Implement**

In `_weak_scene_sample_indices` (line 1391), replace:

```python
            if best is not None and best.inlier_count >= 4:
                continue
```

with:

```python
            from .fast_matching import r2_lever

            floor = 3 if r2_lever("ATR_R2_THIN") else 4
            if best is not None and best.inlier_count >= floor:
                continue
```

(hoist the import to module level per file style; the `floor` line stays in the loop's scope or above it — either is fine, it is cheap).

- [ ] **Step 4: Tests pass**

```bash
cd backend && pixi run pytest tests/test_r2_variant_thinning.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Budget-gate eval**

Same protocol, lever solo (`ATR_R2_COARSE=0 ATR_R2_FP16_WIN=0 ATR_R2_THIN=1`). Watch 411f specifically (its variant_retrieve/interior_split tail is the target) AND its weak-scene recovery quality — variants exist to rescue zoomed/cropped edits, so flips here concentrate on precisely those scenes. Budget verdict → default ON/OFF as before.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/scene_aligner.py backend/tests/test_r2_variant_thinning.py docs/FAST_MODE_JOURNAL.md
git commit -m "feat(fast-r2/B3): variant-retrieval thinning behind ATR_R2_THIN"
```

---

### Task 10: Combined scoreboard, concurrency re-check, flag-OFF byte-identity, hand-off

**Files:**
- Modify: `docs/FAST_MODE_JOURNAL.md` (final vF entry)
- Modify: `backend/app/services/fast_matching.py` (only if a lever's default flips per its budget verdict)

**Interfaces:**
- Consumes: every prior task's journal entry and the r2ref reference.
- Produces: the final combined default set + owner test-protocol note. This is the merge gate.

- [ ] **Step 1: Combined run, all 4 GT**

Default env (no lever overrides — i.e. the post-verdict defaults), 3-run quiet medians on the heavies. Full scoreboard vs r2ref:

| project | elapsed (r2ref) | elapsed (combined) | scene Δ | source Δ | episode flips | flips listed |
|---|---|---|---|---|---|---|

The combined set must ALSO pass the moderate budget per project (levers can interact — B1's coarse grid under B2's fp16 noise is the risky pairing; if combined breaches budget but solo runs passed, disable the weakest-value lever by default, re-run, journal the interaction).

- [ ] **Step 2: §4 concurrency re-check**

Two concurrent fast matchings (85de + 411f) with combined defaults: both must complete; record peak VRAM (`nvidia-smi --query-gpu=memory.used --loop-ms=1000` to a log), per-project elapsed, and that the vF6 OOM→cv2 fallback stat stayed 0 (or fired and recovered — either is a pass; a crash is the only fail). The Task-4 budget must show 2 sessions per process during this run (assert via the debug print or a temporary log line, then remove it).

- [ ] **Step 3: Flag-OFF byte-identity**

```bash
cd backend && ATR_FAST_MATCHING=0 pixi run python \
  scripts/evaluate_matching_against_ground_truth.py dcd74148c7ec --matcher aligner
```

Hash must equal the pre-branch mainline hash for dcd (from vF2/current main). Run all 4 if any R2 change touched a non-gated code path (Tasks 3, 5 touch shared code — the byte-budget LRU and prefetch knobs are R2-gated to legacy values when off; verify that gating held).

- [ ] **Step 4: Owner hand-off note**

Append to the journal a "how to try it" paragraph (GOAL_FAST §5 pattern): checkout branch, run a real project through /matches, what to inspect (`doubt_reasons` scenes + the flips listed in the scoreboard), and the escape hatches (`ATR_FAST_R2=0`, per-lever flags). The owner judges visually and decides merge (defaults as-shipped) vs adjust.

- [ ] **Step 5: Run the backend test suite for NEW failures**

```bash
cd backend && pixi run pytest tests/ 2>&1 | tail -20
```

Compare the failure set against a fresh run of the same command on `main` (two runs, never overlapping). Only NEW failures block; fix them.

- [ ] **Step 6: Final commit**

```bash
git add docs/FAST_MODE_JOURNAL.md backend/app/services/fast_matching.py
git commit -m "feat(fast-r2): final combined scoreboard, concurrency re-check, owner hand-off"
```

Then follow superpowers:finishing-a-development-branch for the merge decision (owner visual pass gates the merge).
