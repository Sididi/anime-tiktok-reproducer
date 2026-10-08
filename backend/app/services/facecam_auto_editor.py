"""Fast silence cuts constrained by the words in the unedited recording."""
from __future__ import annotations

import hashlib
import json
import math

from ..models.facecam import FacecamAutoEditorSettings, FacecamState

FPS = 60
ALGORITHM_VERSION = 2
WORD_LEAD_SECONDS = 0.03
WORD_TAIL_SECONDS = 0.05


def settings_signature(settings: FacecamAutoEditorSettings) -> str:
    # Keep existing 1x previews valid: adding the optional speed control must not
    # reprocess a recording that the user has not asked to retime.
    payload = {"version": ALGORITHM_VERSION, **settings.model_dump(exclude={"speed"})}
    if settings.speed != 1.0:
        payload["speed"] = settings.speed
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def prepared_is_current(state: FacecamState) -> bool:
    prepared = state.prepared or {}
    return bool(state.recording and prepared
                and state.approved_revision == state.revision
                and prepared.get("revision") == state.revision
                and prepared.get("fingerprint") == state.recording["fingerprint"]
                and prepared.get("auto_editor_signature") == settings_signature(state.auto_editor))


def protected_spans(words: list[dict], uncertain_spans=()) -> list[tuple[float, float]]:
    spans = [(float(w["start"]), float(w["end"])) for w in words]
    spans.extend((float(a), float(b)) for a, b in uncertain_spans)
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError("Horodatage de parole invalide. Réimportez l'enregistrement.")
        start, end = start - WORD_LEAD_SECONDS, end + WORD_TAIL_SECONDS
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def keep_intervals(duration: float, silences: list[tuple[float, float]],
                   settings: FacecamAutoEditorSettings | None = None,
                   speech: list[tuple[float, float]] = ()) -> list[tuple[int, int]]:
    """Subtract only silent frames outside protected speech, rounding toward safety.

    Margins refer to the speech: `after` stays after the preceding word and
    `before` stays before the next one. No orphan silence at either file edge.
    """
    settings = settings or FacecamAutoEditorSettings()
    # The last partial frame may contain a consonant. Retain it and let the WAV
    # writer pad only the samples beyond EOF instead of truncating real audio.
    total = math.ceil(duration * FPS - 1e-7)
    cuts = []
    for start, end in sorted(silences):
        if end - start + 1e-7 < settings.min_silence_ms / 1000:
            continue
        start = max(0, start + settings.margin_after_ms / 1000 if start > 0 else 0)
        end = min(duration, end - settings.margin_before_ms / 1000 if end < duration - 1e-5 else duration)
        candidates = [(start, end)]
        for lo, hi in speech:
            remaining = []
            for a, b in candidates:
                if hi <= a or lo >= b:
                    remaining.append((a, b))
                else:
                    if a < lo:
                        remaining.append((a, lo))
                    if hi < b:
                        remaining.append((hi, b))
            candidates = remaining
        for a, b in candidates:
            a, b = max(0, math.ceil(a * FPS - 1e-7)), total if b >= duration - 1e-7 else min(total, math.floor(b * FPS + 1e-7))
            if b - a >= 2:  # Avoid one-frame edits that add jitter without useful time savings.
                cuts.append((a, b))
    keep, cursor = [], 0
    for start, end in sorted(cuts):
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        keep.append((cursor, total))
    return keep


def verify_speech_retained(keep: list[tuple[int, int]], speech: list[tuple[float, float]],
                          duration: float) -> None:
    """Fail closed if any protected interval loses even one video frame."""
    total = math.ceil(duration * FPS - 1e-7)
    for start, end in speech:
        first = max(0, math.floor(start * FPS + 1e-7))
        last = min(total, math.ceil(end * FPS - 1e-7))
        if last > first and not any(a <= first and b >= last for a, b in keep):
            raise ValueError("Une coupe touche la parole. L'aperçu n'a pas été remplacé.")
