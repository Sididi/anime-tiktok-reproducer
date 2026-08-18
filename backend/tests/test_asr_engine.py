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
    CHUNK_LENGTH_SECONDS,
    COVERAGE_GAP_MIN_SECONDS,
    DEGENERATE_MAX_WORD_COUNT,
    LADDER_MAX_DECODES_PER_SPAN,
    TOKEN_DENSE_CHUNK_LENGTH_SECONDS,
    chunk_length_for,
    decode_with_coverage,
    estimate_word_count,
    redecode_spans,
    segment_is_degenerate,
    uncovered_speech_spans,
    _decode_span_with_ladder,
    _prefer_repair,
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


class TestTokenCapResidualRepair:
    """2026-08-17 signature (project eabe25d9b2f4, Hindi): a 15s chunk hit
    the ~448-token decoder cap; the segment's text was truncated mid-word
    (even mid-byte -> U+FFFD) while its span still claimed coverage to the
    chunk end. The batched coverage check trusts spans, so the loss only
    surfaces post-alignment as speech spans without words — those must be
    fed back through the sequential decode path."""

    def test_token_dense_language_gets_shorter_chunks(self):
        assert chunk_length_for("hi") == TOKEN_DENSE_CHUNK_LENGTH_SECONDS
        assert TOKEN_DENSE_CHUNK_LENGTH_SECONDS < CHUNK_LENGTH_SECONDS

    def test_latin_and_auto_keep_default_chunks(self):
        assert chunk_length_for("fr") == CHUNK_LENGTH_SECONDS
        assert chunk_length_for("en") == CHUNK_LENGTH_SECONDS
        assert chunk_length_for(None) == CHUNK_LENGTH_SECONDS

    def test_redecode_spans_covers_every_span(self):
        class _FakeSeg:
            def __init__(self, start, end, text):
                self.start, self.end, self.text = start, end, text

        class _FakeModel:
            def __init__(self):
                self.calls = []

            def transcribe(self, clip, **kwargs):
                self.calls.append(len(clip))
                # One segment spanning the whole clip (padding included);
                # redecode_spans must clamp it back inside each gap.
                return iter([_FakeSeg(0.0, len(clip) / 16000, "recovered")]), None

        import numpy as np

        audio = np.zeros(16000 * 340, dtype=np.float32)
        spans = [(24.01, 26.15), (314.87, 317.63)]
        segments = redecode_spans(_FakeModel(), audio, spans, language="hi")

        assert len(segments) == 2
        for seg, (gap_start, gap_end) in zip(segments, spans):
            assert seg["text"] == "recovered"
            assert seg["start"] >= gap_start
            assert seg["end"] <= gap_end

    def test_redecode_spans_empty_input(self):
        class _NeverCalledModel:
            def transcribe(self, clip, **kwargs):
                raise AssertionError("must not decode when there are no spans")

        import numpy as np

        audio = np.zeros(16000, dtype=np.float32)
        assert redecode_spans(_NeverCalledModel(), audio, [], language="hi") == []


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


# ---------------------------------------------------------------------------
# 2026-08-18 signature (project f6dffffa9a4e, Spanish): the batched decode
# emitted one segment 42.67->56.85 whose whole text was the training-data
# hallucination "¡Suscríbete al canal!" (0.21 words/s). Its span claimed
# coverage, so the coverage check saw nothing; the post-alignment repair
# re-decoded the IDENTICAL window and reproduced the hallucination -> 8
# permanently empty scenes. Defense: degeneracy gate + windowed repair ladder.
# ---------------------------------------------------------------------------

HALLUCINATION_ES = "¡Suscríbete al canal!"
DENSE_ES_12 = "en la armadura de acero del borracho quien resultó ser un criminal"


class _FakeSeg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


def _audio(seconds: float):
    import numpy as np

    return np.zeros(int(16000 * seconds), dtype=np.float32)


