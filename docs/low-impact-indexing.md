# Low-impact indexing

Earlier mitigations for desktop stalls during series indexing.

**Finalized (2026-09-29):** [production decisions](indexing-final-decisions-2026-09-29.md)
retain the three memory optimizations, remove the FFmpeg filter/output cap
experiment, and apply `blur-method = "none";` to the user's Picom configuration.
Picom was reloaded; the existing transparency settings were preserved.

**Measurement update (2026-09-21):** the user still reproduces severe lag during
SSCD encoding with these settings active. Treat the historical diagnosis and
speed estimates below as hypotheses, not proof that responsiveness is fixed.
Use the [measurement command](indexing-measurements.md) to record throughput,
X-server latency, effective scopes, memory pressure and thermal behavior.

**Follow-up (2026-09-22):** the user confirmed that disabling Picom's
`dual_kawase` blur improved external-screen responsiveness while retaining
transparency, identifying blur as the practical trigger. Keep
`blur-method = "none";` in Picom for this remedy. The indexing algorithm and
its resource settings were unchanged. See the
[recorded findings](indexing-findings-2026-09-21.md) for the comparisons and
remaining measurement limits.

## Diagnosis (2026-08-31, i9-14900HX / 32 threads, 31 GiB RAM, RTX 4070 Laptop, single NVMe)

Four compounding causes, none of them "the indexer is slow":

1. **CPU thread storm at normal priority.** Fast mode runs up to 2 jobs × 4
   file workers = 8 ffmpeg processes, each defaulting to ~one thread per core
   (≈32), plus transform pools — hundreds of threads at nice 0. Thread count
   alone does not establish how many are runnable.
   Plain `nice` would not have helped: processes live in cgroups (autogroup
   is bypassed), so only cgroup weights count.
2. **Page-cache eviction.** One observed backend worker had pushed 107 GB of
   reads + 34 GB of writes through the page cache (import copies every
   episode a second time, then the searcher streams it all again, then
   packaging SHA-256s it all again) on a machine where OS, apps, and library
   share one 953 GB NVMe. Streaming ~100 GB through 31 GiB of RAM evicts
   every hot page the desktop owns; each UI interaction becomes a disk fault.
3. **Unreclaimable pinned memory.** The prefetch queue pins each prepared
   batch (`pin_memory()`), ≈340 MB page-locked per subprocess, on top of up
   to ~3 GB of transient full-res frame buffers.
4. **GPU/display contention.** The primary external monitor is scanned out by
   the NVIDIA dGPU via reverse PRIME; SSCD inference bursts compete with that
   display path (see "Residual GPU stutter" below).

## The fix

### systemd user scopes (backend/app/utils/low_impact.py)

Every heavy subprocess — the `anime_searcher` indexing CLI, library-import
ffmpeg transcodes/remuxes, storage-box rclone transfers — is wrapped in a
transient scope:

```
systemd-run --user --scope --collect -p CPUWeight=idle -p MemoryHigh=10G -p IOWeight=50 <cmd>
```

- `CPUWeight=idle` → cgroup `cpu.idle=1`: the group has very low CPU scheduling
  priority relative to its peers. This does not control GPU scheduling or
  guarantee desktop latency or unchanged throughput.
- `MemoryHigh=10G` → exceeding the threshold subjects that scope to reclaim and
  throttling. It does not reserve RAM for desktop applications or guarantee
  that their working sets remain resident.
- `IOWeight` is inert until the optional root setup (below) enables the io
  controller.
- `systemd-run --scope` **execs the payload in place**, so PIDs, process
  groups, and the existing kill/timeout handling are unchanged (verified).

Settings (env prefix `ATR_`): `low_impact_media_jobs` (default on),
`low_impact_cpu_weight` (`idle`, or `1..10000` for proportional sharing),
`low_impact_memory_high`, `low_impact_io_weight`. If `systemd-run --user` is
unavailable the wrapper is a no-op.

These distinctions follow the [kernel's cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html)
for `cpu.idle`, `memory.high` and memory protection.

### Page-cache hygiene

- `copy_file_drop_cache` replaces `shutil.copy2` for library imports:
  fdatasync + `posix_fadvise(DONTNEED)` on both ends — no cache footprint, no
  multi-GB dirty writeback burst.
- The searcher drops each episode's pages after its single sequential read
  (`drop_file_page_cache` in `_process_one_job`).
- Packaging SHA-256 drops pages after hashing each file.

### ffmpeg thread caps (bit-exactness verified)

- Searcher decode: `-threads 8` per ffmpeg (override
  `ANIME_SEARCHER_DECODE_THREADS`). Verified byte-identical output on both
  the CPU path and the `hevc_cuvid` NVDEC path (md5 over rawvideo rgb24).
  8 frame threads decode far faster than the embed stage consumes.
  This input-side decoder option does not cap FFmpeg's filter or output pools;
  measured NVDEC processes can still contain 88 threads each.
- CPU libx264 import fallback: `-threads 16` (x264 `veryfast` gains ~nothing
  past 16; this is an encode, not part of any bit-identity contract).

## Expected speed impact

The earlier estimate of approximately zero speed loss has not been established
by sustained, thermally comparable trials. The current acceptance requirement
is no more than 10% throughput loss. Compare completed, identical workloads and
record both starting temperatures and throttling before assessing that limit.

## Optional root setup

`sudo scripts/setup_low_impact_host.sh` (one-time) adds:
- io controller delegation + blk-iocost on the root NVMe so `IOWeight=50` is
  actually enforced (works with the default `none` scheduler);
- `vm.dirty_background_bytes=256M` / `vm.dirty_bytes=2G` so bulk writes flush
  incrementally instead of in 6 GB storms.

## Residual GPU stutter

On 2026-09-22 the user clarified that only the external monitor lags during
frame embedding; the integrated panel works normally. This supersedes the
earlier description of both monitors lagging. The measured
path uses NVDEC and SSCD inference, not NVENC video encoding. Long X-server
round trips alongside short scheduler wake-up delays make the shared display
path a useful investigation target, but do not isolate a driver, compositor or
GPU scheduling cause. The external screen uses NVIDIA Reverse PRIME with
PRIME Synchronization enabled; the integrated panel uses Intel. GPU contention,
display synchronization and thermal behavior need controlled experiments.
No smaller-batch or reduced-precision mitigation is enabled by
the measurement tooling.
