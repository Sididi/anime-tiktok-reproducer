"""Lightweight process-memory diagnostics and native heap reclamation."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import logging
import os
import resource
import sys
import threading
import time
from typing import Any


logger = logging.getLogger("uvicorn.error")

# A full release right after every heavy batch is what the UI feels as a
# freeze at "job complete"; once every 30 s is plenty for arena hygiene.
RELEASE_DEBOUNCE_SECONDS = 30.0

_release_lock = threading.Lock()
_release_in_flight = False
_last_release_monotonic: float | None = None


def _status_values() -> dict[str, int]:
    """Return selected /proc status values in KiB (or raw count for Threads)."""
    wanted = {"VmRSS", "VmHWM", "VmSwap", "Threads"}
    values: dict[str, int] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as status_file:
            for line in status_file:
                key, separator, remainder = line.partition(":")
                if not separator or key not in wanted:
                    continue
                raw_value = remainder.strip().split()[0]
                values[key] = int(raw_value)
    except (OSError, ValueError, IndexError):
        pass
    return values


def _resource_counts() -> dict[str, Any]:
    """Inspect already-imported services without importing heavyweight stacks."""
    result: dict[str, Any] = {}

    matcher_module = sys.modules.get("app.services.anime_matcher")
    matcher = getattr(matcher_module, "AnimeMatcherService", None)
    if matcher is not None:
        manager = getattr(matcher, "_index_manager", None)
        loaded_series = list(getattr(manager, "_loaded_series", ())) if manager else []
        loaded_frames = 0
        if manager is not None:
            for series in loaded_series:
                index = getattr(manager, "series_indices", {}).get(series)
                loaded_frames += int(getattr(index, "ntotal", 0) or 0)
        result.update(
            sscd_loaded=int(getattr(matcher, "_embedder", None) is not None),
            faiss_loaded_series=len(loaded_series),
            faiss_loaded_frames=loaded_frames,
            frame_embedding_cache=len(
                getattr(matcher, "_video_frame_embedding_cache", ())
            ),
        )

    transcriber_module = sys.modules.get("app.services.transcriber")
    transcriber = getattr(transcriber_module, "TranscriberService", None)
    if transcriber is not None:
        result.update(
            whisper_asr_models=len(getattr(transcriber, "_asr_models", {})),
            whisper_align_models=len(getattr(transcriber, "_align_models", {})),
            active_transcriptions=int(
                getattr(transcriber, "_active_transcriptions", 0)
            ),
        )

    aligner_module = sys.modules.get("app.services.scene_aligner")
    aligner = getattr(aligner_module, "SceneAlignerService", None)
    if aligner is not None:
        result["episode_grid_cache"] = len(
            getattr(aligner, "_episode_grid_cache", ())
        )

    pynv_module = sys.modules.get("app.services.pynv_decode")
    pool = getattr(pynv_module, "_POOL", None)
    if pool is not None:
        try:
            result["pynv_sessions"] = int(pool.session_count())
        except Exception:
            pass

    return result


def memory_snapshot(**extra: Any) -> dict[str, Any]:
    """Capture process RAM, swap, threads, CUDA, and heavyweight cache counts."""
    status = _status_values()
    snapshot: dict[str, Any] = {
        "pid": os.getpid(),
        "rss_mib": round(status.get("VmRSS", 0) / 1024, 1),
        "peak_rss_mib": round(
            status.get(
                "VmHWM",
                int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            )
            / 1024,
            1,
        ),
        "swap_mib": round(status.get("VmSwap", 0) / 1024, 1),
        "threads": status.get("Threads", 0),
    }

    torch = sys.modules.get("torch")
    cuda = getattr(torch, "cuda", None)
    try:
        if cuda is not None and cuda.is_initialized():
            snapshot["cuda_allocated_mib"] = round(
                cuda.memory_allocated() / (1024 * 1024), 1
            )
            snapshot["cuda_reserved_mib"] = round(
                cuda.memory_reserved() / (1024 * 1024), 1
            )
    except Exception:
        pass

    snapshot.update(_resource_counts())
    snapshot.update(extra)
    return snapshot


def log_memory(stage: str, **extra: Any) -> dict[str, Any]:
    """Log and return a structured memory snapshot."""
    snapshot = memory_snapshot(stage=stage, **extra)
    logger.info("runtime_memory %s", snapshot)
    return snapshot


def trim_native_heap() -> bool:
    """Ask glibc to return fully free native heap pages to the operating system."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None)
        malloc_trim = libc.malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        return bool(malloc_trim(0))
    except (AttributeError, OSError):
        return False


def release_unused_memory(stage: str, **extra: Any) -> dict[str, Any]:
    """Collect Python/CUDA garbage, trim glibc, then record the resulting state.

    ``gc.collect()`` holds the GIL for its whole duration wherever it runs, so
    callers on the event loop should prefer :func:`schedule_release_unused_memory`,
    which debounces it and moves the GIL-releasing parts (CUDA cache flush,
    ``malloc_trim``, the ``/proc`` walk) off the loop thread.
    """
    gc.collect()

    torch = sys.modules.get("torch")
    cuda = getattr(torch, "cuda", None)
    try:
        if cuda is not None and cuda.is_initialized():
            cuda.empty_cache()
    except Exception:
        pass

    trimmed = trim_native_heap()
    return log_memory(stage, native_heap_trimmed=trimmed, **extra)


def schedule_release_unused_memory(stage: str, **extra: Any) -> bool:
    """Debounced :func:`release_unused_memory` off the event loop.

    Returns ``True`` when a release was started (on the loop's default
    executor when called from a running loop, inline otherwise) and ``False``
    when one ran less than :data:`RELEASE_DEBOUNCE_SECONDS` ago or is still in
    flight — in which case only a memory snapshot is logged.
    """
    global _release_in_flight, _last_release_monotonic
    now = time.monotonic()
    with _release_lock:
        recent = (
            _last_release_monotonic is not None
            and now - _last_release_monotonic < RELEASE_DEBOUNCE_SECONDS
        )
        if _release_in_flight or recent:
            log_memory(stage, memory_release="skipped", **extra)
            return False
        _release_in_flight = True
        _last_release_monotonic = now

    def _run() -> None:
        global _release_in_flight
        try:
            release_unused_memory(stage, **extra)
        except Exception:
            logger.exception("release_unused_memory failed")
        finally:
            with _release_lock:
                _release_in_flight = False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _run()
        return True
    loop.run_in_executor(None, _run)
    return True


def reset_release_debounce() -> None:
    """Forget the last release time (tests)."""
    global _release_in_flight, _last_release_monotonic
    with _release_lock:
        _release_in_flight = False
        _last_release_monotonic = None