class TestSegmentDegeneracyGate:
    def test_incident4_sparse_hallucination_is_degenerate(self):
        seg = _seg(42.67, 56.85, HALLUCINATION_ES)
        assert segment_is_degenerate(
            seg, language="es", speech_regions=[(42.0, 57.0)], audio_duration=191.0
        )

    def test_dense_segment_is_healthy(self):
        seg = _seg(10.0, 24.0, " ".join(["palabra"] * 30))
        assert not segment_is_degenerate(
            seg, language="es", speech_regions=[(10.0, 24.0)], audio_duration=60.0
        )

    def test_short_segment_exempt(self):
        # Sparse text over a few seconds is indistinguishable from a real
        # short utterance.
        seg = _seg(0.0, 3.5, "sí")
        assert not segment_is_degenerate(
            seg, language="es", speech_regions=[(0.0, 3.5)], audio_duration=60.0
        )

    def test_music_span_with_sparse_vad_speech_exempt(self):
        # 14s span but only 2s of VAD speech inside it: 3 words are plausible.
        seg = _seg(10.0, 24.0, "tres palabras aquí")
        assert not segment_is_degenerate(
            seg, language="es", speech_regions=[(11.0, 13.0)], audio_duration=60.0
        )

    def test_word_count_cap_flags_long_sparse_sentence(self):
        seg = _seg(0.0, 4.5, "cuatro palabras justo aquí")
        assert estimate_word_count(seg["text"], "es") == DEGENERATE_MAX_WORD_COUNT
        assert segment_is_degenerate(
            seg, language="es", speech_regions=[(0.0, 4.5)], audio_duration=60.0
        )

    def test_unspaced_script_char_estimate(self):
        dense = _seg(0.0, 10.0, "とてもながいぶんしょうがここにあるはずですよねみなさん")
        assert not segment_is_degenerate(
            dense, language="ja", speech_regions=[(0.0, 10.0)], audio_duration=60.0
        )
        sparse = _seg(0.0, 10.0, "はいそう")
        assert segment_is_degenerate(
            sparse, language="ja", speech_regions=[(0.0, 10.0)], audio_duration=60.0
        )

    def test_end_overrun_flagged(self):
        # temp>0 hallucination loops emit segments "ending" far past the clip
        # (73.5s claimed inside a 13.4s clip, 2026-08-18) — garbage even when
        # the text itself looks dense.
        seg = _seg(50.0, 73.5, " ".join(["palabra"] * 40))
        assert segment_is_degenerate(seg, language="es", audio_duration=60.0)

    def test_empty_text_degenerate(self):
        assert segment_is_degenerate(
            _seg(0.0, 10.0, "   "), language="es", speech_regions=[(0.0, 10.0)]
        )

    def test_span_fallback_without_regions(self):
        seg = _seg(0.0, 10.0, "tres palabras aquí")
        assert segment_is_degenerate(seg, language="es")


class TestPreferRepair:
    def test_replacement_arbitration_is_conjunctive(self):
        orig_10 = [_seg(0.0, 10.0, " ".join(["palabra"] * 10))]
        # Word gain alone (14 vs 10: gain 4, ratio 1.4 < 1.5) is not enough.
        rep_14 = [_seg(0.0, 10.0, " ".join(["palabra"] * 14))]
        assert not _prefer_repair(orig_10, rep_14, language="es")

        # Density ratio alone (3 vs 1: ratio 3, gain 2 < 3) is not enough.
        orig_1 = [_seg(0.0, 10.0, "palabra")]
        rep_3 = [_seg(0.0, 10.0, "tres palabras aquí")]
        assert not _prefer_repair(orig_1, rep_3, language="es")

        # Both axes strictly better -> replace.
        orig_2 = [_seg(0.0, 10.0, "dos palabras")]
        rep_6 = [_seg(0.0, 10.0, " ".join(["palabra"] * 6))]
        assert _prefer_repair(orig_2, rep_6, language="es")

    def test_zero_word_original_trivially_replaced(self):
        assert _prefer_repair(
            [_seg(0.0, 10.0, " ")], [_seg(0.0, 10.0, "una")], language="es"
        )


