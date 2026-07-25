"""Durable invariants for the default metadata prompt templates.

These guard the plumbing contract (placeholders, output schema keys),
not the prose: ScriptPhasePromptService.build_metadata_prompt substitutes
[OEUVRE]/[SCRIPT] in both files and [TARGET] only on the multi path.
"""
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts" / "default"

FR = (PROMPTS_DIR / "metadata_fr.md").read_text(encoding="utf-8")
MULTI = (PROMPTS_DIR / "metadata_multi.md").read_text(encoding="utf-8")

SCHEMA_KEYS = [
    '"title_candidates"',
    '"facebook"',
    '"instagram"',
    '"youtube"',
    '"description"',
    '"tags"',
    '"hashtags"',
]


def test_fr_placeholders():
    assert "[OEUVRE]" in FR
    assert "[SCRIPT]" in FR
    # The fr path never substitutes [TARGET]; its presence would leak raw.
    assert "[TARGET]" not in FR


def test_multi_placeholders():
    assert "[OEUVRE]" in MULTI
    assert "[SCRIPT]" in MULTI
    assert "[TARGET]" in MULTI


def test_schema_keys_present_in_both():
    for key in SCHEMA_KEYS:
        assert key in FR, f"missing {key} in metadata_fr.md"
        assert key in MULTI, f"missing {key} in metadata_multi.md"


def test_eight_slot_structure_present():
    assert "Les 8 slots" in FR
    assert "The 8 slots" in MULTI


def test_dropped_fixed_facebook_cta():
    assert "Abonne toi pour plus de présentations d'anime" not in FR
    assert "Abonne toi pour plus de présentations d'anime" not in MULTI
