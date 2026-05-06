"""Verify the renderer registry dispatches to the right function."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.title_image_generator import (
    CATEGORY_RENDERERS,
    TITLE_RENDERERS,
    TitleImageGeneratorService,
)


def test_classic_renderers_registered():
    assert "classic" in TITLE_RENDERERS
    assert "classic" in CATEGORY_RENDERERS


def test_minimal_renderers_registered():
    assert "minimal" in TITLE_RENDERERS
    assert "minimal" in CATEGORY_RENDERERS


def test_generate_writes_files_for_classic(tmp_path):
    out = TitleImageGeneratorService.generate(
        title="HELLO",
        category="ACTION",
        output_dir=tmp_path,
        title_style="classic",
        category_style="classic",
    )
    assert out["title"].exists()
    assert out["category"].exists()


def test_generate_writes_files_for_minimal(tmp_path):
    out = TitleImageGeneratorService.generate(
        title="HELLO",
        category="ACTION",
        output_dir=tmp_path,
        title_style="minimal",
        category_style="minimal",
    )
    assert out["title"].exists()
    assert out["category"].exists()


def test_generate_unknown_style_raises(tmp_path):
    with pytest.raises(ValueError):
        TitleImageGeneratorService.generate(
            title="HELLO",
            category="ACTION",
            output_dir=tmp_path,
            title_style="bogus",
            category_style="classic",
        )
