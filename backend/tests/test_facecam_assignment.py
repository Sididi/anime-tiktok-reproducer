from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.facecam_assignment_service import FacecamAssignmentService as Assignment


def scenes(indices):
    return [{"scene_index": index, "original": f"Action {index}.", "duration": 2.0} for index in indices]


@pytest.mark.parametrize("value", [
    None, [], "invalid", {}, {"boundaries": None}, {"boundaries": "2"},
    {"boundaries": [2], "scene_indices": [42, 5]},
    {"boundaries": []}, {"boundaries": [1, 2]},
    {"boundaries": [True]}, {"boundaries": [False]}, {"boundaries": ["2"]}, {"boundaries": [2.0]},
    {"boundaries": [-1]}, {"boundaries": [0]}, {"boundaries": [4]}, {"boundaries": [99]},
])
def test_invalid_response_is_rejected_without_type_coercion(value):
    with pytest.raises(ValueError):
        Assignment.validate_boundaries(value, 4, 2)


@pytest.mark.parametrize("boundaries", [[2, 2], [3, 1], [4, 5]])
def test_duplicate_reversed_or_overfull_boundaries_are_rejected(boundaries):
    with pytest.raises(ValueError):
        Assignment.validate_boundaries({"boundaries": boundaries}, 5, 3)


def test_all_small_partitions_preserve_exact_words_and_arbitrary_source_ids():
    # Exhaust all legal partitions, including one word per scene and a single scene.
    for word_count in range(1, 9):
        words = ["L’héroïne,", "répète", "répète", "puis", "s’arrête…", "Un", "cri", "!"][:word_count]
        for scene_count in range(1, word_count + 1):
            indices = [42 + i*7 for i in range(scene_count)]
            for cuts in combinations(range(1, word_count), scene_count - 1):
                def generate(prompt, **kwargs):
                    return {"boundaries": list(cuts)}
                result = Assignment.assign(words, scenes(indices), generate, context="test")
                assert [r["scene_index"] for r in result] == indices
                assert all(r["text"] for r in result)
                assert " ".join(r["text"] for r in result).split() == words


@pytest.mark.parametrize("words,indices", [(["un", "deux"], [42]), (["un", "deux"], [5, 42])])
def test_forced_partitions_do_not_call_model(words, indices):
    def unexpected(*args, **kwargs):
        pytest.fail("No model needed")
    result = Assignment.assign(words, scenes(indices), unexpected, context="test")
    assert [r["scene_index"] for r in result] == indices
    assert " ".join(r["text"] for r in result).split() == words


@pytest.mark.parametrize("words", [[], ["seul"]])
def test_too_few_words_fail_before_model_call(words):
    def unexpected(*args, **kwargs):
        pytest.fail("An impossible partition cannot be retried")
    with pytest.raises(ValueError, match="allongez ce passage"):
        Assignment.assign(words, scenes([5, 42]), unexpected, context="test")


def test_corrective_retry_includes_diagnostic_and_enforces_schema(caplog):
    calls = []
    def generate(prompt, **kwargs):
        payload = json.loads(prompt.split("\n", 1)[1])
        calls.append(payload)
        schema = kwargs["response_schema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["boundaries"]["minItems"] == 2
        assert schema["properties"]["boundaries"]["maxItems"] == 2
        assert all("scene_index" not in s for s in payload["scenes"])
        if len(calls) == 1:
            return {"boundaries": [4, 2]}
        assert payload["correction"]["previous_response"] == {"boundaries": [4, 2]}
        assert "coupure 2" in payload["correction"]["error"]
        return {"boundaries": [2, 4]}
    result = Assignment.assign("Elle revient. Elle repart. Elle gagne.".split(),
                               scenes([5, 21, 36]), generate, context="project=test, block=body-5")
    assert [r["text"] for r in result] == ["Elle revient.", "Elle repart.", "Elle gagne."]
    assert len(calls) == 2
    assert "project=test, block=body-5" in caplog.text


def test_exhausted_retry_never_substitutes_an_arbitrary_partition():
    calls = []
    def generate(*args, **kwargs):
        calls.append(args)
        return {"boundaries": []}
    with pytest.raises(ValueError, match="3 tentatives"):
        Assignment.assign(["un", "deux", "trois"], scenes([5, 42]), generate, context="test")
    assert len(calls) == 3


def test_provider_failure_is_not_retried_as_a_validation_error():
    calls = []
    def generate(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("provider unavailable")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        Assignment.assign(["un", "deux", "trois"], scenes([5, 42]), generate, context="test")
    assert len(calls) == 1
