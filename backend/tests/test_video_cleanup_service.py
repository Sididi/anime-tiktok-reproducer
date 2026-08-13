"""Tests for the Pure-mode cleanup service (geometry, detection, chunking)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.cleanup import CleanupZone
from app.services.video_cleanup_service import (
    CLIP_CONTEXT_FRAMES,
    CLIP_MAX_FRAMES,
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
