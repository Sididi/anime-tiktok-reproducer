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

VALID_LEVELS: tuple[str, ...] = (
    "off",
    "default",
    "light",
    "moderate",
    "aggressive",
    "nvidia",
    "nvidia_strong_hq",
)
_DURATION_TOLERANCE_S = 0.001
_NVIDIA_SAMPLE_RATE = 44100
_NVIDIA_MU_LAW_CHANNELS = 256

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
    if level == "nvidia":
        return _sample_nvidia_params(rng)
    if level == "nvidia_strong_hq":
        return _sample_nvidia_strong_hq_params(rng)

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


def _sample_nvidia_params(rng: random.Random) -> dict[str, Any]:
    base_coat = rng.choices(
        ("noise", "phase", "mulaw", "median", "pitch"),
        weights=(0.50, 0.25, 0.18, 0.05, 0.02),
        k=1,
    )[0]
    return {
        "pipeline": "nvidia_hq_af_v2",
        "base_coat": base_coat,
        "pitch_semitones": round(rng.uniform(-0.45, 0.45), 4),
        "median_kernel": 3,
        "median_mix": round(rng.uniform(0.12, 0.24), 4),
        "gaussian_sigma": round(rng.uniform(0.00035, 0.0012), 6),
        "mu_law_channels": _NVIDIA_MU_LAW_CHANNELS,
        "mu_law_mix": round(rng.uniform(0.08, 0.18), 4),
        "phase_jitter_std": round(rng.uniform(0.002, 0.006), 6),
        "phase_blend": round(rng.uniform(0.03, 0.08), 4),
        "spectral_noise_floor": round(rng.uniform(0.00004, 0.00016), 6),
        "prosody_gain_depth": round(rng.uniform(0.001, 0.0035), 6),
        "prosody_rate_hz": round(rng.uniform(0.35, 1.1), 4),
        "start_jitter_ms": rng.randint(0, 8),
        "end_jitter_ms": rng.randint(0, 8),
        "precision_enabled": False,
        "precision_l2_per_sample_eps": 0.002,
        "precision_l2_per_sample_alpha": 0.00035,
        "precision_steps": 6,
        "nmr_weight": 0.9,
        "codec_chain": rng.choice(("aac", "opus")),
        "aac_bitrate_k": 192,
        "opus_bitrate_k": 192,
    }


