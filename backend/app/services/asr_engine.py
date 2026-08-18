"""ASR core: fast batched decode with a coverage-checked sequential safety net.

Replaces the whisperx batched pipeline + ~600 lines of repair machinery
(2026-08-13). The old stack silently dropped whole VAD chunks (large-v3
batched-decode pathology, worst on non-Latin speech) and needed layered
defenses. The core:

1. **Batched decode** via faster-whisper's ``BatchedInferencePipeline``
   (silero VAD, same CTranslate2 engine → same speed as before).
2. **Degeneracy gate**: a segment's span only counts as coverage if its text
   is lexically plausible for the VAD speech inside that span. Hallucinated
   captions ("¡Suscríbete al canal!" over 14s of narration, 2026-08-18) and
   sparse-tail dropouts (2026-08-07) claim spans their text doesn't deliver;
   the gate strips that claim while keeping the segment as a fallback
   hypothesis.
3. **Coverage check**: silero speech regions are compared against the spans
   of *trusted* segments. Any speech span left without text is the dropout
   signature.
4. **Windowed repair ladder**: uncovered spans are re-decoded sequentially;
   if the same window reproduces a degenerate result, the ladder splits the
   window and retries — a window-locked hallucination survives an identical
   re-decode (2026-08-18) but not a shifted one. Results are accepted only
   through the same gate, and a flagged original is replaced only by a
   strictly better repair. On healthy media none of this fires.
5. **Post-alignment residual repair** (driven by the transcriber via
   ``log_residual_coverage`` + ``redecode_spans``): token-cap-truncated
   segments claim spans their text doesn't cover with per-word plausible
   density, which step 2 cannot see; after wav2vec2 alignment the wordless
   tails become visible and go through the same ladder.

Word-level timing precision is unaffected: wav2vec2 forced alignment
(whisperx.align, driven by the transcriber) runs on the merged segments
exactly as before.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("uvicorn.error")

SAMPLE_RATE = 16000

# A speech span this long with no decoded text is treated as a dropout and
# re-decoded sequentially. Below it, missing text is indistinguishable from
# breaths/hesitations and alignment noise.
COVERAGE_GAP_MIN_SECONDS = 2.0
# Whisper's decoder is hard-capped at ~448 tokens per chunk. Token-dense
# languages (Hindi et al.) overflow that inside a default 30s chunk and the
# batched pipeline silently truncates the tail. 15s chunks keep any
# language comfortably under the cap (verified 2026-08-13: 30s chunks lost
# ~60% of a dense Hindi narration).
CHUNK_LENGTH_SECONDS = 15
# 15s is still not always enough for scripts that Whisper's BPE encodes at
# ~1+ token per character: a fast Hindi narration (~4.4 words/s, project
# eabe25d9b2f4) overflowed the cap inside 15s chunks, truncating segment
# text mid-word (even mid-byte, U+FFFD) while the segment span still claimed
# coverage. Only applies when the language is known up front (no auto).
TOKEN_DENSE_CHUNK_LENGTH_SECONDS = 10
TOKEN_DENSE_LANGUAGES = {
    # Indic scripts
    "hi", "mr", "ne", "bn", "as", "gu", "pa", "or", "ta", "te", "kn", "ml", "si",
    # Other BPE-dense scripts
    "th", "lo", "km", "my", "ka", "am", "ur",
}
# Padding around a re-decoded gap so the decoder gets acoustic context.
GAP_DECODE_PADDING_SECONDS = 0.35
# A re-decoded segment must overlap its gap by this much to be kept
# (protects against the sequential pass hallucinating over the padding).
GAP_SEGMENT_MIN_OVERLAP_SECONDS = 0.2

# --- Degeneracy gate ---------------------------------------------------------
# Thresholds inherited from the pre-2026-08-13 repair machinery (proven on the
# 08-07 sparse-tail incident) and re-validated on the 08-18 hallucination
# (3 words / 13.5s speech = 0.22 w/s).
DEGENERATE_MIN_SPEECH_SECONDS = 4.0
DEGENERATE_MIN_WORDS_PER_SECOND = 0.8
DEGENERATE_MAX_WORD_COUNT = 4
# A decoded segment claiming to end past the audio (or clip) it was decoded
# from is decoder-state garbage, seen at temperature>0 on hallucination loops
# (a 13.4s clip produced a segment "ending" at 73.5s, 2026-08-18).
SEGMENT_END_OVERRUN_TOLERANCE_SECONDS = 0.5
# Scripts written without spaces: estimate words from character count.
UNSPACED_CHARS_PER_WORD = 3.0
UNSPACED_SCRIPT_LANGUAGES = {"zh", "yue", "ja", "th", "lo", "km", "my"}

# --- Repair ladder -----------------------------------------------------------
GAP_DECODE_WIDE_PADDING_SECONDS = 2.0
# Below this the window can't be split further; a failed decode falls back to
# the widened-padding attempt instead.
LADDER_MIN_SPAN_SECONDS = 3.0
# Hard decode budget per top-level gap: a 157s dropout (2026-08-12 scale)
# must not turn midpoint recursion into dozens of decodes.
LADDER_MAX_DECODES_PER_SPAN = 6

# --- Replacement arbitration (conjunctive, proven values) --------------------
# A flagged original segment is only replaced by a repair that is strictly
# better on BOTH axes; otherwise the original is kept — deleting real sparse
# speech is a worse failure than keeping a suspected hallucination.
REPAIR_MIN_WORD_GAIN = 3
REPAIR_MIN_DENSITY_GAIN = 1.5


def decode_with_coverage(
    model: Any,
    audio: Any,
    *,
    language: str | None,
    batch_size: int,
) -> tuple[list[dict], str, list[tuple[float, float]]]:
    """Decode ``audio`` (float32 mono 16 kHz) into segments with full
    speech coverage.

    Returns ``(segments, detected_language, speech_regions)`` where each
    segment is ``{"start", "end", "text"}`` and ``speech_regions`` are the
    silero VAD speech spans in seconds (for downstream sanity checks).
    """
    # 1) Speech regions — cheap CPU pass, and the coverage ground truth.
    speech_regions = _detect_speech_regions(audio)

    # 2) Fast batched decode.
    segments, detected_language = _batched_transcribe(
        model, audio, language=language, batch_size=batch_size
    )
    audio_duration = len(audio) / SAMPLE_RATE

    # 3) Degeneracy gate: hallucinated/sparse segments lose their coverage
    # claim but stay around as fallback hypotheses.
    healthy: list[dict] = []
    suspects: list[dict] = []
    for seg in segments:
        if segment_is_degenerate(
            seg,
            language=detected_language,
            speech_regions=speech_regions,
            audio_duration=audio_duration,
        ):
            suspects.append(seg)
        else:
            healthy.append(seg)
    if suspects:
        logger.info(
            "ASR degeneracy gate flagged %d segment(s): %s",
            len(suspects),
            [
                (round(s["start"], 2), round(s["end"], 2), (s.get("text") or "")[:60])
                for s in suspects
            ],
        )

    # 4) Coverage check → 5) windowed repair ladder on uncovered spans only.
    gaps = uncovered_speech_spans(speech_regions, healthy)
    final = list(healthy)
    consumed: set[int] = set()
    if gaps:
        logger.info(
            "ASR coverage: %d speech span(s) without trusted text, repairing: %s",
            len(gaps),
            [(round(start, 2), round(end, 2)) for start, end in gaps],
        )
    for gap_start, gap_end in gaps:
        repaired = _decode_span_with_ladder(
            model,
            audio,
            gap_start,
            gap_end,
            language=detected_language,
            speech_regions=speech_regions,
        )
        originals = [
            s
            for s in suspects
            if id(s) not in consumed and _overlaps_span(s, gap_start, gap_end)
        ]
        if repaired and (
            not originals
            or _prefer_repair(originals, repaired, language=detected_language)
        ):
            final.extend(repaired)
        else:
            # Ladder lost (or produced nothing): keep the flagged original
            # hypothesis rather than deleting text without better evidence.
            final.extend(originals)
            if originals:
                logger.warning(
                    "ASR repair could not beat the flagged segment(s) in "
                    "%.2fs -> %.2fs; keeping the original hypothesis",
                    gap_start,
                    gap_end,
                )
        consumed.update(id(s) for s in originals)

    # Suspects that overlap no gap sit over spans VAD saw no speech in
    # (hallucinated caption over music — or real quiet speech). There is no
    # counter-evidence to justify deleting them: keep, but log.
    leftovers = [s for s in suspects if id(s) not in consumed]
    if leftovers:
        logger.info(
            "ASR degeneracy gate: %d flagged segment(s) over non-speech kept as-is",
            len(leftovers),
        )
        final.extend(leftovers)

    final.sort(key=lambda seg: (seg["start"], seg["end"]))
    return final, detected_language, speech_regions


def _detect_speech_regions(audio: Any) -> list[tuple[float, float]]:
    """Silero VAD speech spans in seconds — the coverage ground truth."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    speech = get_speech_timestamps(audio, VadOptions())
    return [
        (float(chunk["start"]) / SAMPLE_RATE, float(chunk["end"]) / SAMPLE_RATE)
        for chunk in speech
    ]


