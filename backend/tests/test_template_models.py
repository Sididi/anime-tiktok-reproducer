"""Tests for Template Pydantic models."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.template import (
    BackgroundConfig,
    ForegroundConfig,
    OverlayConfig,
    OverlaySideConfig,
    SubtitlesConfig,
    Template,
    TemplatesConfig,
    WhiteBorderConfig,
)


def _classic() -> Template:
    return Template(
        label="Classic",
        foreground=ForegroundConfig(prfpset="fg.prfpset", zoom=0.76),
        background=BackgroundConfig(prfpset="bg.prfpset"),
        subtitles=SubtitlesConfig(mogrt="s.mogrt", raw_mogrt="r.mogrt"),
        white_border=WhiteBorderConfig(enabled=True, mogrt="border.mogrt"),
        overlay=OverlayConfig(
            enabled=True,
            title=OverlaySideConfig(style="classic", prfpset=None),
            category=OverlaySideConfig(style="classic", prfpset=None),
        ),
    )


def test_template_zoom_must_be_positive():
    with pytest.raises(ValueError):
        ForegroundConfig(prfpset="x", zoom=-0.1)
    with pytest.raises(ValueError):
        ForegroundConfig(prfpset="x", zoom=0)


def test_white_border_disabled_allows_null_mogrt():
    WhiteBorderConfig(enabled=False, mogrt=None)


def test_white_border_enabled_requires_mogrt():
    with pytest.raises(ValueError):
        WhiteBorderConfig(enabled=True, mogrt=None)


def test_overlay_side_style_required():
    with pytest.raises(ValueError):
        OverlaySideConfig(style="", prfpset=None)


def test_templates_config_default_must_exist():
    cfg = TemplatesConfig(default="classic", templates={"classic": _classic()})
    assert cfg.default == "classic"
    with pytest.raises(ValueError):
        TemplatesConfig(default="missing", templates={"classic": _classic()})
