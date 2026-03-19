from __future__ import annotations

import json
from typing import Any

from ..library_types import LibraryType
from ..models import Project, Transcription
from .prompt_resolver import FR, METADATA, MULTI, OVERLAY, SAME_LANG, SCRIPT, PromptResolver


_LANGUAGE_DISPLAY = {
    "fr": "Français",
    "en": "English",
    "es": "Español",
    "de": "Deutsch",
}


def _script_variant(source_lang: str, target_lang: str) -> str:
    """Determine the script prompt variant based on source/target languages.

    - same_lang: source == target (rewriting, not translation)
    - fr: target is French (dedicated French prompt)
    - multi: everything else (generic multilingual prompt)
    """
    if source_lang == target_lang:
        return SAME_LANG
    if target_lang == "fr":
        return FR
    return MULTI


class ScriptPhasePromptService:
    """Canonical prompt builders for the /script phase."""

    @classmethod
    def language_display(cls, language_code: str) -> str:
        normalized = (language_code or "").strip().lower()
        return _LANGUAGE_DISPLAY.get(normalized, normalized or "fr")

    @classmethod
    def build_script_prompt(
        cls,
        *,
        project: Project,
        transcription: Transcription,
        target_language: str,
    ) -> str:
        target_language_code = (target_language or "").strip().lower() or "fr"
        source_language_code = (transcription.language or "").strip().lower()
        source_language = cls.language_display(source_language_code)
        target_language_name = cls.language_display(target_language_code)
        anime_name = project.anime_name or "Inconnu"

        scenes_payload = [
            {
                "scene_index": scene.scene_index,
                "text": scene.text,
                "duration_seconds": f"{max(scene.end_time - scene.start_time, 0):.2f}",
                "estimated_word_count": len(
                    [token for token in scene.text.split() if token.strip()]
                ),
            }
            for scene in transcription.scenes
        ]

        variant = _script_variant(source_language_code, target_language_code)

        template = PromptResolver.resolve(
            prompt_group=SCRIPT,
            language_variant=variant,
            library_type=project.library_type,
        )
        prompt = (
            template.replace("[SOURCE]", source_language)
            .replace("[OEUVRE]", anime_name)
            .replace("[TARGET]", target_language_name)
        )

        input_json = json.dumps(
            {
                "language": target_language_code,
                "scenes": scenes_payload,
            },
            ensure_ascii=False,
            indent=2,
        )
        return prompt + input_json

    @classmethod
    def build_metadata_prompt(
        cls,
        *,
        anime_name: str,
        script_text: str,
        target_language: str = "fr",
        library_type: LibraryType = LibraryType.ANIME,
    ) -> str:
        target_language_code = (target_language or "").strip().lower() or "fr"
        variant = FR if target_language_code == "fr" else MULTI

        template = PromptResolver.resolve(
            prompt_group=METADATA,
            language_variant=variant,
            library_type=library_type,
        )
        prompt = template.replace("[OEUVRE]", anime_name).replace(
            "[SCRIPT]", script_text
        )
        if target_language_code != "fr":
            display = cls.language_display(target_language_code)
            prompt = prompt.replace("[TARGET]", display)
        return prompt

    @classmethod
    def build_overlay_prompt(
        cls,
        *,
        anime_name: str,
        script_summary: str,
        target_language: str,
        library_type: LibraryType = LibraryType.ANIME,
    ) -> str:
        target_language_code = (target_language or "").strip().lower() or "fr"
        variant = FR if target_language_code == "fr" else MULTI

        template = PromptResolver.resolve(
            prompt_group=OVERLAY,
            language_variant=variant,
            library_type=library_type,
        )
        return (
            template.replace("[OEUVRE]", anime_name)
            .replace("[SCRIPT_SUMMARY]", script_summary)
            .replace("[TARGET]", cls.language_display(target_language_code))
        )
