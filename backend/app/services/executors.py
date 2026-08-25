"""Two thread pools so small I/O never queues behind heavy work.

``light_executor()`` is installed as the event loop's *default* executor: every
``asyncio.to_thread`` / ``run_in_executor(None, ...)`` — project JSON reads,
sqlite calls, small subprocess probes — lands there.  ``heavy_executor()`` is
for the long, resource-bound work (ffmpeg encodes, FAISS/SSCD matching,
Whisper, PySceneDetect, ProPainter, whole-file downloads): route it through
``run_heavy`` / ``submit_heavy``.

The heavy pool keeps the parallelism the matching/playback code was tuned for
(the ``asyncio.Semaphore`` budgets in those services are unchanged); the split
only stops a burst of ffmpeg encodes from starving a request that just needs
to read a 3 KB file.

Rules for functions submitted to the heavy pool: they are leaf, synchronous
functions.  They may report progress with ``loop.call_soon_threadsafe`` but
must never block waiting on another heavy submission (pool self-deadlock),
and ``asyncio.to_thread`` has no running loop inside a worker thread.

Both pools are created lazily (tests without the app lifespan, uvicorn
``--reload`` children) and sized from the environment:

* ``ATR_HEAVY_THREAD_WORKERS`` (default 8, clamp 1..32) — the old
  ``ATR_BACKEND_THREAD_WORKERS`` is honoured as a deprecated alias.
* ``ATR_IO_THREAD_WORKERS`` (default 32, clamp 4..128).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

logger = logging.getLogger("uvicorn.error")

T = TypeVar("T")

HEAVY_WORKERS_ENV = "ATR_HEAVY_THREAD_WORKERS"
LEGACY_HEAVY_WORKERS_ENV = "ATR_BACKEND_THREAD_WORKERS"
IO_WORKERS_ENV = "ATR_IO_THREAD_WORKERS"
DEFAULT_HEAVY_WORKERS = 8
DEFAULT_IO_WORKERS = 32
HEAVY_THREAD_PREFIX = "atr-heavy"
IO_THREAD_PREFIX = "atr-io"

_lock = threading.Lock()
_heavy: ThreadPoolExecutor | None = None
_light: ThreadPoolExecutor | None = None


def _parse_workers(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        value = int(raw) if raw is not None and raw.strip() else default
    except ValueError:
        logger.warning("Ignoring non-integer thread-worker setting %r", raw)
        value = default
    return max(low, min(value, high))


def heavy_worker_count() -> int:
    raw = os.environ.get(HEAVY_WORKERS_ENV)
    if raw is None:
        legacy = os.environ.get(LEGACY_HEAVY_WORKERS_ENV)
        if legacy is not None:
            logger.warning(
                "%s is deprecated; it now sizes the heavy pool — use %s (and %s for I/O)",
                LEGACY_HEAVY_WORKERS_ENV,
                HEAVY_WORKERS_ENV,
                IO_WORKERS_ENV,
            )
            raw = legacy
    return _parse_workers(raw, DEFAULT_HEAVY_WORKERS, 1, 32)


def io_worker_count() -> int:
    return _parse_workers(os.environ.get(IO_WORKERS_ENV), DEFAULT_IO_WORKERS, 4, 128)


def heavy_executor() -> ThreadPoolExecutor:
    global _heavy
    with _lock:
        if _heavy is None:
            _heavy = ThreadPoolExecutor(
                max_workers=heavy_worker_count(), thread_name_prefix=HEAVY_THREAD_PREFIX
            )
        return _heavy


def light_executor() -> ThreadPoolExecutor:
    global _light
    with _lock:
        if _light is None:
            _light = ThreadPoolExecutor(
                max_workers=io_worker_count(), thread_name_prefix=IO_THREAD_PREFIX
            )
        return _light


async def run_heavy(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``fn(*args, **kwargs)`` on the heavy pool and await its result."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(heavy_executor(), partial(fn, *args, **kwargs))


def submit_heavy(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> asyncio.Future[T]:
    """Like :func:`run_heavy` but returns the future (for ``asyncio.shield``)."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(heavy_executor(), partial(fn, *args, **kwargs))


def install_default_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Make the light pool the loop's default executor (``asyncio.to_thread``)."""
    loop.set_default_executor(light_executor())


def shutdown_executors(*, wait: bool = True) -> None:
    global _heavy, _light
    with _lock:
        pools = [pool for pool in (_heavy, _light) if pool is not None]
        _heavy = None
        _light = None
    for pool in pools:
        pool.shutdown(wait=wait, cancel_futures=True)


def executor_stats() -> dict[str, int]:
    with _lock:
        heavy, light = _heavy, _light
    return {
        "heavy_workers": heavy_worker_count(),
        "io_workers": io_worker_count(),
        "heavy_threads": len(getattr(heavy, "_threads", ())) if heavy else 0,
        "io_threads": len(getattr(light, "_threads", ())) if light else 0,
    }
