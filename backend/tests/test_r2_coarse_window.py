from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from app.services.anime_matcher import AnimeMatcherService
from app.services.scene_aligner import _WindowEmbedCache


def test_missing_slot_stride_arithmetic():
    """Pins the stride slot-set arithmetic `window()` uses to decide what's
    missing: a stride=2 request over [0, 10] only asks for every 2nd native
    slot, not the full contiguous range."""
    slots: dict[int, object] = {}
    i0, i1, stride = 0, 10, 2
    missing = [k for k in range(i0, i1 + 1, stride) if k not in slots]
    assert missing == [0, 2, 4, 6, 8, 10]


def _bare_cache() -> _WindowEmbedCache:
    cache = _WindowEmbedCache.__new__(_WindowEmbedCache)  # skip heavy __init__
    cache.fps = 12.0
    cache.zoom_crop = lambda im, zoom: im
    cache.slots = {}
    cache.t_decode = 0.0
    cache.t_embed = 0.0
    cache._frames_lru = OrderedDict()
    cache._frames_lru_bytes = 0
    cache._frames_lru_budget = 0
    cache._staged = {}
    cache._staged_lock = threading.Lock()
    cache._inflight = {}
    return cache


def _fake_frame(t: float) -> tuple[float, Image.Image]:
    return (t, Image.new("RGB", (4, 4)))


def _patch_embed(monkeypatch):
    def fake_embed(images):
        return np.zeros((len(images), 3), dtype=np.float32)

    monkeypatch.setattr(AnimeMatcherService, "_embed_pil_batch", staticmethod(fake_embed))


def test_window_stride_merges_thinned_run_and_fills_only_visited_slots(monkeypatch):
    """A coarse (stride=2) window() call must (a) issue ONE decode run
    spanning the whole thinned span (not one tiny run per visited slot —
    that would defeat the whole point of halving decode/embed cost), and
    (b) leave the skipped (odd) native indices completely absent from
    `slots`, not back-filled to None. Back-filling skipped slots would
    poison them as "seen but empty" for a later fine (stride=1) pass."""
    _patch_embed(monkeypatch)
    cache = _bare_cache()
    cache.get_cap = lambda episode: "fake-cap"

    decode_calls: list[tuple[int, int, int]] = []

    def fake_decode_run(cap, r0, r1, stride=1):
        decode_calls.append((r0, r1, stride))
        return [_fake_frame(k / cache.fps) for k in range(r0, r1 + 1, stride)]

    cache._decode_run = fake_decode_run

    win = cache.window("ep", 1.0, 0.0, 10 / 12.0, stride=2)
    assert win is not None

    # single merged run over the full thinned span, not 6 singleton runs
    assert decode_calls == [(0, 10, 2)]

    slots = cache.slots[("ep", 1.0)]
    visited = {0, 2, 4, 6, 8, 10}
    skipped = {1, 3, 5, 7, 9}
    assert visited <= slots.keys()
    assert not (skipped & slots.keys())


def test_window_fine_pass_after_coarse_still_decodes_skipped_slots(monkeypatch):
    """After a coarse pass thins a span, a subsequent stride=1 (fine) request
    over the SAME span must still be able to fill the natives the coarse
    pass skipped — proving the coarse backfill didn't poison them."""
    _patch_embed(monkeypatch)
    cache = _bare_cache()
    cache.get_cap = lambda episode: "fake-cap"

    decode_calls: list[tuple[int, int, int]] = []

    def fake_decode_run(cap, r0, r1, stride=1):
        decode_calls.append((r0, r1, stride))
        return [_fake_frame(k / cache.fps) for k in range(r0, r1 + 1, stride)]

    cache._decode_run = fake_decode_run

    assert cache.window("ep", 1.0, 0.0, 10 / 12.0, stride=2) is not None
    decode_calls.clear()

    win = cache.window("ep", 1.0, 0.0, 10 / 12.0, stride=1)
    assert win is not None
    # the odd slots the coarse pass skipped must have been (re)decoded now
    assert decode_calls, "fine pass found nothing missing — coarse poisoned the skipped slots"
    redecoded = set()
    for r0, r1, _stride in decode_calls:
        redecoded.update(range(r0, r1 + 1))
    assert {1, 3, 5, 7, 9} <= redecoded

    slots = cache.slots[("ep", 1.0)]
    assert all(k in slots and slots[k] is not None for k in range(0, 11))


def test_window_stride_key_does_not_collide_with_stride1_frames_lru(monkeypatch):
    """A stride!=1 (coarse) run must use a namespaced `_frames_lru` key so it
    can never be handed a stale/poisoned entry cached under the plain
    stride=1 key (or vice versa)."""
    _patch_embed(monkeypatch)
    cache = _bare_cache()
    cache.get_cap = lambda episode: "fake-cap"

    # poison the plain stride=1 key with obviously-wrong data
    cache._frames_lru[("ep", 0, 10)] = [_fake_frame(999.0)]

    decode_calls: list[tuple[int, int, int]] = []

    def fake_decode_run(cap, r0, r1, stride=1):
        decode_calls.append((r0, r1, stride))
        return [_fake_frame(k / cache.fps) for k in range(r0, r1 + 1, stride)]

    cache._decode_run = fake_decode_run

    win = cache.window("ep", 1.0, 0.0, 10 / 12.0, stride=2)
    assert win is not None
    # must have decoded fresh instead of reusing the poisoned stride=1 entry
    assert decode_calls == [(0, 10, 2)]
    assert ("ep", 0, 10, 2) in cache._frames_lru
    # the poisoned legacy entry is untouched
    assert cache._frames_lru[("ep", 0, 10)] == [_fake_frame(999.0)]


