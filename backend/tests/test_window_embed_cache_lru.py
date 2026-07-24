from __future__ import annotations

import sys
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
