from __future__ import annotations

import pytest

from app.services.gemini_service import GeminiService


def test_extract_text_reports_prompt_feedback_when_candidates_missing():
    with pytest.raises(RuntimeError, match="did not contain candidates") as exc_info:
        GeminiService._extract_text(
            {
                "promptFeedback": {
                    "blockReason": "SAFETY",
                    "safetyRatings": [
                        {"category": "HARM_CATEGORY_HATE_SPEECH"},
                        {"category": "HARM_CATEGORY_HARASSMENT"},
                    ],
                },
                "candidates": [],
            }
        )

    message = str(exc_info.value)
    assert "blockReason=SAFETY" in message
    assert "safetyCategories=HARM_CATEGORY_HATE_SPEECH,HARM_CATEGORY_HARASSMENT" in message


def test_generate_json_retries_without_schema_after_empty_candidates(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    def fake_generate_content(**kwargs):
        calls.append(kwargs)
        schema = kwargs.get("response_json_schema")
        if schema is not None:
            return {
                "promptFeedback": {"blockReason": "SAFETY"},
                "candidates": [],
            }
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"facebook":{"title":"x","description":"y","tags":["a"]},"instagram":{"caption":"z"},"youtube":{"title":"a","description":"b","tags":["c"]},"tiktok":{"description":"d"}}'
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(GeminiService, "_generate_content", fake_generate_content)

    result = GeminiService.generate_json(
        "prompt",
        response_json_schema={"type": "object"},
    )

    assert result["facebook"]["title"] == "x"
    assert len(calls) == 2
    assert calls[0]["response_json_schema"] is not None
    assert calls[1]["response_json_schema"] is None
