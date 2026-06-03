"""Voice de-fingerprinting for published TTS audio."""
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

from ..utils.media_binaries import get_media_subprocess_env, rewrite_media_command

logger = logging.getLogger("uvicorn.error")

VALID_LEVELS: tuple[str, ...] = ("off", "default", "light", "moderate", "aggressive")
_DURATION_TOLERANCE_S = 0.001

_GEEKNIK_QUALITY_BANDS: tuple[tuple[float, float, float], ...] = (
    (15000.0, 17000.0, 0.28),  # ElevenLabs-like upper spectral watermark region.
    (18000.0, 19000.0, 0.45),
    (19500.0, 20000.0, 0.65),
    (12000.0, 12500.0, 0.08),
)


@dataclass(frozen=True)
class _PresetBounds:
    noise_dbfs: tuple[float, float]
    pitch_cents: tuple[float, float]
    lowpass_hz: tuple[int, int]
    formant_shift: bool
    reverb_delay_ms: tuple[int, int] | None
    reverb_decay: tuple[float, float] | None
    saturation: bool
    lossy_bitrate_k: int
    lossy_passes: int


_PRESETS: dict[str, _PresetBounds] = {
    "light": _PresetBounds(
        (-60.0, -56.0),
        (-5.0, 5.0),
        (17500, 18500),
        False,
        None,
        None,
        False,
        192,
        1,
    ),
    "default": _PresetBounds(
        (-62.0, -58.0),
        (-4.0, 4.0),
        (18500, 19500),
        False,
        None,
        None,
        False,
        224,
        1,
    ),
    "moderate": _PresetBounds(
        (-48.0, -44.0),
        (-30.0, 30.0),
        (15500, 16500),
        False,
        (18, 18),
        (0.18, 0.18),
        False,
        128,
        1,
    ),
    "aggressive": _PresetBounds(
        (-40.0, -36.0),
        (-50.0, 50.0),
        (13500, 14500),
        True,
        (18, 18),
        (0.18, 0.18),
        True,
        96,
        2,
    ),
}


def normalize_level(level: str | None) -> str:
    candidate = (level or "").strip().lower()
    if candidate in VALID_LEVELS:
        return candidate
    logger.warning(
        "Unknown voice de-fingerprint level %r; falling back to 'default'",
        level,
    )
    return "default"


def _sample_params(level: str, rng: random.Random) -> dict[str, Any]:
    bounds = _PRESETS[level]
    reverb = bounds.reverb_delay_ms is not None and bounds.reverb_decay is not None
    return {
        "noise_dbfs": round(rng.uniform(*bounds.noise_dbfs), 2),
        "pitch_cents": round(rng.uniform(*bounds.pitch_cents), 2),
        "lowpass_hz": rng.randint(*bounds.lowpass_hz),
        "formant_shift": bounds.formant_shift,
        "reverb": reverb,
        "reverb_delay_ms": rng.randint(*bounds.reverb_delay_ms) if reverb else None,
        "reverb_decay": round(rng.uniform(*bounds.reverb_decay), 3) if reverb else None,
        "saturation": bounds.saturation,
        "lossy_bitrate_k": bounds.lossy_bitrate_k,
        "lossy_passes": bounds.lossy_passes,
        "geeknik_first_pass": level == "default",
    }


def _cents_to_ratio(cents: float) -> float:
    return 2.0 ** (cents / 1200.0)


def _dbfs_to_amplitude(dbfs: float) -> float:
    return 10.0 ** (dbfs / 20.0)


def _build_filter_complex(params: dict[str, Any], duration_s: float) -> str:
    chain = [
        "aresample=48000:resampler=soxr",
        "aresample=44100:resampler=soxr",
    ]
    ratio = _cents_to_ratio(params["pitch_cents"])
    formant = "shifted" if params["formant_shift"] else "preserved"
    chain.append(f"rubberband=pitch={ratio:.6f}:formant={formant}")
    chain.append(f"lowpass=f={params['lowpass_hz']}")
    if params["reverb"]:
        chain.append(
            f"aecho=0.8:0.85:{params['reverb_delay_ms']}:{params['reverb_decay']}"
        )
    if params["saturation"]:
        chain.append("asoftclip=type=tanh:threshold=0.9")

    noise_gain = _dbfs_to_amplitude(params["noise_dbfs"])
    voice_chain = ",".join(chain)
    return (
        f"[0:a]{voice_chain}[v];"
        f"[1:a]volume={noise_gain:.6f}[n];"
        f"[v][n]amix=inputs=2:weights=1 1:duration=first:normalize=0[mix];"
        f"[mix]loudnorm=I=-16:TP=-1.5:LRA=11,apad,atrim=0:{duration_s:.6f}[out]"
    )


