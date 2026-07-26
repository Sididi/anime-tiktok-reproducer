# Project Manager instant open (cached-first rows + background refresh)

Date: 2026-07-26
Status: Approved

## Problem

Opening the Project Manager modal blocks for **~5.0s** on every open. The modal
waits on a single request, `GET /api/project-manager/projects`, which calls
`UploadPhaseService.list_manager_rows`
(`backend/app/services/upload_phase.py:389`).

Profiled against the live data set (333 projects, 156k files, 51.55 GB).
End-to-end, `list_manager_rows` measured **4.73s / 5.05s / 5.24s** over three
consecutive warm calls. Indicative phase breakdown:

| Phase | Warm cost | What it feeds |
|---|---|---|
| `_dir_size` over all project dirs, in an 8-worker pool | **~3s** | the `Size` column only |
| Drive client construction | 0.45s | — |
| `list_project_folders_under_parent` (265 folders) | 0.95s | folder id/url fallback |
| `list_root_video_files_by_parent_ids` (2 round trips) | 1.12s | readiness badge |
| `ProjectService.list_all` (333 × json read + validate) | 0.014s | everything |
| `metadata.json` exists × 333 | 0.003s | `has_metadata` |
| `AccountService.list_accounts` (parallel, not blocking) | 0.008s | account dropdown |

**A caveat on absolute walk timings.** The data set is 51.55 GB against 32 GB of
RAM, so dentry/inode cache residency drifts and serial `os.walk` was observed
anywhere from 1.10s to 3.57s across runs. Absolute figures above are therefore
indicative. The three claims the design actually rests on were each measured
**A/B alternating within a single run, three rounds, after priming** — those are
reproducible and stable:

1. **`os.scandir` is 4.6× faster than `os.walk`, byte-identical.**
   0.730s vs 3.354s (best of 3; every round 0.73s vs 3.35–3.53s), and both
   produce exactly 51,549,482,457 bytes.
2. **The 8-worker `ThreadPoolExecutor` makes `_dir_size` 1.45× *slower*.**
   4.929s pooled vs 3.404s serial (best of 3; the pool lost every round). GIL
   contention on `stat()` means the pool is pure overhead.
3. **A row build touching neither Drive nor the disk walk takes ~0.12s** for all
   333 projects. That is the floor for an instant open.

Beyond the timings, one structural finding: **the expensive work serves very
little.** The Drive round trips compute the readiness badge for the **35 of 333
rows** that are not upload-locked; the other 226 are already answered from
persisted data by `_resolve_drive_folder_offline` / `_persisted_drive_video`.
The ~3s walk feeds one sortable `Size` column.

## Goals

- The modal paints rows in **~0.12s** on every open, including the first open
  after a backend restart.
- Sizes and Drive readiness stay correct, refreshed in the background on each
  open, and update in place when the refresh lands.
- Zero Drive API calls on the open path.

## Non-goals

- Changing what a row contains, how readiness is computed, or the upload flow.
- Pagination, virtualization, or any change to the table's rendering.
- Touching the upload-jobs SSE channel.

## Design

### 1. Split `list_manager_rows` into cached and refresh modes

`list_manager_rows(*, refresh: bool = False)`.

**Cached mode** (the open path) issues **no Drive calls** and does **no directory
walking**:

- `local_size_bytes` — read from the new size cache; `0` when absent.
- Drive folder id/url — from `project.drive_folder_id` / `drive_folder_url` via
  the existing `_resolve_drive_folder_offline`, passing `None` for
  `folder_candidates_by_name` so it cannot issue a call.
- Drive video — `_persisted_drive_video(project)`, falling back to
  `_cached_drive_video`.
- `has_metadata` and `local_video_available` stay **live**: they cost 3ms and 0ms
  respectively and reflect local disk state that the user can change directly.

Readiness is then assembled by the existing `_build_readiness`, so a row's shape
and status semantics are unchanged.

**Refresh mode** is today's behavior — the batch folder listing, the batched video
lookup, and a real size computation — plus it **writes both caches** before
returning. It keeps the existing retry/`reset_client` loop and the
`drive_batch_lookup_failed` fallbacks unchanged.

### 2. Two durable caches under `backend/data/`

Following the convention set by `project_upload_jobs.json`:

