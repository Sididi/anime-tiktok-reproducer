from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator, model_validator


class TemporarySourceInput(BaseModel):
    kind: Literal["url", "folder"]
    value: str
    fps: Literal[1, 2, 4] = 2

    @field_validator("value")
    @classmethod
    def nonempty_value(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Temporary source must not be empty")
        return value

    @model_validator(mode="after")
    def validate_url(self):
        if self.kind == "url":
            url = urlsplit(self.value)
            if url.scheme not in {"http", "https"} or not url.netloc:
                raise ValueError("Source URL must be an HTTP(S) video URL")
        return self


class TemporarySource(TemporarySourceInput):
    status: Literal["pending", "preparing", "indexing", "ready", "error"] = "pending"
    error: str | None = None
