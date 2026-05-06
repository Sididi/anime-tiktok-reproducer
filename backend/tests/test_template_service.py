"""Tests for TemplateService."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.template_service import TemplateService


VALID = """\
default: classic
templates:
  classic:
    label: "Classic"
    foreground: { prfpset: fg.prfpset, zoom: 0.76 }
    background: { prfpset: bg.prfpset }
    subtitles: { mogrt: s.mogrt, raw_mogrt: r.mogrt }
    white_border: { enabled: true, mogrt: border.mogrt }
    overlay:
      enabled: true
      title: { style: classic, prfpset: null }
      category: { style: classic, prfpset: null }
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return p


def test_loads_valid_config(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    cfg = TemplateService.get_config(force_reload=True)
    assert cfg.default == "classic"
    assert cfg.templates["classic"].foreground.zoom == 0.76


def test_get_template_returns_known_key(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    TemplateService.get_config(force_reload=True)
    tpl = TemplateService.get("classic")
    assert tpl.label == "Classic"


def test_unknown_template_raises(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    TemplateService.get_config(force_reload=True)
    with pytest.raises(ValueError):
        TemplateService.get("nope")


def test_default_template_resolves(tmp_path, monkeypatch):
    path = _write(tmp_path, VALID)
    monkeypatch.setattr(
        "app.services.template_service.settings.templates_config_path", path
    )
    TemplateService.get_config(force_reload=True)
    assert TemplateService.default_key() == "classic"
