"""Tests for the Pure-mode cleanup service (geometry, detection, chunking)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.cleanup import CleanupZone
from app.services.video_cleanup_service import (
    CLIP_CACHE_VERSION,
    CLIP_CONTEXT_FRAMES,
    CLIP_MAX_FRAMES,
    TEXT_MASK_WINDOW_FRAMES,
    TEXT_SCORE_OFF,
    TEXT_SCORE_ON,
    VideoCleanupService,
    _ZonePlan,
)


def _zone(kind: str = "subtitle", **overrides) -> CleanupZone:
    payload = {"kind": kind, "x": 0.1, "y": 0.6, "w": 0.8, "h": 0.2}
    payload.update(overrides)
    return CleanupZone(**payload)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def test_zone_rect_px_clamps_to_frame():
    zone = _zone(x=0.9, y=0.9, w=0.5, h=0.5)  # validator clamps w/h to 1-x/1-y
    x, y, w, h = VideoCleanupService._zone_rect_px(zone, 1080, 1920)
    assert x + w <= 1080
    assert y + h <= 1920
    assert w > 0 and h > 0


def test_crop_region_alignment_and_margin():
    rect = (100, 1200, 880, 380)
    cx, cy, cw, ch = VideoCleanupService._crop_region(rect, 1080, 1920)
    # Aligned origin, /8 size, contains the rect + some margin.
    assert cx % 16 == 0 and cy % 16 == 0
    assert cw % 8 == 0 and ch % 8 == 0
    assert cx <= rect[0]
    assert cy <= rect[1]
    assert cx + cw >= rect[0] + rect[2]
    assert cy + ch >= rect[1] + rect[3]
    assert cx + cw <= 1080
    assert cy + ch <= 1920


def test_crop_region_at_frame_edges():
    rect = (0, 0, 64, 64)
    cx, cy, cw, ch = VideoCleanupService._crop_region(rect, 1080, 1920)
    assert cx == 0 and cy == 0
    assert cw >= 64 and ch >= 64


# ---------------------------------------------------------------------------
# Text detection
# ---------------------------------------------------------------------------

def test_text_mask_detects_white_text_on_dark_background():
    import cv2

    crop = np.full((80, 320, 3), 40, dtype=np.uint8)  # dark background
    cv2.putText(
        crop, "SOME SUBTITLE", (10, 50), cv2.FONT_HERSHEY_SIMPLEX,
        1.0, (255, 255, 255), 2,
    )
    mask = VideoCleanupService._text_mask(crop)
    assert mask.sum() > 100  # a real amount of text pixels


def test_text_mask_rejects_flat_bright_background():
    crop = np.full((80, 320, 3), 235, dtype=np.uint8)  # bright sky, no edges
    mask = VideoCleanupService._text_mask(crop)
    assert mask.sum() / mask.size < 0.01


def test_text_mask_rejects_saturated_colors():
    crop = np.zeros((80, 320, 3), dtype=np.uint8)
    crop[..., 2] = 255  # pure red (BGR)
    mask = VideoCleanupService._text_mask(crop)
    assert mask.sum() == 0


# ---------------------------------------------------------------------------
# Hysteresis + smoothing
# ---------------------------------------------------------------------------

def test_scores_to_spans_hysteresis_and_padding():
    on = TEXT_SCORE_ON * 2
    off = 0.0
    scores = [off] * 20 + [on] * 30 + [off] * 20
    spans = VideoCleanupService._scores_to_spans(scores, 70)
    assert len(spans) == 1
    start, end = spans[0]
    assert start <= 20 and end >= 50  # padded outward


def test_scores_to_spans_closes_small_gaps():
    on = TEXT_SCORE_ON * 2
    off = 0.0
    scores = [off] * 10 + [on] * 20 + [off] * 3 + [on] * 20 + [off] * 10
    spans = VideoCleanupService._scores_to_spans(scores, 63)
    assert len(spans) == 1  # the 3-frame gap is closed


def test_scores_to_spans_drops_tiny_islands():
    on = TEXT_SCORE_ON * 2
    off = 0.0
    scores = [off] * 30 + [on] * 2 + [off] * 30
    spans = VideoCleanupService._scores_to_spans(scores, 62)
    assert spans == []


def test_scores_to_spans_hysteresis_keeps_low_scores_inside_span():
    on = TEXT_SCORE_ON * 2
    mid = (TEXT_SCORE_ON + TEXT_SCORE_OFF) / 2  # below on, above off
    scores = [0.0] * 10 + [on] * 5 + [mid] * 20 + [0.0] * 10
    spans = VideoCleanupService._scores_to_spans(scores, 45)
    assert len(spans) == 1
    start, end = spans[0]
    assert end >= 35  # the mid-score tail stays inside the span


# ---------------------------------------------------------------------------
# Clip chunking
# ---------------------------------------------------------------------------

def _plan(kind: str, spans: list[tuple[int, int]]) -> _ZonePlan:
    plan = _ZonePlan(
        zone=_zone(kind),
        rect=(100, 1200, 880, 380),
        crop=(96, 1184, 896, 400),
    )
    plan.spans = spans
    return plan


def test_short_span_single_clip_with_context():
    plan = _plan("subtitle", [(100, 160)])
    clips = VideoCleanupService._spans_to_clips(plan, 1000)
    assert len(clips) == 1
    clip = clips[0]
    assert clip.frame_start == 100 - CLIP_CONTEXT_FRAMES
    assert clip.frame_end == 160 + CLIP_CONTEXT_FRAMES
    assert (clip.out_start, clip.out_end) == (100, 160)
    assert (clip.mask_start, clip.mask_end) == (100, 160)


def test_long_span_chunks_cover_everything():
    plan = _plan("watermark", [(0, 900)])
    clips = VideoCleanupService._spans_to_clips(plan, 900)
    # Chunks tile the active region completely and stay model-sized.
    assert clips[0].out_start == 0
    assert clips[-1].out_end == 900
    for previous, current in zip(clips, clips[1:]):
        assert current.out_start == previous.out_end
    for clip in clips:
        assert clip.frame_end - clip.frame_start <= CLIP_MAX_FRAMES
        # Mid-span windows keep the mask active over the whole span.
        assert (clip.mask_start, clip.mask_end) == (0, 900)


def test_span_at_video_start_has_no_negative_context():
    plan = _plan("subtitle", [(0, 40)])
    clips = VideoCleanupService._spans_to_clips(plan, 500)
    assert clips[0].frame_start == 0


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------

def test_build_clip_masks_context_frames_are_clean():
    import cv2

    plan = _plan("subtitle", [(10, 20)])
    clips = VideoCleanupService._spans_to_clips(plan, 100)
    clip = clips[0]
    cx, cy, cw, ch = plan.crop
    length = clip.frame_end - clip.frame_start
    frames = np.full((length, ch, cw, 3), 30, dtype=np.uint8)
    # Draw white text inside the rect (crop-local coords) on active frames.
    rx, ry = plan.rect[0] - cx, plan.rect[1] - cy
    for i in range(length):
        absolute = clip.frame_start + i
        if clip.mask_start <= absolute < clip.mask_end:
            cv2.putText(
                frames[i], "HELLO", (rx + 10, ry + 60),
                cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3,
            )

    masks = VideoCleanupService._build_clip_masks(clip, frames)
    assert masks.shape == (length, ch, cw)
    for i in range(length):
        absolute = clip.frame_start + i
        if clip.mask_start <= absolute < clip.mask_end:
            assert masks[i].any(), "active frame should be masked"
        else:
            assert not masks[i].any(), "context frame must stay clean"


def test_build_clip_masks_watermark_uses_full_rect():
    plan = _plan("watermark", [(0, 30)])
    clips = VideoCleanupService._spans_to_clips(plan, 30)
    clip = clips[0]
    cx, cy, cw, ch = plan.crop
    length = clip.frame_end - clip.frame_start
    frames = np.zeros((length, ch, cw, 3), dtype=np.uint8)
    masks = VideoCleanupService._build_clip_masks(clip, frames)
    rx, ry = plan.rect[0] - cx, plan.rect[1] - cy
    w, h = plan.rect[2], plan.rect[3]
    assert (masks[0][ry : ry + h, rx : rx + w] == 255).all()
    # Nothing outside the rect.
    assert masks[0].sum() == 255 * w * h


def _subtitle_clip_frames(span=(10, 70), total=200):
    """Dark frames + clip covering the given span; returns (clip, frames, geo)."""
    plan = _plan("subtitle", [span])
    clip = VideoCleanupService._spans_to_clips(plan, total)[0]
    cx, cy, cw, ch = plan.crop
    length = clip.frame_end - clip.frame_start
    frames = np.full((length, ch, cw, 3), 30, dtype=np.uint8)
    rx, ry = plan.rect[0] - cx, plan.rect[1] - cy
    return clip, frames, (rx, ry, plan.rect[2], plan.rect[3])


def _draw_text(frames, i, x, y):
    import cv2

    cv2.putText(
        frames[i], "HELLO", (x, y),
        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3,
    )


def test_build_clip_masks_track_text_position_change():
    # Text sits in the left half of the rect, then jumps to the right half:
    # per-frame masks must follow it instead of unioning both positions.
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    switch = clip.mask_start + 30
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if clip.mask_start <= absolute < clip.mask_end:
            if absolute < switch:
                _draw_text(frames, i, rx + 10, ry + 60)
            else:
                _draw_text(frames, i, rx + w - 300, ry + 60)

    masks = VideoCleanupService._build_clip_masks(clip, frames)
    margin = 2 * TEXT_MASK_WINDOW_FRAMES
    early = switch - clip.frame_start - margin - 5  # well before the switch
    late = switch - clip.frame_start + margin + 5  # well after the switch
    mid_col = rx + w // 2
    assert masks[early][:, :mid_col].any(), "early mask should cover left text"
    assert not masks[early][:, mid_col:].any(), "no right-side mask before switch"
    assert masks[late][:, mid_col:].any(), "late mask should cover right text"
    assert not masks[late][:, :mid_col].any(), "left-side mask must decay after switch"


def test_build_clip_masks_window_covers_neighbors():
    # Text on a single active frame: neighbours within the ±window inherit
    # its mask (stride-blend correctness), frames beyond stay empty.
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    k_abs = clip.mask_start + 25
    k = k_abs - clip.frame_start
    _draw_text(frames, k, rx + 10, ry + 60)

    masks = VideoCleanupService._build_clip_masks(clip, frames)
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if not (clip.mask_start <= absolute < clip.mask_end):
            assert not masks[i].any()
        elif abs(i - k) <= TEXT_MASK_WINDOW_FRAMES:
            assert masks[i].any(), f"frame {i} inside the window must be masked"
        else:
            assert not masks[i].any(), f"frame {i} outside the window must be empty"


def test_build_clip_masks_empty_union_falls_back_full_rect():
    # Detection fired but no strokes are found anywhere: keep the historical
    # full-rect fallback rather than leaving the text in place.
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    masks = VideoCleanupService._build_clip_masks(clip, frames)
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if clip.mask_start <= absolute < clip.mask_end:
            assert (masks[i][ry : ry + h, rx : rx + w] == 255).all()
            assert masks[i].sum() == 255 * w * h
        else:
            assert not masks[i].any()


def test_build_clip_masks_halfres_input_matches_recompute():
    # The decode thread hands half-res per-frame masks; the preview path
    # recomputes them from frames. Both must produce identical output.
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if clip.mask_start <= absolute < clip.mask_end:
            _draw_text(frames, i, rx + 10 + (i % 7) * 20, ry + 60)

    recomputed = VideoCleanupService._build_clip_masks(clip, frames)
    supplied = VideoCleanupService._build_clip_masks(
        clip, frames,
        text_masks=VideoCleanupService._per_frame_text_masks(clip, frames),
    )
    assert (recomputed == supplied).all()


# ---------------------------------------------------------------------------
# Karaoke word-highlight subtitles
# ---------------------------------------------------------------------------

def _draw_word(frames, i, x, y, text="AAAA", color=(255, 255, 255)):
    import cv2

    # Dark outline first, then the fill — the karaoke style under test.
    cv2.putText(
        frames[i], text, (x, y),
        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 7,
    )
    cv2.putText(
        frames[i], text, (x, y),
        cv2.FONT_HERSHEY_SIMPLEX, 1.5, color, 3,
    )


HIGHLIGHT_BGR = (0, 220, 255)  # saturated yellow: bright, sat >> 60


def test_build_clip_masks_karaoke_highlight_word_covered():
    # Word A flips white -> saturated yellow for longer than the ±window can
    # bridge; the corroborated highlight class must keep it masked anyway.
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    hi_start, hi_end = clip.mask_start + 20, clip.mask_start + 50
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if not (clip.mask_start <= absolute < clip.mask_end):
            continue
        a_color = HIGHLIGHT_BGR if hi_start <= absolute < hi_end else (255, 255, 255)
        _draw_word(frames, i, rx + 10, ry + 60, "AAAA", a_color)
        _draw_word(frames, i, rx + 320, ry + 60, "BBBB", (255, 255, 255))

    masks = VideoCleanupService._build_clip_masks(clip, frames)
    word_a = (slice(ry + 15, ry + 70), slice(rx + 5, rx + 160))
    for absolute in range(hi_start + TEXT_MASK_WINDOW_FRAMES + 2,
                          hi_end - TEXT_MASK_WINDOW_FRAMES - 2):
        i = absolute - clip.frame_start
        assert masks[i][word_a].any(), (
            f"highlighted word must stay masked mid-highlight (frame {i})"
        )
        assert masks[i][:, rx + 320 : rx + 470].any(), "white word must be masked"


def test_build_clip_masks_karaoke_single_word_line():
    # A one-word line fully highlighted has almost no white pixels: the
    # LINE_MIN_WHITE_PIXELS guard must keep those frames in the same line so
    # the earlier white phase corroborates them.
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    hi_start = clip.mask_start + 25
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if not (clip.mask_start <= absolute < clip.mask_end):
            continue
        color = HIGHLIGHT_BGR if absolute >= hi_start else (255, 255, 255)
        _draw_word(frames, i, rx + 10, ry + 60, "AAAA", color)

    masks = VideoCleanupService._build_clip_masks(clip, frames)
    word_a = (slice(ry + 15, ry + 70), slice(rx + 5, rx + 160))
    for absolute in range(hi_start + TEXT_MASK_WINDOW_FRAMES + 2,
                          clip.mask_end - TEXT_MASK_WINDOW_FRAMES - 2):
        i = absolute - clip.frame_start
        assert masks[i][word_a].any(), (
            f"single-word line must stay masked while highlighted (frame {i})"
        )


def test_build_clip_masks_saturated_background_not_corroborated():
    # A bright saturated blob that is NEVER white text must not be swept into
    # the mask — and its presence must not change the masks at all (byte
    # no-regression for colorful backgrounds).
    clip, frames, (rx, ry, w, h) = _subtitle_clip_frames()
    for i in range(frames.shape[0]):
        absolute = clip.frame_start + i
        if clip.mask_start <= absolute < clip.mask_end:
            _draw_word(frames, i, rx + 10, ry + 60, "AAAA", (255, 255, 255))
    with_blob = frames.copy()
    blob = (slice(ry + 250, ry + 330), slice(rx + 600, rx + 760))
    with_blob[:, blob[0], blob[1]] = (0, 0, 255)  # bright red rectangle

    masks_plain = VideoCleanupService._build_clip_masks(clip, frames)
    masks_blob = VideoCleanupService._build_clip_masks(clip, with_blob)
    assert (masks_plain == masks_blob).all(), "blob must not affect masks"
    assert not masks_blob[:, ry + 260 : ry + 320, rx + 610 : rx + 750].any()


def test_segment_lines_splits_on_text_change_only():
    mh, mw = 40, 200
    left = np.zeros((mh, mw), dtype=bool)
    left[10:30, 10:80] = True
    right = np.zeros((mh, mw), dtype=bool)
    right[10:30, 120:190] = True
    sparse = np.zeros((mh, mw), dtype=bool)
    sparse[12, 12:20] = True  # < LINE_MIN_WHITE_PIXELS: cannot vote

    masks = [None, left, left, sparse, left, right, right, None, right]
    lines = VideoCleanupService._segment_lines(masks)
    # left-run (sparse frame swallowed) | right-run | trailing right.
    assert lines == [[1, 2, 3, 4], [5, 6], [8]]

def test_temporal_stride_blend_restricted_to_neighbor_masks(monkeypatch):
    # Fill of a model frame outside its own mask is that frame's ORIGINAL
    # pixels (subtitle strokes included): the skipped-frame blend must only
    # use a neighbour's fill where that neighbour was actually repaired.
    def fake_ladder(cls, frames_sub, masks_sub, **_kwargs):
        out = np.zeros_like(frames_sub)
        for k in range(frames_sub.shape[0]):
            out[k] = 100 + k
        return out

    monkeypatch.setattr(
        VideoCleanupService, "_inpaint_ladder_inner", classmethod(fake_ladder)
    )

    length = 12  # stride 3 (crop >= 320) -> keep = {0, 3, 6, 9, 11}
    frames = np.full((length, 64, 400, 3), 7, dtype=np.uint8)
    masks = np.zeros((length, 64, 400), dtype=np.uint8)
    # Pixel A: skipped frame 1 covered by both neighbours (0 and 3).
    masks[0][10, 10] = masks[1][10, 10] = masks[3][10, 10] = 255
    # Pixel B: covered by prev (0) only.
    masks[0][20, 20] = masks[1][20, 20] = 255
    # Pixel C: covered by neither neighbour -> original must survive.
    masks[1][30, 30] = 255

    result = VideoCleanupService._inpaint_temporal_strided(frames, masks)
    fill_p, fill_n = 100, 101  # fake fills of kept frames 0 and 3
    assert (result[1][10, 10] == (fill_p + fill_n) // 2).all()
    assert (result[1][20, 20] == fill_p).all()
    assert (result[1][30, 30] == 7).all()
    # Model frames keep their own fill inside their mask.
    assert (result[0][10, 10] == fill_p).all()


def test_temporal_stride_static_masks_match_legacy_blend(monkeypatch):
    # Static masks (watermarks / full-rect fallback): every skipped pixel is
    # covered by both neighbours -> behavior identical to the old 50/50.
    def fake_ladder(cls, frames_sub, masks_sub, **_kwargs):
        out = np.zeros_like(frames_sub)
        for k in range(frames_sub.shape[0]):
            out[k] = 40 + 10 * k
        return out

    monkeypatch.setattr(
        VideoCleanupService, "_inpaint_ladder_inner", classmethod(fake_ladder)
    )

    length = 12
    frames = np.full((length, 64, 400, 3), 7, dtype=np.uint8)
    masks = np.zeros((length, 64, 400), dtype=np.uint8)
    masks[:, 10:30, 10:30] = 255

    result = VideoCleanupService._inpaint_temporal_strided(frames, masks)
    # Skipped frame 1 between kept 0 (fill 40) and 3 (fill 50).
    assert (result[1][10:30, 10:30] == (40 + 50) // 2).all()
    assert (result[1][40, 40] == 7).all()


# ---------------------------------------------------------------------------
# Composite alpha
# ---------------------------------------------------------------------------

def test_composite_alpha_erode_and_feather():
    masks = np.zeros((1, 60, 60), dtype=np.uint8)
    masks[0, 10:50, 10:50] = 255
    alpha = VideoCleanupService._composite_alpha(masks)
    assert alpha.shape == (1, 60, 60, 1)
    assert alpha[0, 30, 30, 0] > 0.99  # fully repaired at the center
    # The 0.5 crossing sits strictly INSIDE the original mask edge …
    assert alpha[0, 10, 30, 0] < 0.5
    assert alpha[0, 14, 30, 0] > 0.5
    # … and the outside fades to ~0 within a few px.
    assert alpha[0, 4, 30, 0] < 0.02


def test_composite_alpha_thin_mask_survives_erosion():
    masks = np.zeros((1, 60, 60), dtype=np.uint8)
    masks[0, 30:32, 10:50] = 255  # thinner than the erosion kernel
    alpha = VideoCleanupService._composite_alpha(masks)
    assert alpha.max() > 0.25  # not erased


# ---------------------------------------------------------------------------
# Cache versioning
# ---------------------------------------------------------------------------

def test_clip_cache_name_versioned():
    plan = _plan("subtitle", [(100, 160)])
    clip = VideoCleanupService._spans_to_clips(plan, 1000)[0]
    assert clip.cache_name.endswith("_v3.npz")


def test_save_clip_atomic_deflates_and_roundtrips(tmp_path):
    import zipfile

    frames = np.random.default_rng(0).integers(
        0, 256, (8, 16, 32, 3), dtype=np.uint8
    )
    masks = np.zeros((8, 16, 32), dtype=np.uint8)
    masks[:, 4:12, 8:24] = 255
    path = tmp_path / f"subtitle_x_000000_000008_v{CLIP_CACHE_VERSION}.npz"

    VideoCleanupService._save_clip_atomic(path, frames, masks)

    assert path.exists()
    assert not list(tmp_path.glob("*.tmp.npz"))  # atomic rename cleaned up
    data = np.load(path)
    np.testing.assert_array_equal(data["frames"], frames)
    np.testing.assert_array_equal(data["masks"], masks)
    with zipfile.ZipFile(path) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in infos.values())
        # Binary masks are the compression win the cache relies on.
        assert infos["masks.npy"].compress_size < masks.nbytes / 10


def test_prune_stale_cache_drops_old_versions_and_partials(tmp_path):
    current = tmp_path / f"subtitle_a_000000_000220_v{CLIP_CACHE_VERSION}.npz"
    stale = tmp_path / f"subtitle_a_000000_000220_v{CLIP_CACHE_VERSION - 1}.npz"
    partial = tmp_path / (
        f"subtitle_a_000220_000440_v{CLIP_CACHE_VERSION}.npz.tmp.npz"
    )
    for f in (current, stale, partial):
        f.write_bytes(b"x")

    VideoCleanupService._prune_stale_cache(tmp_path)

    assert current.exists()
    assert not stale.exists()
    assert not partial.exists()


# ---------------------------------------------------------------------------
# Canary retry
# ---------------------------------------------------------------------------

def test_canary_retry_fp32_and_restore(monkeypatch):
    from app.services.propainter_adapter import ProPainterEngine

    load_calls: list[bool] = []
    inpaint_calls: list[int] = []

    def fake_load(cls, *, fp16=True, progress_cb=None):
        load_calls.append(fp16)

    def fake_inpaint(cls, frames_rgb, masks, **_kwargs):
        inpaint_calls.append(1)
        value = 0 if len(inpaint_calls) == 1 else 128  # black, then sane
        return np.full_like(frames_rgb, value)

    monkeypatch.setattr(ProPainterEngine, "load", classmethod(fake_load))
    monkeypatch.setattr(ProPainterEngine, "inpaint_clip", classmethod(fake_inpaint))

    frames = np.full((8, 128, 128, 3), 50, dtype=np.uint8)
    masks = np.zeros((8, 128, 128), dtype=np.uint8)
    masks[:, 40:90, 40:90] = 255

    result = VideoCleanupService._inpaint_ladder_inner(frames, masks)
    assert len(inpaint_calls) == 2  # one retry, no more
    assert (result[4][60, 60] == 128).all()  # the retried fill won
    assert load_calls == [False, True]  # fp32 retry, then fp16 restored


def test_canary_ignores_black_fill_on_black_scene(monkeypatch):
    # A black fill over black surroundings (fade/dark scene) is CORRECT with
    # per-frame stroke masks — the canary must not waste an fp32 retry on it.
    from app.services.propainter_adapter import ProPainterEngine

    load_calls: list[bool] = []
    inpaint_calls: list[int] = []

    def fake_load(cls, *, fp16=True, progress_cb=None):
        load_calls.append(fp16)

    def fake_inpaint(cls, frames_rgb, masks, **_kwargs):
        inpaint_calls.append(1)
        return np.zeros_like(frames_rgb)

    monkeypatch.setattr(ProPainterEngine, "load", classmethod(fake_load))
    monkeypatch.setattr(ProPainterEngine, "inpaint_clip", classmethod(fake_inpaint))

    frames = np.zeros((8, 128, 128, 3), dtype=np.uint8)  # black scene
    masks = np.zeros((8, 128, 128), dtype=np.uint8)
    masks[:, 40:90, 40:90] = 255

    VideoCleanupService._inpaint_ladder_inner(frames, masks)
    assert len(inpaint_calls) == 1  # no retry
    assert load_calls == []  # engine never reloaded


def test_canary_retry_failure_keeps_suspect_fill(monkeypatch):
    from app.services.propainter_adapter import ProPainterEngine

    load_calls: list[bool] = []
    inpaint_calls: list[int] = []

    def fake_load(cls, *, fp16=True, progress_cb=None):
        load_calls.append(fp16)

    def fake_inpaint(cls, frames_rgb, masks, **_kwargs):
        inpaint_calls.append(1)
        if len(inpaint_calls) > 1:
            raise RuntimeError("boom")
        return np.zeros_like(frames_rgb)  # near-black -> canary trips

    monkeypatch.setattr(ProPainterEngine, "load", classmethod(fake_load))
    monkeypatch.setattr(ProPainterEngine, "inpaint_clip", classmethod(fake_inpaint))

    frames = np.full((8, 128, 128, 3), 50, dtype=np.uint8)
    masks = np.zeros((8, 128, 128), dtype=np.uint8)
    masks[:, 40:90, 40:90] = 255

    result = VideoCleanupService._inpaint_ladder_inner(frames, masks)
    # The suspect (black) fill is returned rather than failing the job …
    assert (result[4][60, 60] == 0).all()
    # … and the engine is restored to fp16 even on failure.
    assert load_calls == [False, True]
