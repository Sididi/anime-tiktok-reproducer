"""Tests for the voice de-fingerprinting service."""
from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_sine_wav(path: Path, *, seconds: float = 2.0, rate: int = 44100) -> None:
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


def test_config_defaults_to_default():
    from app.config import Settings

    settings = Settings(_env_file=None)
    assert settings.voice_defingerprint_level == "default"


def test_config_normalizes_unknown_level_to_default():
    from app.config import Settings

    settings = Settings(_env_file=None, voice_defingerprint_level="banana")
    assert settings.voice_defingerprint_level == "default"


def test_normalize_level_passes_through_valid():
    from app.services.voice_defingerprint import normalize_level

    for level in ("off", "default", "light", "moderate", "aggressive"):
        assert normalize_level(level) == level


def test_normalize_level_is_case_insensitive_and_trims():
    from app.services.voice_defingerprint import normalize_level

    assert normalize_level("  Moderate ") == "moderate"


def test_normalize_level_falls_back_to_default_on_unknown():
    from app.services.voice_defingerprint import normalize_level

    assert normalize_level("banana") == "default"
    assert normalize_level(None) == "default"


def test_sample_params_is_deterministic_for_a_seed():
    import random

    from app.services.voice_defingerprint import _sample_params

    assert _sample_params("moderate", random.Random(42)) == _sample_params(
        "moderate", random.Random(42)
    )


def test_sample_params_respects_moderate_bounds():
    import random

    from app.services.voice_defingerprint import _sample_params

    params = _sample_params("moderate", random.Random(7))
    assert -48.0 <= params["noise_dbfs"] <= -44.0
    assert -30.0 <= params["pitch_cents"] <= 30.0
    assert 15500 <= params["lowpass_hz"] <= 16500
    assert params["formant_shift"] is False
    assert params["reverb"] is True
    assert params["reverb_delay_ms"] == 18
    assert params["reverb_decay"] == 0.18
    assert params["saturation"] is False
    assert params["lossy_bitrate_k"] == 128
    assert params["lossy_passes"] == 1


def test_sample_params_respects_default_bounds_after_geeknik_pass():
    import random

    from app.services.voice_defingerprint import _sample_params

    params = _sample_params("default", random.Random(17))
    assert -62.0 <= params["noise_dbfs"] <= -58.0
    assert -4.0 <= params["pitch_cents"] <= 4.0
    assert 18500 <= params["lowpass_hz"] <= 19500
    assert params["formant_shift"] is False
    assert params["reverb"] is False
    assert params["reverb_delay_ms"] is None
    assert params["reverb_decay"] is None
    assert params["saturation"] is False
    assert params["lossy_bitrate_k"] == 224
    assert params["lossy_passes"] == 1
    assert params["geeknik_first_pass"] is True


def test_sample_params_aggressive_enables_saturation_and_double_lossy():
    import random

    from app.services.voice_defingerprint import _sample_params

    params = _sample_params("aggressive", random.Random(1))
    assert params["formant_shift"] is True
    assert params["saturation"] is True
    assert params["lossy_passes"] == 2
    assert params["lossy_bitrate_k"] == 96


