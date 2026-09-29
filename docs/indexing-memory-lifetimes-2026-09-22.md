# Indexing memory lifetimes — 2026-09-22

Two changes shorten the lifetime of data after its last use:

- `SSCDEmbedder.embed_uint8_hwc_batch` releases the uploaded uint8 input and
  int64 lookup indices after constructing the independent normalized tensor,
  before model inference. The same tensor operations run on the same stream.
- `ParallelFileIndexer._stream_job_batches` clears the full-resolution image
  list after transformation, before waiting to enqueue the prepared batch.
  This also covers the final partial batch. Timestamps retain their own list;
  the producer does not clear a list already owned by the queued item.

No cache flush, forced collection or CUDA synchronization was added to the
production path. Decoder threads, filter/output pools, batch size, prefetch,
file concurrency, sampling, pixel conversions, PIL resize and precision remain
unchanged. These changes do not alter the embedding/index format.
Both changes are enabled in the production path. The validation below supports
lower memory use and no observed slowdown against the warm baseline; it does
not certify a universal 10% throughput bound under changing thermal conditions.

## Isolated measurements

The GPU comparison used one SSCD model, FP32 and a fixed pinned uint8 batch of
64 frames at 512×288. After warmup, sixteen calls alternated saved baseline and
new methods in repeated baseline/new/new/baseline order.

| Observation | Baseline | GPU lifetime change |
| --- | ---: | ---: |
| Newly live tensor memory at model entry | 351 MiB | 108 MiB |
| Peak allocated GPU memory | 2,181.04 MiB | 1,938.04 MiB |
| Median batch wall time | 370.50 ms | 369.22 ms |

All output hashes matched. The **243 MiB** reduction is the 216 MiB lookup-index
tensor plus the 27 MiB uploaded uint8 batch. A caller that already owns a CUDA
input still owns that input after the call; in that case only the temporary
lookup indices become reusable early. These short timings establish no
meaningful speedup and do not represent whole-series throughput.

The CPU probe used separate processes, real PIL resizing and pinned batch
preparation for 64 full-resolution 1920×1080 images, observing memory at the
queue handoff before inference. The prepared tensors and timestamps matched.

| Observation at queue handoff | Baseline | Frame lifetime change |
| --- | ---: | ---: |
| Process RSS growth above initialization | 546.37 MiB | 46.85 MiB |
| Original images still alive | 64 | 1 |
| Prepared uint8 tensor size | 27 MiB | 27 MiB |

The suspended extractor can retain its most recently yielded image; the
producer no longer retains the other 63. The separate regression tests keep
the queue full to verify this lifetime during actual backpressure, for both
full/partial batches and cancellation. This is approximately **500 MiB less
RSS growth per waiting worker in this probe**, not a guaranteed fixed reduction
in an entire indexing run. Allocator behavior and queue occupancy matter.

## Full-workload results

All four runs completed all 17,124 frames without retries, skips or decode
fallback. All six files' embedding and timestamp hashes matched across runs.

| Run | Encoding seconds | Frames/s | Allocated GPU peak per worker | Observed total GPU memory peak | Mean summed process RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| Initially cooler baseline | 210.74 | 81.26 | 2,181.04 MiB | 7,811 MiB | 6,168.59 MiB |
| Both changes, first run | 308.70 | 55.47 | 1,938.04 MiB | 6,693 MiB | 6,166.85 MiB |
| Both changes, repeat | 328.96 | 52.05 | 1,938.04 MiB | 6,693 MiB | 6,216.31 MiB |
| Warm baseline | 358.78 | 47.73 | 2,181.04 MiB | 7,811 MiB | 6,252.24 MiB |

The GPU memory result reproduced exactly on reversal: **243 MiB less allocated
peak per worker** and **1,118 MiB less observed total GPU memory** with the
changes. Baseline peak reserved allocations were 2,700 / 3,022 MiB for Classroom
/ Gachiakuta; both updated runs used 2,302 / 2,302 MiB. The reduction therefore
also affected allocator reservations on this workload.

The whole-run CPU RSS measurements did **not** reproduce a large overall RAM
reduction. Releasing images at a blocked handoff reduces live memory in that
situation; an entire run also includes frame accumulation, transformations,
allocator retention and shared process mappings. Do not multiply the isolated
500 MiB result by six and claim that as a measured whole-run RAM saving.

Throughput needs a separate interpretation. The two updated runs exceeded the
warm baseline by 16.22% and 9.06%, so neither showed a slowdown in that warm
comparison. But the unchanged baseline itself lost **41.26%** between the
initial and final run. Starting CPU/GPU temperatures were 77/61°C, 96/74°C,
90/74°C and 98/75°C respectively; normal desktop work was not controlled.
The arithmetic mean across both baseline runs was 64.49 FPS versus 53.76 FPS
for the changes (16.64% lower). Pooling those differently conditioned trials
cannot establish a causal code slowdown or certify the 10% allowance.

The decision to retain the changes rests on exact output identity, unchanged
computation/concurrency, isolated embedding-time parity, the reproduced memory
reductions and no observed slowdown against the warm baseline. A numerical
speedup or a strict end-to-end performance guarantee would require more stable
test conditions. These memory changes are not claimed to replace the blur fix.

## Validation and comparison method

The full module suite passed **60 tests**. All six new lifetime checks were
also run against the saved pre-change methods and failed at the intended
memory assertions: two GPU allocation checks and four image-retention checks.
The tests verify prepared pixels/timestamps, caller-owned GPU inputs, partial
batches, cancellation, CPU/NVDEC output identity and existing embedding behavior.

Saved baseline methods are substituted only inside measurement worker
processes by the local comparison harness. Each worker records the selected
variant and source paths in its event log. Normal application launches use
the updated code. The FFmpeg thread-pool experiment remains disabled in both
sides of this comparison (`filter=0`, `output=0`, existing decoder limit=8).

The full-workload comparison uses the same two Classroom of the Elite episodes
and four Gachiakuta episodes, 17,124 sampled frames per run, 2 FPS, NVDEC, FP32,
batch 64, prefetch 3 and 2 + 4 active file workers. Index files are not modified.
Thirty seconds of rest plus the normal idle observation precedes each run;
this is not a claim of thermal recovery. Ordinary desktop activity continues.

PyTorch allocator measurements distinguish live tensor memory from reserved
memory ([allocator documentation](https://docs.pytorch.org/docs/2.8/notes/cuda.html#memory-management)).
Early release enables reuse inside the worker; it need not immediately
reduce `nvidia-smi` usage. Similarly, process RSS can include retained allocator
storage, and summing process RSS can count shared mappings more than once.

## Local artifacts

Artifacts are intentionally excluded from Git:

- [GPU stage measurements](../backend/data/measurements/memory-lifetimes-2026-09-22/stage-gpu.json)
- [Baseline frame handoff](../backend/data/measurements/memory-lifetimes-2026-09-22/stage-baseline-frames.json)
- [Updated frame handoff](../backend/data/measurements/memory-lifetimes-2026-09-22/stage-frames-frames.json)
- [Full-workload comparison](../backend/data/measurements/memory-lifetimes-2026-09-22/comparison.json)
- [Comparison driver](../backend/data/measurements/memory-lifetimes-2026-09-22/run_comparison.py)
- [Stage measurement script](../backend/data/measurements/memory-lifetimes-2026-09-22/measure_stages.py)
- [Analysis script](../backend/data/measurements/memory-lifetimes-2026-09-22/analyze_comparison.py)

The same directory contains baseline source snapshots, source hashes, cooling
observations, raw telemetry, worker events and per-attempt output hashes.
