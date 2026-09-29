# Direct pinned batch assembly — 2026-09-22

`ParallelFileIndexer._parallel_transform` now allocates a fresh pinned CPU uint8
tensor and calls `np.stack(arrays, out=batch.numpy())`. The NumPy view shares the
tensor's storage. Previously, `np.stack` allocated a pageable batch, then
`pin_memory()` allocated pinned storage and copied that complete batch again.

The parallel PIL resize and its per-frame arrays are unchanged. Every prepared
batch owns its pinned storage until its consumers release it; there is no
application buffer pool or asynchronous CPU write to a queued tensor. Shape
validation still comes from NumPy stacking, which rejects unequal resized
shapes. Empty input still raises `ValueError`.

This applies to the FFmpeg CPU/NVDEC preparation path feeding uint8 CUDA
inference. Sampling, normalization, precision, batch size, file concurrency,
prefetch and FFmpeg thread settings are unchanged. The earlier GPU/CPU lifetime
improvements remain active in both sides of this comparison.

## Isolated measurements

The probe alternates baseline and candidate in four-call blocks, reversing
block order as well. It uses batch 64, 1920×1080 RGB inputs, the production PIL
resize to 512×288, four transform workers and the application's native thread
defaults. Warmup runs are excluded; output hashes are checked outside timing.

| Operation | Baseline median | Direct pinned median | Time change |
| --- | ---: | ---: | ---: |
| Batch assembly only, 48 samples per variant | 6.515 ms | 3.153 ms | −51.6% |
| Resize and assembly, 16 samples per variant | 146.670 ms | 144.232 ms | −1.7% |
| Four concurrent producers, 12 samples per variant | 634.654 ms | 632.616 ms | −0.3% |

Assembly-only timing uses the same already-resized arrays. The concurrent probe
prepares four batches through one shared transform pool, retaining all outputs
until the four calls complete. Its time is per group of four batches. These
are preparation measurements, excluding decoding and SSCD inference; the small
differences including resize do not establish a whole-indexing speedup.

After pinned allocator warmup, Python/NumPy traced peak allocation during batch
assembly fell from **28,324,032 bytes** to **13,536 bytes**, reproducing in both
orders. This accounts for removing the **27 MiB** intermediate pageable batch.
The final pinned tensor remains 27 MiB in both variants. Tracing does not measure
PyTorch's native pinned allocator, process RSS or GPU memory. Do not interpret
this as a fixed whole-process memory reduction or multiply it by every file
worker regardless of whether assembly is active.

All prepared output hashes matched. Installed NumPy 2.5.1's `stack` implementation
adds axis views and passes `out` through to `concatenate`; it does not allocate
another full stacked result before copying to the supplied destination. The
traced allocation result independently checks that behavior in this environment.

## Full-workload comparison

All three runs completed the same 17,124 sampled frames across two Classroom of
the Elite episodes and four Gachiakuta episodes. Every file's embedding and
timestamp hashes matched across runs. There were no retries, skipped files or
decode fallbacks. Each run retained two simultaneous SSCD processes, FP32,
2 FPS, batch 64, prefetch 3, four transform threads per process, and 2 + 4 active
file workers using NVDEC. FFmpeg retained decoder threads 8 and automatic
filter/output pools (0/0). Index files and Picom settings were not changed.

| Run | Encoding seconds | Frames/s | Starting CPU / GPU temperature |
| --- | ---: | ---: | ---: |
| First baseline | 238.53 | 71.79 | 97 / 64°C |
| Direct pinned assembly | 351.53 | 48.71 | 96 / 78°C |
| Warm baseline reversal | 357.75 | 47.87 | 94 / 75°C |

The updated run had **1.77% higher throughput than the warm baseline**, within
the user's maximum 10% slowdown allowance in that comparison. However, the
unchanged baseline itself lost **33.33%** between its two runs. Against the first
baseline, the updated run was 32.15% slower; against the arithmetic mean of both
baselines, it was 18.58% slower. This sequence therefore cannot certify a strict
end-to-end performance bound or establish a whole-indexing speedup.

CPU thermal throttling was observed in all three runs, and ordinary desktop
activity was uncontrolled. Although starting GPU temperatures differed, mean
encoding GPU temperatures were approximately 84°C in all three; temperature
alone is not established as the explanation for the throughput drift.

Direct pinned assembly remains enabled based on the eliminated allocation/copy,
exact output identity, the interleaved isolated timing results and the absence
of a slowdown against the warm baseline. The four-producer stage comparison's
per-block time changes ranged from −1.44% to +2.34%, also comfortably inside the
10% budget for that stage. These measurements support the implementation choice
without claiming a universal throughput guarantee under changing conditions.

There was no meaningful whole-run memory reduction established: mean summed
process RSS was 6,107 / 6,181 / 6,233 MiB in run order. GPU allocated peaks were
unchanged at 1,938.04 MiB per worker, with 2,302 MiB reserved per worker and an
observed device-memory peak of 6,693 MiB in every run. The demonstrated memory
saving remains the temporary CPU batch during assembly. GPU computation and
the final pinned batch are unchanged.

## Validation

The full module suite passed **66 tests**. Coverage includes exact prepared
pixels and layout for full/partial/singleton batches, different aspect ratios,
four simultaneous producers, retention across subsequent batch preparation,
mismatched shape rejection and recovery, and empty input. The real SSCD test
now obtains its uint8 batch from the production pipeline and compares embeddings
bit-for-bit against the reference CPU preprocessing path. Existing CPU/NVDEC,
cancellation and memory-lifetime checks also pass.

## Artifacts

Measurement artifacts are excluded from Git:

- [Stage results](../backend/data/measurements/pinned-batches-2026-09-22/stages.json)
- [Stage probe](../backend/data/measurements/pinned-batches-2026-09-22/measure_stages.py)
- [Full-workload comparison](../backend/data/measurements/pinned-batches-2026-09-22/comparison.json)
- [Full-workload driver](../backend/data/measurements/pinned-batches-2026-09-22/run_comparison.py)

The artifact directory also retains the exact pre-change pipeline, method
selection hook, source hashes and full-workload reports. The hook only selects
the saved method in explicitly opted-in measurement worker processes; normal
application launches use direct pinned assembly.