- **`project_sizes.json`** — `{project_id: {"bytes": int, "computed_at": iso}}`.
  Written by the refresh pass, read by the cached pass. Entries for projects that
  no longer exist are dropped on write.
- **`drive_video_cache.json`** — a disk backing for
  `UploadPhaseService._drive_video_cache`, which is currently a class-level dict
  that dies with the process. Loaded lazily on first access, written by the
  refresh pass. **This is what makes the first open after a backend restart fast**
  rather than only repeat opens.

Both are written atomically (temp file + `replace`), matching
`_write_jobs_atomic` in `project_startup_service.py`. A malformed or missing file
degrades to an empty cache, never an exception.

### 3. Two independent fixes to the refresh path

These stand on their own and apply regardless of caching:

- `_dir_size` switches from `os.walk` + `Path(root) / filename` + `.stat()` to an
  explicit `os.scandir` stack using `DirEntry.stat(follow_symlinks=False)`. This
  avoids a `Path` construction and an extra syscall per file. Verified
  byte-identical on the live 51.55 GB data set.
- The `ThreadPoolExecutor` around `_build_row` is removed. It costs ~1.5s of GIL
  contention today, and once the cached row build is ~0.12s there is nothing left
  worth parallelizing.

### 4. Routes

- `GET /project-manager/projects` — cached mode. Unchanged path and response
  shape, so any other consumer keeps working.
- `GET /project-manager/projects?refresh=1` — refresh mode.

A query parameter rather than a second route keeps the response shape and the
frontend's `api.listProjectManagerProjects` signature intact.

### 5. Startup prewarm

The FastAPI `lifespan` in `backend/app/main.py:134` fires one background refresh
at startup (`asyncio.create_task` over `asyncio.to_thread`). Failures are logged
and swallowed — a failed prewarm just means the first open serves an empty cache
and the on-open refresh fills it. It must not delay startup or LAN readiness.

### 6. Frontend: two-phase load

In `loadData` (`frontend/src/components/project-manager/ProjectManagerModal.tsx:213`):

1. Request cached rows, render immediately, clear `loading`.
2. Fire the refresh request; on success, swap `rows` in place.

Both phases respect the existing `loadRequestIdRef` staleness guard, so closing
and reopening the modal cannot let an in-flight response clobber newer state.

The transient-retry loop (`LOAD_RETRY_DELAY_MS` / `LOAD_RETRY_WINDOW_MS`, which
exists to survive a still-booting backend) stays on the **cached call only**. A
failed refresh logs and leaves cached rows on screen rather than flipping the
whole view to an error — the user still has a usable table.

The header refresh button calls the refresh path directly. Its existing spinner
runs for the duration of the background refresh, so staleness is visible rather
than silent.

## Tradeoff

A project created since the last refresh renders with `local_size_bytes: 0` and
readiness derived only from persisted data until the refresh lands (~3s). Because
the refresh always fires on open, the window is bounded and self-healing. This is
the staleness the design explicitly trades for a ~40× faster paint.

## Testing

`backend/tests/test_project_manager_rows.py` already provides a hermetic
environment that records Drive batch calls. Extend it with:

- Cached mode records **zero** Drive batch calls and zero folder listings.
- Cached mode returns sizes from a seeded `project_sizes.json`, and `0` for a
  project missing from it.
- Refresh mode populates both cache files; a following cached call reflects them.
- A cached call after a simulated process restart (cleared in-memory
  `_drive_video_cache`) still resolves Drive video info from the persisted file.
- `_dir_size` matches a reference `os.walk` implementation on a temp tree
  containing nested dirs, an empty dir, and a symlink.
- A corrupt cache file yields an empty cache rather than raising.

Existing assertions in that file must continue to pass unchanged under refresh
mode, since refresh mode is today's behavior.

Run with `pixi run -e dev pytest` (the default env lacks `pytest-asyncio`).
Baseline is 17 pre-existing failures; the change must not add to that count.

## Files touched

- `backend/app/services/upload_phase.py` — mode split, `_dir_size`, pool removal,
  cache read/write.
- `backend/app/api/routes/project_manager.py` — `refresh` query parameter.
- `backend/app/main.py` — startup prewarm.
- `frontend/src/api/client.ts` — `refresh` parameter.
- `frontend/src/components/project-manager/ProjectManagerModal.tsx` — two-phase
  `loadData`.
- `backend/tests/test_project_manager_rows.py` — new coverage.
