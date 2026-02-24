from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from ..config import settings


@dataclass(frozen=True)
class VoiceEntry:
    key: str
    display_name: str
    elevenlabs_voice_id: str
    voice_settings: dict[str, Any]


@dataclass(frozen=True)
class VoiceConfig:
    default_voice_key: str
    voices: dict[str, VoiceEntry]


class VoiceConfigService:
    """Loads / validates voice configuration from YAML."""

    _lock = Lock()
    _cached: VoiceConfig | None = None

    @classmethod
    def _path(cls) -> Path:
        return settings.voices_config_path

    @classmethod
    def _parse_voice_settings(cls, voice_key: str, raw_value: Any) -> dict[str, Any]:
        if raw_value is None:
            return {}
        if not isinstance(raw_value, dict):
            raise ValueError(f"Voice '{voice_key}' voice_settings must be a mapping")

        settings_payload: dict[str, Any] = {}

        def _parse_number(
            *,
            field_name: str,
            minimum: float | None = None,
            maximum: float | None = None,
        ) -> None:
            value = raw_value.get(field_name)
            if value is None:
                return
            if not isinstance(value, (int, float)):
                raise ValueError(f"Voice '{voice_key}' voice_settings.{field_name} must be numeric")
            number = float(value)
            if minimum is not None and number < minimum:
                raise ValueError(
                    f"Voice '{voice_key}' voice_settings.{field_name} must be >= {minimum}"
                )
            if maximum is not None and number > maximum:
                raise ValueError(
                    f"Voice '{voice_key}' voice_settings.{field_name} must be <= {maximum}"
                )
            settings_payload[field_name] = number

        _parse_number(field_name="stability", minimum=0.0, maximum=1.0)
        _parse_number(field_name="similarity_boost", minimum=0.0, maximum=1.0)
        _parse_number(field_name="style", minimum=0.0, maximum=1.0)
        _parse_number(field_name="speed", minimum=0.7, maximum=1.2)

        speaker_boost = raw_value.get("use_speaker_boost")
        if speaker_boost is not None:
            if not isinstance(speaker_boost, bool):
                raise ValueError(
                    f"Voice '{voice_key}' voice_settings.use_speaker_boost must be boolean"
                )
            settings_payload["use_speaker_boost"] = speaker_boost

        extra_fields = set(raw_value.keys()) - {
            "stability",
            "similarity_boost",
            "style",
            "speed",
            "use_speaker_boost",
        }
        if extra_fields:
            allowed = ", ".join(sorted(extra_fields))
            raise ValueError(f"Voice '{voice_key}' has unsupported voice_settings keys: {allowed}")

        return settings_payload

    @classmethod
    def _parse(cls, raw: dict[str, Any]) -> VoiceConfig:
        if not isinstance(raw, dict):
            raise ValueError("Voice config root must be a mapping")

        default_key = raw.get("default_voice_key")
        if not isinstance(default_key, str) or not default_key.strip():
            raise ValueError("Voice config must define a non-empty default_voice_key")

        voices_raw = raw.get("voices")
        if not isinstance(voices_raw, dict) or not voices_raw:
            raise ValueError("Voice config must define a non-empty voices mapping")

        voices: dict[str, VoiceEntry] = {}
        for key, value in voices_raw.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Voice keys must be non-empty strings")
            if not isinstance(value, dict):
                raise ValueError(f"Voice entry '{key}' must be a mapping")

            display_name = value.get("display_name")
            voice_id = value.get("elevenlabs_voice_id")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ValueError(f"Voice '{key}' is missing display_name")
            if not isinstance(voice_id, str) or not voice_id.strip():
                raise ValueError(f"Voice '{key}' is missing elevenlabs_voice_id")
            voice_settings = cls._parse_voice_settings(
                key,
                value.get("voice_settings"),
            )

            normalized_key = key.strip()
            voices[normalized_key] = VoiceEntry(
                key=normalized_key,
                display_name=display_name.strip(),
                elevenlabs_voice_id=voice_id.strip(),
                voice_settings=voice_settings,
            )

        default_key = default_key.strip()
        if default_key not in voices:
            raise ValueError(
                f"default_voice_key '{default_key}' does not exist in voices entries"
            )

        return VoiceConfig(default_voice_key=default_key, voices=voices)

    @classmethod
    def _load_from_disk(cls) -> VoiceConfig:
        path = cls._path()
        if not path.exists():
            raise ValueError(
                f"Voice config file not found: {path}. "
                "Create config/voices/config.yaml from config.example.yaml"
            )
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Failed to parse voice config YAML: {exc}") from exc
        return cls._parse(raw)

    @classmethod
    def get_config(cls, *, force_reload: bool = False) -> VoiceConfig:
        with cls._lock:
            if force_reload or cls._cached is None:
                cls._cached = cls._load_from_disk()
            return cls._cached

    @classmethod
    def list_voices(cls) -> list[VoiceEntry]:
        config = cls.get_config()
        return list(config.voices.values())

    @classmethod
    def get_voice(cls, voice_key: str) -> VoiceEntry:
        config = cls.get_config()
        key = voice_key.strip()
        voice = config.voices.get(key)
        if voice is None:
            raise ValueError(f"Unknown voice key '{voice_key}'")
        return voice
