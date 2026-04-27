# Storage Box Network Progress Display — Design

**Date:** 2026-04-27
**Status:** Approved

## Problem

When the backend is uploading or downloading from the Hetzner Storage Box,
the UI shows a static message ("Upload vers le Storage Box...",
"Hydratation..."). The user has no visibility into how much data has been
transferred, the current speed, or the estimated time remaining. This
matters most during the **indexing publish step** (which can move tens of
gigabytes — index + sidecars + source video files) and during
**`/matches` hydration** (downloading required source episodes).

## Goals

Surface, on the existing job/SSE channels, three numbers and an ETA:

- **Bytes transferred / total bytes** (e.g. `7.2 / 12.0 GB`)
- **Current speed** (e.g. `45 MB/s`)
- **ETA** (e.g. `3m 12s`)

Apply to:

1. Indexing → `publish_series` (uploads index, sidecars, and source video
   files in a single multi-file operation).
2. Index-hydration → `_hydrate_index_artifacts` (downloads index files
   when opening `/matches` or running an `update` job).
3. Episode hydration → `_hydrate_episode` via `hydrate_series` (downloads
   source episodes for `/matches`).

## Non-Goals

- Per-file sub-progress lines (chose aggregate-only after discussion).
- Instrumenting background/admin transfers (rename flow, audio-fix
  reupload at `processing.py:447`). Those can opt in later by passing a
  session — no behavior change if they don't.
- Bandwidth limiting / throttling.

## Approach

Polling-based aggregation, transfer-backend agnostic.

We deliberately do **not** parse rsync/lftp stdout or hook into asyncssh's
per-block callback. Instead, every ~500 ms we `stat` the destination of
every active transfer (local file for downloads; remote SFTP `stat` for
uploads) and compute deltas. This works uniformly across all three
transfer backends (sftp / rsync / lftp) with one implementation. The
~1 s lag and ~10 MB granularity are acceptable for a UX-only display.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  StorageBoxTransferProgress (singleton)                 │
│  - sessions: {session_id → TransferSession}             │
│                                                         │
│  TransferSession                                        │
│  - direction: "upload" | "download"                     │
│  - total_bytes: int                                     │
│  - active: list[ActiveTransfer]                         │
│  - completed_bytes: int                                 │
│  - poller: asyncio.Task (runs while ≥1 active)          │
│  - on_update: Callable[[ProgressSnapshot], ...]         │
│  - speed window: deque[(ts, bytes)] (last ~5 samples)   │
└─────────────────────────────────────────────────────────┘
        ▲                             ▲
        │ session.track(...)          │ on_update callback
        │                             │
   StorageBoxTransferService    IndexationQueueService.callback
   (upload_file/download_file   /matches deferred_download generator
    accept optional session=)
```

### Public API

`backend/app/services/storage_box_progress.py`

```python
@dataclass
class ProgressSnapshot:
    bytes_transferred: int
    bytes_total: int
    mib_per_sec: float | None         # None until ≥2 samples
    eta_seconds: float | None         # None until speed is known and >0
    active_transfers: int

class StorageBoxTransferProgress:
    @classmethod
    async def open_session(
        cls,
        session_id: str,
        *,
        direction: Literal["upload", "download"],
        total_bytes: int,
        on_update: Callable[[ProgressSnapshot], Awaitable[None] | None],
        poll_interval_seconds: float = 0.5,
    ) -> "TransferSession": ...

class TransferSession:
    def track(
        self,
        *,
        local_path: Path,
        remote_path: PurePosixPath,
        target_size: int,
    ) -> AsyncContextManager[None]: ...
    async def close(self) -> None: ...
```

### Polling algorithm

Per active transfer, on register:

- Capture `initial_size` = current size of destination (0 if missing).
  This makes resumed rsync transfers count only the bytes added in this
  session, not the whole file.

Every `poll_interval_seconds` (default 500 ms):

- For each active transfer:
  - `current_size = stat(destination).size` (0 if stat raises)
  - `delta_bytes = max(0, current_size - initial_size)`
- `bytes_transferred = completed_bytes + sum(delta_bytes for active)`
- Append `(now, bytes_transferred)` to a 5-sample deque
- `mib_per_sec` = `(latest_bytes - oldest_bytes) / (latest_ts - oldest_ts) / 1024² ` (None until ≥2 samples)
- `eta_seconds` = `(bytes_total - bytes_transferred) / current_bytes_per_sec` (None if speed unknown / 0)
- Build `ProgressSnapshot`, await `on_update(snapshot)`. Catch and log
  any callback exception so subscriber crashes can't stall the poller.

On transfer completion (context manager exit):

- `completed_bytes += target_size` (use `target_size`, not the stat
  result, so we don't double-count from a still-stale stat)
- Remove from active list
- If `len(active) == 0`, the poller naps until the next `track()` or
  until `close()` cancels it.

### Wiring

**`storage_box_transfer.py`:**

`upload_file()` and `download_file()` get an optional `session: TransferSession | None = None` kwarg. When non-None, wrap the actual transfer in `async with session.track(local_path=..., remote_path=..., target_size=...)`. The `target_size` for uploads is `_local_size(local_path)`; for downloads, the value already passed via `_select_download_mode` (or `_remote_size` lookup). No behavior change when `session` is None.

**`storage_box_repository.py` — `publish_series` (around line 1458):**

Already computes `to_upload` artifacts and their sizes. Add:

```python
total_bytes = sum(a.size_bytes for a in to_upload)
session = await StorageBoxTransferProgress.open_session(
    f"publish:{publish_id}",
    direction="upload",
    total_bytes=total_bytes,
    on_update=progress_callback or _noop_callback,
)
try:
    async def _upload_artifact(artifact):
        await StorageBoxTransferService.upload_file(
            artifact.local_path,
            staging_root / artifact.remote_relative_path,
            session=session,
        )
    await _run_bounded(to_upload, settings.storage_box_upload_max_parallel, _upload_artifact)