def test_window_coarse_snaps_offgrid_linspace_frames_and_fine_pass_densifies(monkeypatch):
    """Regression for the linspace-vs-stride-grid mismatch (post-review
    fix): `_decode_run`'s real implementation subsamples via
    `np.linspace` over the FULL native-frame candidate list, which does
    NOT guarantee a returned frame's native slot lands on the assumed
    r0, r0+stride, ... grid. This fake `_decode_run` reproduces that by
    returning natives OFF the stride grid (e.g. odd indices) for a
    (0, 13) stride=2 run.

    Two invariants must hold after the coarse pass:
    1. Native indices that are off the stride grid AND were never used
       as the nearest proxy for a grid slot must be completely ABSENT
       from `slots` (not backfilled to None) — they were never a coarse
       "target" in the first place.
    2. A later stride=1 (fine) window() call over a narrower sub-range
       must still find real decode work to do (missing natives to fill),
       proving the coarse pass didn't poison the whole range to
       "seen but empty".
    """
    _patch_embed(monkeypatch)
    cache = _bare_cache()
    cache.get_cap = lambda episode: "fake-cap"

    decode_calls: list[tuple[int, int, int]] = []

    def fake_decode_run(cap, r0, r1, stride=1):
        decode_calls.append((r0, r1, stride))
        if stride == 1:
            return [_fake_frame(k / cache.fps) for k in range(r0, r1 + 1, stride)]
        # Mimic `_collect_frames_in_window_from_capture`'s real behaviour:
        # linspace subsampling over the full native candidate list lands
        # on natives that do NOT coincide with the assumed
        # range(r0, r1+1, stride) grid.
        return [_fake_frame(k / cache.fps) for k in [0, 1, 3, 5, 7, 9, 11, 13]]

    cache._decode_run = fake_decode_run

    win = cache.window("ep", 1.0, 0.0, 13 / 12.0, stride=2)
    assert win is not None
    # stride=2 from i0=0 only ever visits even natives, so the merged run's
    # r1 lands on 12 (the last even native <= 13), not 13 itself.
    assert decode_calls == [(0, 12, 2)]

    slots = cache.slots[("ep", 1.0)]
    stride_grid = set(range(0, 14, 2))  # {0, 2, 4, 6, 8, 10, 12}
    off_grid = {1, 3, 5, 7, 9, 11, 13}
    # Every slots key must be on the assumed stride grid — an off-grid
    # native decoded by the fake run (e.g. 1, 3, 5, ...) must never
    # itself become a `slots` key, whether snapped-away-from or simply
    # never used as anyone's nearest proxy.
    assert slots.keys() <= stride_grid
    assert not (off_grid & slots.keys())
    # Every stride-grid slot is present (real value from a nearby
    # snapped native, or a genuine None when nothing was close enough).
    assert stride_grid <= slots.keys()

    decode_calls.clear()
    # A fine (stride=1) pass over a narrow sub-range must still find
    # off-grid natives it can decode — proving the coarse pass didn't
    # silently mark the whole span "seen but empty". Natives 3 and 5 were
    # never touched by the coarse pass (they lost the nearest-neighbour
    # tie to 4's slot and to each other), so they must show up as
    # genuinely missing here.
    fine = cache.window("ep", 1.0, 2 / 12.0, 6 / 12.0, stride=1)
    assert fine is not None
    assert decode_calls, "fine pass found nothing missing — coarse poisoned the span"
    redecoded: set[int] = set()
    for r0, r1, _stride in decode_calls:
        redecoded.update(range(r0, r1 + 1))
    assert {3, 5} <= redecoded
    # the densified return grid actually contains the newly-decoded natives
    f_times, _f_embs = fine
    f_slots = {int(round(t * cache.fps)) for t in f_times}
    assert {3, 5} <= f_slots


def test_window_stride2_does_not_consume_staged_stride1_run(monkeypatch):
    """prefetch() only ever stages full (stride=1) runs under a plain
    3-tuple key. A coarse (stride=2) window() call whose own merged run
    happens to compute the identical (episode, r0, r1) key must not pop
    that staged full run out from under whatever stride=1 consumer it was
    staged for."""
    _patch_embed(monkeypatch)
    cache = _bare_cache()
    cache.get_cap = lambda episode: "fake-cap"

    staged_full_run = [_fake_frame(k / cache.fps) for k in range(0, 11)]
    cache._staged[("ep", 0, 10)] = staged_full_run

    decode_calls: list[tuple[int, int, int]] = []

    def fake_decode_run(cap, r0, r1, stride=1):
        decode_calls.append((r0, r1, stride))
        return [_fake_frame(k / cache.fps) for k in range(r0, r1 + 1, stride)]

    cache._decode_run = fake_decode_run

    win = cache.window("ep", 1.0, 0.0, 10 / 12.0, stride=2)
    assert win is not None
    # the coarse call decoded its own thinned run instead of consuming staged
    assert decode_calls == [(0, 10, 2)]
    assert ("ep", 0, 10) in cache._staged
