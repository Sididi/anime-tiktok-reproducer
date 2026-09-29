# Final indexing decisions — 2026-09-29

The three memory optimizations are part of normal indexing, with no experiment
switches. FFmpeg filter/output thread caps were rejected and removed. The
confirmed desktop remedy, disabling Picom background blur while preserving
transparency, has also been applied to the user's actual configuration.

| Decision | Production behavior | Evidence |
| --- | --- | --- |
| Keep early release of GPU temporaries | Release uploaded uint8 data and lookup indices before inference | 243 MiB lower allocated GPU peak per worker in the measured batch-64 workload; exact embeddings |
| Keep early release of original frames | Clear full-resolution images after transformation, before queue backpressure | Lifetime tests cover full/partial batches and cancellation; reduced live memory at a blocked handoff |
| Keep direct pinned batch assembly | Stack resized uint8 pixels into fresh pinned CPU storage | Removes a 27 MiB temporary and copy per full 512×288 batch; assembly median 6.51 → 3.15 ms; exact pixels |
| Keep automatic FFmpeg filter/output pools | No filter or output thread override is emitted | The 8/1 experiment reduced thread counts but established no reliable responsiveness or throughput advantage |
| Keep Picom transparency with blur disabled | `blur-method = "none";` | The user confirmed blur as the practical external-display lag trigger |

CUDA preparation always uses the uint8/pinned path. The normal CLI factory used
by both backend `index` and `update` subprocesses constructs `ParallelFileIndexer`
directly. New indexing subprocesses pick up these changes without an experiment
environment or a backend restart.

Batch size 64, prefetch 3, four transform workers, existing file concurrency,
FP32 inference, sampling, PIL resizing and normalization remain unchanged.
Automatic decoding still selects the verified NVDEC path when available and
retains the CPU fallback. The existing eight-thread input decoder default and
its operational override remain; the removed overrides only concerned the
experimental filter/output pools.

## Cleanup

- Removed `ANIME_SEARCHER_FILTER_THREADS` and `ANIME_SEARCHER_OUTPUT_THREADS`
  parsing, constants and FFmpeg option builders from production. Old values
  left in a launcher environment have no effect.
- Removed the unused `PipelinedEmbedder`, `AsyncDataLoader` and
  `OptimizedEmbedder` implementations after checking for callers throughout
  the repository. `ParallelFileIndexer` is the indexing implementation.
- Removed the CUDA capability fallback to the older float32 CPU preparation
  path; the actual SSCD embedder supplies the uint8 method.
- Removed broad constructor `TypeError` fallbacks that silently dropped
  precision or decoder arguments. Real initialization errors now propagate.
- Replaced obsolete cap-comparison tests with checks for the final thread
  policy, including launch environments retaining old experiment values.
- Updated current instructions; previous comparison reports are marked as
  historical where their experiment code no longer exists.

The shared worker policy, useful benchmark/measurement commands and correctness
tests remain. They verify actual production settings and retain diagnostic
value. Historical measurement artifacts remain outside application imports and
are excluded from Git; no model, media or index data was removed or rebuilt.

## Applied desktop configuration

`/home/sid/.config/picom/picom.conf` now sets `blur-method = "none";`.
Opacity rules, GLX, VSync, frame pacing, synchronization and shadows were
preserved. The running `picom -b` process received its documented `SIGUSR1`
reinitialization signal, remained running, and retained the X compositor
selection. The existing i3 startup command uses this saved configuration.

The original configuration and reload record are saved under
`backend/data/measurements/indexing-finalization-2026-09-29/`.

## Validation and performance limits

The complete searcher suite passed **61 tests**, including a new integration
test through the real production factory. It verifies the standard batch and
prefetch settings, FP32, actual delivery of pinned uint8 batches to the SSCD
embedder, and exact embeddings/timestamps against reference CPU preprocessing.
The existing lifetime, concurrent batch ownership, CPU/NVDEC identity and decode
fallback tests pass. **16 backend indexing/queue/preflight tests** also passed.

This finalization removes experiments and dead code without introducing a new
inference algorithm, changing concurrency or reducing sampling/precision.
Previous full-workload comparisons verified all 17,124 frame outputs per run.
Those sustained timing results were variable: the direct-pinned run measured
48.71 FPS against a warm baseline of 47.87 FPS, but the same baseline measured
71.79 FPS earlier. The retained changes have direct allocation/lifetime evidence
and stable isolated preparation timings; a strict universal 10% end-to-end
throughput bound was not established. No whole-indexing speedup or fixed
whole-process RAM reduction is claimed.

Detailed evidence: [GPU/CPU lifetimes](indexing-memory-lifetimes-2026-09-22.md),
[pinned batches](indexing-pinned-batches-2026-09-22.md),
[rejected FFmpeg caps](ffmpeg-thread-pools-2026-09-22.md), and
[desktop findings](indexing-findings-2026-09-21.md).
