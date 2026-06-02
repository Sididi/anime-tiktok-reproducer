# Voice De-fingerprinting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Post-process the published TTS voiceover (`tts_edited.wav`) through a configurable, duration-preserving ffmpeg chain that strips ElevenLabs/AI fingerprints, to stop TikTok false-positive "low quality" strikes.

**Architecture:** A new stateless service `VoiceDefingerprintService.apply()` transforms a WAV in place via a two-pass ffmpeg pipeline (DSP filtergraph + lossy round-trip), with parameters randomized per run within preset bounds (`off`/`light`/`moderate`/`aggressive`). It is fail-open (never breaks a run) and preserves exact audio duration (subtitle timing is locked). It is invoked as the final step of the processing pipeline, just before the run is marked complete, operating on a fresh `tts_edited.raw.wav` backup.

**Tech Stack:** Python 3, ffmpeg (n8.1, already a dependency via pydub: `rubberband`, `anoisesrc`, `amix`, `asoftclip`, `aecho`, `lowpass`, `loudnorm`, `aresample` soxr), `pydantic-settings`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-02-voice-defingerprint-design.md`

---

## File Structure

- **Create:** `backend/app/services/voice_defingerprint.py` — the de-fingerprinting service: preset table, pure param-sampling + filtergraph builders, ffmpeg orchestration, fail-open `apply()`.
- **Create:** `backend/tests/test_voice_defingerprint.py` — unit tests (pure builders, fail-open, off no-op) + one integration smoke test against real ffmpeg.
- **Modify:** `backend/app/config.py` — add `voice_defingerprint_level` setting.
- **Modify:** `backend/app/services/processing.py` — invoke the service as the final pipeline step (around line 3090, before `clear_processing_state`).

All tests run from the `backend/` directory with the project venv active:
```bash
source .venv/bin/activate   # per user setup (uv venv)
cd backend && python -m pytest tests/test_voice_defingerprint.py -v
```

---

## Task 1: Config setting

**Files:**
- Modify: `backend/app/config.py:92` (insert after `elevenlabs_output_format`)
- Test: `backend/tests/test_voice_defingerprint.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_voice_defingerprint.py` with:

```python
"""Tests for the voice de-fingerprinting service."""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_config_defaults_to_moderate():
    from app.config import Settings

    settings = Settings()
    assert settings.voice_defingerprint_level == "moderate"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py::test_config_defaults_to_moderate -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'voice_defingerprint_level'`

- [ ] **Step 3: Add the setting**

In `backend/app/config.py`, immediately after line 92 (`elevenlabs_output_format: str = "pcm_44100"`), add:

```python
    # Voice de-fingerprinting: post-process the published TTS voiceover to strip
    # ElevenLabs/AI fingerprints and avoid TikTok false-positive strikes.
    # One of: "off" | "light" | "moderate" | "aggressive". Env: ATR_VOICE_DEFINGERPRINT_LEVEL.
    voice_defingerprint_level: str = "moderate"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py::test_config_defaults_to_moderate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_voice_defingerprint.py
git commit -m "feat(voice): add voice_defingerprint_level setting"
```

---

## Task 2: Level normalization + preset table + param sampling

**Files:**
- Create: `backend/app/services/voice_defingerprint.py`
- Test: `backend/tests/test_voice_defingerprint.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voice_defingerprint.py`:

```python
def test_normalize_level_passes_through_valid():
    from app.services.voice_defingerprint import normalize_level

    for level in ("off", "light", "moderate", "aggressive"):
        assert normalize_level(level) == level


def test_normalize_level_is_case_insensitive_and_trims():
    from app.services.voice_defingerprint import normalize_level

    assert normalize_level("  Moderate ") == "moderate"


def test_normalize_level_falls_back_to_moderate_on_unknown():
    from app.services.voice_defingerprint import normalize_level

    assert normalize_level("banana") == "moderate"
    assert normalize_level(None) == "moderate"


