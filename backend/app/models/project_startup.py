from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from ..library_types import LibraryType


class ProjectStartupJob(BaseModel):
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    anime_name: str | None = None
    series_id: str | None = None
    library_type: LibraryType
    tiktok_url: str | None = None
    status: Literal["queued", "running", "complete", "error"] = "queued"
    progress: float = 0.0
    phase: str | None = None
    message: str | None = None
    error: str | None = None
    ready_url: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
