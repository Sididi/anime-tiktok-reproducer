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
    assert '"title_candidates"' in prompt
    assert "62 caractères maximum" in prompt


def test_validate_candidate_payload_accepts_expected_shape():
    payload = MetadataService.validate_candidate_payload(
        {
            "title_candidates": [f"Titre {idx}" for idx in range(1, 11)],
            "facebook": {
                "description": "Description Facebook",
                "tags": ["anime"],
            },
            "instagram": {
                "hashtags": ["anime", "#recommandation"],
            },
            "youtube": {
                "description": "Description YouTube",
                "tags": ["anime"],
            },
        }
    )

    assert payload.title_candidates[0] == "Titre 1"
    assert payload.instagram.hashtags == ["#anime", "#recommandation"]


def test_validate_candidate_payload_rejects_wrong_title_count():
    with pytest.raises(ValueError, match="exactly 10 titles"):
        MetadataService.validate_candidate_payload(
            {
                "title_candidates": ["Titre 1"],
                "facebook": {
                    "description": "Description Facebook",
                    "tags": ["anime"],
                },
                "instagram": {
                    "hashtags": ["#anime"],
                },
                "youtube": {
                    "description": "Description YouTube",
                    "tags": ["anime"],
                },
            }
        )


def test_validate_candidate_payload_truncates_overlong_title():
    overlong = "x" * 63
    payload = MetadataService.validate_candidate_payload(
        {
            "title_candidates": [overlong] + [f"Titre {idx}" for idx in range(2, 11)],
            "facebook": {
                "description": "Description Facebook",
                "tags": ["anime"],
            },
            "instagram": {
                "hashtags": ["#anime"],
            },
            "youtube": {
                "description": "Description YouTube",
                "tags": ["anime"],
            },
        }
    )
    assert len(payload.title_candidates[0]) <= 62


def test_validate_candidate_payload_truncates_at_word_boundary():
    # 70 chars with spaces — should truncate at a word boundary
    overlong = "Il rejette une fille parce que elle est trop gentille et il est frappé"
    payload = MetadataService.validate_candidate_payload(
        {
            "title_candidates": [overlong] + [f"Titre {idx}" for idx in range(2, 11)],
            "facebook": {
                "description": "Description Facebook",
                "tags": ["anime"],
            },
            "instagram": {
                "hashtags": ["#anime"],
            },
            "youtube": {
                "description": "Description YouTube",
                "tags": ["anime"],
            },
        }
    )
    truncated = payload.title_candidates[0]
    assert len(truncated) <= 62
    # Should end at a word boundary, not mid-word
    assert not truncated.endswith("fra")


def test_resolve_candidate_payload_builds_final_platform_metadata():
    resolved = MetadataService.resolve_candidate_payload(
        {
            "title_candidates": [f"Titre {idx}" for idx in range(1, 11)],
            "facebook": {
                "description": "Description Facebook",
                "tags": ["anime"],
            },
            "instagram": {
                "hashtags": ["#anime", "#recommandation"],
            },
            "youtube": {
                "description": "Description YouTube",
                "tags": ["anime"],
            },
        },
        selected_title="Titre retenu",
    )

    assert resolved.facebook.title == "Titre retenu"
    assert resolved.youtube.title == "Titre retenu"
    assert resolved.instagram.caption == "Titre retenu #anime #recommandation"
    assert resolved.tiktok.description == "Titre retenu #Anime #animerecommendations"
