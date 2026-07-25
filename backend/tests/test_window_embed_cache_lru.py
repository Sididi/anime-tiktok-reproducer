from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app.services.scene_aligner import _WindowEmbedCache


def _frames(n: int, w: int = 100, h: int = 50):
    return [(float(i), Image.new("RGB", (w, h))) for i in range(n)]


def _cache_with_budget(budget_bytes: int) -> _WindowEmbedCache:
    cache = _WindowEmbedCache.__new__(_WindowEmbedCache)  # skip heavy __init__
    cache._frames_lru = OrderedDict()
    cache._frames_lru_bytes = 0
    cache._frames_lru_budget = budget_bytes
    return cache


def test_frames_nbytes_counts_rgb_pixels():
    frames = _frames(2, w=100, h=50)
    assert _WindowEmbedCache._frames_nbytes(frames) == 2 * 100 * 50 * 3


def test_trim_respects_byte_budget_and_lru_order():
    one_run = 100 * 50 * 3 * 2  # 2 frames per run
    cache = _cache_with_budget(budget_bytes=3 * one_run)
    for k in range(5):
        run = _frames(2)
        cache._frames_lru[("ep", k, k + 1)] = run
        cache._frames_lru_bytes += _WindowEmbedCache._frames_nbytes(run)
        cache._trim_frames_lru()
    assert cache._frames_lru_bytes <= 3 * one_run
    # oldest runs evicted first
    assert ("ep", 0, 1) not in cache._frames_lru
    assert ("ep", 4, 5) in cache._frames_lru


def test_zero_budget_keeps_legacy_six_window_bound():
    cache = _cache_with_budget(budget_bytes=0)
    for k in range(8):
        run = _frames(1)
        cache._frames_lru[("ep", k, k)] = run
        cache._frames_lru_bytes += _WindowEmbedCache._frames_nbytes(run)
        cache._trim_frames_lru()
    assert len(cache._frames_lru) == 6


def test_prefetch_depth_fallbacks_preserve_legacy_constants():
    """Verify that prefetch() and prefetch_probe() each preserve their own
    legacy depth constants when the knob is unset. vF13 caught a regression
    where collapsing both to one shared default silently changed one of them
    even at the "off" setting, breaking hash-identity on 411f (doubt_reasons
    flip)."""
    cache = _WindowEmbedCache.__new__(_WindowEmbedCache)  # skip heavy __init__
    cache._prefetch_depth = None
    # When unset, prefetch() falls back to 8, prefetch_probe() to 12.
    assert cache._prefetch_depth_limit() == 8
    assert cache._probe_depth_limit() == 12

    # When explicitly set, both use the same override value.
    cache._prefetch_depth = 16
    assert cache._prefetch_depth_limit() == 16
    assert cache._probe_depth_limit() == 16


def _probe_gate_cache() -> _WindowEmbedCache:
    cache = _WindowEmbedCache.__new__(_WindowEmbedCache)  # skip heavy __init__
    cache._staged = {}
    cache._inflight = {}
    cache._staged_lock = threading.Lock()
    return cache


def test_probe_staged_true_when_key_in_staged():
    """A4 escalation gate: probe_staged() must report True for a key that
    prefetch_probe already staged, so the parallel precompute path is
    allowed to call probe_frames() (which will hit the staged dict, not
    the thread-unsafe get_cap() fallback)."""
    cache = _probe_gate_cache()
    cache._staged[("probe", "ep1", 12.345)] = [(12.3, object())]
    assert cache.probe_staged("ep1", 12.345) is True


def test_probe_staged_true_when_key_inflight():
    """Same gate, but for a probe still being decoded by a prefetch worker
    (future present in _inflight, not yet moved to _staged) — probe_frames()
    will block on the future then find it in _staged, so this must also
    count as safe for the parallel path."""
    cache = _probe_gate_cache()
    cache._inflight[("probe", "ep1", 12.345)] = object()
    assert cache.probe_staged("ep1", 12.345) is True


def test_probe_staged_false_when_key_absent():
    """No prefetch_probe ever ran for this (episode, pred) — probe_frames()
    would fall through to the unlocked get_cap()/self.caps mutation, so the
    A4 escalation must leave this candidate out of the parallel pool and let
    the existing sequential cand_rect path handle it instead."""
    cache = _probe_gate_cache()
    cache._staged[("probe", "ep1", 12.345)] = [(12.3, object())]
    assert cache.probe_staged("ep2", 99.999) is False
    # rounding must match prefetch_probe's own `round(pred, 3)` key shape
    assert cache.probe_staged("ep1", 12.3450001) is True
    assert cache.probe_staged("ep1", 12.35) is False
