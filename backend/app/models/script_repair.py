from typing import Literal
from typing import Any

from pydantic import BaseModel, Field


class ScriptValidationIssue(BaseModel):
    code: Literal["invalid_json", "invalid_structure", "empty_narration", "raw_text"]
    message: str
    scene_index: int | None = None


class ScriptRepairChange(BaseModel):
    scene_index: int
    before: str
    after: str


class ScriptRepairReport(BaseModel):
    status: Literal["not_needed", "repaired", "partial", "failed", "invalid"]
    attempts: int = 0
    changes: list[ScriptRepairChange] = Field(default_factory=list)
    unresolved_scene_indices: list[int] = Field(default_factory=list)
    source_gap_scene_indices: list[int] = Field(default_factory=list)
    issues: list[ScriptValidationIssue] = Field(default_factory=list)
    warning: str | None = None
    review_required: bool = False
    model: str | None = None


class ScriptRepairResponse(BaseModel):
    run_id: str
    script_json: dict[str, Any]
    repair_report: ScriptRepairReport
