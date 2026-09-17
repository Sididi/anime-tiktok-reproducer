"""Persisted editorial state; offsets and revisions refer to the spoken document."""
from typing import Any, Literal

from pydantic import BaseModel, Field


ENDING = "Le nom de l’anime c’est :"


class FacecamCandidate(BaseModel):
    id: str
    text: str = Field(min_length=1)
    original: bool = False


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
