from __future__ import annotations

from app.services.script_phase_prompt_service import ScriptPhasePromptService


def test_build_overlay_prompt_fr_does_not_raise_on_literal_json_shape():
    prompt = ScriptPhasePromptService.build_overlay_prompt(
        anime_name="Test Anime",
        script_summary="Resume court de test",
        target_language="fr",
    )

    assert '"title_hooks": ["hook 1", "hook 2", "..."]' in prompt
    assert '"category": "Genre • Genre"' in prompt


def test_build_overlay_prompt_non_fr_does_not_raise_on_literal_json_shape():
    prompt = ScriptPhasePromptService.build_overlay_prompt(
        anime_name="Test Anime",
        script_summary="Short test summary",
        target_language="en",
    )

    assert '"title_hooks": ["hook 1", "hook 2", "..."]' in prompt
    assert '"category": "Genre • Genre"' in prompt