def test_sample_params_is_deterministic_for_a_seed():
    import random

    from app.services.voice_defingerprint import _sample_params

    a = _sample_params("moderate", random.Random(42))
    b = _sample_params("moderate", random.Random(42))
    assert a == b


def test_sample_params_respects_moderate_bounds():
    import random

    from app.services.voice_defingerprint import _sample_params

    p = _sample_params("moderate", random.Random(7))
    assert -48.0 <= p["noise_dbfs"] <= -44.0
    assert -30.0 <= p["pitch_cents"] <= 30.0
    assert 15500 <= p["lowpass_hz"] <= 16500
    assert p["formant_shift"] is False
    assert p["reverb"] is True
    assert p["saturation"] is False
    assert p["lossy_bitrate_k"] == 128
    assert p["lossy_passes"] == 1


def test_sample_params_aggressive_enables_saturation_and_double_lossy():
    import random

    from app.services.voice_defingerprint import _sample_params

    p = _sample_params("aggressive", random.Random(1))
    assert p["formant_shift"] is True
    assert p["saturation"] is True
    assert p["lossy_passes"] == 2
    assert p["lossy_bitrate_k"] == 96
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "normalize_level or sample_params"`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.voice_defingerprint'`

- [ ] **Step 3: Create the module with the preset table and sampling**

Create `backend/app/services/voice_defingerprint.py`:

```python
"""Voice de-fingerprinting: strip ElevenLabs/AI fingerprints from TTS audio.

Applies a configurable, duration-preserving ffmpeg processing chain to the
published voiceover so TikTok's automated detector stops flagging it as
"low quality" (a false positive). See
docs/superpowers/specs/2026-06-02-voice-defingerprint-design.md.
"""
from __future__ import annotations

import contextlib
import logging
import os
import random
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

VALID_LEVELS: tuple[str, ...] = ("off", "light", "moderate", "aggressive")
_DURATION_TOLERANCE_S = 0.05


@dataclass(frozen=True)
class _PresetBounds:
    noise_dbfs: tuple[float, float]
    pitch_cents: tuple[float, float]
    lowpass_hz: tuple[int, int]
    formant_shift: bool
    reverb: bool
    saturation: bool
    lossy_bitrate_k: int
    lossy_passes: int


_PRESETS: dict[str, _PresetBounds] = {
    "light": _PresetBounds((-60.0, -56.0), (-5.0, 5.0), (17500, 18500), False, False, False, 192, 1),
    "moderate": _PresetBounds((-48.0, -44.0), (-30.0, 30.0), (15500, 16500), False, True, False, 128, 1),
    "aggressive": _PresetBounds((-40.0, -36.0), (-50.0, 50.0), (13500, 14500), True, True, True, 96, 2),
}


def normalize_level(level: str | None) -> str:
    candidate = (level or "").strip().lower()
    if candidate in VALID_LEVELS:
        return candidate
    logger.warning(
        "Unknown voice de-fingerprint level %r; falling back to 'moderate'", level
    )
    return "moderate"


def _sample_params(level: str, rng: random.Random) -> dict[str, Any]:
    bounds = _PRESETS[level]
    return {
        "noise_dbfs": round(rng.uniform(*bounds.noise_dbfs), 2),
        "pitch_cents": round(rng.uniform(*bounds.pitch_cents), 2),
        "lowpass_hz": rng.randint(*bounds.lowpass_hz),
        "formant_shift": bounds.formant_shift,
        "reverb": bounds.reverb,
        "saturation": bounds.saturation,
        "lossy_bitrate_k": bounds.lossy_bitrate_k,
        "lossy_passes": bounds.lossy_passes,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "normalize_level or sample_params"`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/voice_defingerprint.py backend/tests/test_voice_defingerprint.py
