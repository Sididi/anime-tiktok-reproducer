"""Shared auto-editor profiles for production and preview generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AutoEditorProfile:
    """Describe one auto-editor tuning profile."""

    id: str
    threshold: str
    margin: str = "0.04sec,0.04sec"
    silent_speed: int = 99999
    stream: str = "all"

    def edit_value(self) -> str:
        return f"audio:threshold={self.threshold},stream={self.stream}"

    def command_args(self) -> list[str]:
        return [
            "--edit",
            self.edit_value(),
            "--margin",
            self.margin,
            "--silent-speed",
            str(self.silent_speed),
            "--no-open",
        ]

    def preview_filename(self, suffix: str = ".wav") -> str:
        return f"{self.id}{suffix}"

    def preview_path(self, output_dir: Path, suffix: str = ".wav") -> Path:
        return output_dir / self.preview_filename(suffix=suffix)


PRODUCTION_AUTO_EDITOR_PROFILE = AutoEditorProfile(
    id="production_selected_t080_m004_024",
    threshold="0.080",
    margin="0.04sec,0.24sec",
)

PREVIEW_AUTO_EDITOR_PROFILES: tuple[AutoEditorProfile, ...] = (
    AutoEditorProfile(id="precision_plus_1_t080_m004_016", threshold="0.080", margin="0.04sec,0.16sec"),
    AutoEditorProfile(id="precision_plus_2_t080_m004_018", threshold="0.080", margin="0.04sec,0.18sec"),
    AutoEditorProfile(id="precision_plus_3_t080_m004_020", threshold="0.080", margin="0.04sec,0.20sec"),
    AutoEditorProfile(id="precision_plus_4_t080_m004_022", threshold="0.080", margin="0.04sec,0.22sec"),
    AutoEditorProfile(id="precision_plus_5_t080_m004_024", threshold="0.080", margin="0.04sec,0.24sec"),
)
