from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.project import Project
from app.models.template import (
    BackgroundConfig,
    ForegroundConfig,
    OverlayConfig,
    OverlaySideConfig,
    SubtitlesConfig,
    Template,
    WhiteBorderConfig,
)
from app.services.script_automation_service import ScriptAutomationService


def _template(
    *,
    title_enabled: bool,
    category_enabled: bool,
    title_text: str | None = None,
) -> Template:
    return Template(
        label="Test",
        foreground=ForegroundConfig(prfpset="fg.prfpset", zoom=0.76),
        background=BackgroundConfig(prfpset="bg.prfpset"),
        subtitles=SubtitlesConfig(mogrt="s.mogrt", raw_mogrt="r.mogrt"),
        white_border=WhiteBorderConfig(enabled=True, mogrt="border.mogrt"),
        overlay=OverlayConfig(
            enabled=True,
            title=OverlaySideConfig(
                enabled=title_enabled,
                style="minimal",
                text=title_text,
            ),
            category=OverlaySideConfig(
                enabled=category_enabled,
                style="minimal",
            ),
        ),
    )


def _generate(monkeypatch, template: Template, llm_result: dict | None = None):
    monkeypatch.setattr(
        "app.services.template_service.TemplateService.get",
        classmethod(lambda cls, key: template),
    )
    calls: list[str] = []

    def fake_generate(cls, prompt, **kwargs):
        calls.append(prompt)
        return llm_result or {}

    monkeypatch.setattr(
        "app.services.script_automation_service.LLMService.generate_json",
        classmethod(fake_generate),
    )
    result = ScriptAutomationService.generate_video_overlay(
        project=Project(template="test", anime_name="Test"),
        script_payload={"scenes": [{"text": "Script"}]},
        target_language="fr",
    )
    return result, calls


def test_disabled_overlay_sides_make_no_llm_call(monkeypatch):
    result, calls = _generate(
        monkeypatch,
        _template(title_enabled=False, category_enabled=False),
    )
    assert calls == []
    assert result == {"title": "", "title_hooks": [], "category": ""}


def test_title_only_template_requests_no_category(monkeypatch):
    hooks = [f"Hook {index}" for index in range(8)]
    result, calls = _generate(
        monkeypatch,
        _template(title_enabled=True, category_enabled=False),
        {"title_hooks": hooks},
    )
    assert len(calls) == 1
    assert "Generate only `title_hooks`" in calls[0]
    assert result["category"] == ""
    assert result["title"] == "Hook 0"


def test_fixed_title_generates_only_category(monkeypatch):
    result, calls = _generate(
        monkeypatch,
        _template(title_enabled=True, category_enabled=True, title_text="#1"),
        {"category": "Action • Fantasy"},
    )
    assert len(calls) == 1
    assert "Generate only `category`" in calls[0]
    assert result == {
        "title": "#1",
        "title_hooks": ["#1"],
        "category": "Action • Fantasy",
    }