def _wav_duration(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    if rate <= 0:
        raise RuntimeError(f"Invalid sample rate in {path}")
    return frames / float(rate)


def _trim_or_pad_audio(audio: Any, target_samples: int) -> Any:
    import numpy as np

    if audio.shape[0] > target_samples:
        return audio[:target_samples]
    if audio.shape[0] < target_samples:
        pad_width = ((0, target_samples - audio.shape[0]), (0, 0))
        return np.pad(audio, pad_width, mode="constant")
    return audio


def _rms(audio: Any) -> float:
    import numpy as np

    return float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0


def _blend_rms_to_original(processed: Any, original: Any) -> Any:
    import numpy as np

    original_rms = _rms(original)
    processed_rms = _rms(processed)
    if original_rms > 1e-9 and processed_rms > 1e-9:
        processed = processed * min(4.0, original_rms / processed_rms)
    return np.clip(processed, -0.98, 0.98)


def _safe_bandstop(
    audio: Any,
    sr: int,
    low_hz: float,
    high_hz: float,
    blend: float,
) -> Any:
    from scipy import signal

    nyquist = sr / 2.0
    low_hz = max(20.0, low_hz)
    high_hz = min(nyquist * 0.98, high_hz)
    if low_hz >= high_hz or (high_hz - low_hz) < 40.0:
        return audio

    try:
        sos = signal.butter(
            2,
            [low_hz / nyquist, high_hz / nyquist],
            btype="bandstop",
            output="sos",
        )
        filtered = signal.sosfiltfilt(sos, audio)
        return (1.0 - blend) * audio + blend * filtered
    except Exception as exc:
        logger.warning(
            "Geeknik quality bandstop skipped for %.0f-%.0f Hz: %s",
            low_hz,
            high_hz,
            exc,
        )
        return audio


def _safe_highpass(audio: Any, sr: int, cutoff_hz: float) -> Any:
    from scipy import signal

    nyquist = sr / 2.0
    if cutoff_hz >= nyquist * 0.95:
        return audio
    try:
        sos = signal.butter(2, cutoff_hz / nyquist, btype="highpass", output="sos")
        return signal.sosfiltfilt(sos, audio)
    except Exception as exc:
        logger.warning("Geeknik quality highpass noise shaping skipped: %s", exc)
        return audio


def _smooth_envelope(audio: Any, sr: int) -> Any:
    import numpy as np
    from scipy import signal

    try:
        cutoff = min(25.0, sr / 8.0)
        sos = signal.butter(2, cutoff / (sr / 2.0), btype="lowpass", output="sos")
        envelope = signal.sosfiltfilt(sos, np.abs(audio))
    except Exception:
        envelope = np.abs(audio)

    max_envelope = float(np.max(envelope)) if len(envelope) else 0.0
    if max_envelope <= 1e-9:
        return np.ones_like(audio) * 0.25
    return 0.25 + 0.75 * (envelope / max_envelope)


def _geeknik_quality_channel(audio: Any, sr: int, rng: Any) -> Any:
    import numpy as np

    original = audio.copy()
    result = audio.copy()

    for low_hz, high_hz, blend in _GEEKNIK_QUALITY_BANDS:
        result = _safe_bandstop(result, sr, low_hz, high_hz, blend)

    envelope = _smooth_envelope(result, sr)
    noise = rng.normal(0.0, 1.0, len(result))
    hf_noise = _safe_highpass(noise, sr, 14000.0)
    result = result + hf_noise * envelope * 1.2e-5

    phase = rng.uniform(0.0, 2.0 * np.pi)
    cycles = rng.uniform(8.0, 14.0)
    micro_dynamics = 1.0 + 0.00035 * np.sin(
        np.linspace(0.0, cycles * np.pi, len(result)) + phase
    )
    result = result * micro_dynamics

    harmonic_amount = 0.0008
    result = result - harmonic_amount * np.sin(2.0 * np.pi * result)
    result = _blend_rms_to_original(result, original)

    return np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)


