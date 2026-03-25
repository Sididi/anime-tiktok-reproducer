from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Transcription
from .tts_text_normalizer import TtsTextNormalizer


@dataclass(frozen=True)
class NormalizedScriptPayload:
    public_payload: dict[str, Any]
    internal_payload: dict[str, Any]
    language: str


class ScriptPayloadService:
    """Validate and enrich /script JSON against the project's transcription."""

    @classmethod
    def resolve_language(
        cls,
        *,
        payload: dict[str, Any],
        target_language: str | None = None,
    ) -> str:
        candidate = (target_language or "").strip().lower()
        if not candidate:
            payload_language = payload.get("language")
            if isinstance(payload_language, str):
                candidate = payload_language.strip().lower()
        if not candidate:
            candidate = "fr"
        return TtsTextNormalizer.resolve_language(candidate)

    @classmethod
    def normalize(
        cls,
        *,
        payload: dict[str, Any],
        transcription: Transcription,
        target_language: str | None = None,
    ) -> NormalizedScriptPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Script JSON root must be an object")

        scenes = payload.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise RuntimeError("Script JSON must contain a non-empty 'scenes' array")

        expected_scenes = transcription.scenes
        if len(scenes) != len(expected_scenes):
            raise RuntimeError(
                f"Script scene count mismatch: expected {len(expected_scenes)}, got {len(scenes)}"
            )

        normalized_language = cls.resolve_language(
            payload=payload,
            target_language=target_language,
        )

        public_scenes: list[dict[str, Any]] = []
        internal_scenes: list[dict[str, Any]] = []

        for idx, expected_scene in enumerate(expected_scenes):
            item = scenes[idx]
            if not isinstance(item, dict):
                raise RuntimeError(f"Scene at position {idx} is not an object")

            scene_index = item.get("scene_index")
            if not isinstance(scene_index, int):
                raise RuntimeError(
                    f"Scene at position {idx} must contain a numeric 'scene_index'"
                )
            if scene_index != expected_scene.scene_index:
                raise RuntimeError(
                    "Script scene_index mismatch at position "
                    f"{idx}: expected {expected_scene.scene_index}, got {scene_index}"
                )

            raw_text = item.get("text")
            if not isinstance(raw_text, str):
                raise RuntimeError(
                    f"Scene {expected_scene.scene_index} must contain a 'text' string"
                )

            normalized_text = raw_text.strip()

            public_scene = {
                "scene_index": expected_scene.scene_index,
                "text": normalized_text,
            }
            public_scenes.append(public_scene)
            internal_scenes.append(
                {
                    **public_scene,
                    "start_time": expected_scene.start_time,
                    "end_time": expected_scene.end_time,
                }
            )

        public_payload = {
            "language": normalized_language,
            "scenes": public_scenes,
        }
        internal_payload = {
            "language": normalized_language,
            "scenes": internal_scenes,
        }
        return NormalizedScriptPayload(
            public_payload=public_payload,
            internal_payload=internal_payload,
            language=normalized_language,
        )
