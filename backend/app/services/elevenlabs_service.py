from __future__ import annotations

from typing import Any

import requests

from ..config import settings


class ElevenLabsService:
    """ElevenLabs API helper."""

    _BASE_URL = "https://api.elevenlabs.io/v1"

    @classmethod
    def is_configured(cls) -> bool:
        return bool((settings.elevenlabs_api_key or "").strip())

    @classmethod
    def _headers(cls, *, accept: str = "application/json") -> dict[str, str]:
        api_key = (settings.elevenlabs_api_key or "").strip()
        if not api_key:
            raise RuntimeError("ElevenLabs API key is missing (ATR_ELEVENLABS_API_KEY)")
        return {
            "xi-api-key": api_key,
            "Accept": accept,
            "Content-Type": "application/json",
        }

    @classmethod
    def list_models(cls) -> list[dict[str, Any]]:
        response = requests.get(
            f"{cls._BASE_URL}/models",
            headers=cls._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ElevenLabs models list failed: {response.text}")
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        raise RuntimeError("Unexpected ElevenLabs models response")

    @classmethod
    def list_voices(cls) -> list[dict[str, Any]]:
        response = requests.get(
            f"{cls._BASE_URL}/voices",
            headers=cls._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ElevenLabs voices list failed: {response.text}")
        payload = response.json()
        voices = payload.get("voices") if isinstance(payload, dict) else None
        if isinstance(voices, list):
            return [item for item in voices if isinstance(item, dict)]
        raise RuntimeError("Unexpected ElevenLabs voices response")

    @classmethod
    def get_subscription(cls) -> dict[str, Any]:
        response = requests.get(
            f"{cls._BASE_URL}/user/subscription",
            headers=cls._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ElevenLabs subscription failed: {response.text}")
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise RuntimeError("Unexpected ElevenLabs subscription response")

    @classmethod
    def synthesize(
        cls,
        *,
        voice_id: str,
        text: str,
        model_id: str | None = None,
        output_format: str | None = None,
        voice_settings: dict[str, Any] | None = None,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> bytes:
        if not voice_id.strip():
            raise ValueError("voice_id is required")

        clean_text = text.strip()
        if not clean_text:
            raise ValueError("TTS text cannot be empty")

        selected_model = (model_id or settings.elevenlabs_model_id).strip()
        selected_format = (output_format or settings.elevenlabs_output_format).strip()
        accept_header = "audio/mpeg" if selected_format.lower().startswith("mp3") else "*/*"

        body: dict[str, Any] = {
            "text": clean_text,
            "model_id": selected_model,
            "voice_settings": voice_settings
            or {
                "stability": 0.45,
                "similarity_boost": 0.8,
                "style": 0.0,
                "speed": 1.0,
                "use_speaker_boost": True,
            },
        }
        if previous_text:
            body["previous_text"] = previous_text
        if next_text:
            body["next_text"] = next_text

        response = requests.post(
            f"{cls._BASE_URL}/text-to-speech/{voice_id}",
            params={"output_format": selected_format},
            headers=cls._headers(accept=accept_header),
            json=body,
            timeout=120,
        )

        if response.status_code >= 400:
            detail = response.text
            try:
                parsed = response.json()
                detail = parsed.get("detail", {}).get("message", detail)
            except Exception:
                pass
            raise RuntimeError(f"ElevenLabs TTS error: {detail}")

        if not response.content:
            raise RuntimeError("ElevenLabs returned an empty audio payload")
        return response.content

    @classmethod
    def get_preview_url_map(cls) -> dict[str, str | None]:
        """Return {voice_id: preview_url} for all voices in one API call."""
        try:
            voices = cls.list_voices()
            return {v["voice_id"]: v.get("preview_url") for v in voices if "voice_id" in v}
        except Exception:
            return {}

    @classmethod
    def check_api_health(cls) -> dict[str, Any]:
        if not cls.is_configured():
            return {"status": "skipped", "detail": "ElevenLabs API key not configured"}
        try:
            subscription = cls.get_subscription()
            tier = subscription.get("tier") if isinstance(subscription, dict) else None
            return {
                "status": "ok",
                "detail": "ElevenLabs API reachable",
                "tier": tier,
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
