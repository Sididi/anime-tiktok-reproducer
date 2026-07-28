"""Tests for PCM sample-rate handling of ElevenLabs TTS output."""
from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.script_automation_service import ScriptAutomationService


def test_pcm_sample_rate_parsed_from_output_format(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_output_format", "pcm_48000")
    assert ScriptAutomationService._pcm_sample_rate() == 48000


def test_pcm_sample_rate_falls_back_to_44100_when_unparseable(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_output_format", "pcm")
    assert ScriptAutomationService._pcm_sample_rate() == 44100


def test_wrap_pcm_for_settings_uses_configured_sample_rate(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_output_format", "pcm_48000")
    pcm = b"\x00\x00" * 4800
    wav_bytes = ScriptAutomationService._wrap_pcm_for_settings(pcm)
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        assert wf.getframerate() == 48000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getnframes() == 4800
