"""Models for the Pure-mode cleanup phase (burned-in overlay removal).

The user draws rectangles over the downloaded tiktok: one subtitle zone
(inpainted only on frames where text is detected) and optional watermark
zones (inpainted for the whole video). Coordinates are normalized to the
video FRAME (0..1 of videoWidth/videoHeight), not to the player element.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CleanupZone(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    kind: Literal["subtitle", "watermark"]
    # Normalized frame coordinates, 0..1.
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _clamp_extent(self) -> "CleanupZone":
        # Keep the rect inside the frame even if x+w slightly overflows
        # (rounding from the drag UI).
        if self.x + self.w > 1.0:
            self.w = max(1e-4, 1.0 - self.x)
        if self.y + self.h > 1.0:
            self.h = max(1e-4, 1.0 - self.y)
        return self


class CleanFeedRect(BaseModel):
    """Where the clean feed lies within the source frame (normalized 0..1).

    Pure mode only: the source tiktok already embeds a clean-feed band with a
    blurred background around it. Premiere automation crops to this rect and
    rebuilds the band + blurred background from it. Take the whole frame when
    the source has no blurred background. NOT a cleanup zone — never inpainted.
    """

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _clamp_extent(self) -> "CleanFeedRect":
        if self.x + self.w > 1.0:
            self.w = max(1e-4, 1.0 - self.x)
        if self.y + self.h > 1.0:
            self.h = max(1e-4, 1.0 - self.y)
        return self


CleanupStatus = Literal["idle", "running", "complete", "error"]


class CleanupState(BaseModel):
    zones: list[CleanupZone] = Field(default_factory=list)
    clean_feed_rect: CleanFeedRect | None = None
    status: CleanupStatus = "idle"
    progress: float = 0.0
    message: str | None = None
    error: str | None = None
    cleaned_video_path: str | None = None
    updated_at: datetime | None = None
