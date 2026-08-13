"""Tests for the ASR core (asr_engine) and transcriber helpers.

Regression lineage (both incidents must stay covered by the new core):
- 2026-08-07 (project ee650bd67cb9): large-v3 decoded an 18s chunk as one
  6-word sentence — sparse coverage inside a speech region.
- 2026-08-12 (project eabe25d9b2f4, Hindi): large-v3 decoded WHOLE chunks to
  nothing (~157s dropped) — speech regions with zero segments.
Both signatures reduce to the same invariant here: every silero speech span
must be covered by decoded text, or it is re-decoded sequentially.
"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import asr_engine
from app.services.asr_engine import (
    COVERAGE_GAP_MIN_SECONDS,
    uncovered_speech_spans,
)
from app.services.transcriber import TranscriberService


def _seg(start: float, end: float, text: str = "some text") -> dict:
    return {"start": start, "end": end, "text": text}


class TestUncoveredSpeechSpans:
    def test_full_coverage_yields_no_gaps(self):
        speech = [(0.0, 10.0), (12.0, 30.0)]
        segments = [_seg(0.0, 10.2), _seg(11.8, 30.1)]
        assert uncovered_speech_spans(speech, segments) == []

    def test_fully_dropped_region_is_a_gap(self):
        # 2026-08-12 signature: a whole speech region with no text at all.
        speech = [(0.0, 10.0), (12.0, 30.0), (32.0, 40.0)]
        segments = [_seg(0.0, 10.0), _seg(32.0, 40.0)]
        assert uncovered_speech_spans(speech, segments) == [(12.0, 30.0)]

    def test_sparse_coverage_inside_region_is_a_gap(self):
        # 2026-08-07 signature: one short segment inside a long speech region.
        speech = [(0.0, 20.0)]
        segments = [_seg(0.0, 2.0)]
        assert uncovered_speech_spans(speech, segments) == [(2.0, 20.0)]

    def test_small_gaps_below_threshold_are_ignored(self):
        speech = [(0.0, 10.0)]
        segments = [_seg(0.0, 4.5), _seg(5.5, 10.0)]  # 1.0s hole < threshold
        assert uncovered_speech_spans(speech, segments) == []
        assert COVERAGE_GAP_MIN_SECONDS > 1.0

    def test_empty_text_segments_do_not_count_as_coverage(self):
        speech = [(0.0, 10.0)]
        segments = [_seg(0.0, 10.0, text="   ")]
        assert uncovered_speech_spans(speech, segments) == [(0.0, 10.0)]

    def test_gap_in_middle_of_region(self):
        speech = [(0.0, 30.0)]
        segments = [_seg(0.0, 10.0), _seg(22.0, 30.0)]
        assert uncovered_speech_spans(speech, segments) == [(10.0, 22.0)]

    def test_no_speech_no_gaps(self):
        assert uncovered_speech_spans([], [_seg(0.0, 5.0)]) == []


class TestSequentialSpanClamping:
    def test_segments_outside_gap_are_dropped(self, monkeypatch):
        # The sequential pass sees padding around the gap; anything it emits
        # that lives in the padding (context, hallucination) must not leak.
        class _FakeSeg:
            def __init__(self, start, end, text):
                self.start, self.end, self.text = start, end, text

        class _FakeModel:
            def transcribe(self, clip, **kwargs):
                # Clip starts at gap_start - padding. Emit one segment fully
                # inside the leading padding and one inside the gap.
                return iter([
                    _FakeSeg(0.0, 0.2, "padding noise"),
                    _FakeSeg(1.0, 3.0, "real recovered text"),
                ]), None

        import numpy as np

        audio = np.zeros(16000 * 30, dtype=np.float32)
        segments = asr_engine._sequential_decode_span(
            _FakeModel(), audio, 10.0, 14.0, language="hi",
        )
        assert len(segments) == 1
        seg = segments[0]
        assert seg["text"] == "real recovered text"
        assert seg["start"] >= 10.0
        assert seg["end"] <= 14.0

    def test_decode_failure_returns_empty(self):
        class _BrokenModel:
            def transcribe(self, clip, **kwargs):
                raise RuntimeError("boom")

        import numpy as np

        audio = np.zeros(16000 * 30, dtype=np.float32)
        assert asr_engine._sequential_decode_span(
            _BrokenModel(), audio, 10.0, 14.0, language="hi",
        ) == []


class TestNormalizeToken:
    """The Unicode fix (2026-08-12) must survive the rework: ASCII-only
    normalization made every non-Latin token empty, breaking script/word
    sequence alignment for non-Latin languages."""

    def test_devanagari_tokens_survive(self):
        assert TranscriberService._normalize_token("भेड़") != ""
        assert TranscriberService._normalize_token("खरीदते") != ""

    def test_latin_behavior_unchanged(self):
        assert TranscriberService._normalize_token("Hello,") == "hello"
        assert TranscriberService._normalize_token("été") == "ete"
        assert TranscriberService._normalize_token("...") == ""
