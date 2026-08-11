"""Instagram thumb_offset scaling for the thumbnail feature."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.upload_phase import UploadPhaseService


def test_thumb_offset_passthrough_no_speed():
    assert UploadPhaseService._instagram_thumb_offset(2350, None, 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "1.0", 90.0) == 2350


def test_thumb_offset_scaled_when_sped_up():
    # 10s into the original maps to 8s in a 1.25x sped-up artifact
    assert UploadPhaseService._instagram_thumb_offset(10_000, "1.25", 90.0) == 8000


def test_thumb_offset_clamped_to_max_duration():
    # cut video: offset can never exceed the prepared duration (minus margin)
    assert UploadPhaseService._instagram_thumb_offset(95_000, None, 90.0) == 89_500


def test_thumb_offset_garbage_speed_treated_as_one():
    assert UploadPhaseService._instagram_thumb_offset(2350, "abc", 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "0.0", 90.0) == 2350
    assert UploadPhaseService._instagram_thumb_offset(2350, "-3", 90.0) == 2350


def test_thumb_offset_never_negative():
    assert UploadPhaseService._instagram_thumb_offset(0, None, 90.0) == 0