class _ClipLengthModel:
    """Fake model dispatching on clip length: long windows reproduce the
    window-locked hallucination, shorter (shifted/split) windows decode the
    real narration — the exact pathology measured on f6dffffa9a4e."""

    def __init__(self, hallucinate_above_seconds=8.0, real_text=DENSE_ES_12):
        self.hallucinate_above = hallucinate_above_seconds
        self.real_text = real_text
        self.clip_lengths = []

    def transcribe(self, clip, **kwargs):
        clip_seconds = len(clip) / 16000
        self.clip_lengths.append(clip_seconds)
        if clip_seconds > self.hallucinate_above:
            return iter([_FakeSeg(0.0, clip_seconds, HALLUCINATION_ES)]), None
        return iter([_FakeSeg(0.0, clip_seconds, self.real_text)]), None


class _AlwaysGarbageModel:
    def __init__(self):
        self.clip_lengths = []

    def transcribe(self, clip, **kwargs):
        self.clip_lengths.append(len(clip) / 16000)
        return iter([_FakeSeg(0.0, len(clip) / 16000, "x")]), None


class TestRepairLadder:
    def test_first_rung_success_single_decode(self):
        model = _ClipLengthModel(hallucinate_above_seconds=100.0)
        segments = _decode_span_with_ladder(
            model, _audio(60), 10.0, 20.0, language="es",
            speech_regions=[(10.0, 20.0)],
        )
        assert len(model.clip_lengths) == 1
        assert segments and segments[0]["text"] == DENSE_ES_12

    def test_window_locked_hallucination_recovered_by_split(self):
        model = _ClipLengthModel()
        segments = _decode_span_with_ladder(
            model, _audio(191), 43.5, 56.9, language="es",
            speech_regions=[(43.0, 57.0)],
        )
        texts = [seg["text"] for seg in segments]
        assert texts and all(HALLUCINATION_ES not in text for text in texts)
        assert all(text == DENSE_ES_12 for text in texts)
        # The split halves must fully cover the span again.
        assert uncovered_speech_spans([(43.5, 56.9)], segments) == []
        # Full window (fails) + two halves.
        assert len(model.clip_lengths) == 3

    def test_budget_and_recursion_floor(self):
        model = _AlwaysGarbageModel()
        segments = _decode_span_with_ladder(
            model, _audio(60), 10.0, 30.0, language="es",
            speech_regions=[(10.0, 30.0)],
        )
        assert segments == []
        assert len(model.clip_lengths) <= LADDER_MAX_DECODES_PER_SPAN

    def test_wide_padding_rung_reached(self):
        model = _AlwaysGarbageModel()
        _decode_span_with_ladder(
            model, _audio(60), 10.0, 14.0, language="es",
            speech_regions=[(10.0, 14.0)],
        )
        # A 4s span cannot split (< 2x floor); the second attempt must be the
        # widened-context window: 4s + 2 x 2.0s padding.
        assert any(abs(length - 8.0) < 0.05 for length in model.clip_lengths)

    def test_overrun_segment_dropped_pre_clamp(self):
        class _OverrunModel:
            def transcribe(self, clip, **kwargs):
                clip_seconds = len(clip) / 16000
                return iter([
                    _FakeSeg(0.0, clip_seconds + 60.0, "decoder state garbage"),
                    _FakeSeg(1.0, 3.0, "ok"),
                ]), None

        segments = asr_engine._sequential_decode_span(
            _OverrunModel(), _audio(60), 10.0, 14.0, language="es",
        )
        assert [seg["text"] for seg in segments] == ["ok"]