def _batched_transcribe(
    model: Any,
    audio: Any,
    *,
    language: str | None,
    batch_size: int,
) -> tuple[list[dict], str]:
    """Fast batched decode. ``without_timestamps=False`` is load-bearing:
    the default emits one chunk-wide segment whose span claims coverage
    even when the text was token-cap truncated — honest per-sentence spans
    are what make the coverage check meaningful."""
    from faster_whisper import BatchedInferencePipeline

    pipeline = BatchedInferencePipeline(model)
    segment_iter, info = pipeline.transcribe(
        audio,
        language=language,
        batch_size=batch_size,
        vad_filter=True,
        word_timestamps=False,
        without_timestamps=False,
        chunk_length=chunk_length_for(language),
    )
    segments = [
        {"start": float(seg.start), "end": float(seg.end), "text": seg.text or ""}
        for seg in segment_iter
    ]
    detected_language = getattr(info, "language", None) or (language or "en")
    return segments, detected_language


def chunk_length_for(language: str | None) -> int:
    """Batched-decode chunk length in seconds, shrunk for token-dense scripts."""
    if language in TOKEN_DENSE_LANGUAGES:
        return TOKEN_DENSE_CHUNK_LENGTH_SECONDS
    return CHUNK_LENGTH_SECONDS


def estimate_word_count(text: str, language: str | None) -> int:
    """Lexical word estimate; char-based for scripts written without spaces."""
    tokens = [tok for tok in text.split() if tok]
    if language in UNSPACED_SCRIPT_LANGUAGES:
        chars = sum(1 for ch in text if ch.isalnum())
        return max(len(tokens), math.ceil(chars / UNSPACED_CHARS_PER_WORD))
    return len(tokens)


