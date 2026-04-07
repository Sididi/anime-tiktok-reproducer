from __future__ import annotations

import pytest

from app.services.claude_service import ClaudeService


def test_parse_json_value_plain_object():
    raw = '{"name": "test", "value": 42}'
    result = ClaudeService._parse_json_value(raw)
    assert result == {"name": "test", "value": 42}


def test_parse_json_value_strips_code_fence():
    raw = '```json\n{"key": "value"}\n```'
    result = ClaudeService._parse_json_value(raw)
    assert result == {"key": "value"}


def test_parse_json_value_finds_json_in_text():
    raw = 'Here is the result: {"scenes": [1, 2, 3]} as requested'
    result = ClaudeService._parse_json_value(raw)
    assert result == {"scenes": [1, 2, 3]}


def test_parse_json_value_array():
    raw = '[{"a": 1}, {"b": 2}]'
    result = ClaudeService._parse_json_value(raw)
    assert result == [{"a": 1}, {"b": 2}]


def test_parse_json_value_raises_on_no_json():
    with pytest.raises(RuntimeError, match="Unable to parse Claude JSON response"):
        ClaudeService._parse_json_value("no json here at all")


def test_strip_json_fence_no_fence():
    assert ClaudeService._strip_json_fence('{"a": 1}') == '{"a": 1}'


def test_strip_json_fence_with_fence():
    raw = '```json\n{"a": 1}\n```'
    assert ClaudeService._strip_json_fence(raw) == '{"a": 1}'


def test_sanitize_schema_clamps_min_items():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 10,
                "maxItems": 10,
                "items": {"type": "string"},
            },
        },
    }
    sanitized = ClaudeService._sanitize_schema(schema)
    assert sanitized["properties"]["items"]["minItems"] == 1
    # maxItems should be left untouched
    assert sanitized["properties"]["items"]["maxItems"] == 10
    # Original should not be mutated
    assert schema["properties"]["items"]["minItems"] == 10


def test_sanitize_schema_leaves_small_min_items():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
            },
        },
    }
    sanitized = ClaudeService._sanitize_schema(schema)
    assert sanitized["properties"]["tags"]["minItems"] == 1


def test_sanitize_schema_handles_nested_arrays():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "inner": {
                        "type": "array",
                        "minItems": 6,
                        "items": {"type": "string"},
                    },
                },
            },
        },
    }
    sanitized = ClaudeService._sanitize_schema(schema)
    assert sanitized["properties"]["outer"]["properties"]["inner"]["minItems"] == 1
