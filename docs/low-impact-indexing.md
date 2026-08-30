# Low-impact indexing

Why the desktop used to freeze during series indexing, and how the fix works.

## Diagnosis (2026-08-31, i9-14900HX / 32 threads, 31 GiB RAM, RTX 4070 Laptop, single NVMe)

Four compounding causes, none of them "the indexer is slow":

1. **CPU thread storm at normal priority.** Fast mode runs up to 2 jobs × 4
   file workers = 8 ffmpeg processes, each defaulting to ~one thread per core
   (≈32), plus transform pools — hundreds of runnable threads at nice 0.
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

- `CPUWeight=idle` → cgroup `cpu.idle=1`: the subtree may use **every idle
  core** (zero throughput loss on an unloaded machine) but yields instantly
  when an interactive task wants CPU.
- `MemoryHigh=10G` → the kernel reclaims the *scope's own* page cache and
  anon memory first; streaming a series can no longer evict the desktop's
  working set.
- `IOWeight` is inert until the optional root setup (below) enables the io
  controller.
- `systemd-run --scope` **execs the payload in place**, so PIDs, process
  groups, and the existing kill/timeout handling are unchanged (verified).

Settings (env prefix `ATR_`): `low_impact_media_jobs` (default on),
`low_impact_cpu_weight` (`idle`, or `1..10000` for proportional sharing),
`low_impact_memory_high`, `low_impact_io_weight`. If `systemd-run --user` is
unavailable the wrapper is a no-op.

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
- CPU libx264 import fallback: `-threads 16` (x264 `veryfast` gains ~nothing
  past 16; this is an encode, not part of any bit-identity contract).

## Expected speed impact

≈0% with the desktop idle (idle-class scheduling only matters under
contention; thread caps stay above the pipeline's consumption rate; fadvise
touches data that is never re-read). Under active desktop CPU load, indexing
now yields — by design.

## Optional root setup

`sudo scripts/setup_low_impact_host.sh` (one-time) adds:
- io controller delegation + blk-iocost on the root NVMe so `IOWeight=50` is
  actually enforced (works with the default `none` scheduler);
- `vm.dirty_background_bytes=256M` / `vm.dirty_bytes=2G` so bulk writes flush
  incrementally instead of in 6 GB storms.

## Residual GPU stutter

If light stutter remains on the **external monitor only** while indexing,
that is SSCD/NVENC competing with the reverse-PRIME scanout on the 4070 —
there is no NVIDIA knob to deprioritize CUDA vs display. Mitigations trade
indexing speed (smaller batch, fp16 is forbidden) and are deliberately not
applied; report whether it is still noticeable after this fix before
considering them.
