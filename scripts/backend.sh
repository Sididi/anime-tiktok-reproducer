#!/bin/bash
# Run the backend with conservative native allocator and thread-pool limits.

set -euo pipefail

# glibc otherwise creates up to hundreds of 64 MiB arenas on this 32-core host.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-4}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-131072}"

# Two heavy jobs may run concurrently. Four CPU workers per native library keeps
# useful parallelism without allowing every library to create 32 more threads.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-4}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-4}"
export JOBLIB_MULTIPROCESSING="${JOBLIB_MULTIPROCESSING:-0}"

# --reload-dir app: without it uvicorn's watcher walks the whole cwd, which
# includes backend/data (~100 GB of projects). Only source edits restart.
# --timeout-graceful-shutdown: the browser's shared event stream keeps SSE
# connections open forever, and uvicorn's default (no timeout) waits for them
# before swapping in the new worker → every reload with the app open became a
# zombie (port listening, nothing answering; seen 2026-08-28). Clients
# reconnect on their own.
exec uvicorn app.main:app --reload --reload-dir app --timeout-graceful-shutdown 5 --host 127.0.0.1 --port 8000
