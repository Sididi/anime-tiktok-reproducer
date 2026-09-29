# Encoding responsiveness: reproduced baseline, 2026-09-21

**Final decision (2026-09-29):** the FFmpeg filter/output thread experiment was
removed after the [four-run comparison](ffmpeg-thread-pools-2026-09-22.md) showed
no reliable benefit. Automatic pools remain in use. The three memory
optimizations are always active, and Picom's saved configuration now disables
blur while retaining transparency. See [final decisions](indexing-final-decisions-2026-09-29.md).

The sustained measurement reproduced the user's usual desktop lag. Both
indexing jobs completed successfully, with no CUDA OOM, retry, skipped file or
decoder fallback. The user described the desktop as **“About the usual lag.”**
No performance tuning or change to the production embedding operations was
applied.

**Practical finding confirmed by the user on 2026-09-22:** disabling Picom's
`dual_kawase` blur while keeping compositing and transparency enabled improved
external-screen responsiveness in the guided comparison. The user confirmed,
"Yes it was blur." The recommended setting is `blur-method = "none";`, retaining
the existing opacity rules, GLX backend, VSync, normal frame pacing and shadows.
No indexing algorithm or resource settings were changed to obtain this result.
The precise driver/display blocking mechanism has not been traced. The manual
setting changes were not timestamped, so these runs do not establish a
controlled throughput difference or a measured speed improvement.

**Clarification on 2026-09-22:** the user confirms that the lag affects only
the external monitor; the integrated panel works normally. This supersedes
earlier descriptions of both monitors lagging. XSync measures a shared X-server
round trip and does not establish which physical monitor is affected.
Read-only display inspection found the integrated `eDP-2` panel on Intel at
2560×1600/240 Hz and external `DP-1-0` on `NVIDIA-G0` at 1920×1080/240 Hz,
with `PRIME Synchronization: 1`. This localizes the investigation toward the
NVIDIA/Reverse PRIME output path without establishing its exact blocking point.

The user reported disabling Picom frame pacing as unchanged or worse, and
disabling `xrender-sync-fence` as unchanged or perhaps slightly better.
At inspection both `no-frame-pacing = true` and `xrender-sync-fence = false`
were present, so the latter result cannot be treated as a verified comparison
with only one setting changed. No display setting was changed by the assistant
during this inspection.

The subsequent PRIME Synchronization on/off test also produced approximately
the same lag according to the user. A later inspection confirmed the original
Picom behavior had been restored (`no-frame-pacing = false`,
`xrender-sync-fence = true`, GLX, VSync enabled), with external
`PRIME Synchronization: 1`. These negative setting tests do not exclude the
NVIDIA output path or Picom's rendering work.

In the subsequent 30-second compositor-off test, the user reported that the
external screen was smoother. Read-only inspection confirmed Picom restarted
with the original configuration and both indexing workers were still running.
Whether the same lag returned immediately on restart has been asked separately;
the off/on transition was not explicitly timestamped by the measurement tool.
The subsequent test disabled the configured `dual_kawase` blur while retaining
GLX, VSync, opacity rules and shadows, preserving transparency and changing
one rendering effect at a time. The user confirmed blur as the practical
trigger, as recorded above. Retaining the improvement requires leaving
`blur-method = "none";` after the comparison and reloading Picom.

## Workload and throughput

- Classroom of the Elite: two available episodes, 5,764 sampled frames.
- Gachiakuta: four episodes, 11,360 sampled frames.
- All six sources: 1920×1080 HEVC, 10-bit YUV 4:2:0.
- Sampling: 2 FPS. Inference: FP32, batch 64. Prefetch: 3. Transform workers: 4.
- Each job configured four file workers; actual active counts were two and
  four because Classroom contained only two source files.
- CUDA inference and `ffmpeg_cuda` decoding in the backend's resource scopes.
  Both scopes reported `cpu.idle=1`, `memory.high=10 GiB`, and no memory-high or
  OOM events.

The combined result was **17,124 frames / 356.15 seconds = 48.08 frames/second**
from the encoding start barrier through process exit. Including initial model
setup and binary selection: 360.13 seconds, or 47.55 frames/second. These are
aggregate elapsed-time rates, not sums of individual job rates.

This is one successful run under the user's desktop workload and thermal
conditions. It does not establish a cold-machine maximum or the speed impact
of a proposed change. In particular, the two-job interval lasted 304 seconds,
followed by roughly 47 seconds with only Gachiakuta's process alive.

## Responsiveness and resource evidence

| Observation window | X-server p99 | X-server maximum | Scheduler wake-up p99 |
| --- | ---: | ---: | ---: |
| Initial idle, 10 seconds | 9.16 ms | 12.17 ms | 0.22 ms |
| Both jobs encoding | 176.41 ms | 397.58 ms | 0.85 ms |
| Gachiakuta alone after Classroom exited | 224.58 ms | 402.56 ms | 0.57 ms |
| Entire encoding interval | 184.02 ms | 402.56 ms | 0.82 ms |
| Recovery, 5 seconds | 25.39 ms | 35.46 ms | 0.21 ms |