def test_build_filter_complex_includes_core_filters():
    from app.services.voice_defingerprint import _build_filter_complex

    params = {
        "noise_dbfs": -46.0,
        "pitch_cents": 0.0,
        "lowpass_hz": 16000,
        "formant_shift": False,
        "reverb": True,
        "reverb_delay_ms": 18,
        "reverb_decay": 0.18,
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
    assert "aecho=0.8:0.85:18:0.18" in graph
    assert "asoftclip" not in graph
    assert "amix=inputs=2" in graph
    assert "loudnorm=" in graph
    assert "atrim=0:12.500000" in graph


def test_build_filter_complex_pitch_ratio_for_positive_cents():
    from app.services.voice_defingerprint import _build_filter_complex

    params = {
        "noise_dbfs": -46.0,
        "pitch_cents": 1200.0,
        "lowpass_hz": 16000,
        "formant_shift": True,
        "reverb": False,
        "reverb_delay_ms": None,
        "reverb_decay": None,
        "saturation": True,
        "lossy_bitrate_k": 96,
        "lossy_passes": 2,
    }
    graph = _build_filter_complex(params, duration_s=5.0)
    assert "rubberband=pitch=2.000000" in graph
    assert "formant=shifted" in graph
    assert "asoftclip" in graph
    assert "aecho=" not in graph


def test_build_filter_complex_default_has_no_reverb_after_geeknik_pass():
    from app.services.voice_defingerprint import _build_filter_complex

    params = {
        "noise_dbfs": -60.0,
        "pitch_cents": 2.0,
        "lowpass_hz": 19000,
        "formant_shift": False,
        "reverb": False,
        "reverb_delay_ms": None,
        "reverb_decay": None,
        "saturation": False,
        "lossy_bitrate_k": 224,
        "lossy_passes": 1,
        "geeknik_first_pass": True,
    }
    graph = _build_filter_complex(params, duration_s=5.0)
    assert "aecho=" not in graph
    assert "lowpass=f=19000" in graph
    assert "asoftclip" not in graph


def test_build_filter_complex_light_has_no_reverb():
    from app.services.voice_defingerprint import _build_filter_complex

    params = {
        "noise_dbfs": -58.0,
        "pitch_cents": 0.0,
        "lowpass_hz": 18000,
        "formant_shift": False,
        "reverb": False,
        "reverb_delay_ms": None,
        "reverb_decay": None,
        "saturation": False,
        "lossy_bitrate_k": 192,
        "lossy_passes": 1,
    }
    graph = _build_filter_complex(params, duration_s=5.0)
    assert "aecho=" not in graph


def test_geeknik_quality_pass_preserves_duration_and_rewrites_audio(tmp_path):
    from app.services.voice_defingerprint import _run_geeknik_quality_pass, _wav_duration

    src = tmp_path / "in.wav"
    dst = tmp_path / "geeknik.wav"
    _write_sine_wav(src, seconds=2.0)

    _run_geeknik_quality_pass(src, dst, seed=22, duration_s=_wav_duration(src))

    assert dst.exists()
    assert abs(_wav_duration(dst) - _wav_duration(src)) <= 0.001
    assert dst.read_bytes() != src.read_bytes()


def test_wav_duration_reads_seconds(tmp_path):
    from app.services.voice_defingerprint import _wav_duration

    wav = tmp_path / "tone.wav"
    _write_sine_wav(wav, seconds=1.5)
    assert abs(_wav_duration(wav) - 1.5) < 0.01


def test_run_ffmpeg_raises_on_failure():
    from app.services.voice_defingerprint import _run_ffmpeg

    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        _run_ffmpeg(["-i", "/nonexistent/does-not-exist.wav", "/tmp/never.wav"])


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
    assert dst.read_bytes() == src.read_bytes()


def test_apply_moderate_real_ffmpeg_preserves_duration(tmp_path):
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
    assert abs(out_duration - src_duration) <= 0.001
    with wave.open(str(dst), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 44100


def test_apply_default_real_ffmpeg_preserves_duration(tmp_path):
    from app.services.voice_defingerprint import VoiceDefingerprintService, _wav_duration

    src = tmp_path / "in.wav"
    dst = tmp_path / "out.wav"
    _write_sine_wav(src, seconds=3.0)
    src_duration = _wav_duration(src)

    result = VoiceDefingerprintService.apply(src, dst, level="default", seed=101)

    assert result["applied"] is True
    assert result["level"] == "default"
    assert result["seed"] == 101
    assert result["params"]["geeknik_first_pass"] is True
    assert result["params"]["reverb"] is False
    assert dst.exists()
    out_duration = _wav_duration(dst)
    assert abs(out_duration - src_duration) <= 0.001
    with wave.open(str(dst), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 44100
