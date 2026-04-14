from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectUploadJob(BaseModel):
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    account_id: str | None = None
    platforms: list[str] | None = None
    facebook_strategy: str | None = None
    youtube_strategy: str | None = None
    status: Literal["queued", "running", "complete", "error"] = "queued"
    phase: str | None = None
    message: str | None = None
    error: str | None = None
    platform_results: list[dict[str, Any]] | None = None
    result: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
