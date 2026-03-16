from __future__ import annotations

import pytest

from app.services.metadata import MetadataService


def test_build_prompt_from_script_payload_rejects_insufficient_script_text():
    with pytest.raises(ValueError, match="Script text insufficient"):
        MetadataService.build_prompt_from_script_payload(
            anime_name="Test Anime",
            script_payload={
                "language": "fr",
                "scenes": [
                    {"scene_index": 0, "text": ""},
                    {"scene_index": 1, "text": "   "},
                ],
            },
            target_language="fr",
        )


def test_build_prompt_from_script_payload_accepts_non_empty_script_with_raw_gaps():
    prompt = MetadataService.build_prompt_from_script_payload(
        anime_name="Test Anime",
        script_payload={
            "language": "fr",
            "scenes": [
                {
                    "scene_index": 0,
                    "text": "Ceci est un texte de test suffisamment long pour les metadonnees.",
                },
                {"scene_index": 1, "text": ""},
            ],
        },
        target_language="fr",
    )

    assert "Test Anime" in prompt
    assert "Ceci est un texte de test" in prompt
