"""Durable store for publishes whose remote upload has not completed yet.

A record is written right after a series' release has been fully prepared
locally (artifacts hashed, manifest built, index shard staged). From that
moment the series is usable locally; the record freezes the entire remote
upload plan so the upload can run — and resume after a crash/restart —
without re-indexing or re-hashing anything.

This module intentionally imports nothing from other services (only the
settings) so it can be used from both the repository and the queue layers
without import cycles.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import settings

logger = logging.getLogger("uvicorn.error")

SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PendingArtifact(BaseModel):
    """One artifact of the frozen upload plan.

    ``local_path`` is set for artifacts that exist locally (uploads and
    hardlink candidates, which fall back to upload when the server lacks
    hardlink support). ``previous_remote_path`` is the absolute remote path
    (posix, relative to the Storage Box root) of the identical artifact in
    the previous release, set for hardlink and preserved entries.
    """

    remote_relative_path: str
    size_bytes: int
    sha256: str
    artifact_type: str
    local_path: str | None = None
    local_relative_path: str | None = None
    previous_remote_path: str | None = None


class PendingPublishRecord(BaseModel):
    schema_version: int = SCHEMA_VERSION
    publish_id: str
    library_type: str
    series_id: str
    release_id: str
    display_name: str
    series_dir: str
    staged_index_dir: str
    is_brand_new_series: bool
    previous_release_id: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)
    attempts: int = 0
    last_error: str | None = None
    uploads: list[PendingArtifact] = Field(default_factory=list)
    hardlinks: list[PendingArtifact] = Field(default_factory=list)
    preserved: list[PendingArtifact] = Field(default_factory=list)
    manifest: dict = Field(default_factory=dict)


class PendingPublishStore:
    """File-per-record persistence under ``data_dir/pending_publishes``."""

    @classmethod
    def records_root(cls) -> Path:
        return settings.data_dir / "pending_publishes"

    @classmethod
    def staging_dir_root(cls) -> Path:
        # Deliberately NOT under cache_dir/storage_box/tmp nor matching the
        # storage_box_release_* glob: both are swept at startup, and staged
        # index shards must survive restarts for the upload to resume.
        return settings.cache_dir / "storage_box" / "pending_publish"

    @classmethod
    def record_path(cls, publish_id: str) -> Path:
        return cls.records_root() / f"{publish_id}.json"

    @classmethod
    def save(cls, record: PendingPublishRecord) -> None:
        path = cls.record_path(record.publish_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    @classmethod
    def load(cls, publish_id: str) -> PendingPublishRecord | None:
        return cls._load_path(cls.record_path(publish_id))

    @classmethod
    def _load_path(cls, path: Path) -> PendingPublishRecord | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return PendingPublishRecord.model_validate(payload)
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("Unreadable pending publish record %s", path, exc_info=True)
            return None

    @classmethod
    def delete(cls, publish_id: str) -> None:
        """Remove the record and its staged index directory."""
        record = cls.load(publish_id)
        if record is not None and record.staged_index_dir:
            shutil.rmtree(record.staged_index_dir, ignore_errors=True)
        cls.record_path(publish_id).unlink(missing_ok=True)

    @classmethod
    def list_all(cls) -> list[PendingPublishRecord]:
        root = cls.records_root()
        if not root.exists():
            return []
        records = []
        for path in sorted(root.glob("*.json")):
            record = cls._load_path(path)
            if record is not None:
                records.append(record)
        return records

    @classmethod
    def find_by_series(
        cls, library_type: str, series_id: str
    ) -> PendingPublishRecord | None:
        for record in cls.list_all():
            if record.library_type == library_type and record.series_id == series_id:
                return record
        return None

    @classmethod
    def find_by_display_name(
        cls, library_type: str, display_name: str
    ) -> PendingPublishRecord | None:
        for record in cls.list_all():
            if (
                record.library_type == library_type
                and record.display_name == display_name
            ):
                return record
        return None