class TestDecodeWithCoverageGate:
    @staticmethod
    def _patch_batched(monkeypatch, regions, segments, language="es"):
        monkeypatch.setattr(
            asr_engine, "_detect_speech_regions", lambda audio: regions
        )
        monkeypatch.setattr(
            asr_engine,
            "_batched_transcribe",
            lambda model, audio, *, language=None, batch_size=0, **kw: (
                [dict(seg) for seg in segments],
                "es",
            ),
        )

    def test_degenerate_batched_segment_loses_claim_and_is_repaired(
        self, monkeypatch
    ):
        regions = [(0.0, 60.0)]
        batched = [
            _seg(0.0, 42.6, " ".join(["palabra"] * 100)),
            _seg(42.67, 56.85, HALLUCINATION_ES),
            _seg(56.9, 60.0, "cinco palabras más aquí"),
        ]
        self._patch_batched(monkeypatch, regions, batched)
        model = _ClipLengthModel()

        final, language, speech = decode_with_coverage(
            model, _audio(60), language=None, batch_size=8
        )
        assert language == "es"
        texts = " ".join(seg["text"] for seg in final)
        assert HALLUCINATION_ES not in texts
        assert DENSE_ES_12 in texts
        assert uncovered_speech_spans(speech, final) == []

    def test_keep_original_when_ladder_loses(self, monkeypatch):
        regions = [(0.0, 30.0)]
        flagged_text = "cuatro palabras justo aquí"
        batched = [
            _seg(0.0, 10.0, " ".join(["palabra"] * 20)),
            _seg(10.0, 20.0, flagged_text),  # 4 words / 10s -> flagged
            _seg(20.0, 30.0, " ".join(["palabra"] * 20)),
        ]
        self._patch_batched(monkeypatch, regions, batched)

        class _ScriptedModel:
            # Full window: garbage. First half: a healthy 5-word repair
            # (passes the gate but only +1 word vs the original). Rest: garbage.
            def __init__(self):
                self.responses = [
                    "x",
                    "cinco palabras reales aquí hoy",
                    "x",
                    "x",
                    "x",
                    "x",
                ]

            def transcribe(self, clip, **kwargs):
                text = self.responses.pop(0)
                return iter([_FakeSeg(0.0, len(clip) / 16000, text)]), None

        final, _language, _speech = decode_with_coverage(
            _ScriptedModel(), _audio(30), language=None, batch_size=8
        )
        texts = [seg["text"] for seg in final]
        assert flagged_text in texts
        assert "cinco palabras reales aquí hoy" not in texts

    def test_healthy_media_makes_zero_sequential_decodes(self, monkeypatch):
        regions = [(0.0, 20.0)]
        batched = [_seg(0.0, 20.0, " ".join(["palabra"] * 40))]
        self._patch_batched(monkeypatch, regions, batched)

        class _MustNotDecode:
            def transcribe(self, clip, **kwargs):
                raise AssertionError("healthy media must not re-decode")

        final, _language, _speech = decode_with_coverage(
            _MustNotDecode(), _audio(20), language=None, batch_size=8
        )
        assert len(final) == 1

    def test_suspect_over_non_speech_is_kept(self, monkeypatch):
        # Flagged (end overrun) but VAD saw no speech under it: there is no
        # counter-evidence, so it must be kept, not deleted.
        regions = [(0.0, 5.0)]
        overrun_text = " ".join(["palabra"] * 20)
        batched = [
            _seg(0.0, 5.0, " ".join(["palabra"] * 10)),
            _seg(25.0, 40.0, overrun_text),  # audio is 30s -> overrun
        ]
        self._patch_batched(monkeypatch, regions, batched)

        class _MustNotDecode:
            def transcribe(self, clip, **kwargs):
                raise AssertionError("no gap -> no re-decode")

        final, _language, _speech = decode_with_coverage(
            _MustNotDecode(), _audio(30), language=None, batch_size=8
        )
        assert any(seg["text"] == overrun_text for seg in final)