def _sample_nvidia_strong_hq_params(rng: random.Random) -> dict[str, Any]:
    return {
        "pipeline": "nvidia_strong_hq_v06",
        "base_coat": "phase",
        "pitch_enabled": True,
        "pitch_semitones": round(rng.uniform(0.10, 0.18), 4),
        "median_kernel": 3,
        "median_mix": round(rng.uniform(0.18, 0.22), 4),
        "gaussian_sigma": round(rng.uniform(0.0014, 0.0021), 6),
        "continuous_noise_dbfs": round(rng.uniform(-60.9, -59.8), 2),
        "mu_law_channels": _NVIDIA_MU_LAW_CHANNELS,
        "mu_law_mix": round(rng.uniform(0.20, 0.24), 4),
        "phase_jitter_std": round(rng.uniform(0.008, 0.011), 6),
        "phase_blend": round(rng.uniform(0.078, 0.095), 4),
        "spectral_noise_floor": round(rng.uniform(0.00034, 0.00046), 6),
        "prosody_gain_depth": round(rng.uniform(0.0052, 0.0068), 6),
        "prosody_rate_hz": round(rng.uniform(0.72, 0.90), 4),
        "start_jitter_ms": rng.randint(22, 28),
        "end_jitter_ms": rng.randint(24, 31),
        "lowpass_hz": rng.randint(14540, 14680),
        "saturation": True,
        "saturation_threshold": round(rng.uniform(0.970, 0.978), 4),
        "precision_enabled": False,
        "precision_l2_per_sample_eps": 0.002,
        "precision_l2_per_sample_alpha": 0.00035,
        "precision_steps": 6,
        "nmr_weight": 0.9,
        "codec_chain": "opus",
        "aac_bitrate_k": 160,
        "opus_bitrate_k": 144,
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


def _nvidia_pitch_ratio(semitones: float) -> float:
    return 2.0 ** (semitones / 12.0)


def _build_nvidia_pitch_filter(params: dict[str, Any], duration_s: float) -> str:
    chain = ["aresample=48000:resampler=soxr"]
    if params.get("pitch_enabled") or params.get("base_coat") == "pitch":
        ratio = _nvidia_pitch_ratio(float(params["pitch_semitones"]))
        chain.append(f"rubberband=pitch={ratio:.6f}:formant=preserved")
    lowpass_hz = params.get("lowpass_hz")
    if lowpass_hz is not None:
        chain.append(f"lowpass=f={int(lowpass_hz)}")
    if params.get("saturation"):
        threshold = float(params.get("saturation_threshold", 0.97))
        chain.append(f"asoftclip=type=tanh:threshold={threshold:.4f}")
    chain.extend(
        [
            f"apad",
            f"atrim=0:{duration_s:.6f}",
        ]
    )
    return f"[0:a]{','.join(chain)}[out]"


def _run_nvidia_pitch_stage(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    duration_s: float,
) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-filter_complex",
            _build_nvidia_pitch_filter(params, duration_s),
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            str(_NVIDIA_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def _nvidia_fit_samples(audio: Any, target_samples: int) -> Any:
    import numpy as np

    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    audio = np.asarray(audio, dtype=np.float64)
    if audio.shape[0] > target_samples:
        return audio[:target_samples]
    if audio.shape[0] < target_samples:
        return np.pad(audio, (0, target_samples - audio.shape[0]), mode="constant")
    return audio


def _nvidia_mu_law_roundtrip(audio: Any, channels: int) -> Any:
    import numpy as np

    mu = float(max(2, channels - 1))
    clipped = np.clip(audio, -1.0, 1.0)
    encoded = np.sign(clipped) * np.log1p(mu * np.abs(clipped)) / np.log1p(mu)
    quantized = np.round((encoded + 1.0) * 0.5 * mu)
    expanded = (quantized / mu) * 2.0 - 1.0
    decoded = np.sign(expanded) * (np.expm1(np.abs(expanded) * np.log1p(mu)) / mu)
    return np.asarray(decoded, dtype=np.float64)


def _nvidia_phase_flatness_stage(
    audio: Any,
    sr: int,
    params: dict[str, Any],
    rng: Any,
) -> Any:
    import numpy as np
    from scipy import signal

    if audio.shape[0] < 512:
        return audio

    nperseg = min(2048, max(512, 2 ** int(np.floor(np.log2(audio.shape[0])))))
    noverlap = min(nperseg - 1, int(nperseg * 0.75))
    freqs, _, spectrum = signal.stft(
        audio,
        fs=sr,
        nperseg=nperseg,
        noverlap=noverlap,
        boundary="zeros",
    )
    mask = (freqs >= 2000.0) & (freqs <= min(8000.0, sr * 0.48))
    if not np.any(mask):
        return audio

    band = spectrum[mask]
    phase_noise = rng.normal(0.0, params["phase_jitter_std"], size=band.shape)
    band = band * np.exp(1j * phase_noise)

    median_mag = float(np.median(np.abs(band))) if band.size else 0.0
    noise_floor = max(params["spectral_noise_floor"], median_mag * 0.06)
    random_phase = rng.uniform(-np.pi, np.pi, size=band.shape)
    frame_shape = rng.uniform(0.55, 1.45, size=band.shape)
    band = band + noise_floor * frame_shape * np.exp(1j * random_phase)
    spectrum[mask] = band

    _, restored = signal.istft(
        spectrum,
        fs=sr,
        nperseg=nperseg,
        noverlap=noverlap,
        input_onesided=True,
    )
    restored = _nvidia_fit_samples(restored, audio.shape[0])
    blend = float(params.get("phase_blend", 0.05))
    return (1.0 - blend) * audio + blend * restored


def _nvidia_prosody_stage(audio: Any, sr: int, params: dict[str, Any], rng: Any) -> Any:
    import numpy as np

    if audio.shape[0] == 0:
        return audio

    t = np.arange(audio.shape[0], dtype=np.float64) / float(sr)
    depth = float(params["prosody_gain_depth"])
    phase = rng.uniform(0.0, 2.0 * np.pi)
    gain = 1.0 + depth * np.sin(2.0 * np.pi * params["prosody_rate_hz"] * t + phase)

    control_hop = max(1, int(sr * 0.09))
    control_count = int(np.ceil(audio.shape[0] / control_hop)) + 1
    control = rng.normal(0.0, depth * 0.45, control_count)
    control_x = np.arange(control_count) * control_hop
    gain += np.interp(np.arange(audio.shape[0]), control_x, control)

    return audio * np.clip(gain, 0.94, 1.06)


def _nvidia_edge_jitter(audio: Any, sr: int, params: dict[str, Any], rng: Any) -> Any:
    import numpy as np

    result = audio.copy()
    for edge, ms in (("start", params["start_jitter_ms"]), ("end", params["end_jitter_ms"])):
        samples = min(result.shape[0], int(round(sr * float(ms) / 1000.0)))
        if samples <= 0:
            continue
        if edge == "start":
            fade = np.linspace(0.0, 1.0, samples, dtype=np.float64)
            shaped = result[:samples] * fade
            shaped += rng.normal(0.0, 1.0e-5, samples) * (1.0 - fade)
            result[:samples] = shaped
        else:
            fade = np.linspace(1.0, 0.0, samples, dtype=np.float64)
            shaped = result[-samples:] * fade
            shaped += rng.normal(0.0, 1.0e-5, samples) * (1.0 - fade)
            result[-samples:] = shaped
    return result


def _nvidia_rms(audio: Any) -> float:
    import numpy as np

    return float(np.sqrt(np.mean(np.square(audio)))) if audio.shape[0] else 0.0


def _nvidia_match_rms(processed: Any, original: Any) -> Any:
    import numpy as np

    original_rms = _nvidia_rms(original)
    processed_rms = _nvidia_rms(processed)
    if original_rms > 1e-9 and processed_rms > 1e-9:
        processed = processed * np.clip(original_rms / processed_rms, 0.35, 2.85)
    return np.clip(processed, -0.98, 0.98)


def _run_nvidia_waveform_stage(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    duration_s: float,
    *,
    seed: int,
) -> None:
    import numpy as np
    import soundfile as sf
    from scipy import signal

    audio, sr = sf.read(str(input_path), always_2d=True, dtype="float64")
    target_samples = int(round(duration_s * sr))
    audio = _nvidia_fit_samples(audio, target_samples)
    original = audio.copy()

    rng = np.random.default_rng(seed ^ 0x5EEDC0DE)
    base_coat = params.get("base_coat")
    if base_coat == "median":
        kernel = int(params["median_kernel"])
        if kernel > 1 and audio.shape[0] >= kernel:
            filtered = signal.medfilt(audio, kernel_size=kernel)
            mix = float(params["median_mix"])
            audio = (1.0 - mix) * audio + mix * filtered
    elif base_coat == "noise":
        audio = audio + rng.normal(0.0, params["gaussian_sigma"], audio.shape[0])
    elif base_coat == "mulaw":
        encoded = _nvidia_mu_law_roundtrip(audio, int(params["mu_law_channels"]))
        mix = float(params["mu_law_mix"])
        audio = (1.0 - mix) * audio + mix * encoded
    elif base_coat == "phase":
        audio = _nvidia_phase_flatness_stage(audio, sr, params, rng)

    continuous_noise_dbfs = params.get("continuous_noise_dbfs")
    if continuous_noise_dbfs is not None:
        audio = audio + rng.normal(
            0.0,
            _dbfs_to_amplitude(float(continuous_noise_dbfs)),
            audio.shape[0],
        )

    audio = _nvidia_prosody_stage(audio, sr, params, rng)
    audio = _nvidia_edge_jitter(audio, sr, params, rng)
    audio = _nvidia_fit_samples(audio, target_samples)
    audio = _nvidia_match_rms(audio, original)

    sf.write(str(output_path), audio, sr, subtype="PCM_16")


def _run_nvidia_precision_coat(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    duration_s: float,
) -> None:
    try:
        import numpy as np
        import soundfile as sf
        import torch
    except Exception as exc:
        logger.warning("Nvidia precision coat skipped; dependencies unavailable: %s", exc)
        params["precision_status"] = "skipped_missing_dependency"
        shutil.copyfile(input_path, output_path)
        return

    try:
        audio, sr = sf.read(str(input_path), always_2d=True, dtype="float64")
        target_samples = int(round(duration_s * sr))
        audio = _nvidia_fit_samples(audio, target_samples)

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        params["precision_device"] = device.type
        x = torch.as_tensor(audio, dtype=torch.float32, device=device)
        delta = torch.zeros_like(x)

        n_fft = min(2048, max(512, 2 ** int(np.floor(np.log2(max(512, audio.shape[0]))))))
        hop = max(128, n_fft // 4)
        window = torch.hann_window(n_fft, device=device)
        freqs = torch.fft.rfftfreq(n_fft, d=1.0 / float(sr)).to(device)
        mask = (freqs >= 2000.0) & (freqs <= min(8000.0, sr * 0.48))
        if int(mask.sum().item()) == 0:
            params["precision_status"] = "skipped_empty_band"
            shutil.copyfile(input_path, output_path)
            return

        eps = float(params["precision_l2_per_sample_eps"]) * float(x.numel() ** 0.5)
        alpha = float(params["precision_l2_per_sample_alpha"]) * float(x.numel() ** 0.5)
        nmr_weight = float(params["nmr_weight"])
        source_rms = torch.sqrt(torch.mean(torch.square(x))).detach().clamp_min(1.0e-8)

        for _ in range(int(params["precision_steps"])):
            x_adv = torch.clamp(x + delta, -0.98, 0.98).detach().requires_grad_(True)
            spectrum = torch.stft(
                x_adv,
                n_fft=n_fft,
                hop_length=hop,
                win_length=n_fft,
                window=window,
                return_complex=True,
                center=True,
            )
            band = spectrum[mask]
            magnitude = torch.abs(band).clamp_min(1.0e-7)
            flatness = torch.exp(torch.mean(torch.log(magnitude), dim=0))
            flatness = flatness / torch.mean(magnitude, dim=0).clamp_min(1.0e-7)
            loss_flatness = -torch.mean(flatness)

            if band.shape[1] > 1:
                phase = torch.angle(band)
                loss_phase = torch.mean(torch.cos(phase[:, 1:] - phase[:, :-1]))
            else:
                loss_phase = torch.zeros((), dtype=torch.float32, device=device)

            candidate_delta = x_adv - x
            perturb_rms = torch.sqrt(torch.mean(torch.square(candidate_delta)))
            loss_nmr = torch.relu((perturb_rms / source_rms) - 0.08) ** 2
            adv_rms = torch.sqrt(torch.mean(torch.square(x_adv))).clamp_min(1.0e-8)
            loss_loudness = torch.square((adv_rms - source_rms) / source_rms)

            total = (
                loss_flatness
                + 0.35 * loss_phase
                + nmr_weight * loss_nmr
                + 0.25 * loss_loudness
            )
            grad = torch.autograd.grad(total, x_adv)[0]
            grad_norm = torch.linalg.vector_norm(grad).clamp_min(1.0e-8)
            delta = (x_adv.detach() - alpha * grad / grad_norm) - x
            delta_norm = torch.linalg.vector_norm(delta)
            if float(delta_norm.detach().cpu()) > eps:
                delta = delta * (eps / delta_norm)

        processed = torch.clamp(x + delta, -0.98, 0.98).detach().cpu().numpy()
        processed = _nvidia_fit_samples(processed, target_samples)
        processed = _nvidia_match_rms(processed, audio)
        processed = np.nan_to_num(processed, nan=0.0, posinf=0.98, neginf=-0.98)

        source_p95 = float(np.percentile(np.abs(audio), 95)) if audio.size else 0.0
        processed_p95 = (
            float(np.percentile(np.abs(processed), 95)) if processed.size else 0.0
        )
        source_rms_np = _nvidia_rms(audio)
        processed_rms_np = _nvidia_rms(processed)
        p95_ratio = processed_p95 / max(source_p95, 1.0e-8)
        rms_ratio = processed_rms_np / max(source_rms_np, 1.0e-8)
        if not (
            np.isfinite(p95_ratio)
            and np.isfinite(rms_ratio)
            and 0.35 <= p95_ratio <= 2.85
            and 0.5 <= rms_ratio <= 2.0
        ):
            logger.warning(
                "Nvidia precision coat skipped by quality guard: "
                "p95_ratio=%.3f rms_ratio=%.3f",
                p95_ratio,
                rms_ratio,
            )
            params["precision_status"] = "skipped_quality_guard"
            shutil.copyfile(input_path, output_path)
            return

        sf.write(str(output_path), processed, sr, subtype="PCM_16")
        params["precision_status"] = "applied"
    except Exception as exc:
        logger.warning("Nvidia precision coat skipped after failure: %s", exc)
        params["precision_status"] = "skipped_error"
        shutil.copyfile(input_path, output_path)


def _run_nvidia_platform_roundtrip(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    duration_s: float,
    *,
    workdir: Path,
) -> None:
    codec_chain = params.get("codec_chain", "aac")
    encoded = workdir / f"nvidia_codec.{ 'opus' if codec_chain == 'opus' else 'm4a' }"
    if codec_chain == "opus":
        encode_args = [
            "-i",
            str(input_path),
            "-c:a",
            "libopus",
            "-b:a",
            f"{params['opus_bitrate_k']}k",
            "-application",
            "audio",
            str(encoded),
        ]
    else:
        encode_args = [
            "-i",
            str(input_path),
            "-ar",
            "44100",
            "-c:a",
            "aac",
            "-b:a",
            f"{params['aac_bitrate_k']}k",
            str(encoded),
        ]
    _run_ffmpeg(encode_args)
    _run_ffmpeg(
        [
            "-i",
            str(encoded),
            "-af",
            f"apad,atrim=0:{duration_s:.6f}",
            "-ac",
            "1",
            "-ar",
            str(_NVIDIA_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def _nvidia_resample_like(audio: Any, source_sr: int, target_sr: int) -> Any:
    if source_sr == target_sr:
        return audio

    from math import gcd

    from scipy import signal

    factor = gcd(source_sr, target_sr)
    return signal.resample_poly(audio, target_sr // factor, source_sr // factor)


def _run_nvidia_final_quality_pass(
    original_path: Path,
    input_path: Path,
    output_path: Path,
    duration_s: float,
) -> None:
    import numpy as np
    import soundfile as sf

    original, original_sr = sf.read(str(original_path), always_2d=True, dtype="float64")
    processed, processed_sr = sf.read(str(input_path), always_2d=True, dtype="float64")

    original = _nvidia_fit_samples(original, int(round(duration_s * original_sr)))
    processed = _nvidia_fit_samples(processed, int(round(duration_s * processed_sr)))
    original = _nvidia_resample_like(original, original_sr, processed_sr)
    original = _nvidia_fit_samples(original, processed.shape[0])

    source_p95 = float(np.percentile(np.abs(original), 95)) if original.size else 0.0
    processed_p95 = (
        float(np.percentile(np.abs(processed), 95)) if processed.size else 0.0
    )
    if source_p95 > 1.0e-8 and processed_p95 > 1.0e-8:
        processed = processed * np.clip(source_p95 / processed_p95, 0.5, 2.0)

    source_rms = _nvidia_rms(original)
    processed_rms = _nvidia_rms(processed)
    if source_rms > 1.0e-8 and processed_rms > 1.0e-8:
        processed = processed * np.clip(source_rms / processed_rms, 0.65, 1.55)

    processed = np.nan_to_num(processed, nan=0.0, posinf=0.98, neginf=-0.98)
    processed = np.clip(processed, -0.98, 0.98)

    final_p95 = float(np.percentile(np.abs(processed), 95)) if processed.size else 0.0
    final_rms = _nvidia_rms(processed)
    p95_ratio = final_p95 / max(source_p95, 1.0e-8)
    rms_ratio = final_rms / max(source_rms, 1.0e-8)
    if not (
        np.isfinite(p95_ratio)
        and np.isfinite(rms_ratio)
        and 0.45 <= p95_ratio <= 2.2
        and 0.55 <= rms_ratio <= 1.9
    ):
        raise RuntimeError(
            "nvidia final quality guard failed: "
            f"p95_ratio={p95_ratio:.3f} rms_ratio={rms_ratio:.3f}"
        )

    sf.write(str(output_path), processed, processed_sr, subtype="PCM_16")


def _run_nvidia_pipeline(
    input_path: Path,
    output_path: Path,
    params: dict[str, Any],
    duration_s: float,
    *,
    seed: int,
    workdir: Path,
) -> None:
    pitched = workdir / "nvidia_pitch.wav"
    statistical = workdir / "nvidia_statistical.wav"
    precision = workdir / "nvidia_precision.wav"
    transcoded = workdir / "nvidia_transcoded.wav"
    restored = workdir / "nvidia_restored.wav"

    _run_nvidia_pitch_stage(input_path, pitched, params, duration_s)
    _run_nvidia_waveform_stage(pitched, statistical, params, duration_s, seed=seed)
    if params.get("precision_enabled"):
        _run_nvidia_precision_coat(statistical, precision, params, duration_s)
        codec_input = precision
    else:
        params["precision_status"] = "disabled_quality"
        codec_input = statistical
    _run_nvidia_platform_roundtrip(
        codec_input,
        transcoded,
        params,
        duration_s,
        workdir=workdir,
    )
    _run_nvidia_final_quality_pass(input_path, transcoded, restored, duration_s)

    out_duration = _wav_duration(restored)
    if abs(out_duration - duration_s) > _DURATION_TOLERANCE_S:
        raise RuntimeError(
            f"duration drift: {out_duration:.6f}s vs {duration_s:.6f}s"
        )
    shutil.copyfile(restored, output_path)


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
                if level in {"nvidia", "nvidia_strong_hq"}:
                    _run_nvidia_pipeline(
                        input_path,
                        output_path,
                        params,
                        src_duration,
                        seed=seed,
                        workdir=workdir,
                    )
                    logger.info(
                        "Voice de-fingerprint applied: level=%s seed=%s params=%s",
                        level,
                        seed,
                        params,
                    )
                    return {
                        "applied": True,
                        "level": level,
                        "seed": seed,
                        "params": params,
                    }

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
