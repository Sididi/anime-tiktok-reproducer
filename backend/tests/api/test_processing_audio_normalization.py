"""Tests for audio normalization in the /script processing routes."""

from __future__ import annotations

from pathlib import Path
import sys
import wave

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api.routes import processing as processing_routes


def _write_wave_file(path: Path) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(44100)
        wav_file.writeframes(b"\x00\x00" * 64)


def test_normalize_audio_file_to_wav_keeps_existing_wav_bytes(tmp_path: Path) -> None:
    source_path = tmp_path / "input.wav"
    output_path = tmp_path / "output.wav"
    _write_wave_file(source_path)

    processing_routes._normalize_audio_file_to_wav(source_path, output_path)

    assert output_path.read_bytes() == source_path.read_bytes()
    assert processing_routes._is_wave_file(output_path) is True


def test_normalize_audio_file_to_wav_transcodes_non_wav_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "input.mp3"
    output_path = tmp_path / "output.wav"
    source_path.write_bytes(b"not-a-wave-file")

    captured: dict[str, str] = {}

    class FakeSegment:
        def export(self, output_file: str, format: str) -> None:
            captured["output_file"] = output_file
            captured["format"] = format
            _write_wave_file(Path(output_file))

    class FakeAudioSegment:
        @staticmethod
        def from_file(input_file: str) -> FakeSegment:
            captured["input_file"] = input_file
            return FakeSegment()

    monkeypatch.setattr(processing_routes, "AudioSegment", FakeAudioSegment)

    processing_routes._normalize_audio_file_to_wav(source_path, output_path)

    assert captured == {
        "input_file": str(source_path),
        "output_file": str(output_path),
        "format": "wav",
    }
    assert processing_routes._is_wave_file(output_path) is True
