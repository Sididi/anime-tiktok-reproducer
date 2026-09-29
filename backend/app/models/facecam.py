"""Persisted editorial state; offsets and revisions refer to the spoken document."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


ENDING = "Le nom de l’anime c’est :"


class FacecamCandidate(BaseModel):
    id: str
    text: str = Field(min_length=1)
    original: bool = False


class StoryClip(BaseModel):
    """Word bounds index the immutable source scene, never generated French words."""
    model_config = ConfigDict(extra="forbid")
    scene_index: StrictInt
    start_word: StrictInt | None = None
    end_word: StrictInt | None = None


class StoryArc(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = ""  # Assigned by the application after validation.
    title: str = Field(min_length=1)
    premise: str = Field(min_length=1)
    ending: str = Field(min_length=1)
    steps: list[str] = Field(min_length=1, max_length=6)
    omitted: list[str] = Field(default_factory=list)
    clips: list[StoryClip] = Field(min_length=1)
    raw_duration_seconds: float = 0


class ScriptBlock(BaseModel):
    id: str
    kind: Literal["hook", "body", "raw", "opinion", "ending"]
    text: str = ""
    scene_indices: list[int] = Field(default_factory=list)


class FacecamFinding(BaseModel):
    quote: str
    start: int
    end: int
    kind: Literal["contradiction", "uncertain"]
    explanation: str
    correction: str = ""
    sources: list[dict[str, str]] = Field(default_factory=list)


class FacecamState(BaseModel):
    revision: int = 0
    stories: list[StoryArc] = Field(default_factory=list)
    stories_fingerprint: str | None = None
    selected_story_id: str | None = None
    montage_map: list[dict[str, Any]] = Field(default_factory=list)
    estimated_duration_seconds: float | None = None
    hooks: list[FacecamCandidate] = Field(default_factory=list)
    opinions: list[FacecamCandidate] = Field(default_factory=list)
    selected_hook: str = ""
    selected_opinion: str = ""
    blocks: list[ScriptBlock] = Field(default_factory=list)
    approved_revision: int | None = None
    assignment: list[dict[str, Any]] = Field(default_factory=list)
    fact_revision: int | None = None
    fact_status: Literal["unchecked", "complete", "unavailable"] = "unchecked"
    fact_error: str | None = None
    fact_context: dict[str, Any] | None = None
    findings: list[FacecamFinding] = Field(default_factory=list)
    recording: dict[str, Any] | None = None
    prepared: dict[str, Any] | None = None
    processed_signature: str | None = None
    delivery: dict[str, Any] | None = None
    cover: dict[str, Any] | None = None
    job: dict[str, Any] | None = None

    def spoken_text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.kind != "raw")
