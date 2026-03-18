"""Torrent metadata and indexation job models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

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


# --- Torrent replacement models ---


class TorrentReplacementRequest(BaseModel):
    """One torrent to replace within a source."""

    torrent_id: str
    new_magnet_uri: str


class ReplaceTorrentsRequest(BaseModel):
    source_name: str
    library_type: LibraryType
    replacements: list[TorrentReplacementRequest]


class ConfirmReindexRequest(BaseModel):
    source_name: str
    library_type: LibraryType
    torrent_ids: list[str]


class VerificationResult(BaseModel):
    torrent_id: str
    status: Literal["pass", "warn", "fail"]
    match_rate: float
    avg_similarity: float
    offset_median: float
    message: str


class ReplacementProgress(BaseModel):
    phase: Literal[
        "downloading_verification",
        "verifying",
        "results",
        "saving",
        "downloading_reindex",
        "removing_old_index",
        "reindexing",
        "cache_cleanup",
        "complete",
        "error",
        "stalled",
    ]
    torrent_id: str | None = None
    progress: float = 0.0
    message: str = ""
    verification_results: list[VerificationResult] | None = None
    error: str | None = None


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
    unmatched_files: list[str] = []  # files not linked to any torrent
    linked_torrents: int = 0  # number of torrents linked after indexation
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