finally:
    await session.close()
```

`publish_series` accepts a new `progress_callback` kwarg threaded from
its caller (`LibraryHydrationService.publish_series_release`), which in
turn accepts one threaded from `IndexationQueueService._run_job`.

**`library_hydration_service.py`:**

- `_hydrate_index_artifacts` accepts a `progress_callback` (replacing the
  existing `ArtifactProgressCallback` which is count-based — we'll keep
  both: count callback unchanged, plus the new byte-level one). Computes
  `total_bytes = sum(int(a["size_bytes"]) for a in index_artifacts)`,
  opens a download session, passes through to `download_file`.
- `hydrate_series` accepts a `progress_callback`. Computes
  `total_bytes = sum(media size + sidecars sizes for selected episodes)`
  from the manifest, opens a download session, passes through to
  `_hydrate_episode` → forwarded to each `download_file`.

**`indexation_queue.py`:**

In `_run_job`, build a callback that updates the four new fields on the
`IndexationJob` and broadcasts:

```python
async def _on_progress(snapshot: ProgressSnapshot) -> None:
    job.network_bytes_transferred = snapshot.bytes_transferred
    job.network_bytes_total = snapshot.bytes_total
    job.network_mib_per_sec = snapshot.mib_per_sec
    job.network_eta_seconds = snapshot.eta_seconds
    job.network_active_transfers = snapshot.active_transfers
    self._broadcast(job)
```

Pass it to `publish_series_release` (for the upload phase) and to
`ensure_series_index_hydrated` (for the index-hydration phase on
`update` jobs). Reset all four fields to `None` when entering a new
phase that doesn't transfer.

Throttling: the poller's 500 ms cadence already throttles broadcasts; no
further coalescing needed.

**`api/routes/matching.py` — `/matches/deferred-download`:**

Replace the static yields with an `asyncio.Queue`-driven generator. The
network callback pushes events onto the queue; the SSE generator drains
the queue and yields `data: {...}\n\n`. Event payload extends the
existing shape:

```json
{
  "status": "running",
  "phase": "hydrate_episode",
  "message": "Hydrating missing episodes from Storage Box...",
  "progress": 0.42,
  "network_bytes_transferred": 1843523584,
  "network_bytes_total": 4503599627,
  "network_mib_per_sec": 28.4,
  "network_eta_seconds": 88.5,
  "network_active_transfers": 4
}
```

### Data model

`backend/app/models/torrent.py` — add to `IndexationJob`:

```python
network_bytes_transferred: int | None = None
network_bytes_total: int | None = None
network_mib_per_sec: float | None = None
network_eta_seconds: float | None = None
network_active_transfers: int = 0
```

Frontend mirror in `frontend/src/types/index.ts` (or wherever
`IndexationJob` lives).

### Frontend

**`IndexJobsPanel.tsx`:** new sub-line, rendered when
`job.network_bytes_total != null`, mirroring the existing `current_file`
sub-line pattern at lines 165-193:

```
Upload vers le Storage Box...
  3.4 / 12.0 GB · 45 MB/s · ETA 3m 12s
```

**`MatchValidation.tsx` — deferred-download SSE consumer (line ~1145):**
same sub-line during `hydrate_index` / `hydrate_episode` phases.

A small `formatBytes(n)`, `formatSpeed(mibPerSec)`, and
`formatEta(seconds)` helper picks units (KB / MB / GB / TB; s / m / h).

## Edge Cases

- **Resumed rsync uploads:** captured `initial_size` makes the delta
  start from the resume offset, not from zero. `bytes_transferred` is
  monotonic and never larger than `bytes_total + small_overshoot`.
- **Stat races:** if `stat` fails (file not yet created, brief filesystem
  race), treat as the previous value (or 0 if first poll). Don't crash
  the poller.
- **SFTP stat cost during upload:** one small roundtrip per active
  transfer per 500 ms. With 4 parallel uploads that's 8 SFTP stats/sec —
  trivial alongside the actual upload bandwidth.
- **Speed momentarily 0:** if all active transfers stall briefly (e.g.,
  an rsync hashing pause), `mib_per_sec` may compute as 0. Display ETA
  as `null` rather than infinity.
- **Subscriber callback raises:** caught and logged inside the poller;
  next tick continues normally.
- **Session close races with in-flight callback:** `close()` cancels the
  poller task and awaits it before returning. Any in-flight callback
  finishes naturally.

## Testing

- Unit test `StorageBoxTransferProgress` with a fake stat function:
  - Speed and ETA computation across the rolling window.
  - Resumed transfer (initial_size > 0) reports correct delta.
  - Parallel transfers aggregate correctly.
  - `close()` cancels the poller cleanly.
- Integration smoke: run an indexing publish against a small fixture and
  assert `IndexationJob.network_bytes_total` becomes non-null and
  `bytes_transferred` reaches `bytes_total` by completion.

## Out of Scope

- ETA-based scheduling decisions
- Per-file detail UI
- Background admin transfer instrumentation