The X probe measures synchronous server round trips. It does not directly
measure physical refresh rate, mouse latency, or frame presentation. Its
agreement with the user's report nevertheless makes it useful for subsequent
comparisons. The probe cannot sample another round trip while one is blocked.

Other recorded observations:

- GPU memory peaked at 7,811 MiB of 8,188 MiB. After Classroom exited, it was
  mostly around 4,385 MiB while large X-server stalls continued.
- CPU temperature reached 100°C; thermal counters increased. The CPU was
  already throttling during the initial idle window, whose temperature peaked
  at 95°C. Temperature alone therefore does not explain the change in latency.
- GPU temperature peaked at 89°C. Among 355 samples wholly within the workers'
  encoding span, the GPU reported a software power-limit flag in 166 samples
  and a software thermal-slowdown flag in 112. These are sampled flags, not
  exact durations or counts of distinct throttling events.
- Host memory PSI `some avg10` remained 0.00%; available RAM stayed above
  7.88 GiB. Neither scope reached its memory-high threshold. This run does not
  support RAM exhaustion or memory-high reclaim as the primary explanation.
- CPU PSI `some avg10` reached 31.31%; IO PSI reached 1.56%. The independent
  desktop-priority wake-up probe nevertheless stayed far below X-server
  latency. This distinction does not exclude CPU work inside the display path.
- Six FFmpeg processes contained 528 threads in total (88 each). This is
  thread count, not evidence that 528 threads were simultaneously runnable.

## What the pipeline is actually doing

“Encoding frames” here means computing SSCD embeddings. It is not an NVENC
video-encoding workload. NVDEC decodes the source; FFmpeg samples frames and
returns full-resolution RGB to the CPU. PIL resizes each frame to short edge
288 while preserving aspect ratio. Prepared uint8 batches are pinned, copied
to CUDA, normalized through the existing lookup table, passed through SSCD in
FP32, then copied back as embeddings.

At 1920×1080, 64 RGB frames contain about 380 MiB of pixel data before object
and decoder overhead. The corresponding 512×288 uint8 input batch is 27 MiB.
The current producer retains its full-resolution frame list while a prepared
batch waits for queue space. That is a concrete memory-lifetime opportunity,
but it has not been changed or established as the source of the observed lag.

Aspect ratio matters even with a fixed short edge: a 1920×816 movie produces
roughly 32% more resized pixels per frame than these 16:9 episodes. Activation
memory and inference cost therefore cannot be predicted from batch size alone.
That wider input was present in an earlier failed experiment, not this
successful baseline.

The resize, sampling and normalization choices define the existing embedding
contract. Replacing PIL resize with a GPU resize, changing sampling density,
cropping, or reducing precision requires separate output validation. The
measurement worker calls the production pipeline factory and records hashes
of embeddings and timestamps without changing those operations.

## Interpretation and next experiments

The baseline interpretation below preceded the user-guided compositor tests.
Those later tests identified disabling Picom blur as the practical remedy;
further low-level tracing is only needed to explain the internal mechanism or
investigate a configuration that can keep blur enabled without the lag.

The strongest localization is the X/display path during sustained GPU-backed
encoding. Ordinary system-wide scheduler delay is much smaller than the
observed X stalls. Thermal constraints are real, but the current measurements
do not distinguish GPU scheduling, driver synchronization, compositor behavior
or shared display transport as the immediate blocking mechanism.

VRAM saturation and two-job concurrency are not necessary for the measured
stall: the final single-job interval still had large delays with substantially
more free VRAM. That interval is a useful observation, not a controlled
one-job-versus-two-job benchmark; its temperatures and desktop activity differ.
Simply limiting indexing to one series is therefore not a demonstrated remedy.

The next discriminating measurements are display/compositor and GPU/driver
traces correlated to individual embedding bursts, plus thermally comparable
baseline/candidate trials. Any proposed change must retain exact outputs and
at least 90% of baseline throughput. A claimed speed improvement needs the
user's confirmation before implementation. No such change is enabled here.

## Reproduction and artifacts

See [measurement instructions](indexing-measurements.md). This run used:

```bash
pixi run measure-anime-index ./library --type anime \
  --series "Classroom of the Elite" --series "Gachiakuta" \
  --idle-seconds 10 --recovery-seconds 5 --timeout 600
```

Local generated artifacts, intentionally excluded from Git:

- [Complete report](../backend/data/measurements/anime-two-jobs-baseline-resumed/report.json)
- [Phase analysis and user observation](../backend/data/measurements/anime-two-jobs-baseline-resumed/analysis.json)
- [Timeline PNG](../backend/data/measurements/anime-two-jobs-baseline-resumed/timeline.png)
- [Timeline SVG](../backend/data/measurements/anime-two-jobs-baseline-resumed/timeline.svg)

The same directory contains raw resource samples, latency samples, worker
events, selected-file requests and attempt stderr. A separate synthetic smoke
test verified two jobs with four active workers each. The indexer test suite
passed, including GPU output-identity tests; ten measurement regressions cover
worker fidelity, one-model residency, FPS selection, retries, invalid partial
results, missing probes, timeout and termination cleanup.
