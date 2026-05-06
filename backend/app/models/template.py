"""Pydantic models for the JSX-template catalog (config/templates/config.yaml)."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class OverlaySideConfig(BaseModel):
    """One side of the overlay (title or category)."""

    style: str = Field(..., min_length=1)
    prfpset: str | None = None
    model_config = {"extra": "forbid"}


class OverlayConfig(BaseModel):
    enabled: bool
    title: OverlaySideConfig
    category: OverlaySideConfig
    model_config = {"extra": "forbid"}


class WhiteBorderConfig(BaseModel):
    enabled: bool
    mogrt: str | None = None
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _mogrt_required_when_enabled(self) -> "WhiteBorderConfig":
        if self.enabled and not self.mogrt:
            raise ValueError("white_border.mogrt is required when enabled is true")
        return self


class ForegroundConfig(BaseModel):
    prfpset: str = Field(..., min_length=1)
    zoom: float = Field(..., gt=0, le=2.0)
    model_config = {"extra": "forbid"}


class BackgroundConfig(BaseModel):
    prfpset: str = Field(..., min_length=1)
    model_config = {"extra": "forbid"}


class SubtitlesConfig(BaseModel):
    mogrt: str = Field(..., min_length=1)
    raw_mogrt: str = Field(..., min_length=1)
    model_config = {"extra": "forbid"}


class Template(BaseModel):
    label: str = Field(..., min_length=1)
    foreground: ForegroundConfig
    background: BackgroundConfig
    subtitles: SubtitlesConfig
    white_border: WhiteBorderConfig
    overlay: OverlayConfig
    model_config = {"extra": "forbid"}


class TemplatesConfig(BaseModel):
    default: str = Field(..., min_length=1)
    templates: dict[str, Template]
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _default_must_exist(self) -> "TemplatesConfig":
        if self.default not in self.templates:
            raise ValueError(
                f"default template '{self.default}' is not in templates keys: "
                f"{sorted(self.templates.keys())}"
            )
        return self
