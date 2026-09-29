"""Wire-contract tests: no paid API calls in the test suite."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.elevenlabs_service import ElevenLabsService


@pytest.fixture
def requests_sent(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "elevenlabs_api_key", "test-key")

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(status_code=200, content=b"audio", headers={"request-id": "generated"})

    monkeypatch.setattr("app.services.elevenlabs_service.requests.post", post)
    return calls


def test_v4_dialogue_contract_and_context_boundaries(requests_sent):
    result = ElevenLabsService.synthesize(
        voice_id="voice", text=" Bonjour. ", model_id="eleven_v4",
        output_format="pcm_48000", seed=2**32 - 1, language_code="fr",
        voice_settings={"stability": 0.4, "similarity_boost": 0.8,
                        "speed": 1.1, "style": 0.2, "use_speaker_boost": True},
        previous_text="old " + "p" * 100, next_text="n" * 100 + " later",
    )
    call = requests_sent[0]
    assert call["url"] == "https://api.elevenlabs.io/v1/text-to-dialogue"
    assert call["params"] == {"output_format": "pcm_48000"}
    assert call["json"] == {
        "model_id": "eleven_v4",
        "inputs": [{"voice_id": "voice", "text": "Bonjour."}],
        "settings": {"stability": 0.4, "similarity": 0.8},
        "previous_text": "p" * 100, "future_text": "n" * 100,
        "seed": 2**32 - 1, "language_code": "fr",
    }
    assert result.audio_bytes == b"audio" and result.request_id == "generated"


def test_v4_audio_context_preferred_for_regenerated_middle_block(requests_sent):
    ElevenLabsService.synthesize(
        voice_id="voice", text="Middle.", model_id="eleven_v4", seed=0,
        previous_request_ids=[" first ", "second", "nearest"],
        next_request_ids=["next", "last"], previous_text="Before.", next_text="After.",
    )
    assert requests_sent[0]["json"] == {
        "model_id": "eleven_v4",
        "inputs": [{"voice_id": "voice", "text": "Middle."}],
        "settings": {"stability": 0.5, "similarity": 0.75},
        "previous_request_ids": ["first", "second", "nearest"],
        "next_request_ids": ["next", "last"], "seed": 0,
    }


@pytest.mark.parametrize("model", ["eleven_multilingual_v2", "eleven_v3"])
def test_legacy_models_keep_tts_endpoint_and_settings(requests_sent, model):
    voice_settings = {"stability": 0.5, "speed": 1.05}
    ElevenLabsService.synthesize(
        voice_id="voice", text="Hello.", model_id=model, voice_settings=voice_settings,
        seed=123, previous_request_ids=["before"], next_text="After.",
    )
    call = requests_sent[0]
    assert call["url"].endswith("/text-to-speech/voice")
    expected = {"text": "Hello.", "model_id": model, "voice_settings": voice_settings, "seed": 123}
    if model == "eleven_multilingual_v2":
        expected.update(previous_request_ids=["before"], next_text="After.")
    assert call["json"] == expected


@pytest.mark.parametrize("seed", [-1, 2**32, True, 0.5, "123"])
def test_invalid_seed_fails_before_network(requests_sent, seed):
    with pytest.raises(ValueError, match="seed must be an integer"):
        ElevenLabsService.synthesize(voice_id="voice", text="Hello.", model_id="eleven_v4", seed=seed)
    assert not requests_sent


@pytest.mark.parametrize("side", ["previous_request_ids", "next_request_ids"])
def test_too_many_context_ids_fail_before_network(requests_sent, side):
    with pytest.raises(ValueError, match="at most 3"):
        ElevenLabsService.synthesize(
            voice_id="voice", text="Hello.", model_id="eleven_v4", **{side: ["id"] * 4},
        )
    assert not requests_sent


def test_v4_request_size_limit_includes_audio_tags(requests_sent):
    with pytest.raises(ValueError, match="2000 characters"):
        ElevenLabsService.synthesize(voice_id="voice", text="x" * 2001, model_id="eleven_v4")
    assert not requests_sent


def test_turbo_is_not_silently_sent_to_an_unsupported_endpoint(requests_sent):
    with pytest.raises(ValueError, match="WebSocket"):
        ElevenLabsService.synthesize(voice_id="voice", text="Hello.", model_id="eleven_v4_turbo")
    assert not requests_sent
