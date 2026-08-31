"""Pydantic models for the JSX-template catalog (config/templates/config.yaml)."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class OverlaySideConfig(BaseModel):
    """One side of the overlay (title or category)."""

    enabled: bool = True
    style: str = Field(..., min_length=1)
    prfpset: str | None = None
    text: str | None = None
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _normalize_fixed_text(self) -> "OverlaySideConfig":
        if self.text is not None:
            self.text = self.text.strip()
            if not self.text:
                raise ValueError("overlay side text must be non-empty when provided")
        return self


class OverlayConfig(BaseModel):
    enabled: bool
    title: OverlaySideConfig
    category: OverlaySideConfig
    model_config = {"extra": "forbid"}


class WhiteBorderConfig(BaseModel):
    enabled: bool
    # Visible separator thickness per side (px in the 1080x1920 sequence).
    # Defaults measured from the retired "White border 10px.mogrt": its white
    # slab spanned seq 541.2..1376.4, i.e. 8.4px above / 6.0px below the
    # classic 76% band — so the generated PNG renders identically.
    top_px: float = Field(default=8.4, ge=0.0, le=200.0)
    bottom_px: float = Field(default=6.0, ge=0.0, le=200.0)
    model_config = {"extra": "forbid"}


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
    voice_key: str | None = None
    music_key: str | None = None
    llm_preset: str | None = None
    min_playback_speed: float | None = Field(default=None, gt=0.10, le=1.0)
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