def _run_geeknik_quality_pass(
    input_path: Path,
    output_path: Path,
    *,
    seed: int,
    duration_s: float,
) -> None:
    """High-quality local port of geeknik's metadata/spectral/statistical first pass.

    The archived project uses librosa/mutagen-heavy orchestration. This keeps the
    relevant high-quality ideas locally: clean WAV rewrite, targeted upper-band
    suppression, masked high-frequency dither, micro-dynamics, and tiny harmonic
    imperfection.
    """
    import numpy as np
    import soundfile as sf

    audio, sr = sf.read(str(input_path), always_2d=True, dtype="float64")
    target_samples = int(round(duration_s * sr))
    audio = _trim_or_pad_audio(audio, target_samples)

    rng = np.random.default_rng(seed)
    processed = audio.copy()
    for channel_index in range(processed.shape[1]):
        processed[:, channel_index] = _geeknik_quality_channel(
            processed[:, channel_index],
            sr,
            rng,
        )

    processed = _trim_or_pad_audio(processed, target_samples)
    sf.write(str(output_path), processed, sr, subtype="PCM_16")


def _run_ffmpeg(args: list[str]) -> None:
    cmd = rewrite_media_command(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    )
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=get_media_subprocess_env(cmd),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:500]}")


def _run_dsp_pass(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    duration_s: float,
) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:amplitude=1:sample_rate=44100",
            "-filter_complex",
            _build_filter_complex(params, duration_s),
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
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
    _run_ffmpeg(
        ["-i", str(input_path), "-c:a", "aac", "-b:a", f"{bitrate_k}k", str(encoded)]
    )
    _run_ffmpeg(
        [
            "-i",
            str(encoded),
            "-af",
            f"apad,atrim=0:{duration_s:.6f}",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


class VoiceDefingerprintService:
    """Applies a duration-preserving ffmpeg chain to TTS WAV audio."""

    @classmethod
    def apply(
        cls,
        input_path: Path | str,
        output_path: Path | str,
        *,
        level: str,
        seed: int | None = None,
    ) -> dict[str, Any]:
        input_path = Path(input_path)
        output_path = Path(output_path)
        level = normalize_level(level)

        if level == "off":
            if input_path != output_path:
                shutil.copyfile(input_path, output_path)
            return {"applied": False, "level": "off", "seed": None, "params": None}

        if seed is None:
            seed = int.from_bytes(os.urandom(4), "big")
        params = _sample_params(level, random.Random(seed))

        try:
            src_duration = _wav_duration(input_path)
            with tempfile.TemporaryDirectory() as temp_dir:
                workdir = Path(temp_dir)
                dsp_input = input_path
                if level == "default":
                    geeknik_out = workdir / "geeknik_quality.wav"
                    _run_geeknik_quality_pass(
                        input_path,
                        geeknik_out,
                        seed=seed,
                        duration_s=src_duration,
                    )
                    dsp_input = geeknik_out

                dsp_out = workdir / "dsp.wav"
                _run_dsp_pass(dsp_input, dsp_out, params, src_duration)

                current = dsp_out
                for index in range(params["lossy_passes"]):
                    next_path = workdir / f"lossy_{index}.wav"
                    _run_lossy_roundtrip(
                        current,
                        next_path,
                        params["lossy_bitrate_k"],
                        src_duration,
                        workdir=workdir,
                        index=index,
                    )
                    current = next_path

                out_duration = _wav_duration(current)
                if abs(out_duration - src_duration) > _DURATION_TOLERANCE_S:
                    raise RuntimeError(
                        f"duration drift: {out_duration:.6f}s vs {src_duration:.6f}s"
                    )
                shutil.copyfile(current, output_path)

            logger.info(
                "Voice de-fingerprint applied: level=%s seed=%s params=%s",
                level,
                seed,
                params,
            )
            return {"applied": True, "level": level, "seed": seed, "params": params}
        except Exception as exc:
            logger.warning("Voice de-fingerprint failed (%s); keeping original audio", exc)
            if input_path != output_path:
                shutil.copyfile(input_path, output_path)
            return {
                "applied": False,
                "level": level,
                "seed": seed,
                "params": params,
                "error": str(exc),
            }
