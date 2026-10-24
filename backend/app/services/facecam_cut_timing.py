"""Recover false starts hidden inside stretched ASR words, for cut review only."""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
import wave
from pathlib import Path

from .atomic_files import write_text_atomic

VERSION = 1


def timing_key(timing: dict, duration: float) -> str:
    return hashlib.sha256(json.dumps([VERSION, duration, timing], sort_keys=True).encode()).hexdigest()


def cached_cut_timing(root: Path, timing: dict, duration: float) -> dict | None:
    try:
        cached = json.loads((root / "cut_speech_timing.json").read_text())
        if cached.get("source_key") == timing_key(timing, duration) and cached.get("words"):
            return cached
    except (OSError, ValueError):
        pass
    return None


def recovery_windows(words: list[dict], duration: float) -> list[dict]:
    """An implausibly long word can hide a discarded attempt plus its restart."""
    windows = []
    for word in words:
        if float(word["end"]) - float(word["start"]) <= 1.5:
            continue
        lo, hi = max(0, word["start"] - 3), min(duration, word["end"] + 3)
        first = next(i for i, w in enumerate(words) if w["end"] > lo)
        last = next((i for i, w in enumerate(words) if w["start"] >= hi), len(words))
        # Expand context to whole words; replacement uses indices, never fuzzy deduplication.
        lo = max(0, words[first]["start"] - .1)
        if first:
            lo = max(lo, (words[first - 1]["end"] + words[first]["start"]) / 2)
        hi = min(duration, words[last - 1]["end"] + .1)
        if last < len(words):
            hi = min(hi, (words[last - 1]["end"] + words[last]["start"]) / 2)
        suspect = [word["start"], word["end"]]
        if windows and first <= windows[-1]["last"]:
            windows[-1].update(last=last, end=hi)
            windows[-1]["suspects"].append(suspect)
        else:
            windows.append({"first": first, "last": last, "start": lo, "end": hi, "suspects": [suspect]})
    return windows


def pause_midpoint(source, start: float, end: float) -> float | None:
    """Split only inside an actual quiet gap, never in the middle of a phoneme."""
    import numpy as np
    rate = source.getframerate()
    source.setpos(min(source.getnframes(), round(start * rate)))
    samples = np.frombuffer(source.readframes(round((end - start) * rate)), dtype="<i2")
    hop = max(1, rate // 100)
    count = len(samples) // hop
    if not count:
        return None
    peaks = np.abs(samples[:count * hop].astype(np.int32).reshape(count, hop)).max(axis=1)
    quiet = peaks < 32768 * 10 ** (-30 / 20)
    best, begin = (0, 0), None
    for i, silent in enumerate([*quiet, False]):
        if silent and begin is None:
            begin = i
        elif not silent and begin is not None:
            if i - begin > best[1] - best[0]:
                best = (begin, i)
            begin = None
    if (best[1] - best[0]) * hop / rate < .2:
        return None
    return start + (best[0] + best[1]) / 2 * hop / rate


def refine_cut_timing(root: Path, timing: dict, duration: float) -> dict:
    cached = cached_cut_timing(root, timing, duration)
    if cached:
        return cached
    windows = recovery_windows(timing["words"], duration)
    if not windows:
        return timing
    from .transcriber import TranscriberService
    words, uncertain, cursor = [], list(timing.get("uncertain_spans", [])), 0
    reports = []
    # Keep the generic ASR/protected-silence cache immutable. This independent
    # transcript is only evidence for advisory cuts and the editor's word navigation.
    with TranscriberService.model_session(), tempfile.TemporaryDirectory(prefix="facecam-cuts-") as temp, \
            wave.open(str(root / "voice.wav"), "rb") as source:
        if source.getsampwidth() != 2 or source.getnchannels() != 1:
            raise ValueError("Format audio de la prise incompatible.")
        rate = source.getframerate()
        for window in windows:
            splits = [pause_midpoint(source, a, b) for a, b in window["suspects"]]
            bounds = sorted({window["start"], window["end"], *(t for t in splits if t is not None)})
            recovered = []
            for start, end in zip(bounds, bounds[1:]):
                # Sample-quantize the offset as well as the clip itself.
                first, last = round(start * rate), min(source.getnframes(), round(end * rate))
                offset = first / rate
                clip = Path(temp) / "window.wav"
                source.setpos(first)
                with wave.open(str(clip), "wb") as target:
                    target.setparams(source.getparams())
                    target.writeframes(source.readframes(last - first))
                result = TranscriberService._transcribe_sync(clip, "fr", "large-v3")
                decoded, _ = result
                for word in decoded:
                    a, b = word["start"], word["end"]
                    if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= (last - first) / rate + .05):
                        raise ValueError("Repérage des reprises : horodatage local invalide.")
                    recovered.append({**word, "start": round(offset + a, 6), "end": round(min(last / rate, offset + b), 6)})
                uncertain.extend((offset + a, offset + b) for a, b in getattr(result, "unrecovered_gaps", []))
            if not recovered:
                raise ValueError("Repérage des reprises : passage non transcrit.")
            words.extend(timing["words"][cursor:window["first"]])
            words.extend(recovered)
            cursor = window["last"]
            # A recovered repetition needs human audition even if the LLM is
            # certain: a second recognition pass is evidence, not acoustic proof.
            uncertain.append((window["start"], window["end"]))
            reports.append({**window, "splits": bounds[1:-1], "word_count": len(recovered)})
    words.extend(timing["words"][cursor:])
    result = {"version": VERSION, "source_key": timing_key(timing, duration), "words": words,
              "uncertain_spans": uncertain, "recovery_windows": reports}
    write_text_atomic(root / "cut_speech_timing.json", json.dumps(result, ensure_ascii=False))
    return result
