"""Persisted editorial state; offsets and revisions refer to the spoken document."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


ENDING = "Le nom de l’anime c’est :"


class FacecamCandidate(BaseModel):
    id: str
    text: str = Field(min_length=1)
    original: bool = False


class StoryClip(BaseModel):
    """Source word indices; analysis resolves open bounds before persistence.

    A stored clip is whole (null/null) or has two explicit, half-open bounds.
    These never index generated French words.
    """
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
    estimated_duration_seconds: float | None = None


class ScriptBlock(BaseModel):
    id: str
    kind: Literal["hook", "body", "raw", "opinion", "ending"]
    text: str = ""
    scene_indices: list[int] = Field(default_factory=list)


class FacecamFinding(BaseModel):
    id: str = ""
    quote: str
    start: int
    end: int
    kind: Literal["contradiction", "uncertain"]
    explanation: str
    correction: str = ""
    sources: list[dict[str, str]] = Field(default_factory=list)


class FindingRange(BaseModel):
    id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class RecordingCut(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    enabled: bool = True
    origin: Literal["manual", "ai"] = "manual"


class RecordingSuggestion(RecordingCut):
    confidence: Literal["high", "review"]
    reason: str
    removed_text: str
    retained_text: str


class RecordingEdit(BaseModel):
    id: str
    version: int = 0
    source_directory: str
    source_fingerprint: str
    original_name: str
    duration: float
    scale: float
    analysis_status: Literal["analyzing", "ready", "error"] = "analyzing"
    analysis_error: str | None = None
    suggestions: list[RecordingSuggestion] = Field(default_factory=list)
    cuts: list[RecordingCut] = Field(default_factory=list)
    applied: bool = False


class RecordingWordChange(BaseModel):
    id: str
    block_id: str
    kind: Literal["insert", "delete", "replace"]
    start: int
    end: int
    expected: str
    spoken: str
    replacement: str


class RecordingReview(BaseModel):
    id: str
    revision: int
    fingerprint: str
    version: int = 0
    blocks: list[ScriptBlock]
    changes: list[RecordingWordChange] = Field(default_factory=list)
    validated_revision: int | None = None


class FacecamPlacement(BaseModel):
    key: str
    revision: int
    fingerprint: str
    status: Literal["pending", "running", "complete", "error"] = "pending"
    assignment_complete: bool = False
    error: str | None = None


class FacecamAutoEditorSettings(BaseModel):
    """Real-voice silence detection; word protection is always enabled."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    noise_threshold_db: float = Field(default=-26.0, ge=-60, le=-15)
    min_silence_ms: int = Field(default=120, ge=80, le=1000)
    margin_before_ms: int = Field(default=50, ge=0, le=300)
    margin_after_ms: int = Field(default=60, ge=0, le=500)
    speed: float = Field(default=1.0, ge=0.9, le=1.5)


class FacecamState(BaseModel):
    revision: int = 0
    generation_mode: Literal["story", "original"] = "story"
    original_brief: dict[str, Any] | None = None
    stories: list[StoryArc] = Field(default_factory=list)
    full_story: StoryArc | None = None  # Derived from immutable references when loading.
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
    findings_revision: int | None = None  # Revision of the anchors, not a new fact check.
    fact_status: Literal["unchecked", "complete", "unavailable"] = "unchecked"
    fact_error: str | None = None
    fact_context: dict[str, Any] | None = None
    findings: list[FacecamFinding] = Field(default_factory=list)
    recording: dict[str, Any] | None = None
    recording_edit: RecordingEdit | None = None
    recording_review: RecordingReview | None = None
    placement: FacecamPlacement | None = None
    auto_editor: FacecamAutoEditorSettings = Field(default_factory=FacecamAutoEditorSettings)
    prepared: dict[str, Any] | None = None
    preview_enabled: bool = False
    processed_signature: str | None = None
    delivery: dict[str, Any] | None = None
    cover: dict[str, Any] | None = None
    metadata_generation: dict[str, Any] | None = None
    thumbnail: dict[str, Any] | None = None
    job: dict[str, Any] | None = None

    def spoken_text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.kind != "raw")
