"""Tests for WhisperX decoder-dropout detection and repair scaffolding.

Regression context (project ee650bd67cb9): faster-whisper decoded the last
VAD chunk (127.13s -> 145.54s) as a single 6-word sentence, silently skipping
~17s of narration.  The degenerate-segment repair pass never fired because it
ran after whisperx.align, which rewrites segment start/end to the aligned word
span (collapsing 18.4s -> 0.72s, below the 4s repair threshold).
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.transcriber import (
    ALIGNMENT_REPAIR_MIN_SEGMENT_DURATION,
    TranscriberService,
)


def _word(text: str, start: float, end: float, score: float = 0.9) -> dict:
    return {"word": text, "start": start, "end": end, "score": score}


def _dense_segment(start: float, end: float, words_per_second: float = 2.5) -> dict:
    count = max(1, int((end - start) * words_per_second))
    step = (end - start) / count
    words = [
        _word(f"w{i}", start + i * step, start + i * step + min(step, 0.3))
        for i in range(count)
    ]
    return {
        "start": start,
        "end": end,
        "text": " ".join(w["word"] for w in words),
        "words": words,
    }


class TestSparseAsrSegmentDetection:
    """Pre-alignment detection: raw ASR windows still carry the true span."""

    def test_decoder_dropout_segment_needs_repair(self):
        # The exact shape from ee650bd67cb9: 6 words over an 18.4s VAD window.
        segment = {
            "start": 127.13,
            "end": 145.54,
            "text": "El viejo soltó una sonrisa maligna.",
        }
        assert TranscriberService._segment_needs_alignment_repair(segment) is True

    def test_dense_asr_segment_untouched(self):
        segment = {
            "start": 0.03,
            "end": 24.47,
            "text": " ".join(f"palabra{i}" for i in range(55)),
        }
        assert TranscriberService._segment_needs_alignment_repair(segment) is False


class TestUncoveredWindowSegments:
    """Post-alignment safety net: aligned words must cover the ASR windows."""

    def test_tail_gap_detected(self):
        # Aligned words squeezed into the first 0.72s of an 18.4s window.
        aligned = [{
            "start": 127.13,
            "end": 127.85,
            "text": "El viejo soltó una sonrisa maligna.",
            "words": [
                _word("El", 127.13, 127.17, 0.1),
                _word("viejo", 127.19, 127.29, 0.1),
                _word("soltó", 127.31, 127.41, 0.1),
                _word("una", 127.43, 127.49, 0.1),
                _word("sonrisa", 127.51, 127.65, 0.1),
                _word("maligna.", 127.67, 127.85, 0.1),
            ],
        }]
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(127.13, 145.54)],
        )
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap["text"] == ""
        assert gap["words"] == []
        assert abs(gap["start"] - 127.85) < 0.01
        assert abs(gap["end"] - 145.54) < 0.01

    def test_dense_coverage_yields_no_gaps(self):
        aligned = [_dense_segment(10.0, 38.0)]
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(10.0, 38.0)],
        )
        assert gaps == []

    def test_interior_gap_detected(self):
        # Decoder skipped the middle of a window; both ends align correctly.
        aligned = [
            _dense_segment(10.0, 15.0),
            _dense_segment(24.0, 30.0),
        ]
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(10.0, 30.0)],
        )
        assert len(gaps) == 1
        assert abs(gaps[0]["start"] - 15.0) < 0.35
        assert abs(gaps[0]["end"] - 24.0) < 0.35

    def test_short_pauses_ignored(self):
        # Natural inter-phrase pauses below the repair threshold are fine.
        aligned = [
            _dense_segment(10.0, 15.0),
            _dense_segment(17.0, 22.0),
        ]
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(10.0, 22.0)],
        )
        assert gaps == []

    def test_leading_gap_detected(self):
        aligned = [_dense_segment(50.0, 56.0)]
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(40.0, 56.0)],
        )
        assert len(gaps) == 1
        assert abs(gaps[0]["start"] - 40.0) < 0.01
        assert abs(gaps[0]["end"] - 50.0) < 0.35

    def test_window_shorter_than_threshold_ignored(self):
        aligned = []
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(10.0, 10.0 + ALIGNMENT_REPAIR_MIN_SEGMENT_DURATION - 0.5)],
        )
        assert gaps == []

    def test_untimed_word_fallback_counts_as_coverage(self):
        # Segments whose words lack timings fall back to text-spread words,
        # which cover the whole window uniformly: no gap to repair.
        aligned = [{
            "start": 10.0,
            "end": 30.0,
            "text": " ".join(f"w{i}" for i in range(40)),
            "words": [],
        }]
        gaps = TranscriberService._uncovered_window_segments(
            segments=aligned,
            asr_windows=[(10.0, 30.0)],
        )
        assert gaps == []


class TestGapSegmentRepairContract:
    """Injected gap pseudo-segments must flow through the repair machinery."""

    def test_empty_gap_segment_triggers_repair_check(self):
        gap = {"start": 127.85, "end": 145.54, "text": "", "words": []}
        assert TranscriberService._segment_needs_alignment_repair(gap) is True

    def test_recovered_words_accepted_over_empty_original(self):
        gap = {"start": 127.85, "end": 145.54, "text": "", "words": []}
        repaired = _dense_segment(127.85, 145.54)
        assert TranscriberService._should_use_repaired_segment(gap, repaired) is True
