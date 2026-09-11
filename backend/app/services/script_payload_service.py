from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import Transcription
from ..models.script_repair import ScriptValidationIssue
from .tts_text_normalizer import TtsTextNormalizer


@dataclass(frozen=True)
class NormalizedScriptPayload:
    public_payload: dict[str, Any]
    internal_payload: dict[str, Any]
    language: str


class ScriptPayloadService:
    """Validate and enrich /script JSON against the project's transcription."""

    @staticmethod
    def has_narration(text: str) -> bool:
        return any(character.isalnum() for character in text)

    @classmethod
    def inspect(cls, payload: Any, transcription: Transcription) -> list[ScriptValidationIssue]:
        """Collect diagnostics without relaxing the downstream normalization gate."""
        issues: list[ScriptValidationIssue] = []

        def add(code: str, message: str, index: int | None = None) -> None:
            issues.append(ScriptValidationIssue(code=code, message=message, scene_index=index))

        if not isinstance(payload, dict):
            add("invalid_structure", "Script JSON root must be an object")
            return issues
        scenes = payload.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            add("invalid_structure", "Script JSON must contain a non-empty 'scenes' array")
            return issues
        if len(scenes) != len(transcription.scenes):
            add("invalid_structure", f"Script scene count mismatch: expected {len(transcription.scenes)}, got {len(scenes)}")
        for position, (item, expected) in enumerate(zip(scenes, transcription.scenes)):
            index = expected.scene_index
            if not isinstance(item, dict):
                add("invalid_structure", f"Scene at position {position} is not an object", index)
                continue
            if type(item.get("scene_index")) is not int or item["scene_index"] != index:
                add("invalid_structure", f"Script scene_index mismatch at position {position}: expected {index}, got {item.get('scene_index')}", index)
            text = item.get("text")
            if not isinstance(text, str):
                add("invalid_structure", f"Scene {index} must contain a 'text' string", index)
            elif expected.is_raw and text.strip():
                add("raw_text", f"Scene {index} is raw and must keep an empty text", index)
            elif not expected.is_raw and not cls.has_narration(text):
                add("empty_narration", f"Scene {index} must contain non-empty text (spoken words)", index)
        return issues

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
        issues = cls.inspect(payload, transcription)
        if issues:
            raise RuntimeError("; ".join(issue.message for issue in issues))
        scenes = payload["scenes"]
        expected_scenes = transcription.scenes
        normalized_language = cls.resolve_language(payload=payload, target_language=target_language)

        public_scenes: list[dict[str, Any]] = []
        internal_scenes: list[dict[str, Any]] = []

        for idx, expected_scene in enumerate(expected_scenes):
            item = scenes[idx]
            normalized_text = "" if expected_scene.is_raw else item["text"].strip()

            public_scene = {
                "scene_index": expected_scene.scene_index,
                "text": normalized_text,
            }
            public_scenes.append(public_scene)
            internal_scenes.append(
                {
                    **public_scene,
                    "is_raw": expected_scene.is_raw,
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
