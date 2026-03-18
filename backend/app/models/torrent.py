"""Torrent metadata and indexation job models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..library_types import LibraryType


class TorrentFileMapping(BaseModel):
    torrent_file_index: int
    torrent_filename: str
    library_path: str
    file_size: int


class TorrentEntry(BaseModel):
    id: str = Field(default_factory=lambda: f"t_{uuid.uuid4().hex[:12]}")
    info_hash: str
    magnet_uri: str
    torrent_name: str
    torrent_file_path: str | None = None
    added_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    files: list[TorrentFileMapping] = []


class SourceTorrentMetadata(BaseModel):
    torrents: list[TorrentEntry] = []
    purge_protection: bool = False


class IndexationJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    source_name: str
    library_type: LibraryType
    source_path: str
    fps: float = 2.0
    status: str = "queued"  # queued | indexing | complete | error
    progress: float = 0.0
    phase: str | None = None
    message: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
