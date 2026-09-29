# Measuring indexing responsiveness

The [final production decisions](indexing-final-decisions-2026-09-29.md) describe
which changes are always active and which experimental code was removed.

The [2026-09-21 findings](indexing-findings-2026-09-21.md) record a completed
anime workload that reproduced the user's usual lag, with a timeline and raw
report links.

The [memory-lifetime results](indexing-memory-lifetimes-2026-09-22.md) compare
early release of GPU normalization temporaries and original CPU images,
including exact-output checks, GPU memory savings and throughput limits.

The [direct pinned-batch results](indexing-pinned-batches-2026-09-22.md) measure
removing the intermediate pageable batch, with those lifetime changes retained
in both the baseline and updated code.

Run from the repository root:

```bash
pixi run measure-anime-index ./library --type anime \
  --series "Classroom of the Elite" --series "Gachiakuta"
```

The Pixi task runs inside `modules/anime_searcher`, so `./library` means
`modules/anime_searcher/library`. Absolute library paths also work. Omit the
second `--series` to measure one indexing job. Each job uses the first four
videos in sorted order; use `--max-files N` to change that sample. This reads
source files and writes measurement reports, without modifying indexes.

Reports go to a new directory under `backend/data/measurements/indexing/`.
`--output-dir /absolute/new/directory` chooses a specific location. The command
prints the report path and exits nonzero for any incomplete trial. Ctrl-C or
SIGTERM stops worker process groups, including FFmpeg, and saves a partial
report. `--timeout 600` limits each trial, including initialization and retries.

## Workload fidelity

Each job uses the production CLI's pipeline factory, one SSCD model in its own
process, FP32, batch size 64, prefetch 3, and four transform workers by default.
The shared worker policy selects up to four file workers for CUDA with FFmpeg;
two simultaneous series can therefore run eight decoders. Default sampling is
the series' stored manifest FPS, otherwise the application's 2 FPS. Override
with `--fps` only when that is the workload being investigated.

The controller applies the environment defaults from `scripts/backend.sh` and
the backend's actual `wrap_low_impact` settings. Explicit environment overrides
remain effective. Every attempt records whether a scope was requested, its
actual cgroup, effective workers, batch size, precision, decoder, binary paths,
source dimensions, file sizes and modification times. An unavailable systemd
scope is recorded as unwrapped; it must not be mistaken for a scoped run.

Initial jobs wait at a barrier after model warmup and binary selection, then
begin encoding together. CUDA OOM uses the backend's error classifier and
current retry policy: one fresh process at half the original batch size. The
measurement restarts all selected files on retry; the app can resume persisted
checkpoints. Decode fallback and unreadable files are also recorded. Any
skipped file or failed job invalidates the combined throughput result.

This is an encoding measurement, excluding import, FAISS persistence and
upload. It does not exactly reproduce their timing or the app's job staggering.
The production path's file-cache advice still runs. GPU peak allocator counters
include warmup. The controller loads no SSCD model and stays outside the heavy
job scopes so it can supervise and record during stalls.

## Reading the report

- `trials[].encoding_frames_per_second` is completed frames across **all** jobs
  divided by elapsed time from the initial start barrier to final process exit.
  It includes failed-attempt time, restart and cleanup. Repeated work is counted
  once. It is null if any job fails, skips a file, times out or is interrupted.
- `trials[].combined_frames_per_second` additionally includes initial model
  setup and binary selection. Neither metric sums individual job rates.
- Each successful file records embedding and timestamp SHA-256 hashes. Compare
  corresponding files between runs to detect any output change. Different
  batch sizes can also affect floating-point output, so retain effective
  attempt settings when making that comparison.
- `windows` separates idle, encoding, the full trial and recovery. Each contains
  scheduler wake-up and XSync latency percentiles, GPU summaries, peak process
  RSS, FFmpeg thread counts, CPU temperatures and individual thermal counter
  deltas. Missing probes remain missing; they are not reported as zero latency
  or absence of throttling. Only samples wholly within a window are summarized.
- `resources.jsonl` keeps one-second samples, including host/cgroup pressure,
  memory events, process CPU counters, GPU power and clock-limit reasons.
  `latency.jsonl` keeps raw latency observations. `events.jsonl`, request JSON
  and each attempt's stderr explain retries and failures.

The X probe sleeps at least 10 ms between observations. XSync measures a server
round trip; it does **not** measure refresh rate, compositor presentation or
input-to-photon latency. A stall reduces its sampling frequency. Desktop
perception remains useful alongside the measurements. Summed process RSS can
double-count shared memory. Polling and output hashing have observer overhead;
use the same instrumentation in both sides of a comparison.

## Evaluating a later change

Keep identical source files, sampling, precision, effective settings, monitors,
charger and desktop workload. Use `--repeat 3` for repeated trials, with
`--idle-seconds` and `--recovery-seconds` chosen to obtain comparable starting
temperatures. The defaults (10 and 5 seconds) are observation windows, not a
guarantee of thermal recovery. Alternate baseline and candidate runs and inspect
thermal counters, clocks and power before attributing differences to code.

The acceptance requirement is at least 90% of baseline throughput, identical
outputs and improved responsiveness under comparable conditions. A failed
two-job run cannot supply that baseline, even if its surviving job is fast.
Short synthetic smoke tests validate the harness, not sustained indexing speed.
No performance or precision change is enabled by this measurement command.

## FFmpeg thread policy

The filter and rawvideo output pools always use automatic sizing. The
[archived four-run comparison](ffmpeg-thread-pools-2026-09-22.md) did not establish
a reliable advantage for eight filter threads and one output thread, so those
overrides and their implementation were removed on 2026-09-29. Historical
commands setting them no longer select a different variant.

Reports retain `effective.ffmpeg_threads` with the existing decoder limit
(default 8) and `filter=0`, `output=0` for automatic sizing. Observed thread
counts remain useful diagnostics; zero does not mean no worker threads.
The measurement command records production behavior and writes no indexes.

The older `benchmark-anime-index` command remains useful for serial/parallel
and precision comparisons. It now shares production worker selection and keeps
only one model resident. Its default sampling remains 1 FPS; pass `--fps 2`
for the application's usual setting. It does not collect desktop telemetry or
reproduce two concurrent application jobs.
