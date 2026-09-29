# FFmpeg filter/output thread experiment — 2026-09-22

**Final decision, 2026-09-29:** keep automatic filter/output pools. The `8/1`
experiment and its environment overrides have been removed from production.
The results below are historical evidence, not instructions for current code.
See [final production decisions](indexing-final-decisions-2026-09-29.md).

The experiment reduced FFmpeg thread count and preserved measured outputs, but
it did not establish a reliable throughput or responsiveness benefit within
the user's maximum 10% slowdown allowance.

## Historical implementation

- `ANIME_SEARCHER_FILTER_THREADS=8` adds global `-filter_threads 8`.
- `ANIME_SEARCHER_OUTPUT_THREADS=1` adds output-side `-threads:v 1`, after `-i`,
  targeting the rawvideo encoder rather than the input decoder.
- Both settings default to `0`, which omits their options and preserves the
  previous automatic behavior. Empty, malformed and negative values also use
  that default. Either pool could be varied independently in that implementation.
- The overrides apply to CPU/NVDEC extraction, CUDA capability probes and the
  media-profile benchmark. The existing input decoder limit remains eight.
- Sampling, pixel formats, conversion chains, PIL resize, FP32 inference,
  batch size, prefetch and file concurrency are unchanged.
- Measurement reports record the environment overrides and each attempt's
  resolved extraction settings in `effective.ffmpeg_threads`.

These pipelines use simple `-vf` graphs, so `-filter_complex_threads` is not
applicable. See FFmpeg's documentation for the independent
[filter pool](https://www.ffmpeg.org/ffmpeg.html#Advanced-options),
[codec thread option](https://ffmpeg.org/ffmpeg-codecs.html#Codec-Options), and
[input/output option scope](https://www.ffmpeg.org/ffmpeg.html#Description).

## Sustained comparison

Four complete runs used baseline/candidate/candidate/baseline order. Every run
processed the same 17,124 frames from two Classroom of the Elite episodes and
four Gachiakuta episodes: 1080p HEVC 10-bit, 2 FPS, NVDEC, FP32, batch 64, prefetch
3, four transform workers and 2 + 4 active file workers. Both jobs began at the
measurement barrier after initialization. The selected binary was
`/usr/bin/ffmpeg`, version `n9.0.1`. No indexes were written.

| Run | Filter / output threads | Encoding seconds | Frames/s | Peak threads per FFmpeg | XSync p99 |
| --- | --- | ---: | ---: | ---: | ---: |
| Baseline 1 | Automatic / automatic | 236.45 | 72.42 | 88 | 200.08 ms |
| Candidate 1 | 8 / 1 | 274.72 | 62.33 | 32 | 213.03 ms |
| Candidate 2 | 8 / 1 | 278.98 | 61.38 | 32 | 219.94 ms |
| Baseline 2 | Automatic / automatic | 289.50 | 59.15 | 88 | 215.51 ms |

Encoding time includes final worker exit. Arithmetic mean trial throughput was
**65.78 FPS baseline versus 61.86 FPS candidate**, a **5.97% decrease**. This
descriptive average falls within 10%, but does not establish a reliable bound:

- Baseline throughput itself fell **18.32%** between the first and last run.
- Candidate 1 was **13.93% slower** than the preceding baseline.
- Candidate 2 was **3.77% faster** than the following baseline.
- The comparison changes sign depending on the neighboring baseline.
  Attributing the whole initial slowdown to the code would be unjustified.

Peak FFmpeg thread total dropped from **528 to 192** (63.6%). These include
internal threads beyond the two configurable pools; eight filter threads plus
one output thread does not mean nine total threads. Thread count alone does not
establish runnable CPU demand, heat or throughput.

Every run completed without OOM retries, skipped files or decode fallback. All
six files' embedding and timestamp SHA-256 hashes matched across all four runs.
The final module suite passed **54 tests**, including exact pixels/timestamps
with automatic versus capped pools for H.264 and 1080p HEVC 10-bit, on both CPU
and NVDEC, and checks that decoder/output thread options have separate scopes.

## Environment and limits

| Run | CPU / GPU temperature before launch | Peak CPU / GPU during encoding |
| --- | --- | --- |
| Baseline 1 | 86 / 64°C | 99 / 90°C |
| Candidate 1 | 87 / 68°C | 99 / 88°C |
| Candidate 2 | 94 / 68°C | 98 / 88°C |
| Baseline 2 | 92 / 68°C | 98 / 88°C |

The driver waited at least 30 seconds and tried to obtain three successive
samples with GPU at most 67°C and CPU at most 90°C, with a 180-second limit.
The first run met that condition; later runs reached the time limit instead.
CPU thermal counters increased in every run, and GPU thermal slowdown appeared
in every run. Normal desktop work continued. Neither CPU clocks nor background
work were controlled; equal GPU temperatures do not imply equal thermal state
throughout the laptop.

Picom's config hash was identical before and after every run; the saved config
still selected `dual_kawase` blur. No compositor settings were changed by this
experiment. Config hashes do not independently verify live compositor state.
XSync measures server round trips rather than physical refresh/input latency,
and these results show no clear responsiveness gain or heat reduction.

The final decision retains automatic pools and removes the `8/1` candidate.
Fewer threads did not establish a performance improvement or replace the
confirmed blur remedy.

## Archived artifacts

The saved reports and analysis preserve the measured comparison. The removed
overrides no longer affect current production code; replaying the old launch
environment will not reproduce the capped candidate.

Local artifacts (ignored by Git):

- [Comparison JSON](../backend/data/measurements/ffmpeg-thread-pools-2026-09-22/comparison.json)
- [Run order and cooling observations](../backend/data/measurements/ffmpeg-thread-pools-2026-09-22/runs.json)
- [Reproducible analysis](../backend/data/measurements/ffmpeg-thread-pools-2026-09-22/analyze_comparison.py)
- Each numbered run directory contains its full report, raw telemetry, hashes,
  attempt settings and stderr logs.
