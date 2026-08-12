# Playback Encode Speedup — Design

**Date:** 2026-08-12
**Status:** Approved by owner
**Scope:** Backend only — `MatchPlaybackService` clip encoding after matching completes.

## Goal

Cut the wall-clock time of the full playback prepare (the `/matches` clip build that
runs after matching) by ~4–5×, with:

- **no change to clip output profiles** (resolution / fps / qp stay identical, so the
  x16 fast-watch experience is untouched),
- no quality loss,
- graceful degradation on machines or files where the GPU path is unavailable.

Measured baseline on real project data (RTX 4070 Laptop, ffmpeg 8.1, HEVC 10-bit
episode + HEVC TikTok video):

| Path | Per-clip wall |
|---|---|
| Current: CPU decode → `h264_nvenc -preset p5` | ~0.70s |
| New: NVDEC decode → `scale_cuda` → `h264_nvenc -preset p1` | ~0.63s |
| 4 concurrent GPU jobs, same episode | ~0.31s effective per clip |

A 176-scene project ≈ 350 clips. Today source clips are serialized per episode
(`match_playback_max_workers_per_episode = 1`), which dominates wall-clock
(~100–150s). Expected after: ~25–35s.

## Rejected approaches

- **Stream copy (owner's initial idea):** the library and TikTok videos are HEVC
  (episodes 10-bit), so the h264-only stream-copy rule almost never fires, and
  extending it to HEVC would (a) bet on browser HEVC hardware decode, (b) put
  full-resolution HEVC decode at x16 on two panes at once — the exact fast-watch
  risk the owner wants to avoid, and (c) break scene alignment: measured keyframe
  spacing is 5–7s on the TikTok video and up to 12s on episodes, so keyframe-snapped
  cuts would start seconds before the scene window. Rejected.
- **Lower quality further:** encode cost is not the bottleneck (decode +
  serialization is); quality reduction saves almost nothing. Rejected.

## Design

All changes live in `backend/app/services/match_playback_service.py` and
`backend/app/config.py`.

### 1. New first rung on the transcode ladder: full-GPU

New command builder `_build_full_gpu_command_sync` producing:

```
ffmpeg -y -v error
  -hwaccel cuda -hwaccel_output_format cuda
  -ss <start> -i <input> -t <duration>
  -map 0:v:0 -an -sn -dn
  -vf "fps=<profile.fps>,scale_cuda=w=<w>:h=<h>:force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12"
  -c:v h264_nvenc -preset p1 -rc constqp -qp <profile.crf>
  -profile:v high -movflags +faststart <output>
```

Notes:

- `fps` runs **before** `scale_cuda` (drops frames prior to scaling; frame selection
  is identical either way — verified the `fps` filter passes CUDA hw frames through).
- `format=nv12` handles the 10-bit (p010) episode output from NVDEC.
- No `-pix_fmt yuv420p` flag: nv12 from `scale_cuda` encodes to yuv420p-compatible
  output; the encoder consumes hw frames directly.

The encode ladder in `_encode_clip_sync` becomes:

1. Stream copy (unchanged — h264 mp4 sources only, `track == "source"`)
2. **Full-GPU transcode (new)** — only when the one-time capability probe passes
3. Current NVENC with CPU decode (unchanged, `-preset p5`)
4. `libx264 -preset ultrafast` (unchanged, final fallback)

Each rung appends to `error_details` on failure and falls through, exactly like
today; a scene fails only if every rung fails.

### 2. Capability probe

`_is_full_gpu_available_sync`, modeled on `_is_nvenc_available_sync` (class-level
cached booleans): requires `h264_nvenc` in encoders, `hevc_cuvid`/NVDEC support in
decoders (`ffmpeg -decoders` contains `hevc_cuvid` and `h264_cuvid`), and
`scale_cuda` in `ffmpeg -filters`. Any per-clip runtime GPU failure (VRAM pressure,
odd file) still falls through the ladder, so the probe only gates the attempt.

### 3. Parallelism

- `match_playback_max_workers` default **4 → 8** (existing clamp already max 8;
  8 matches the consumer NVENC concurrent-session cap).
- `match_playback_max_workers_per_episode` default **1 → 4** (existing clamp already
  max 4). The per-episode=1 rule protected against CPU decode thrash of a single
  large file; with NVDEC decode the CPU stays nearly idle and 4-wide same-episode
  concurrency measured ~2× throughput with correct output.

Both remain env-overridable as today (e.g. the second machine can keep lower values
in its `.env` if needed).

### 4. Cache compatibility

`ENCODE_PROFILE_VERSION` is **not** bumped. Output resolution/fps/qp are unchanged;
p1 vs p5 at constant QP only marginally changes file size. Existing cached clips
stay valid and reusable; new and old clips mix freely in manifests.

## Error handling

Unchanged semantics: per-clip ladder with accumulated `error_details`, temp-file
validation (`_validate_clip_sync`) before moving into the clip store, meta sidecar
written only after validation. The new rung uses the same timeout
(`FFMPEG_TIMEOUT_SECONDS`) and cleanup pattern as the existing rungs.

## Known accepted risk

If a prepare overlaps heavy GPU work (matching/indexing), NVDEC init can fail and
clips silently take the slower CPU rung — correct output, just slower. Accepted;
no GPU-budget gate in this scope.

## Testing

1. Unit tests (mocked `subprocess.run`): full-GPU command construction from a
   profile; ladder ordering (GPU rung attempted only when probe passes; failure
   falls through to NVENC-CPU then libx264; `error_details` accumulation).
2. Probe caching behavior (single subprocess call, cached result).
3. Acceptance: timed before/after `prepare_playback(force=True)` on a real project
   (e.g. 176-scene `ca56e019da2e`), verifying manifest `ready: true`, all scenes
   `ready`, and wall-clock in the expected range.
