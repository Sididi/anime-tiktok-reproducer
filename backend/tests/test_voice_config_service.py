"""Validation of per-voice delivery tag overrides."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.voice_config_service import VoiceConfigService


def parse_voice(**overrides):
    return VoiceConfigService._parse({
        "default_voice_key": "test",
        "voices": {"test": {
            "display_name": "Test", "elevenlabs_voice_id": "voice", **overrides,
        }},
    }).voices["test"]


def test_omitted_tags_keep_model_defaults():
    assert parse_voice().audio_control_tags is None


@pytest.mark.parametrize("raw, expected", [
    (None, None),
    ([], ()),
    ([" TikTok narrator "], ("TikTok narrator",)),
    (["TikTok narrator", "rapid"], ("TikTok narrator", "rapid")),
])
def test_audio_control_tags_override(raw, expected):
    assert parse_voice(audio_control_tags=raw).audio_control_tags == expected


@pytest.mark.parametrize("raw", ["TikTok narrator", {}, False, [None], [1], [" "], ["[rapid]"]])
def test_invalid_audio_control_tags_are_rejected(raw):
    with pytest.raises(ValueError, match="audio_control_tags"):
        parse_voice(audio_control_tags=raw)
