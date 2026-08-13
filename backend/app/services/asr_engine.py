"""ASR core: fast batched decode with a coverage-checked sequential safety net.

Replaces the whisperx batched pipeline + ~600 lines of repair machinery
(2026-08-13). The old stack silently dropped whole VAD chunks (large-v3
batched-decode pathology, worst on non-Latin speech) and needed layered
defenses. The new core:

1. **Batched decode** via faster-whisper's ``BatchedInferencePipeline``
   (silero VAD, same CTranslate2 engine → same speed as before).
2. **Coverage check**: silero speech regions are compared against decoded
   segment spans. Any speech span left without text is the dropout
   signature.
3. **Sequential re-decode of only the uncovered spans** — a genuinely
   different decode path that does not reproduce the batched dropout.
   On healthy media this never fires and costs nothing.

Word-level timing precision is unaffected: wav2vec2 forced alignment
(whisperx.align, driven by the transcriber) runs on the merged segments
exactly as before.
"""

from __future__ import annotations

import logging
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
# Padding around a re-decoded gap so the decoder gets acoustic context.
GAP_DECODE_PADDING_SECONDS = 0.35
# A re-decoded segment must overlap its gap by this much to be kept
# (protects against the sequential pass hallucinating over the padding).
GAP_SEGMENT_MIN_OVERLAP_SECONDS = 0.2


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
    from faster_whisper import BatchedInferencePipeline
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    # 1) Speech regions — cheap CPU pass, and the coverage ground truth.
    speech = get_speech_timestamps(audio, VadOptions())
    speech_regions = [
        (float(chunk["start"]) / SAMPLE_RATE, float(chunk["end"]) / SAMPLE_RATE)
        for chunk in speech
    ]

    # 2) Fast batched decode. ``without_timestamps=False`` is load-bearing:
    # the default emits one chunk-wide segment whose span claims coverage
    # even when the text was token-cap truncated — honest per-sentence spans
    # are what make the coverage check below meaningful.
    pipeline = BatchedInferencePipeline(model)
    segment_iter, info = pipeline.transcribe(
        audio,
        language=language,
        batch_size=batch_size,
        vad_filter=True,
        word_timestamps=False,
        without_timestamps=False,
        chunk_length=CHUNK_LENGTH_SECONDS,
    )
    segments = [
        {"start": float(seg.start), "end": float(seg.end), "text": seg.text or ""}
        for seg in segment_iter
    ]
    detected_language = getattr(info, "language", None) or (language or "en")

    # 3) Coverage check → 4) sequential re-decode of uncovered spans only.
    gaps = uncovered_speech_spans(speech_regions, segments)
    if gaps:
        logger.info(
            "ASR coverage: %d speech span(s) decoded to nothing, re-decoding "
            "sequentially: %s",
            len(gaps),
            [(round(start, 2), round(end, 2)) for start, end in gaps],
        )
        for gap_start, gap_end in gaps:
            segments.extend(
                _sequential_decode_span(
                    model,
                    audio,
                    gap_start,
                    gap_end,
                    language=detected_language,
                )
            )
        segments.sort(key=lambda seg: (seg["start"], seg["end"]))

    return segments, detected_language, speech_regions


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


def _sequential_decode_span(
    model: Any,
    audio: Any,
    gap_start: float,
    gap_end: float,
    *,
    language: str,
) -> list[dict]:
    """Re-decode one uncovered span with faster-whisper's sequential path."""
    clip_start = max(0.0, gap_start - GAP_DECODE_PADDING_SECONDS)
    clip_end = min(len(audio) / SAMPLE_RATE, gap_end + GAP_DECODE_PADDING_SECONDS)
    if clip_end <= clip_start:
        return []
    clip = audio[int(clip_start * SAMPLE_RATE) : int(clip_end * SAMPLE_RATE)]

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
    """Final sanity check after alignment: speech spans still without words.

    Purely observational (returns and logs); regressions become visible in
    the logs instead of silently producing empty scenes.
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
