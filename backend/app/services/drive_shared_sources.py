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

Concurrency: per-shared-name asyncio locks (see ``_acquire_names``) make sure
the same bytes are never uploaded twice, and an in-flight table lets a job
waiting on someone else's upload relay that upload's progress to its user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ..config import settings
from ..models import Project
from .executors import run_heavy
from .google_drive_rclone import GoogleDriveRclone, RestartCallback
from .google_drive_service import GoogleDriveService
from .project_service import ProjectService
from .rclone_runner import RcloneStats, StatsCallback

logger = logging.getLogger("uvicorn.error")

SHARED_FOLDER_NAME = "_SPM_SHARED_SOURCES"
REMOTE_MANIFEST_FILENAME = "atr_remote_sources.json"
LOCAL_MANIFEST_FILENAME = "drive_export_manifest.json"
SCHEMA_VERSION = 1
# Only names of this exact shape are ever deleted by GC/audit — a guard
# against wiping anything a human (or another tool) put in the folder.
SHARED_NAME_RE = re.compile(r"^[0-9a-f]{16}__.+")
# While a job waits for a name another job holds, its ``on_wait`` callback is
# fed the holder's progress at this cadence (keeps the export's SSE stream —
# and the page's stall watchdog — alive during a long pre-warm upload).
WAIT_HEARTBEAT_SECONDS = 1.0
_MIB = 1024 * 1024

# One lock per shared name (``{sha16}__basename``). A job takes the locks of
# every name it needs (sorted order → deadlock-free) while it lists the folder
# and decides reuse vs upload; names already on Drive are released right
# there, names it uploads stay locked through upload + verify. So two jobs
# wanting the same missing bytes — duplicate projects exported at once, or an
# export starting while the script-phase pre-warm is still uploading —
# serialize on exactly those names (the first uploads, the second re-lists
# and reuses), while a job that merely *reuses* a file never queues behind
# someone else's upload of a different one.
_shared_name_locks: dict[str, asyncio.Lock] = {}
# Progress of the files being uploaded right now, keyed by shared name, so a
# waiting job can show its user what it is waiting for.
_inflight_uploads: dict[str, "InflightUpload"] = {}
_shared_folder_id_cache: str | None = None


@dataclass
class InflightUpload:
    """A shared file some job is uploading right now."""

    shared_name: str
    owner: str  # human-readable job label, e.g. "the script-phase pre-warm of project X"
    size: int
    bytes_done: int = 0
    speed_bytes_per_sec: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def basename(self) -> str:
        return self.shared_name.split("__", 1)[1] if "__" in self.shared_name else self.shared_name

    @property
    def fraction(self) -> float:
        if self.size <= 0:
            return 0.0
        return max(0.0, min(1.0, self.bytes_done / self.size))

    @property
    def eta_seconds(self) -> float | None:
        if self.size <= 0 or self.speed_bytes_per_sec <= 0:
            return None
        return max(0.0, (self.size - self.bytes_done) / self.speed_bytes_per_sec)


WaitCallback = Callable[[list[InflightUpload]], None]


def _name_lock(name: str) -> asyncio.Lock:
    lock = _shared_name_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _shared_name_locks[name] = lock
    return lock


def _inflight_for(names: Iterable[str]) -> list[InflightUpload]:
    return [_inflight_uploads[name] for name in names if name in _inflight_uploads]


async def _acquire_names(
    names: Iterable[str],
    *,
    on_wait: WaitCallback | None,
    heartbeat_seconds: float,
) -> list[str]:
    """Take the locks of ``names`` in sorted order; return the held names.

    While a lock is held by another job, ``on_wait`` is called at once and
    then every ``heartbeat_seconds`` with the in-flight uploads among the
    names still to acquire. Nothing stays held if this raises.
    """
    ordered = sorted(set(names))
    held: list[str] = []
    try:
        for index, name in enumerate(ordered):
            lock = _name_lock(name)
            if on_wait is not None and lock.locked():
                on_wait(_inflight_for(ordered[index:]))
            while True:
                try:
                    await asyncio.wait_for(
                        lock.acquire(),
                        timeout=heartbeat_seconds if on_wait is not None else None,
                    )
                    break
                except asyncio.TimeoutError:
                    on_wait(_inflight_for(ordered[index:]))  # type: ignore[misc]
            held.append(name)
    except BaseException:
        _release_names(held)
        raise
    return held


def _release_names(names: Iterable[str]) -> None:
    """Release locks the caller holds (callers only pass names they acquired)."""
    for name in names:
        lock = _shared_name_locks.get(name)
        if lock is not None and lock.locked():
            lock.release()


def _format_rate(bytes_per_sec: float) -> str:
    return f"{bytes_per_sec / _MIB:.1f} MB/s"