git commit -m "feat(voice): add de-fingerprint preset table and param sampling"
```

---

## Task 3: Pure filtergraph builder

**Files:**
- Modify: `backend/app/services/voice_defingerprint.py`
- Test: `backend/tests/test_voice_defingerprint.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voice_defingerprint.py`:

```python
def test_build_filter_complex_includes_core_filters():
    from app.services.voice_defingerprint import _build_filter_complex

    params = {
        "noise_dbfs": -46.0,
        "pitch_cents": 0.0,
        "lowpass_hz": 16000,
        "formant_shift": False,
        "reverb": True,
        "saturation": False,
        "lossy_bitrate_k": 128,
        "lossy_passes": 1,
    }
    graph = _build_filter_complex(params, duration_s=12.5)
    assert "aresample=48000" in graph
    assert "aresample=44100" in graph
    assert "rubberband=pitch=" in graph
    assert "formant=preserved" in graph
    assert "lowpass=f=16000" in graph
    assert "aecho=" in graph            # reverb enabled
    assert "asoftclip" not in graph     # saturation disabled
    assert "amix=inputs=2" in graph
    assert "loudnorm=" in graph
    assert "atrim=0:12.500000" in graph


def test_build_filter_complex_pitch_ratio_for_positive_cents():
    from app.services.voice_defingerprint import _build_filter_complex

    params = {
        "noise_dbfs": -46.0,
        "pitch_cents": 1200.0,  # one octave => ratio 2.0
        "lowpass_hz": 16000,
        "formant_shift": True,
        "reverb": False,
        "saturation": True,
        "lossy_bitrate_k": 96,
        "lossy_passes": 2,
    }
    graph = _build_filter_complex(params, duration_s=5.0)
    assert "rubberband=pitch=2.000000" in graph
    assert "formant=shifted" in graph
    assert "asoftclip" in graph         # saturation enabled
    assert "aecho=" not in graph        # reverb disabled
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "build_filter_complex"`
Expected: FAIL with `ImportError: cannot import name '_build_filter_complex'`

- [ ] **Step 3: Add the builder helpers**

In `backend/app/services/voice_defingerprint.py`, add after `_sample_params`:

```python
def _cents_to_ratio(cents: float) -> float:
    return 2.0 ** (cents / 1200.0)


def _dbfs_to_amplitude(dbfs: float) -> float:
    return 10.0 ** (dbfs / 20.0)