def _speech_seconds_within(
    start: float, end: float, speech_regions: list[tuple[float, float]]
) -> float:
    return sum(
        max(0.0, min(end, region_end) - max(start, region_start))
        for region_start, region_end in speech_regions
    )


def _overlaps_span(seg: dict, start: float, end: float) -> bool:
    seg_start = seg.get("start")
    seg_end = seg.get("end")
    if not isinstance(seg_start, (int, float)) or not isinstance(seg_end, (int, float)):
        return False
    return min(float(seg_end), end) - max(float(seg_start), start) > 0.0


def segment_is_degenerate(
    seg: dict,
    *,
    language: str | None,
    speech_regions: list[tuple[float, float]] | None = None,
    audio_duration: float | None = None,
) -> bool:
    """Does this segment's text fail to plausibly cover the speech it claims?

    The density denominator is the VAD speech inside the span (span duration
    as fallback), so a long span that is mostly music with one short sentence
    is NOT flagged. Short spans are exempt: sparse text over a few seconds is
    indistinguishable from a real short utterance.
    """
    start = seg.get("start")
    end = seg.get("end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        # Never counted as coverage by uncovered_speech_spans; nothing to gate.
        return False

    text = (seg.get("text") or "").strip()
    if not text:
        return True

    if (
        audio_duration is not None
        and float(end) > audio_duration + SEGMENT_END_OVERRUN_TOLERANCE_SECONDS
    ):
        return True

    if speech_regions:
        speech = _speech_seconds_within(float(start), float(end), speech_regions)
    else:
        speech = float(end) - float(start)
    if speech < DEGENERATE_MIN_SPEECH_SECONDS:
        return False

    words = estimate_word_count(text, language)
    if words <= DEGENERATE_MAX_WORD_COUNT:
        return True
    return words / speech < DEGENERATE_MIN_WORDS_PER_SECOND


def _prefer_repair(
    originals: list[dict],
    repaired: list[dict],
    *,
    language: str | None,
) -> bool:
    """Replace flagged original segment(s) only with a strictly better repair.

    Conjunctive on purpose (word gain AND density gain): the arbitration
    protects real sparse speech from being swapped for a different decode of
    the same window that merely reworded it.
    """
    original_words = sum(
        estimate_word_count(seg.get("text") or "", language) for seg in originals
    )
    if original_words == 0:
        return True

    repaired_words = sum(
        estimate_word_count(seg.get("text") or "", language) for seg in repaired
    )
    if repaired_words < original_words + REPAIR_MIN_WORD_GAIN:
        return False
    # Both sides are measured over the same gap, so the density-gain check
    # reduces to a word-ratio check — kept explicit for parity with the
    # proven pre-08-13 rule.
    return repaired_words >= original_words * REPAIR_MIN_DENSITY_GAIN


def redecode_spans(
    model: Any,
    audio: Any,
    spans: list[tuple[float, float]],
    *,
    language: str,
    speech_regions: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Re-decode the given speech spans through the repair ladder.

    Exposed for the post-alignment residual repair: token-cap-truncated
    segments claim coverage the batched check trusts, so their wordless
    tails only become visible once alignment has run. The ladder (rather
    than a bare same-window decode) is what makes window-locked
    hallucinations recoverable (2026-08-18).
    """
    segments: list[dict] = []
    for span_start, span_end in spans:
        segments.extend(
            _decode_span_with_ladder(
                model,
                audio,
                span_start,
                span_end,
                language=language,
                speech_regions=speech_regions,
            )
        )
    segments.sort(key=lambda seg: (seg["start"], seg["end"]))
    return segments


def uncovered_speech_spans(
    speech_regions: list[tuple[float, float]],
    segments: list[dict],
    *,
    min_gap: float = COVERAGE_GAP_MIN_SECONDS,
) -> list[tuple[float, float]]:
    """Spans of speech (per VAD) not covered by any decoded segment.

    Pure interval math: for each speech region, subtract all segment
    intervals; keep leftovers of at least ``min_gap`` seconds.
    """
    intervals = sorted(
        (float(seg["start"]), float(seg["end"]))
        for seg in segments
        if isinstance(seg.get("start"), (int, float))
        and isinstance(seg.get("end"), (int, float))
        and (seg.get("text") or "").strip()
    )

    gaps: list[tuple[float, float]] = []
    for region_start, region_end in speech_regions:
        cursor = region_start
        for seg_start, seg_end in intervals:
            if seg_end <= cursor or seg_start >= region_end:
                continue
            if seg_start - cursor >= min_gap:
                gaps.append((cursor, seg_start))
            cursor = max(cursor, seg_end)
        if region_end - cursor >= min_gap:
            gaps.append((cursor, region_end))
    return gaps


def _clip_regions(
    speech_regions: list[tuple[float, float]] | None, start: float, end: float
) -> list[tuple[float, float]]:
    """Speech regions intersected with [start, end]; the whole span if none."""
    if not speech_regions:
        return [(start, end)]
    clipped = [
        (max(region_start, start), min(region_end, end))
        for region_start, region_end in speech_regions
        if min(region_end, end) - max(region_start, start) > 0.0
    ]
    return clipped or []


def _decode_span_with_ladder(
    model: Any,
    audio: Any,
    gap_start: float,
    gap_end: float,
    *,
    language: str,
    speech_regions: list[tuple[float, float]] | None = None,
    _budget: dict | None = None,
) -> list[dict]:
    """Repair one uncovered span with window diversity.

    Rung (a): sequential decode of the span; keep gate-passing segments.
    Rung (b): if the just-tried window itself failed, split it at the
    midpoint and recurse — never retry an identical window (a window-locked
    hallucination reproduces on the same clip, 2026-08-18); genuinely
    smaller residual sub-gaps recurse directly (already a new window).
    Rung (c): at the split floor, one attempt with widened acoustic context.

    A shared decode budget caps the total cost per top-level gap. Healthy
    media never enters this function.
    """
    if _budget is None:
        _budget = {"decodes": LADDER_MAX_DECODES_PER_SPAN}
    span = gap_end - gap_start
    if span <= 0 or _budget["decodes"] <= 0:
        return []
    audio_duration = len(audio) / SAMPLE_RATE

    _budget["decodes"] -= 1
    decoded = _sequential_decode_span(
        model, audio, gap_start, gap_end, language=language
    )
    healthy = [
        seg
        for seg in decoded
        if not segment_is_degenerate(
            seg,
            language=language,
            speech_regions=speech_regions,
            audio_duration=audio_duration,
        )
    ]

    speech_in_span = _clip_regions(speech_regions, gap_start, gap_end)
    remaining = uncovered_speech_spans(speech_in_span, healthy)
    if not remaining:
        healthy.sort(key=lambda seg: (seg["start"], seg["end"]))
        return healthy

    for sub_start, sub_end in remaining:
        if _budget["decodes"] <= 0:
            break
        if (sub_end - sub_start) >= 0.9 * span:
            # The window we just decoded failed for (essentially) its whole
            # extent — retrying it identically would reproduce the failure.
            if span >= 2 * LADDER_MIN_SPAN_SECONDS:
                midpoint = (sub_start + sub_end) / 2
                healthy.extend(
                    _decode_span_with_ladder(
                        model,
                        audio,
                        sub_start,
                        midpoint,
                        language=language,
                        speech_regions=speech_regions,
                        _budget=_budget,
                    )
                )
                healthy.extend(
                    _decode_span_with_ladder(
                        model,
                        audio,
                        midpoint,
                        sub_end,
                        language=language,
                        speech_regions=speech_regions,
                        _budget=_budget,
                    )
                )
            else:
                # Split floor: change the window the only other way we can —
                # widened acoustic context.
                _budget["decodes"] -= 1
                wide = _sequential_decode_span(
                    model,
                    audio,
                    sub_start,
                    sub_end,
                    language=language,
                    padding=GAP_DECODE_WIDE_PADDING_SECONDS,
                )
                healthy.extend(
                    seg
                    for seg in wide
                    if not segment_is_degenerate(
                        seg,
                        language=language,
                        speech_regions=speech_regions,
                        audio_duration=audio_duration,
                    )
                )
        else:
            # A residual sub-gap is already a different (smaller) window.
            healthy.extend(
                _decode_span_with_ladder(
                    model,
                    audio,
                    sub_start,
                    sub_end,
                    language=language,
                    speech_regions=speech_regions,
                    _budget=_budget,
                )
            )

    healthy.sort(key=lambda seg: (seg["start"], seg["end"]))
    return healthy


def _sequential_decode_span(
    model: Any,
    audio: Any,
    gap_start: float,
    gap_end: float,
    *,
    language: str,
    padding: float = GAP_DECODE_PADDING_SECONDS,
) -> list[dict]:
    """Re-decode one uncovered span with faster-whisper's sequential path."""
    clip_start = max(0.0, gap_start - padding)
    clip_end = min(len(audio) / SAMPLE_RATE, gap_end + padding)
    if clip_end <= clip_start:
        return []
    clip = audio[int(clip_start * SAMPLE_RATE) : int(clip_end * SAMPLE_RATE)]
    clip_duration = clip_end - clip_start

    try:
        segment_iter, _info = model.transcribe(
            clip,
            language=language,
            vad_filter=False,
            beam_size=5,
            condition_on_previous_text=False,
        )
        raw = list(segment_iter)
    except Exception:
        logger.warning(
            "Sequential re-decode failed for %.2fs -> %.2fs",
            gap_start,
            gap_end,
            exc_info=True,
        )
        return []

    segments: list[dict] = []
    for seg in raw:
        # A segment claiming to end past the clip it was decoded from is
        # decoder-state garbage; clamping it would fabricate a full-gap
        # coverage claim that can look lexically healthy. Drop pre-clamp.
        if float(seg.end) > clip_duration + SEGMENT_END_OVERRUN_TOLERANCE_SECONDS:
            logger.info(
                "Sequential re-decode emitted an overrun segment "
                "(%.2fs end in a %.2fs clip) — dropped",
                float(seg.end),
                clip_duration,
            )
            continue
        start = clip_start + float(seg.start)
        end = clip_start + float(seg.end)
        # Keep only material that genuinely lives inside the gap.
        overlap = min(end, gap_end) - max(start, gap_start)
        if overlap < GAP_SEGMENT_MIN_OVERLAP_SECONDS:
            continue
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(
            {
                "start": max(start, gap_start),
                "end": min(end, gap_end),
                "text": text,
            }
        )
    if segments:
        logger.info(
            "Sequential re-decode recovered %d segment(s) in %.2fs -> %.2fs",
            len(segments),
            gap_start,
            gap_end,
        )
    return segments


def log_residual_coverage(
    words: list[dict],
    speech_regions: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Coverage check after alignment: speech spans still without words.

    Logs and returns the gaps; the transcriber feeds them back through
    ``redecode_spans`` so token-cap truncation doesn't silently produce
    empty scenes.
    """
    pseudo_segments = [
        {"start": word["start"], "end": word["end"], "text": word.get("text") or "w"}
        for word in words
        if isinstance(word.get("start"), (int, float))
        and isinstance(word.get("end"), (int, float))
    ]
    residual = uncovered_speech_spans(speech_regions, pseudo_segments)
    if residual:
        logger.warning(
            "ASR residual coverage gaps after alignment (speech with no "
            "words): %s",
            [(round(start, 2), round(end, 2)) for start, end in residual],
        )
    return residual
