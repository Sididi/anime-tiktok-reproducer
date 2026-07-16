"""Lightweight process-memory diagnostics and native heap reclamation."""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import resource
import sys
from typing import Any


logger = logging.getLogger("uvicorn.error")


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
    """Collect Python/CUDA garbage, trim glibc, then record the resulting state."""
    gc.collect()
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
