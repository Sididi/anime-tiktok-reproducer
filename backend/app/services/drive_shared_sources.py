"""Shared-source dedup for the Drive export.

Large ``sources/`` files (episodes, music) are byte-identical across
duplicate projects — and often across unrelated projects on the same series.
When ``drive_shared_sources_enabled`` is on, the export uploads each unique
file once into a shared ``_SPM_SHARED_SOURCES`` Drive folder (named
``{sha256[:16]}__{basename}``) and publishes a small
``atr_remote_sources.json`` manifest in the project folder instead; the CEP
extension resolves the manifest back into the local ``sources/`` layout.

Refcounting: every export persists ``drive_export_manifest.json`` in the
project dir. A manifest whose project dir still exists counts as a live
reference; ``collect_garbage`` (called at managed teardown, after the project
Drive folder is deleted but before the local dir goes) removes shared files
nobody references anymore. ``audit`` reconciles Drive against the manifests.

Ordering contract (GC race guard): the local manifest is written with
``status: "pending"`` BEFORE any shared-Drive mutation, so an in-flight
export's references are always visible to a concurrent teardown's GC scan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import settings
from ..models import Project
from .executors import run_heavy
from .google_drive_rclone import GoogleDriveRclone
from .google_drive_service import GoogleDriveService
from .project_service import ProjectService
from .rclone_runner import StatsCallback

logger = logging.getLogger("uvicorn.error")

SHARED_FOLDER_NAME = "_SPM_SHARED_SOURCES"
REMOTE_MANIFEST_FILENAME = "atr_remote_sources.json"
LOCAL_MANIFEST_FILENAME = "drive_export_manifest.json"
SCHEMA_VERSION = 1
# Only names of this exact shape are ever deleted by GC/audit — a guard
# against wiping anything a human (or another tool) put in the folder.
SHARED_NAME_RE = re.compile(r"^[0-9a-f]{16}__.+")

# Serializes the shared folder's list→upload→verify phase across concurrent
# exports in this single-process backend.
_shared_upload_lock = asyncio.Lock()
_shared_folder_id_cache: str | None = None


@dataclass
class SharedFileRecord:
    path_in_folder: str  # e.g. "sources/<basename>", POSIX
    size: int
    sha256: str
    md5: str | None
    drive_file_id: str
    shared_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path_in_folder,
            "size": self.size,
            "sha256": self.sha256,
            "md5": self.md5,
            "drive_file_id": self.drive_file_id,
            "shared_name": self.shared_name,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DriveSharedSources:
    @classmethod
    def is_enabled(cls) -> bool:
        return bool(settings.drive_shared_sources_enabled)

    @staticmethod
    def shared_name(sha256: str, basename: str) -> str:
        return f"{sha256[:16].lower()}__{basename}"

    # ------------------------------------------------------------------ #
    # Manifest entry partitioning                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def partition_entries(cls, entries: list) -> tuple[list, list]:
        """Split build_manifest entries into (kept inline, externalized).

        Externalized = a real file under ``sources/`` at least
        ``drive_shared_sources_min_bytes`` big. Everything else stays in the
        project folder exactly as before.
        """
        min_bytes = max(1, int(settings.drive_shared_sources_min_bytes))
        kept: list = []
        externalized: list = []
        for entry in entries:
            if entry.source_path is None:
                kept.append(entry)
                continue
            parts = Path(entry.relative_path).parts
            inside = parts[1:] if len(parts) > 1 else parts
            is_source = len(inside) >= 2 and inside[0] == "sources"
            if is_source and entry.source_path.stat().st_size >= min_bytes:
                externalized.append(entry)
            else:
                kept.append(entry)
        return kept, externalized

    @staticmethod
    def path_in_folder(entry) -> str:
        """Relative POSIX path inside the project folder (SPM_ level stripped)."""
        parts = Path(entry.relative_path).parts
        inside = parts[1:] if len(parts) > 1 else parts
        return "/".join(inside)

    # ------------------------------------------------------------------ #
    # Shared Drive folder                                                #
    # ------------------------------------------------------------------ #

    @classmethod
    def ensure_shared_folder(cls) -> str:
        global _shared_folder_id_cache
        if _shared_folder_id_cache:
            return _shared_folder_id_cache
        folder_id = GoogleDriveService.ensure_child_folder_id(SHARED_FOLDER_NAME)
        _shared_folder_id_cache = folder_id
        return folder_id

    @classmethod
    async def ensure_uploaded(
        cls,
        externalized: list[tuple[Any, str]],
        *,
        shared_folder_id: str,
        stats_callback: StatsCallback | None = None,
    ) -> list[SharedFileRecord]:
        """Make sure every (entry, sha256) exists in the shared folder.

        Reuses present files (by deterministic name + size), uploads missing
        ones with one rclone ``copy`` batch, then verifies by re-listing.
        """
        async with _shared_upload_lock:
            children = await run_heavy(
                GoogleDriveService.list_children_with_hashes, shared_folder_id
            )
            by_name: dict[str, dict[str, Any]] = {}
            for child in children:
                name = str(child.get("name") or "")
                if name:
                    by_name.setdefault(name, child)

            missing: list[tuple[Any, str, str]] = []
            for entry, sha256 in externalized:
                name = cls.shared_name(sha256, entry.source_path.name)
                present = by_name.get(name)
                if present is not None:
                    expected_size = entry.source_path.stat().st_size
                    if int(present.get("size") or -1) == expected_size:
                        continue
                    # Torn/mismatched upload: replace it.
                    logger.warning(
                        "Shared source %s has wrong size on Drive "
                        "(expected %d, got %s); re-uploading",
                        name,
                        expected_size,
                        present.get("size"),
                    )
                    await run_heavy(
                        GoogleDriveService.delete_file, str(present.get("id") or "")
                    )
                missing.append((entry, sha256, name))

            if missing:
                stage_dir = Path(
                    tempfile.mkdtemp(
                        prefix="atr-shared-sources-", dir=str(settings.cache_dir)
                    )
                )
                try:

                    def _stage() -> None:
                        for entry, _sha, name in missing:
                            (stage_dir / name).symlink_to(
                                Path(entry.source_path).resolve()
                            )

                    await run_heavy(_stage)
                    await GoogleDriveRclone.copy_tree(
                        stage_dir,
                        folder_id=shared_folder_id,
                        stats_callback=stats_callback,
                    )
                finally:
                    await run_heavy(shutil.rmtree, stage_dir, True)

            # Verify-after: one re-list settles uploads AND concurrent-writer
            # duplicates (keep the lexicographically-first id, drop extras).
            children = await run_heavy(
                GoogleDriveService.list_children_with_hashes, shared_folder_id
            )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for child in children:
                name = str(child.get("name") or "")
                if name:
                    grouped.setdefault(name, []).append(child)

            records: list[SharedFileRecord] = []
            for entry, sha256 in externalized:
                name = cls.shared_name(sha256, entry.source_path.name)
                candidates = sorted(
                    grouped.get(name, []), key=lambda item: str(item.get("id") or "")
                )
                if not candidates:
                    raise RuntimeError(
                        f"Shared source upload verification failed: {name} "
                        "is missing from the shared Drive folder after upload"
                    )
                keeper = candidates[0]
                for extra in candidates[1:]:
                    logger.warning(
                        "Removing duplicate shared source %s (id %s)",
                        name,
                        extra.get("id"),
                    )
                    await run_heavy(
                        GoogleDriveService.delete_file, str(extra.get("id") or "")
                    )
                expected_size = entry.source_path.stat().st_size
                if int(keeper.get("size") or -1) != expected_size:
                    raise RuntimeError(
                        f"Shared source upload verification failed: {name} has "
                        f"size {keeper.get('size')} on Drive, expected {expected_size}"
                    )
                records.append(
                    SharedFileRecord(
                        path_in_folder=cls.path_in_folder(entry),
                        size=expected_size,
                        sha256=sha256,
                        md5=str(keeper.get("md5Checksum") or "") or None,
                        drive_file_id=str(keeper.get("id") or ""),
                        shared_name=name,
                    )
                )
            return records

    # ------------------------------------------------------------------ #
    # Manifests                                                          #
    # ------------------------------------------------------------------ #

    @classmethod
    def build_remote_manifest(
        cls,
        project: Project,
        records: list[SharedFileRecord],
        *,
        shared_folder_id: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "project_id": project.id,
            "shared_folder_id": shared_folder_id,
            "min_bytes": int(settings.drive_shared_sources_min_bytes),
            "files": [record.to_dict() for record in records],
        }

    @classmethod
    def _local_manifest_path(cls, project_id: str) -> Path:
        return ProjectService.get_project_dir(project_id) / LOCAL_MANIFEST_FILENAME

    @classmethod
    def persist_local_manifest(
        cls,
        project_id: str,
        *,
        status: str,
        records: list[SharedFileRecord],
        drive_folder_id: str | None = None,
    ) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "status": status,
            "updated_at": _utc_now_iso(),
            "drive_folder_id": drive_folder_id,
            "shared_files": [record.to_dict() for record in records],
        }
        manifest_path = cls._local_manifest_path(project_id)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8"
        )
        tmp_path.replace(manifest_path)

    @classmethod
    def load_local_manifest(cls, project_id: str) -> dict[str, Any] | None:
        manifest_path = cls._local_manifest_path(project_id)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def iter_local_manifests(cls) -> Iterator[tuple[str, dict[str, Any] | None]]:
        """(project_id, manifest|None) for every project dir carrying one.

        ``None`` signals an unreadable manifest — callers must treat that as
        "unknown references" and act conservatively.
        """
        for manifest_path in sorted(
            settings.projects_dir.glob(f"*/{LOCAL_MANIFEST_FILENAME}")
        ):
            project_id = manifest_path.parent.name
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                yield project_id, None
                continue
            yield project_id, payload if isinstance(payload, dict) else None

    @staticmethod
    def _manifest_shared_names(manifest: dict[str, Any]) -> set[str]:
        names: set[str] = set()
        for item in manifest.get("shared_files") or []:
            if isinstance(item, dict) and item.get("shared_name"):
                names.add(str(item["shared_name"]))
        return names

    # ------------------------------------------------------------------ #
    # GC + audit                                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    def collect_garbage(
        cls, released: dict[str, Any], *, exclude_project_id: str
    ) -> dict[str, Any]:
        """Delete shared files only the torn-down project referenced.

        Conservative by construction: any unreadable live manifest aborts the
        sweep, and only ``{16 hex}__``-named files are ever deleted.
        """
        result: dict[str, Any] = {"deleted": [], "kept": [], "warnings": []}
        released_names = cls._manifest_shared_names(released)
        if not released_names:
            return result

        referenced: set[str] = set()
        for project_id, manifest in cls.iter_local_manifests():
            if project_id == exclude_project_id:
                continue
            if manifest is None:
                result["warnings"].append(
                    f"Unreadable {LOCAL_MANIFEST_FILENAME} for project "
                    f"{project_id}; aborting shared-source GC"
                )
                result["kept"] = sorted(released_names)
                return result
            referenced |= cls._manifest_shared_names(manifest)

        candidates = sorted(released_names - referenced)
        result["kept"] = sorted(released_names & referenced)
        if not candidates:
            return result

        shared_folder_id = cls.ensure_shared_folder()
        children = GoogleDriveService.list_children_with_hashes(shared_folder_id)
        by_name: dict[str, list[dict[str, Any]]] = {}
        for child in children:
            name = str(child.get("name") or "")
            if name:
                by_name.setdefault(name, []).append(child)

        for name in candidates:
            if not SHARED_NAME_RE.match(name):
                result["warnings"].append(
                    f"Refusing to GC unexpected shared-source name: {name}"
                )
                continue
            for child in by_name.get(name, []):
                file_id = str(child.get("id") or "")
                if not file_id:
                    continue
                GoogleDriveService.delete_file(file_id)
                result["deleted"].append(name)
        return result

    @classmethod
    def audit(cls, *, apply: bool = False) -> dict[str, Any]:
        """Reconcile the shared Drive folder against all live manifests."""
        referenced: dict[str, list[str]] = {}
        unreadable: list[str] = []
        for project_id, manifest in cls.iter_local_manifests():
            if manifest is None:
                unreadable.append(project_id)
                continue
            for name in cls._manifest_shared_names(manifest):
                referenced.setdefault(name, []).append(project_id)

        shared_folder_id = cls.ensure_shared_folder()
        children = GoogleDriveService.list_children_with_hashes(shared_folder_id)
        on_drive: dict[str, dict[str, Any]] = {}
        for child in children:
            name = str(child.get("name") or "")
            if name:
                on_drive.setdefault(name, child)

        orphans = sorted(
            name
            for name in on_drive
            if name not in referenced and SHARED_NAME_RE.match(name)
        )
        missing = sorted(
            {
                name: projects
                for name, projects in referenced.items()
                if name not in on_drive
            }.items()
        )

        deleted: list[str] = []
        if apply and not unreadable:
            for name in orphans:
                child = on_drive[name]
                file_id = str(child.get("id") or "")
                if file_id:
                    GoogleDriveService.delete_file(file_id)
                    deleted.append(name)

        return {
            "shared_folder_id": shared_folder_id,
            "drive_file_count": len(on_drive),
            "referenced_count": len(referenced),
            "orphans": orphans,
            "missing": [
                {"shared_name": name, "projects": projects}
                for name, projects in missing
            ],
            "unreadable_manifests": unreadable,
            "deleted": deleted,
            "applied": bool(apply and not unreadable),
        }