def _build_filter_complex(params: dict[str, Any], duration_s: float) -> str:
    """Build the pass-1 ffmpeg -filter_complex string.

    Input 0 is the voice WAV, input 1 is a full-scale noise source. The voice is
    processed (resample detour, pitch/formant shift, HF roll-off, optional reverb
    and saturation), mixed with attenuated noise, loudness-normalized, then padded
    and trimmed to the exact original duration. Output label is [out].
    """
    chain = ["aresample=48000:resampler=soxr", "aresample=44100:resampler=soxr"]
    ratio = _cents_to_ratio(params["pitch_cents"])
    formant = "shifted" if params["formant_shift"] else "preserved"
    chain.append(f"rubberband=pitch={ratio:.6f}:formant={formant}")
    chain.append(f"lowpass=f={params['lowpass_hz']}")
    if params["reverb"]:
        chain.append("aecho=0.8:0.85:18:0.18")
    if params["saturation"]:
        chain.append("asoftclip=type=tanh:threshold=0.9")
    voice_chain = ",".join(chain)
    noise_gain = _dbfs_to_amplitude(params["noise_dbfs"])
    return (
        f"[0:a]{voice_chain}[v];"
        f"[1:a]volume={noise_gain:.6f}[n];"
        f"[v][n]amix=inputs=2:weights=1 1:duration=first:normalize=0[mix];"
        f"[mix]loudnorm=I=-16:TP=-1.5:LRA=11,apad,atrim=0:{duration_s:.6f}[out]"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "build_filter_complex"`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/voice_defingerprint.py backend/tests/test_voice_defingerprint.py
git commit -m "feat(voice): add pure ffmpeg filtergraph builder"
```

---

## Task 4: ffmpeg pass helpers + duration probe

**Files:**
- Modify: `backend/app/services/voice_defingerprint.py`
- Test: `backend/tests/test_voice_defingerprint.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voice_defingerprint.py`:

```python
def _write_sine_wav(path: Path, *, seconds: float = 2.0, rate: int = 44100) -> None:
    """Write a mono 16-bit sine-ish tone WAV without external deps."""
    import math
    import struct

    n = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            value = int(12000 * math.sin(2 * math.pi * 220.0 * (i / rate)))
            frames += struct.pack("<h", value)
        wf.writeframes(bytes(frames))


def test_wav_duration_reads_seconds(tmp_path):
    from app.services.voice_defingerprint import _wav_duration

    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav, seconds=1.5)
    assert abs(_wav_duration(wav) - 1.5) < 0.01


def test_run_ffmpeg_raises_on_failure():
    from app.services.voice_defingerprint import _run_ffmpeg

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        _run_ffmpeg(["-i", "/nonexistent/does-not-exist.wav", "/tmp/never.wav"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "wav_duration or run_ffmpeg"`
Expected: FAIL with `ImportError: cannot import name '_wav_duration'`

- [ ] **Step 3: Add the helpers**

In `backend/app/services/voice_defingerprint.py`, add after `_build_filter_complex`:

```python
def _wav_duration(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    if rate <= 0:
        raise RuntimeError(f"Invalid sample rate in {path}")
    return frames / float(rate)


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:500]}")


def _run_dsp_pass(
    input_path: Path, output_path: Path, params: dict[str, Any], duration_s: float
) -> None:
    filter_complex = _build_filter_complex(params, duration_s)
    _run_ffmpeg(
        [
            "-i", str(input_path),
            "-f", "lavfi",
            "-i", "anoisesrc=color=pink:amplitude=1:sample_rate=44100",
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
            str(output_path),
        ]
    )


def _run_lossy_roundtrip(
    input_path: Path,
    output_path: Path,
    bitrate_k: int,
    duration_s: float,
    *,
    workdir: Path,
    index: int,
) -> None:
    encoded = workdir / f"enc_{index}.m4a"
    _run_ffmpeg(["-i", str(input_path), "-c:a", "aac", "-b:a", f"{bitrate_k}k", str(encoded)])
    _run_ffmpeg(
        [
            "-i", str(encoded),
            "-af", f"apad,atrim=0:{duration_s:.6f}",
            "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
            str(output_path),
        ]
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "wav_duration or run_ffmpeg"`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/voice_defingerprint.py backend/tests/test_voice_defingerprint.py
git commit -m "feat(voice): add ffmpeg DSP and lossy round-trip helpers"
```

---

## Task 5: Public `apply()` — off no-op, fail-open, integration smoke

**Files:**
- Modify: `backend/app/services/voice_defingerprint.py`
- Test: `backend/tests/test_voice_defingerprint.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_voice_defingerprint.py`:

```python
def test_apply_off_is_noop_copy(tmp_path):
    from app.services.voice_defingerprint import VoiceDefingerprintService

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    _write_sine_wav(src, seconds=1.0)

    result = VoiceDefingerprintService.apply(src, dst, level="off")

    assert result["applied"] is False
    assert result["level"] == "off"
    assert dst.exists()
    assert dst.read_bytes() == src.read_bytes()


def test_apply_fail_open_keeps_original(tmp_path, monkeypatch):
    from app.services import voice_defingerprint as mod

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    _write_sine_wav(src, seconds=1.0)

    def boom(*_args, **_kwargs):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(mod, "_run_dsp_pass", boom)

    result = mod.VoiceDefingerprintService.apply(src, dst, level="moderate", seed=123)

    assert result["applied"] is False
    assert "error" in result
    assert result["seed"] == 123
    assert dst.exists()
    assert dst.read_bytes() == src.read_bytes()  # original preserved


def test_apply_moderate_real_ffmpeg_preserves_duration(tmp_path):
    """Integration smoke test against the real ffmpeg binary."""
    from app.services.voice_defingerprint import VoiceDefingerprintService, _wav_duration

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    _write_sine_wav(src, seconds=3.0)
    src_duration = _wav_duration(src)

    result = VoiceDefingerprintService.apply(src, dst, level="moderate", seed=99)

    assert result["applied"] is True
    assert result["seed"] == 99
    assert dst.exists()
    out_duration = _wav_duration(dst)
    assert abs(out_duration - src_duration) <= 0.05
    with wave.open(str(dst), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 44100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v -k "apply"`
Expected: FAIL with `ImportError: cannot import name 'VoiceDefingerprintService'`

- [ ] **Step 3: Add the public service class**

In `backend/app/services/voice_defingerprint.py`, add at the end of the file:

```python
class VoiceDefingerprintService:
    """Strips AI/ElevenLabs fingerprints from a TTS voiceover WAV (fail-open)."""

    @classmethod
    def apply(
        cls,
        input_path: Path | str,
        output_path: Path | str,
        *,
        level: str,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Process ``input_path`` into ``output_path`` (same exact duration).

        Never raises: on any failure the original audio is preserved at
        ``output_path`` and the returned dict has ``applied=False``.
        """
        input_path = Path(input_path)
        output_path = Path(output_path)
        level = normalize_level(level)

        if level == "off":
            if input_path != output_path:
                shutil.copyfile(input_path, output_path)
            return {"applied": False, "level": "off", "seed": None, "params": None}

        if seed is None:
            seed = int.from_bytes(os.urandom(4), "big")
        rng = random.Random(seed)
        params = _sample_params(level, rng)

        try:
            src_duration = _wav_duration(input_path)
            with tempfile.TemporaryDirectory() as td:
                workdir = Path(td)
                dsp_out = workdir / "dsp.wav"
                _run_dsp_pass(input_path, dsp_out, params, src_duration)

                current = dsp_out
                for index in range(params["lossy_passes"]):
                    nxt = workdir / f"lossy_{index}.wav"
                    _run_lossy_roundtrip(
                        current,
                        nxt,
                        params["lossy_bitrate_k"],
                        src_duration,
                        workdir=workdir,
                        index=index,
                    )
                    current = nxt

                out_duration = _wav_duration(current)
                if abs(out_duration - src_duration) > _DURATION_TOLERANCE_S:
                    raise RuntimeError(
                        f"duration drift: {out_duration:.3f}s vs {src_duration:.3f}s"
                    )
                shutil.copyfile(current, output_path)

            logger.info(
                "Voice de-fingerprint applied: level=%s seed=%s params=%s",
                level,
                seed,
                params,
            )
            return {"applied": True, "level": level, "seed": seed, "params": params}
        except Exception as exc:  # fail-open: a usable run beats a broken one
            logger.warning(
                "Voice de-fingerprint failed (%s); keeping original audio", exc
            )
            if input_path != output_path:
                shutil.copyfile(input_path, output_path)
            return {
                "applied": False,
                "level": level,
                "seed": seed,
                "params": params,
                "error": str(exc),
            }
```

- [ ] **Step 4: Run the full module test suite**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py -v`
Expected: PASS (all tests, including the real-ffmpeg integration smoke test)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/voice_defingerprint.py backend/tests/test_voice_defingerprint.py
git commit -m "feat(voice): add fail-open VoiceDefingerprintService.apply"
```

---

## Task 6: Wire into the processing pipeline

**Files:**
- Modify: `backend/app/services/processing.py` (import near other service imports ~line 42; new step before line 3091 `cls.clear_processing_state(project.id)`)

This step has no standalone unit test (it is glue inside a ~700-line async generator). Correctness is verified by (a) the service's own test suite, and (b) a manual processing run. Keep the edit minimal and rely on fail-open.

- [ ] **Step 1: Add the import**

In `backend/app/services/processing.py`, find the service import block near line 42 (where `from .forced_alignment import ForcedAlignmentService, PreparedAlignmentAudio` lives) and add:

```python
from .voice_defingerprint import VoiceDefingerprintService
```

- [ ] **Step 2: Insert the de-fingerprint step**

In `backend/app/services/processing.py`, locate this block (around line 3091):

```python
            # Clear processing state now that we're done
            cls.clear_processing_state(project.id)

            yield ProcessingProgress(
                "complete",
                "overlay_image_generation",
                1.0,
                "Processing complete!",
            )
```

Insert the following IMMEDIATELY BEFORE the `# Clear processing state now that we're done` comment:

```python
            # Step 7: De-fingerprint the published TTS voiceover to avoid TikTok
            # false-positive strikes. Fail-open: never blocks completion.
            tts_final_path = output_dir / "tts_edited.wav"
            if tts_final_path.exists() and settings.voice_defingerprint_level != "off":
                yield ProcessingProgress(
                    "processing",
                    "voice_defingerprint",
                    0.95,
                    "Applying voice de-fingerprinting...",
                )
                # tts_edited.wav was rebuilt fresh this run; snapshot it as the
                # clean original, then process from that snapshot.
                raw_backup = output_dir / "tts_edited.raw.wav"
                shutil.copyfile(tts_final_path, raw_backup)
                defingerprint_meta = await asyncio.to_thread(
                    VoiceDefingerprintService.apply,
                    raw_backup,
                    tts_final_path,
                    level=settings.voice_defingerprint_level,
                )
                logger.info(
                    "Voice de-fingerprint result: %s",
                    {
                        key: defingerprint_meta.get(key)
                        for key in ("applied", "level", "seed")
                    },
                )

```

- [ ] **Step 3: Verify the module still imports**

Run: `cd backend && python -c "import app.services.processing"`
Expected: no output, exit code 0 (no syntax/import errors)

- [ ] **Step 4: Run the full backend test suite to confirm no regressions**

Run: `cd backend && python -m pytest tests/test_voice_defingerprint.py tests/test_processing_overlay_jsx.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/processing.py
git commit -m "feat(voice): run de-fingerprinting as final processing step"
```

---

## Task 7: Documentation note

**Files:**
- Modify: `backend/app/config.py` (already documented inline in Task 1 — no action unless a `.env.example` exists)

- [ ] **Step 1: Check for an env example file**

Run: `ls backend/.env.example backend/.env.sample .env.example 2>/dev/null`

- [ ] **Step 2: If one exists, document the variable**

If a file was found, append:

```bash
# Voice de-fingerprinting level: off | light | moderate | aggressive (default: moderate)
ATR_VOICE_DEFINGERPRINT_LEVEL=moderate
```

If no file exists, skip this task (the inline comment in `config.py` is the documentation).

- [ ] **Step 3: Commit (only if a file was modified)**

```bash
git add -A && git commit -m "docs(voice): document ATR_VOICE_DEFINGERPRINT_LEVEL"
```

---

## Manual Verification (after all tasks)

1. Set `ATR_VOICE_DEFINGERPRINT_LEVEL=moderate` (default) and run a full processing job on a real project.
2. Confirm both `tts_edited.wav` (processed) and `tts_edited.raw.wav` (original) exist in the output dir.
3. Listen to both: the processed one should sound subtly more "recorded" but still high quality; durations should be identical.
4. Confirm subtitles still sync (timing is unchanged).
5. Publish and observe strike behavior over time. If strikes persist, set `ATR_VOICE_DEFINGERPRINT_LEVEL=aggressive` (no code change) and re-test. If quality is the priority and strikes have stopped, try `light`.
