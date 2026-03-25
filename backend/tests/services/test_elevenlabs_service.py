from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.config import settings
from app.services.elevenlabs_service import ElevenLabsService


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"audio",
        headers: dict[str, str] | None = None,
        payload: Any | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("No JSON payload")
        return self._payload


def test_synthesize_uses_request_stitching_seed_and_returns_request_id(monkeypatch: pytest.MonkeyPatch):
    signature = inspect.signature(ElevenLabsService.synthesize)
    assert "previous_request_ids" in signature.parameters
    assert "seed" in signature.parameters
    assert "previous_text" not in signature.parameters
    assert "next_text" not in signature.parameters

    captured: dict[str, Any] = {}

    def fake_post(url, *, params, headers, json, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(
            content=b"fake-audio",
            headers={"request-id": "req-123"},
        )

    monkeypatch.setattr(settings, "elevenlabs_api_key", "api-key")
    monkeypatch.setattr(settings, "elevenlabs_model_id", "eleven_multilingual_v2")
    monkeypatch.setattr(settings, "elevenlabs_output_format", "mp3_44100_128")
    monkeypatch.setattr("app.services.elevenlabs_service.requests.post", fake_post)

    result = ElevenLabsService.synthesize(
        voice_id="voice-id",
        text="Bonjour",
        previous_request_ids=["req-000"],
        seed=4242,
    )

    assert captured["params"] == {"output_format": "mp3_44100_128"}
    assert captured["json"]["text"] == "Bonjour"
    assert captured["json"]["model_id"] == "eleven_multilingual_v2"
    assert captured["json"]["previous_request_ids"] == ["req-000"]
    assert captured["json"]["seed"] == 4242
    assert "previous_text" not in captured["json"]
    assert "next_text" not in captured["json"]
    assert getattr(result, "audio_bytes", None) == b"fake-audio"
    assert getattr(result, "request_id", None) == "req-123"


def test_synthesize_rejects_blank_voice_id():
    with pytest.raises(ValueError, match="voice_id is required"):
        ElevenLabsService.synthesize(voice_id=" ", text="Bonjour")


def test_synthesize_rejects_blank_text():
    with pytest.raises(ValueError, match="TTS text cannot be empty"):
        ElevenLabsService.synthesize(voice_id="voice-id", text=" ")