def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rem:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


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

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> "SharedFileRecord":
        return cls(
            path_in_folder=str(item.get("path") or ""),
            size=int(item.get("size") or 0),
            sha256=str(item.get("sha256") or ""),
            md5=str(item.get("md5") or "") or None,
            drive_file_id=str(item.get("drive_file_id") or ""),
            shared_name=str(item.get("shared_name") or ""),
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DriveSharedSources:
    @classmethod
    def is_enabled(cls) -> bool:
        return bool(settings.drive_shared_sources_enabled)

    @staticmethod
    def shared_name(sha256: str, basename: str) -> str:
        return f"{sha256[:16].lower()}__{basename}"

    @staticmethod
    def names_in_flight(names: Iterable[str]) -> list[str]:
        """Shared names currently locked by another upload job."""
        return sorted(
            name
            for name in set(names)
            if (lock := _shared_name_locks.get(name)) is not None and lock.locked()
        )

    @staticmethod
    def inflight_uploads(names: Iterable[str]) -> list[InflightUpload]:
        """Progress of the given shared names' uploads, if any is in flight."""
        return _inflight_for(sorted(set(names)))

    @staticmethod
    def describe_wait(inflight: list[InflightUpload]) -> str:
        """Human-readable line for a job waiting on other jobs' shared names."""
        if not inflight:
            return "Waiting for another job to finish checking the shared Drive folder..."
        first = inflight[0]
        line = (
            f"Waiting for {first.basename} — being uploaded by {first.owner}: "
            f"{int(first.fraction * 100)}% at {_format_rate(first.speed_bytes_per_sec)}"
        )
        eta = first.eta_seconds
        if eta is not None:
            line += f", ~{_format_duration(eta)} left"
        if len(inflight) > 1:
            line += f" (+{len(inflight) - 1} more)"
        return line

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
        on_wait: WaitCallback | None = None,
        on_plan: Callable[[int, int], None] | None = None,
        on_restart: RestartCallback | None = None,
        owner: str = "another Drive upload",
    ) -> list[SharedFileRecord]:
        """Make sure every (entry, sha256) exists in the shared folder.

        Reuses present files (by deterministic name + size), uploads missing
        ones with one rclone ``copy`` batch, then verifies by re-listing.

        Lock discipline: every wanted name is locked while the folder is
        listed and the reuse/upload split decided; names already on Drive are
        released right away, names being uploaded stay locked through
        upload + verify. ``on_wait(inflight)`` fires (and keeps firing every
        :data:`WAIT_HEARTBEAT_SECONDS`) while another job holds a wanted
        name, with that job's progress; ``on_plan(reused, missing)`` once the
        split is known; ``on_restart(reason)`` when a throttled upload session
        is abandoned for a fresh one. ``owner`` labels this job for waiters.
        """
        wanted: list[tuple[Any, str, str]] = [
            (entry, sha256, cls.shared_name(sha256, entry.source_path.name))
            for entry, sha256 in externalized
        ]
        held = await _acquire_names(
            (name for _, _, name in wanted),
            on_wait=on_wait,
            heartbeat_seconds=WAIT_HEARTBEAT_SECONDS,
        )
        try:
            grouped = await cls._list_grouped(shared_folder_id)
            records: dict[str, SharedFileRecord] = {}
            missing: list[tuple[Any, str, str]] = []
            for entry, sha256, name in wanted:
                if name in records:
                    continue
                expected_size = entry.source_path.stat().st_size
                keeper = await cls._settle(
                    name, grouped.get(name, []), expected_size, after_upload=False
                )
                if keeper is None:
                    missing.append((entry, sha256, name))
                    continue
                records[name] = cls._record(entry, sha256, name, keeper, expected_size)

            # Files already on Drive need no protection any more: a job that
            # only reuses them (an export whose pre-warm already landed) must
            # not queue behind this job's upload of something else.
            _release_names(name for name in held if name in records)
            held = [name for name in held if name not in records]

            if on_plan is not None:
                on_plan(len(records), len(missing))

            if missing:
                await cls._upload_missing(
                    missing,
                    shared_folder_id=shared_folder_id,
                    stats_callback=stats_callback,
                    on_restart=on_restart,
                    owner=owner,
                )
                # Verify-after: one re-list settles uploads AND concurrent-writer
                # duplicates (keep the lexicographically-first id, drop extras).
                grouped = await cls._list_grouped(shared_folder_id)
                for entry, sha256, name in missing:
                    expected_size = entry.source_path.stat().st_size
                    keeper = await cls._settle(
                        name, grouped.get(name, []), expected_size, after_upload=True
                    )
                    if keeper is None:
                        raise RuntimeError(
                            f"Shared source upload verification failed: {name} "
                            "is missing from the shared Drive folder after upload"
                        )
                    records[name] = cls._record(entry, sha256, name, keeper, expected_size)
            return [records[name] for _, _, name in wanted]
        finally:
            _release_names(held)

    @classmethod
    async def _list_grouped(cls, shared_folder_id: str) -> dict[str, list[dict[str, Any]]]:
        children = await run_heavy(
            GoogleDriveService.list_children_with_hashes, shared_folder_id
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for child in children:
            name = str(child.get("name") or "")
            if name:
                grouped.setdefault(name, []).append(child)
        return grouped

    @classmethod
    async def _settle(
        cls,
        name: str,
        candidates: list[dict[str, Any]],
        expected_size: int,
        *,
        after_upload: bool,
    ) -> dict[str, Any] | None:
        """Pick the file to reference for ``name`` (caller holds its lock).

        Duplicates from concurrent writers are dropped (first id wins). A
        size mismatch means a torn upload: before our own upload it is
        deleted and ``None`` returned so the file gets re-uploaded; after it,
        it is an error.
        """
        ordered = sorted(candidates, key=lambda item: str(item.get("id") or ""))
        if not ordered:
            return None
        keeper = ordered[0]
        for extra in ordered[1:]:
            logger.warning(
                "Removing duplicate shared source %s (id %s)", name, extra.get("id")
            )
            await run_heavy(GoogleDriveService.delete_file, str(extra.get("id") or ""))
        if int(keeper.get("size") or -1) == expected_size:
            return keeper
        if after_upload:
            raise RuntimeError(
                f"Shared source upload verification failed: {name} has "
                f"size {keeper.get('size')} on Drive, expected {expected_size}"
            )
        logger.warning(
            "Shared source %s has wrong size on Drive (expected %d, got %s); re-uploading",
            name,
            expected_size,
            keeper.get("size"),
        )
        await run_heavy(GoogleDriveService.delete_file, str(keeper.get("id") or ""))
        return None

    @classmethod
    def _record(
        cls,
        entry: Any,
        sha256: str,
        name: str,
        child: dict[str, Any],
        size: int,
    ) -> SharedFileRecord:
        return SharedFileRecord(
            path_in_folder=cls.path_in_folder(entry),
            size=size,
            sha256=sha256,
            md5=str(child.get("md5Checksum") or "") or None,
            drive_file_id=str(child.get("id") or ""),
            shared_name=name,
        )

    @classmethod
    async def _upload_missing(
        cls,
        missing: list[tuple[Any, str, str]],
        *,
        shared_folder_id: str,
        stats_callback: StatsCallback | None,
        on_restart: RestartCallback | None,
        owner: str,
    ) -> None:
        """One rclone copy batch of ``missing`` (caller holds their locks).

        The batch is registered in the in-flight table for its whole life so
        jobs waiting on these names can relay real progress to their users.
        """
        inflight = {
            name: InflightUpload(
                shared_name=name, owner=owner, size=entry.source_path.stat().st_size
            )
            for entry, _sha, name in missing
        }
        _inflight_uploads.update(inflight)
        stage_dir = Path(
            tempfile.mkdtemp(prefix="atr-shared-sources-", dir=str(settings.cache_dir))
        )

        def _stage() -> None:
            for entry, _sha, name in missing:
                (stage_dir / name).symlink_to(Path(entry.source_path).resolve())

        def _relay(stats: RcloneStats):
            active = {item.name: item for item in stats.transferring}
            for name, upload in inflight.items():
                item = active.get(name)
                if item is not None:
                    upload.bytes_done = item.bytes_done
                    upload.speed_bytes_per_sec = item.speed_bytes_per_sec
            return stats_callback(stats) if stats_callback is not None else None

        try:
            await run_heavy(_stage)
            await GoogleDriveRclone.copy_tree(
                stage_dir,
                folder_id=shared_folder_id,
                stats_callback=_relay,
                on_restart=on_restart,
            )
        finally:
            for name in inflight:
                if _inflight_uploads.get(name) is inflight[name]:
                    _inflight_uploads.pop(name, None)
            await run_heavy(shutil.rmtree, stage_dir, True)

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

    @classmethod
    def merge_records(
        cls, manifest: dict[str, Any] | None, records: list[SharedFileRecord]
    ) -> list[SharedFileRecord]:
        """Union of a manifest's records and ``records`` (new wins per name).

        Used by the pre-warm so its writes never drop references another
        writer (an in-flight export) has already put on disk.
        """
        merged: dict[str, SharedFileRecord] = {}
        for item in (manifest or {}).get("shared_files") or []:
            if isinstance(item, dict) and item.get("shared_name"):
                record = SharedFileRecord.from_dict(item)
                merged[record.shared_name] = record
        for record in records:
            merged[record.shared_name] = record
        return [merged[name] for name in sorted(merged)]

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
