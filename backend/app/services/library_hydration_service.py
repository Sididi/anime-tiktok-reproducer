from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

from .executors import run_heavy
from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from .anime_library import AnimeLibraryService
from .library_state_db import LibraryStateDb, OperationRow, SeriesStateRow
from .pending_publish_store import PendingPublishRecord, PendingPublishStore
from .project_service import ProjectService
from .storage_box_progress import ProgressCallback, ProgressSnapshot
from .storage_box_rclone import StorageBoxRclone
from .storage_box_repository import HashingProgressCallback, StorageBoxRepository
from .storage_box_sftp_client import StorageBoxSftpClient


logger = logging.getLogger("uvicorn.error")


HYDRATION_STATUS_NOT_HYDRATED = "not_hydrated"
HYDRATION_STATUS_HYDRATING_INDEX = "hydrating_index"
HYDRATION_STATUS_INDEX_READY = "index_ready"
HYDRATION_STATUS_HYDRATING_EPISODES = "hydrating_episodes"
HYDRATION_STATUS_FULLY_LOCAL = "fully_local"
HYDRATION_STATUS_ERROR = "error"

OPERATION_PENDING = "pending"
OPERATION_RUNNING = "running"
OPERATION_COMPLETE = "complete"
OPERATION_ERROR = "error"

OPERATION_TYPE_PUBLISH = "publish"


class SeriesDeleteBlockedError(RuntimeError):
    def __init__(
        self,
        *,
        library_type: LibraryType | str,
        series_id: str,
        referencing_projects: list[dict[str, Any]],
    ) -> None:
        self.library_type = coerce_library_type(library_type)
        self.series_id = series_id
        self.referencing_projects = referencing_projects
        super().__init__(
            "Cette source est encore utilisee par un ou plusieurs projets enregistres."
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": "series_delete_blocked",
            "message": str(self),
            "referencing_projects": self.referencing_projects,
        }


class SeriesRenameConflictError(RuntimeError):
    pass


def _json_load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid JSON object: {path}")
    return payload


def _json_write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


ActivationProgressCallback = Callable[[float, str], Awaitable[None] | None]


async def _call_progress_callback(callback, *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


async def _sha256_files_parallel(
    paths: list[Path], *, max_workers: int = 4
) -> dict[Path, str]:
    """Hash all files on a small thread pool (post-download verification)."""
    if not paths:
        return {}

    def _hash_all() -> dict[Path, str]:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return dict(zip(paths, pool.map(_sha256_file, paths)))

    return await run_heavy(_hash_all)


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"  # pragma: no cover - unreachable


def _network_detail(snapshot: ProgressSnapshot) -> dict[str, Any]:
    """Operation-row payload matching the frontend network_* field names."""
    return {
        "network_bytes_transferred": snapshot.bytes_transferred,
        "network_bytes_total": snapshot.bytes_total,
        "network_mib_per_sec": snapshot.mib_per_sec,
        "network_eta_seconds": snapshot.eta_seconds,
        "network_active_transfers": snapshot.active_transfers,
    }


@dataclass(frozen=True)
class _EpisodeDownloadItem:
    remote_relative: PurePosixPath
    final_target: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class _EpisodeDownloadPlan:
    episode_key: str
    items: tuple[_EpisodeDownloadItem, ...]


class LibraryHydrationService:
    """Owns activation, local matcher cache materialization, hydration, and eviction."""

    _series_locks: dict[tuple[str, str], asyncio.Lock] = {}
    _library_locks: dict[str, asyncio.Lock] = {}
    _background_tasks: set[asyncio.Task[Any]] = set()
    # How stale the catalog may get before a startup warmup re-derives it from
    # the remote series tree. See :meth:`reconcile_catalog`.
    _catalog_reconcile_interval_hours: float = 12.0

    @classmethod
    def _storage_cache_root(cls) -> Path:
        return settings.cache_dir / "storage_box"

    @classmethod
    def _temp_root(cls) -> Path:
        return cls._storage_cache_root() / "tmp"

    @classmethod
    def _manifest_cache_path(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str,
    ) -> Path:
        scoped_type = coerce_library_type(library_type).value
        return cls._storage_cache_root() / "manifests" / scoped_type / series_id / f"{release_id}.json"

    @classmethod
    def _series_lock(cls, library_type: LibraryType | str, series_id: str) -> asyncio.Lock:
        key = (coerce_library_type(library_type).value, series_id)
        lock = cls._series_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._series_locks[key] = lock
        return lock

    @classmethod
    def _library_lock(cls, library_type: LibraryType | str) -> asyncio.Lock:
        key = coerce_library_type(library_type).value
        lock = cls._library_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            cls._library_locks[key] = lock
        return lock

    @classmethod
    def _spawn_background_task(
        cls,
        coroutine: Awaitable[Any],
        *,
        description: str,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=description)
        cls._background_tasks.add(task)

        def _cleanup(completed: asyncio.Task[Any]) -> None:
            cls._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Background library operation failed: %s", description)

        task.add_done_callback(_cleanup)
        return task

    @staticmethod
    def _operation_is_active(operation: OperationRow | None) -> bool:
        return bool(operation and operation.status in {OPERATION_PENDING, OPERATION_RUNNING})

    @classmethod
    def _select_operation_from_rows(
        cls,
        operations: list[OperationRow],
        preferred_types: tuple[str, ...],
    ) -> OperationRow | None:
        by_type = {operation.operation_type: operation for operation in operations}
        for operation_type in preferred_types:
            operation = by_type.get(operation_type)
            if cls._operation_is_active(operation):
                return operation

        active_operations = [
            operation for operation in operations if cls._operation_is_active(operation)
        ]
        if active_operations:
            return max(active_operations, key=lambda operation: operation.updated_at)

        for operation_type in preferred_types:
            operation = by_type.get(operation_type)
            if operation is not None:
                return operation

        return operations[0] if operations else None

    @classmethod
    async def _selected_operation(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        preferred_types: tuple[str, ...],
    ) -> OperationRow | None:
        operations = await asyncio.to_thread(
            LibraryStateDb.list_operations,
            library_type=library_type,
            series_id=series_id,
        )
        return cls._select_operation_from_rows(operations, preferred_types)

    @classmethod
    async def _describe_state(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        preferred_operation_types: tuple[str, ...],
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        state = await asyncio.to_thread(LibraryStateDb.get_series_state, scoped_type, series_id)
        operation = await cls._selected_operation(
            library_type=scoped_type,
            series_id=series_id,
            preferred_types=preferred_operation_types,
        )
        project_pin_count = await asyncio.to_thread(LibraryStateDb.count_project_pins, series_id)
        return cls._state_payload(
            series_state=state,
            operation=operation,
            project_pin_count=project_pin_count,
        )

    @staticmethod
    def _normalize_episode_reference(value: Any) -> str:
        return str(value or "").strip().replace("\\", "/").casefold()

    @classmethod
    def _episode_matches_reference(
        cls,
        library_type: LibraryType | str,
        episode: dict[str, Any],
        reference: str,
    ) -> bool:
        normalized_reference = cls._normalize_episode_reference(reference)
        if not normalized_reference:
            return False

        media = episode.get("media", {})
        local_relative_path = str(media.get("local_relative_path") or "").strip() if isinstance(media, dict) else ""
        episode_key = str(episode.get("episode_key") or "").strip()
        library_root = AnimeLibraryService.get_library_path(library_type)

        candidates: set[str] = set()
        for candidate in (episode_key, local_relative_path):
            normalized_candidate = cls._normalize_episode_reference(candidate)
            if normalized_candidate:
                candidates.add(normalized_candidate)
                path_candidate = Path(candidate)
                candidates.add(cls._normalize_episode_reference(path_candidate.name))
                candidates.add(cls._normalize_episode_reference(path_candidate.stem))

        if local_relative_path:
            local_path = library_root / local_relative_path
            candidates.add(cls._normalize_episode_reference(local_path))
            candidates.add(cls._normalize_episode_reference(local_path.resolve(strict=False)))

        return normalized_reference in candidates

    @classmethod
    async def startup_cleanup(cls) -> None:
        await asyncio.to_thread(LibraryStateDb.mark_incomplete_operations_interrupted)
        temp_root = cls._temp_root()
        if temp_root.exists():
            await asyncio.to_thread(shutil.rmtree, temp_root, True)
        temp_root.mkdir(parents=True, exist_ok=True)

        # StorageBoxRepository.publish_series / rename_series stage artifacts
        # under settings.cache_dir/storage_box_release_* and storage_box_rename_*
        # tempdirs. Those are removed in a `finally` block on normal exit, but
        # SIGKILL or a crashed reload leaves them behind. Sweep them on
        # startup so the cache doesn't grow unbounded across crashes.
        cache_root = settings.cache_dir
        if cache_root.exists():
            for prefix in ("storage_box_release_", "storage_box_rename_"):
                for stale in cache_root.glob(f"{prefix}*"):
                    if stale.is_dir():
                        await asyncio.to_thread(shutil.rmtree, stale, True)

        # Reconcile durable pending publishes. Their staged index dirs live
        # outside the swept globs on purpose (uploads must resume across
        # restarts); here we only drop what can no longer be uploaded.
        records = await asyncio.to_thread(PendingPublishStore.list_all)
        known_publish_ids = {record.publish_id for record in records}
        pending_staging_root = PendingPublishStore.staging_dir_root()
        if pending_staging_root.exists():
            for entry in pending_staging_root.iterdir():
                if entry.is_dir() and entry.name not in known_publish_ids:
                    await asyncio.to_thread(shutil.rmtree, entry, True)
        for record in records:
            if not Path(record.series_dir).exists():
                # The series was removed locally; the pending publish is moot.
                logger.info(
                    "Dropping pending publish %s: local series dir %s is gone",
                    record.publish_id,
                    record.series_dir,
                )
                await asyncio.to_thread(PendingPublishStore.delete, record.publish_id)
            elif not Path(record.staged_index_dir).exists():
                record.last_error = (
                    "Staged index artifacts are missing (cache directory "
                    "wiped?). Re-index the series to rebuild the release."
                )
                await asyncio.to_thread(PendingPublishStore.save, record)

    @classmethod
    async def reconcile_catalog(cls, library_type: LibraryType | str) -> None:
        """Ensure the catalog exists, and periodically re-derive it from the
        remote series tree.

        Publishing a series updates its catalog entry in place, which is one
        read plus one write instead of a full rescan — but a read-modify-write
        of a file shared by several backends has no compare-and-swap. Two
        publishes overlapping from different machines can lose one entry, and
        an in-place update can never notice, because it never looks at the
        series tree. A rebuild derives from that tree (ground truth) and heals
        the drift.

        Paying it on every publish is what made publishing slow; paying it
        never is what makes drift permanent. So it runs here, bounded by
        ``_catalog_reconcile_interval_hours``, off a marker stored in the
        catalog itself — which means the two machines share the schedule
        rather than each rebuilding on their own restarts.
        """
        if not StorageBoxRepository.is_enabled():
            return

        if not await cls._catalog_reconcile_is_due(library_type):
            return

        await StorageBoxRepository.rebuild_catalog(library_type)

    @classmethod
    async def _catalog_reconcile_is_due(cls, library_type: LibraryType | str) -> bool:
        scoped_type = coerce_library_type(library_type)
        try:
            payload = await StorageBoxRepository._read_remote_json(
                StorageBoxRepository._catalog_path(scoped_type),
                context=f"{scoped_type.value} catalog",
            )
        except Exception:
            # Missing or unreadable: the rebuild is the repair path.
            return True

        raw_marker = str(payload.get("reconciled_at") or "").strip()
        if not raw_marker:
            # Catalog predates the marker (or was only ever upserted).
            return True
        try:
            reconciled_at = datetime.fromisoformat(raw_marker)
        except ValueError:
            logger.warning(
                "Storage Box %s catalog has an unparsable reconciled_at (%r); "
                "reconciling",
                scoped_type.value,
                raw_marker,
            )
            return True
        if reconciled_at.tzinfo is None:
            reconciled_at = reconciled_at.replace(tzinfo=timezone.utc)

        age_hours = (
            datetime.now(timezone.utc) - reconciled_at
        ).total_seconds() / 3600.0
        return age_hours >= cls._catalog_reconcile_interval_hours

    @classmethod
    async def get_activation_state(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        return await cls._describe_state(
            library_type=library_type,
            series_id=series_id,
            preferred_operation_types=("activate", "hydrate", "evict"),
        )

    @classmethod
    async def ensure_index_ready(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> bool:
        state = await asyncio.to_thread(
            LibraryStateDb.get_series_state,
            library_type,
            series_id,
        )
        if not state or not state.release_id:
            return False
        if state.hydration_status not in {
            HYDRATION_STATUS_INDEX_READY,
            HYDRATION_STATUS_HYDRATING_EPISODES,
            HYDRATION_STATUS_FULLY_LOCAL,
        }:
            return False

        cached_path = cls._manifest_cache_path(library_type, series_id, state.release_id)
        if not cached_path.exists():
            return False

        try:
            manifest = await asyncio.to_thread(_json_load, cached_path)
        except Exception:
            return False

        return await asyncio.to_thread(
            cls._local_index_ready_for_manifest_sync,
            library_type,
            manifest,
        )

    @classmethod
    async def _resolve_release_manifest(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> tuple[dict[str, Any], PendingPublishRecord | None]:
        """Return the manifest that should drive activation/hydration.

        A series whose publish is still pending (finalized locally, upload
        queued or in flight) has no remote ``current.json`` yet: its durable
        pending record carries the authoritative manifest, and every artifact
        it references already lives in the local library. Only published
        series go to the Storage Box.
        """
        scoped_type = coerce_library_type(library_type)
        pending = await asyncio.to_thread(
            PendingPublishStore.find_by_series,
            scoped_type.value,
            series_id,
        )
        if pending is not None and pending.manifest:
            return dict(pending.manifest), pending
        current = await StorageBoxRepository.get_current_release(scoped_type, series_id)
        manifest = await StorageBoxRepository.get_series_manifest(
            scoped_type,
            series_id,
            str(current["release_id"]),
        )
        return manifest, None

    @staticmethod
    def _pending_release_unusable_error(pending: PendingPublishRecord) -> RuntimeError:
        return RuntimeError(
            f"Series '{pending.display_name}' has a pending publish "
            f"({pending.publish_id}) but its local matcher cache is missing or "
            "stale, and the release is not on the Storage Box yet. Re-index "
            "the series to rebuild it."
        )

    @classmethod
    async def ensure_series_index_hydrated(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        already_locked: bool = False,
        network_progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        lock_ctx = (
            nullcontext()
            if already_locked
            else cls._series_lock(scoped_type, series_id)
        )
        async with lock_ctx:
            manifest, pending = await cls._resolve_release_manifest(scoped_type, series_id)
            expected_episode_count = int(
                manifest.get("episode_count", len(manifest.get("episodes", [])))
            )
            state = await asyncio.to_thread(
                LibraryStateDb.get_series_state,
                scoped_type,
                series_id,
            )
            local_index_ready = await asyncio.to_thread(
                cls._local_index_ready_for_manifest_sync,
                scoped_type,
                manifest,
            )
            if pending is not None and not local_index_ready:
                # Nothing to download from: the release only exists locally.
                raise cls._pending_release_unusable_error(pending)
            if (
                state is not None
                and state.release_id == str(manifest["release_id"])
                and state.hydration_status
                in {HYDRATION_STATUS_INDEX_READY, HYDRATION_STATUS_FULLY_LOCAL}
                and local_index_ready
            ):
                await cls._cache_manifest(scoped_type, manifest)
                return manifest

            local_episode_count = await asyncio.to_thread(
                cls._count_local_episodes_from_manifest,
                scoped_type,
                manifest,
            )

            if local_index_ready:
                logger.info(
                    "Storage Box index hydration skipped for %s/%s; local matcher cache already matches release %s",
                    scoped_type.value,
                    series_id,
                    str(manifest["release_id"]),
                )
                hydration_status = (
                    HYDRATION_STATUS_FULLY_LOCAL
                    if expected_episode_count > 0 and local_episode_count >= expected_episode_count
                    else HYDRATION_STATUS_INDEX_READY
                )
                await cls._cache_manifest(scoped_type, manifest)
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest["release_id"]),
                    hydration_status=hydration_status,
                    local_episode_count=local_episode_count,
                    expected_episode_count=expected_episode_count,
                    last_error=None,
                )
                return manifest

            try:
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest["release_id"]),
                    hydration_status=HYDRATION_STATUS_HYDRATING_INDEX,
                    local_episode_count=local_episode_count,
                    expected_episode_count=expected_episode_count,
                    last_error=None,
                )
                await cls._cache_manifest(scoped_type, manifest)
                await cls._hydrate_index_artifacts(
                    scoped_type,
                    manifest,
                    network_progress_callback=network_progress_callback,
                )
                local_episode_count = await asyncio.to_thread(
                    cls._count_local_episodes_from_manifest,
                    scoped_type,
                    manifest,
                )
                hydration_status = (
                    HYDRATION_STATUS_FULLY_LOCAL
                    if expected_episode_count > 0 and local_episode_count >= expected_episode_count
                    else HYDRATION_STATUS_INDEX_READY
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest["release_id"]),
                    hydration_status=hydration_status,
                    local_episode_count=local_episode_count,
                    expected_episode_count=expected_episode_count,
                    last_error=None,
                )
                return manifest
            except Exception as exc:
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest.get("release_id") or ""),
                    hydration_status=HYDRATION_STATUS_ERROR,
                    local_episode_count=await asyncio.to_thread(
                        cls._count_local_episodes_from_manifest,
                        scoped_type,
                        manifest,
                    ),
                    expected_episode_count=expected_episode_count,
                    last_error=str(exc),
                )
                raise

    @classmethod
    async def activate_project_series(
        cls,
        *,
        project_id: str,
        library_type: LibraryType | str,
        series_id: str,
        progress_callback: ActivationProgressCallback | None = None,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        await asyncio.to_thread(LibraryStateDb.add_project_pin, project_id, series_id)
        async with cls._series_lock(scoped_type, series_id):
            await asyncio.to_thread(
                LibraryStateDb.upsert_operation,
                library_type=scoped_type,
                series_id=series_id,
                operation_type="activate",
                status=OPERATION_RUNNING,
                progress=0.0,
                error=None,
            )
            pending: PendingPublishRecord | None = None
            try:
                manifest, pending = await cls._resolve_release_manifest(scoped_type, series_id)
                expected_episode_count = int(manifest.get("episode_count", len(manifest.get("episodes", []))))
                await cls._cache_manifest(scoped_type, manifest)
                await _call_progress_callback(progress_callback, 0.05, "Loaded release manifest.")
                local_episode_count = await asyncio.to_thread(
                    cls._count_local_episodes_from_manifest,
                    scoped_type,
                    manifest,
                )
                local_index_ready = await asyncio.to_thread(
                    cls._local_index_ready_for_manifest_sync,
                    scoped_type,
                    manifest,
                )
                if pending is not None and not local_index_ready:
                    # The release is not on the Storage Box yet, so there is
                    # nothing to hydrate from; the local cache must be intact.
                    raise cls._pending_release_unusable_error(pending)

                if not local_index_ready:
                    await asyncio.to_thread(
                        LibraryStateDb.upsert_series_state,
                        library_type=scoped_type,
                        series_id=series_id,
                        release_id=str(manifest["release_id"]),
                        hydration_status=HYDRATION_STATUS_HYDRATING_INDEX,
                        local_episode_count=local_episode_count,
                        expected_episode_count=expected_episode_count,
                        last_error=None,
                    )
                    await asyncio.to_thread(
                        LibraryStateDb.upsert_operation,
                        library_type=scoped_type,
                        series_id=series_id,
                        operation_type="activate",
                        status=OPERATION_RUNNING,
                        progress=0.15,
                        error=None,
                    )
                    await _call_progress_callback(
                        progress_callback,
                        0.15,
                        "Hydrating matcher cache from Storage Box...",
                    )

                    async def _network_progress(snapshot: ProgressSnapshot) -> None:
                        ratio = (
                            snapshot.bytes_transferred / snapshot.bytes_total
                            if snapshot.bytes_total > 0
                            else 0.0
                        )
                        activation_progress = 0.15 + 0.75 * min(1.0, ratio)
                        await asyncio.to_thread(
                            LibraryStateDb.upsert_operation,
                            library_type=scoped_type,
                            series_id=series_id,
                            operation_type="activate",
                            status=OPERATION_RUNNING,
                            progress=activation_progress,
                            error=None,
                            detail=_network_detail(snapshot),
                        )
                        speed = (
                            f" · {(snapshot.mib_per_sec or 0.0) * 1.048576:.0f} MB/s"
                            if snapshot.mib_per_sec is not None
                            else ""
                        )
                        await _call_progress_callback(
                            progress_callback,
                            activation_progress,
                            (
                                "Downloading matcher cache — "
                                f"{_format_bytes(snapshot.bytes_transferred)} / "
                                f"{_format_bytes(snapshot.bytes_total)}{speed}"
                            ),
                        )

                    await cls._hydrate_index_artifacts(
                        scoped_type,
                        manifest,
                        network_progress_callback=_network_progress,
                    )
                    local_episode_count = await asyncio.to_thread(
                        cls._count_local_episodes_from_manifest,
                        scoped_type,
                        manifest,
                    )
                else:
                    logger.info(
                        "Storage Box activation skipped index download for %s/%s; local matcher cache already matches release %s",
                        scoped_type.value,
                        series_id,
                        str(manifest["release_id"]),
                    )
                    await _call_progress_callback(
                        progress_callback,
                        0.90,
                        "Matcher cache already ready locally.",
                    )

                hydration_status = (
                    HYDRATION_STATUS_FULLY_LOCAL
                    if expected_episode_count > 0 and local_episode_count >= expected_episode_count
                    else HYDRATION_STATUS_INDEX_READY
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest["release_id"]),
                    hydration_status=hydration_status,
                    local_episode_count=local_episode_count,
                    expected_episode_count=expected_episode_count,
                    last_error=None,
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=series_id,
                    operation_type="activate",
                    status=OPERATION_COMPLETE,
                    progress=1.0,
                    error=None,
                )
                await _call_progress_callback(
                    progress_callback,
                    1.0,
                    "Library activation complete.",
                )
                return await cls.get_activation_state(
                    library_type=scoped_type,
                    series_id=series_id,
                )
            except Exception as exc:
                if pending is None:
                    # Failed against the remote release: the local row is
                    # rebuilt by the next sync/activation. With a pending
                    # publish the row written at finalize time is the only
                    # thing keeping the series matchable — never blank it.
                    await asyncio.to_thread(
                        LibraryStateDb.upsert_series_state,
                        library_type=scoped_type,
                        series_id=series_id,
                        release_id=None,
                        hydration_status=HYDRATION_STATUS_ERROR,
                        local_episode_count=0,
                        expected_episode_count=0,
                        last_error=str(exc),
                    )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=series_id,
                    operation_type="activate",
                    status=OPERATION_ERROR,
                    progress=0.0,
                    error=str(exc),
                )
                raise

    @classmethod
    async def enqueue_project_activation(
        cls,
        *,
        project_id: str,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        await asyncio.to_thread(LibraryStateDb.add_project_pin, project_id, series_id)

        if await cls.ensure_index_ready(library_type=scoped_type, series_id=series_id):
            await asyncio.to_thread(
                LibraryStateDb.upsert_operation,
                library_type=scoped_type,
                series_id=series_id,
                operation_type="activate",
                status=OPERATION_COMPLETE,
                progress=1.0,
                error=None,
            )
            return await cls.get_activation_state(
                library_type=scoped_type,
                series_id=series_id,
            )

        active_operation = await cls._selected_operation(
            library_type=scoped_type,
            series_id=series_id,
            preferred_types=("activate", "hydrate", "evict"),
        )
        if cls._operation_is_active(active_operation):
            return await cls.get_activation_state(
                library_type=scoped_type,
                series_id=series_id,
            )

        await asyncio.to_thread(
            LibraryStateDb.upsert_operation,
            library_type=scoped_type,
            series_id=series_id,
            operation_type="activate",
            status=OPERATION_PENDING,
            progress=0.0,
            error=None,
        )
        cls._spawn_background_task(
            cls._run_background_activation(
                project_id=project_id,
                library_type=scoped_type,
                series_id=series_id,
            ),
            description=f"library-activate:{scoped_type.value}:{series_id}",
        )
        return await cls.get_activation_state(
            library_type=scoped_type,
            series_id=series_id,
        )

    @classmethod
    async def hydrate_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        episode_keys: list[str] | None = None,
        full_series: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        async with cls._series_lock(scoped_type, series_id):
            manifest = await cls._load_or_fetch_manifest(scoped_type, series_id)
            await asyncio.to_thread(
                LibraryStateDb.upsert_operation,
                library_type=scoped_type,
                series_id=series_id,
                operation_type="hydrate",
                status=OPERATION_RUNNING,
                progress=0.0,
                error=None,
            )
            await asyncio.to_thread(
                LibraryStateDb.upsert_series_state,
                library_type=scoped_type,
                series_id=series_id,
                release_id=str(manifest["release_id"]),
                hydration_status=HYDRATION_STATUS_HYDRATING_EPISODES,
                local_episode_count=await asyncio.to_thread(
                    cls._count_local_episodes_from_manifest,
                    scoped_type,
                    manifest,
                ),
                expected_episode_count=int(manifest.get("episode_count", len(manifest.get("episodes", [])))),
                last_error=None,
            )
            try:
                episodes = manifest.get("episodes", [])
                if not isinstance(episodes, list):
                    raise RuntimeError("Manifest episodes payload is invalid")
                target_keys = {key for key in (episode_keys or []) if key}
                if full_series or not target_keys:
                    selected_episodes = [entry for entry in episodes if isinstance(entry, dict)]
                else:
                    selected_episodes = [
                        entry
                        for entry in episodes
                        if isinstance(entry, dict)
                        and any(
                            cls._episode_matches_reference(scoped_type, entry, requested_key)
                            for requested_key in target_keys
                        )
                    ]

                plans = await asyncio.to_thread(
                    cls._plan_episode_downloads,
                    scoped_type,
                    manifest,
                    selected_episodes,
                )
                total_bytes = sum(
                    item.size_bytes for plan in plans for item in plan.items
                )
                release_root = StorageBoxRepository._release_root(
                    scoped_type,
                    series_id,
                    str(manifest["release_id"]),
                )
                batch_temp_root = (
                    cls._temp_root()
                    / scoped_type.value
                    / series_id
                    / "episodes-batch"
                    / uuid.uuid4().hex[:8]
                )

                last_db_write = 0.0

                async def _batch_progress(snapshot: ProgressSnapshot) -> None:
                    # Persist byte progress on the operation row (throttled)
                    # so /state pollers render bytes/speed, then forward the
                    # snapshot to the caller's own display (e.g. SSE).
                    nonlocal last_db_write
                    now = time.monotonic()
                    if now - last_db_write >= 1.0:
                        last_db_write = now
                        ratio = (
                            snapshot.bytes_transferred / snapshot.bytes_total
                            if snapshot.bytes_total > 0
                            else 0.0
                        )
                        await asyncio.to_thread(
                            LibraryStateDb.upsert_operation,
                            library_type=scoped_type,
                            series_id=series_id,
                            operation_type="hydrate",
                            status=OPERATION_RUNNING,
                            progress=0.05 + 0.9 * min(1.0, ratio),
                            error=None,
                            detail=_network_detail(snapshot),
                        )
                    await _call_progress_callback(progress_callback, snapshot)

                episode_errors: list[str] = []
                try:
                    if plans:
                        await StorageBoxRclone.download_batch(
                            [
                                item.remote_relative
                                for plan in plans
                                for item in plan.items
                            ],
                            remote_base=release_root,
                            dest_root=batch_temp_root,
                            total_bytes=total_bytes,
                            progress_callback=_batch_progress,
                        )
                        downloaded_paths = [
                            batch_temp_root / Path(*item.remote_relative.parts)
                            for plan in plans
                            for item in plan.items
                        ]
                        hashes = await _sha256_files_parallel(downloaded_paths)

                        def _verify_and_move_all() -> None:
                            for plan in plans:
                                try:
                                    cls._verify_and_move_episode(
                                        plan, batch_temp_root, hashes
                                    )
                                except Exception as exc:
                                    episode_errors.append(
                                        f"{plan.episode_key}: {exc}"
                                    )

                        await asyncio.to_thread(_verify_and_move_all)
                        display_name = str(manifest["display_name"])
                        await asyncio.to_thread(
                            StorageBoxRepository.write_local_series_metadata,
                            series_dir=AnimeLibraryService.get_library_path(
                                scoped_type
                            )
                            / display_name,
                            series_id=series_id,
                            display_name=display_name,
                            release_id=str(manifest["release_id"]),
                        )
                finally:
                    await asyncio.to_thread(shutil.rmtree, batch_temp_root, True)
                    # Newly hydrated episode files must become visible to
                    # AnimeLibraryService.resolve_episode_path consumers
                    # (processing pipeline, gap resolution, playback).
                    await AnimeLibraryService.ensure_episode_manifest(
                        force_refresh=True,
                        library_type=scoped_type,
                    )

                if episode_errors:
                    raise RuntimeError(
                        "Episode hydration failed for: " + "; ".join(episode_errors)
                    )

                local_episode_count = await asyncio.to_thread(
                    cls._count_local_episodes_from_manifest,
                    scoped_type,
                    manifest,
                )
                expected_episode_count = int(manifest.get("episode_count", len(episodes)))
                hydration_status = (
                    HYDRATION_STATUS_FULLY_LOCAL
                    if expected_episode_count > 0 and local_episode_count >= expected_episode_count
                    else HYDRATION_STATUS_INDEX_READY
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest["release_id"]),
                    hydration_status=hydration_status,
                    local_episode_count=local_episode_count,
                    expected_episode_count=expected_episode_count,
                    last_error=None,
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=series_id,
                    operation_type="hydrate",
                    status=OPERATION_COMPLETE,
                    progress=1.0,
                    error=None,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest.get("release_id") or ""),
                    hydration_status=HYDRATION_STATUS_ERROR,
                    local_episode_count=await asyncio.to_thread(
                        cls._count_local_episodes_from_manifest,
                        scoped_type,
                        manifest,
                    ),
                    expected_episode_count=int(manifest.get("episode_count", len(manifest.get("episodes", [])))),
                    last_error=str(exc),
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=series_id,
                    operation_type="hydrate",
                    status=OPERATION_ERROR,
                    progress=0.0,
                    error=str(exc),
                )
                raise

            return await cls.describe_series(scoped_type, series_id)

    @classmethod
    async def enqueue_hydrate_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        episode_keys: list[str] | None = None,
        full_series: bool = False,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        active_operation = await cls._selected_operation(
            library_type=scoped_type,
            series_id=series_id,
            preferred_types=("hydrate", "evict", "activate"),
        )
        if cls._operation_is_active(active_operation):
            return await cls.describe_series(scoped_type, series_id)

        await asyncio.to_thread(
            LibraryStateDb.upsert_operation,
            library_type=scoped_type,
            series_id=series_id,
            operation_type="hydrate",
            status=OPERATION_PENDING,
            progress=0.0,
            error=None,
        )
        cls._spawn_background_task(
            cls._run_background_hydration(
                library_type=scoped_type,
                series_id=series_id,
                episode_keys=list(episode_keys or []),
                full_series=full_series,
            ),
            description=f"library-hydrate:{scoped_type.value}:{series_id}",
        )
        return await cls.describe_series(scoped_type, series_id)

    @classmethod
    async def toggle_permanent_pin(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        await asyncio.to_thread(
            LibraryStateDb.set_permanent_pin,
            scoped_type,
            series_id,
            enabled,
        )

        hydration_started = False
        if enabled:
            state = await asyncio.to_thread(LibraryStateDb.get_series_state, scoped_type, series_id)
            if state is None or state.hydration_status != HYDRATION_STATUS_FULLY_LOCAL:
                current = await cls.describe_series(scoped_type, series_id)
                operation = current.get("operation")
                if not (
                    isinstance(operation, dict)
                    and str(operation.get("status") or "") in {OPERATION_PENDING, OPERATION_RUNNING}
                ):
                    hydration_started = True
                    await cls.enqueue_hydrate_series(
                        library_type=scoped_type,
                        series_id=series_id,
                        full_series=True,
                    )

        return {
            "permanent_pin": enabled,
            "hydration_started": hydration_started,
        }

    @classmethod
    async def evict_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        async with cls._series_lock(scoped_type, series_id):
            state = await asyncio.to_thread(LibraryStateDb.get_series_state, scoped_type, series_id)
            project_pin_count = await asyncio.to_thread(LibraryStateDb.count_project_pins, series_id)
            if state and state.permanent_pin:
                raise RuntimeError("Series is permanently pinned and cannot be evicted.")
            if project_pin_count > 0:
                raise RuntimeError("Series is still pinned by at least one project and cannot be evicted.")

            manifest = None
            with suppress(Exception):
                manifest = await cls._load_or_fetch_manifest(scoped_type, series_id)

            await asyncio.to_thread(
                LibraryStateDb.upsert_operation,
                library_type=scoped_type,
                series_id=series_id,
                operation_type="evict",
                status=OPERATION_RUNNING,
                progress=0.0,
                error=None,
            )
            try:
                await asyncio.to_thread(
                    cls._evict_local_series_sync,
                    scoped_type,
                    series_id,
                    manifest,
                )
                release_id = str(manifest["release_id"]) if manifest else (state.release_id if state else None)
                expected_episode_count = (
                    int(manifest.get("episode_count", len(manifest.get("episodes", []))))
                    if manifest
                    else (state.expected_episode_count if state else 0)
                )
                permanent_pin = state.permanent_pin if state else False
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=release_id,
                    permanent_pin=permanent_pin,
                    hydration_status=HYDRATION_STATUS_NOT_HYDRATED,
                    local_episode_count=0,
                    expected_episode_count=expected_episode_count,
                    last_error=None,
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=series_id,
                    operation_type="evict",
                    status=OPERATION_COMPLETE,
                    progress=1.0,
                    error=None,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=series_id,
                    operation_type="evict",
                    status=OPERATION_ERROR,
                    progress=0.0,
                    error=str(exc),
                )
                raise

        return await cls.describe_series(scoped_type, series_id)

    @classmethod
    async def enqueue_evict_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        active_operation = await cls._selected_operation(
            library_type=scoped_type,
            series_id=series_id,
            preferred_types=("evict", "hydrate", "activate"),
        )
        if cls._operation_is_active(active_operation):
            return await cls.describe_series(scoped_type, series_id)

        state = await asyncio.to_thread(LibraryStateDb.get_series_state, scoped_type, series_id)
        project_pin_count = await asyncio.to_thread(LibraryStateDb.count_project_pins, series_id)
        if state and state.permanent_pin:
            raise RuntimeError("Series is permanently pinned and cannot be evicted.")
        if project_pin_count > 0:
            raise RuntimeError("Series is still pinned by at least one project and cannot be evicted.")

        await asyncio.to_thread(
            LibraryStateDb.upsert_operation,
            library_type=scoped_type,
            series_id=series_id,
            operation_type="evict",
            status=OPERATION_PENDING,
            progress=0.0,
            error=None,
        )
        cls._spawn_background_task(
            cls._run_background_evict(
                library_type=scoped_type,
                series_id=series_id,
            ),
            description=f"library-evict:{scoped_type.value}:{series_id}",
        )
        return await cls.describe_series(scoped_type, series_id)

    @classmethod
    async def describe_series(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        return await cls._describe_state(
            library_type=library_type,
            series_id=series_id,
            preferred_operation_types=("hydrate", "evict", "activate"),
        )

    @classmethod
    async def list_source_details(
        cls,
        *,
        library_type: LibraryType | str,
    ) -> list[dict[str, Any]]:
        scoped_type = coerce_library_type(library_type)
        if scoped_type is LibraryType.PURE:
            # Pure projects are built from the TikTok itself: the type has no
            # series, no local library and no Storage Box tree. Probing the
            # remote catalog only buys two doomed SFTP round-trips (the read
            # 404s, then the rebuild fallback 404s on the missing series root)
            # and an alarming traceback in the log.
            return []
        try:
            catalog = await StorageBoxRepository.list_catalog(scoped_type)
        except Exception:
            # A dead Storage Box must not blank out the library: series with
            # a pending (not yet uploaded) publish are fully usable locally
            # and are listed below from their durable records.
            logger.warning(
                "Storage Box catalog unavailable for %s; listing pending "
                "local series only",
                scoped_type.value,
                exc_info=True,
            )
            catalog = []
        pending_records = [
            record
            for record in await asyncio.to_thread(PendingPublishStore.list_all)
            if record.library_type == scoped_type.value
        ]
        pending_by_series = {record.series_id: record for record in pending_records}
        library_path = AnimeLibraryService.get_library_path(scoped_type)
        state_by_series = await asyncio.to_thread(LibraryStateDb.list_series_states, scoped_type)
        pin_counts = await asyncio.to_thread(
            LibraryStateDb.get_project_pin_counts,
            list(
                {str(entry.get("series_id")) for entry in catalog}
                | set(pending_by_series)
            ),
        )
        results: list[dict[str, Any]] = []
        for entry in catalog:
            series_id = str(entry.get("series_id"))
            state = state_by_series.get(series_id)
            storage_release_id = str(entry.get("storage_release_id", ""))
            pending_record = pending_by_series.get(series_id)
            if pending_record is None and (
                state is None
                or (storage_release_id and state.release_id != storage_release_id)
            ):
                # Skipped when a pending publish exists: the state row then
                # points at the newer, locally-finalized release and syncing
                # against the (older) catalog release would clobber it.
                display_name = str(entry.get("name", "")).strip()
                local_series_dir = library_path / display_name
                local_metadata = await asyncio.to_thread(
                    StorageBoxRepository.read_local_series_metadata,
                    local_series_dir,
                )
                if (
                    isinstance(local_metadata, dict)
                    and str(local_metadata.get("series_id") or "").strip() == series_id
                ):
                    state = await cls.sync_local_series_state(
                        library_type=scoped_type,
                        series_id=series_id,
                        release_id=storage_release_id or None,
                    )
                    if state is not None:
                        state_by_series[series_id] = state
            local_episode_count = state.local_episode_count if state else 0
            expected_episode_count = state.expected_episode_count if state else int(entry.get("episode_count", 0) or 0)
            hydration_status = state.hydration_status if state else HYDRATION_STATUS_NOT_HYDRATED
            if pending_record is not None:
                manifest = pending_record.manifest
                entry = {
                    **entry,
                    "episode_count": manifest.get("episode_count", entry.get("episode_count", 0)),
                    "total_size_bytes": manifest.get("total_size_bytes", entry.get("total_size_bytes", 0)),
                    "fps": manifest.get("fps", entry.get("fps", 0.0)),
                    "torrent_count": manifest.get("torrent_count", entry.get("torrent_count", 0)),
                    "storage_release_id": pending_record.release_id,
                }
                storage_release_id = pending_record.release_id
            results.append(
                {
                    "name": str(entry.get("name", "")),
                    "series_id": series_id,
                    "episode_count": int(entry.get("episode_count", 0) or 0),
                    "local_episode_count": local_episode_count,
                    "total_size_bytes": int(entry.get("total_size_bytes", 0) or 0),
                    "fps": float(entry.get("fps", 0.0) or 0.0),
                    "is_fully_local": expected_episode_count > 0 and local_episode_count >= expected_episode_count,
                    "project_pin_count": pin_counts.get(series_id, 0),
                    "permanent_pin": bool(state.permanent_pin) if state else False,
                    "storage_release_id": storage_release_id,
                    "torrent_count": int(entry.get("torrent_count", 0) or 0),
                    "hydration_status": hydration_status,
                    "pending_upload": pending_record is not None,
                    "updated_at": str(
                        (state.updated_at if state else None)
                        or entry.get("updated_at")
                        or ""
                    ),
                }
            )

        # Series that only exist locally so far (finalized, upload pending or
        # in flight): surface them exactly like published ones.
        emitted = {row["series_id"] for row in results}
        for record in pending_records:
            if record.series_id in emitted:
                continue
            manifest = record.manifest
            state = state_by_series.get(record.series_id)
            local_episode_count = state.local_episode_count if state else 0
            expected_episode_count = (
                state.expected_episode_count
                if state
                else int(manifest.get("episode_count", 0) or 0)
            )
            results.append(
                {
                    "name": record.display_name,
                    "series_id": record.series_id,
                    "episode_count": int(manifest.get("episode_count", 0) or 0),
                    "local_episode_count": local_episode_count,
                    "total_size_bytes": int(manifest.get("total_size_bytes", 0) or 0),
                    "fps": float(manifest.get("fps", 0.0) or 0.0),
                    "is_fully_local": expected_episode_count > 0
                    and local_episode_count >= expected_episode_count,
                    "project_pin_count": pin_counts.get(record.series_id, 0),
                    "permanent_pin": bool(state.permanent_pin) if state else False,
                    "storage_release_id": record.release_id,
                    "torrent_count": int(manifest.get("torrent_count", 0) or 0),
                    "hydration_status": (
                        state.hydration_status
                        if state
                        else HYDRATION_STATUS_INDEX_READY
                    ),
                    "pending_upload": True,
                    "updated_at": str(
                        (state.updated_at if state else None) or record.created_at
                    ),
                }
            )
        return results

    @classmethod
    async def sync_local_series_state(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str | None = None,
    ) -> SeriesStateRow | None:
        scoped_type = coerce_library_type(library_type)
        manifest = await cls._load_or_fetch_manifest(scoped_type, series_id)
        if release_id and str(manifest.get("release_id") or "") != release_id:
            manifest = await StorageBoxRepository.get_series_manifest(
                scoped_type,
                series_id,
                release_id,
            )
            await cls._cache_manifest(scoped_type, manifest)

        local_episode_count = await asyncio.to_thread(
            cls._count_local_episodes_from_manifest,
            scoped_type,
            manifest,
        )
        expected_episode_count = int(
            manifest.get("episode_count", len(manifest.get("episodes", [])))
        )
        local_series_dir = (
            AnimeLibraryService.get_library_path(scoped_type) / str(manifest["display_name"])
        )
        local_metadata = await asyncio.to_thread(
            StorageBoxRepository.read_local_series_metadata,
            local_series_dir,
        )
        has_local_index_metadata = (
            isinstance(local_metadata, dict)
            and str(local_metadata.get("series_id") or "").strip() == series_id
        )
        local_index_ready = await asyncio.to_thread(
            cls._local_index_ready_for_manifest_sync,
            scoped_type,
            manifest,
        )
        hydration_status = (
            HYDRATION_STATUS_FULLY_LOCAL
            if expected_episode_count > 0 and local_episode_count >= expected_episode_count
            else HYDRATION_STATUS_INDEX_READY
            if has_local_index_metadata and local_index_ready
            else HYDRATION_STATUS_NOT_HYDRATED
        )
        await asyncio.to_thread(
            LibraryStateDb.upsert_series_state,
            library_type=scoped_type,
            series_id=series_id,
            release_id=str(manifest["release_id"]),
            hydration_status=hydration_status,
            local_episode_count=local_episode_count,
            expected_episode_count=expected_episode_count,
            last_error=None,
        )
        return await asyncio.to_thread(
            LibraryStateDb.get_series_state,
            scoped_type,
            series_id,
        )

    @classmethod
    async def get_episode_sources(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        manifest = await cls._load_or_fetch_manifest(scoped_type, series_id)
        state = await asyncio.to_thread(LibraryStateDb.get_series_state, scoped_type, series_id)
        try:
            torrent_metadata = await StorageBoxRepository.read_remote_torrent_metadata(
                scoped_type,
                series_id,
                str(manifest["release_id"]),
            )
        except Exception:
            # For a pending (not yet uploaded) release the remote artifact
            # doesn't exist yet — the series dir's local copy is identical.
            torrent_metadata = None
            local_torrents_path = (
                AnimeLibraryService.get_library_path(scoped_type)
                / str(manifest.get("display_name") or "")
                / ".atr_torrents.json"
            )
            if local_torrents_path.is_file():
                with suppress(Exception):
                    torrent_metadata = await asyncio.to_thread(
                        _json_load, local_torrents_path
                    )
            if torrent_metadata is None:
                logger.warning(
                    "Torrent metadata unavailable for %s/%s (remote and local)",
                    scoped_type.value,
                    series_id,
                )
        episodes: list[dict[str, Any]] = []
        for episode in manifest.get("episodes", []):
            if not isinstance(episode, dict):
                continue
            media = episode.get("media", {})
            local_relative_path = media.get("local_relative_path")
            local_exists = False
            if isinstance(local_relative_path, str) and local_relative_path:
                local_exists = (
                    AnimeLibraryService.get_library_path(scoped_type) / local_relative_path
                ).exists()
            episodes.append(
                {
                    "episode_key": episode.get("episode_key"),
                    "size_bytes": int(media.get("size_bytes", 0) or 0),
                    "local": local_exists,
                    "local_relative_path": local_relative_path,
                }
            )
        return {
            "storage_box": {
                "available": True,
                "series_id": series_id,
                "release_id": str(manifest["release_id"]),
                "episode_count": int(manifest.get("episode_count", len(episodes))),
                "local_episode_count": state.local_episode_count if state else 0,
                "episodes": episodes,
            },
            "torrents": {
                "torrent_count": len(torrent_metadata.get("torrents", [])) if isinstance(torrent_metadata, dict) else 0,
                "items": torrent_metadata.get("torrents", []) if isinstance(torrent_metadata, dict) else [],
            },
        }

    @classmethod
    async def ensure_matcher_ready_for_project(
        cls,
        *,
        project_id: str,
        library_type: LibraryType | str,
        series_id: str | None,
    ) -> None:
        if not series_id:
            raise RuntimeError("Project is missing series_id for matcher activation.")
        ready = await cls.ensure_index_ready(library_type=library_type, series_id=series_id)
        if not ready:
            state = await cls.enqueue_project_activation(
                project_id=project_id,
                library_type=library_type,
                series_id=series_id,
            )
            while True:
                operation = state.get("operation")
                if await cls.ensure_index_ready(library_type=library_type, series_id=series_id):
                    return
                if isinstance(operation, dict):
                    status = str(operation.get("status") or "")
                    if status == OPERATION_ERROR:
                        raise RuntimeError(
                            str(operation.get("error") or state.get("last_error") or "Library activation failed.")
                        )
                await asyncio.sleep(0.25)
                state = await cls.get_activation_state(
                    library_type=library_type,
                    series_id=series_id,
                )

    @classmethod
    async def _run_background_activation(
        cls,
        *,
        project_id: str,
        library_type: LibraryType | str,
        series_id: str,
    ) -> None:
        try:
            await cls.activate_project_series(
                project_id=project_id,
                library_type=library_type,
                series_id=series_id,
            )
        except Exception:
            logger.exception(
                "Background activation failed for %s/%s",
                library_type,
                series_id,
            )

    @classmethod
    async def _run_background_hydration(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        episode_keys: list[str] | None = None,
        full_series: bool = False,
    ) -> None:
        try:
            await cls.hydrate_series(
                library_type=library_type,
                series_id=series_id,
                episode_keys=episode_keys,
                full_series=full_series,
            )
        except Exception:
            logger.exception("Background hydration failed for %s/%s", library_type, series_id)

    @classmethod
    async def _run_background_evict(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> None:
        try:
            await cls.evict_series(
                library_type=library_type,
                series_id=series_id,
            )
        except Exception:
            logger.exception("Background eviction failed for %s/%s", library_type, series_id)

    @classmethod
    async def finalize_series_release(
        cls,
        *,
        library_type: LibraryType | str,
        display_name: str,
        series_id: str | None = None,
        already_locked: bool = False,
        expected_min_episodes: int | None = None,
        merge_existing_release: bool = False,
        hashing_progress_callback: HashingProgressCallback | None = None,
    ) -> PendingPublishRecord:
        """Prepare a release and commit it as a durable pending publish.

        After this returns, the series is fully usable locally (listable,
        matchable) regardless of when — or whether — the remote upload runs.
        Saving the record is the commit point; :meth:`run_pending_upload`
        executes or resumes the frozen upload plan later.
        """
        scoped_type = coerce_library_type(library_type)

        async def _finalize() -> PendingPublishRecord:
            record = await StorageBoxRepository.prepare_series_release(
                library_type=scoped_type,
                display_name=display_name,
                series_id=series_id,
                expected_min_episodes=expected_min_episodes,
                merge_existing_release=merge_existing_release,
                hashing_progress_callback=hashing_progress_callback,
            )
            # Supersede: prepare snapshots the entire series dir, so a newer
            # pending publish strictly replaces any older one for the series.
            # The caller (indexation queue) has already cancelled the stale
            # upload job before starting this finalize.
            stale = await asyncio.to_thread(
                PendingPublishStore.find_by_series,
                record.library_type,
                record.series_id,
            )
            if stale is not None and stale.publish_id != record.publish_id:
                await StorageBoxRepository.abandon_prepared_release(stale)
                await asyncio.to_thread(PendingPublishStore.delete, stale.publish_id)
            await asyncio.to_thread(PendingPublishStore.save, record)
            await cls.apply_local_publish_state(record)
            return record

        if series_id and not already_locked:
            async with cls._series_lock(scoped_type, series_id):
                return await _finalize()
        return await _finalize()

    @classmethod
    async def apply_local_publish_state(cls, record: PendingPublishRecord) -> None:
        """Make a finalized-but-not-yet-uploaded release fully usable locally.

        Writes everything the purely-local matching gate
        (:meth:`ensure_index_ready`) checks: the cached manifest, the series
        dir's ``.atr_storage_box.json``, and the ``series_state`` row. The
        state is computed locally on purpose — ``sync_local_series_state``
        would fall back to a remote manifest fetch that cannot succeed before
        the upload. Idempotent; re-run on startup resume to heal a crashed
        upload's local state.
        """
        scoped_type = coerce_library_type(record.library_type)
        manifest = record.manifest
        await cls._cache_manifest(scoped_type, manifest)
        await asyncio.to_thread(
            StorageBoxRepository.write_local_series_metadata,
            series_dir=Path(record.series_dir),
            series_id=record.series_id,
            display_name=record.display_name,
            release_id=record.release_id,
        )
        local_episode_count = await asyncio.to_thread(
            cls._count_local_episodes_from_manifest,
            scoped_type,
            manifest,
        )
        expected_episode_count = int(
            manifest.get("episode_count", len(manifest.get("episodes", [])))
        )
        await asyncio.to_thread(
            LibraryStateDb.upsert_series_state,
            library_type=scoped_type,
            series_id=record.series_id,
            release_id=record.release_id,
            hydration_status=(
                HYDRATION_STATUS_FULLY_LOCAL
                if expected_episode_count > 0
                and local_episode_count >= expected_episode_count
                else HYDRATION_STATUS_INDEX_READY
            ),
            local_episode_count=local_episode_count,
            expected_episode_count=expected_episode_count,
            last_error=None,
        )
        await asyncio.to_thread(
            LibraryStateDb.upsert_operation,
            library_type=scoped_type,
            series_id=record.series_id,
            operation_type=OPERATION_TYPE_PUBLISH,
            status=OPERATION_PENDING,
        )

    @classmethod
    async def run_pending_upload(
        cls,
        record: PendingPublishRecord,
        *,
        progress_callback: ProgressCallback | None = None,
        already_locked: bool = False,
    ) -> dict[str, Any] | None:
        """Execute (or resume) the remote upload of a pending publish.

        Returns ``None`` when the record no longer exists (superseded or
        cancelled between enqueue and execution). On success the record is
        deleted; on failure it is kept with ``attempts``/``last_error``
        updated — the remote staging dir stays in place as resume state.
        """
        scoped_type = coerce_library_type(record.library_type)
        lock_ctx = (
            nullcontext()
            if already_locked
            else cls._series_lock(scoped_type, record.series_id)
        )
        async with lock_ctx:
            fresh = await asyncio.to_thread(PendingPublishStore.load, record.publish_id)
            if fresh is None:
                logger.info(
                    "Pending publish %s for '%s' no longer exists "
                    "(superseded or cancelled); skipping upload",
                    record.publish_id,
                    record.display_name,
                )
                return None
            record = fresh
            await asyncio.to_thread(
                LibraryStateDb.upsert_operation,
                library_type=scoped_type,
                series_id=record.series_id,
                operation_type=OPERATION_TYPE_PUBLISH,
                status=OPERATION_RUNNING,
            )
            try:
                await StorageBoxRepository.sweep_series_staging(
                    scoped_type,
                    record.series_id,
                    keep_publish_ids={record.publish_id},
                )
                result = await StorageBoxRepository.upload_prepared_release(
                    record,
                    progress_callback=progress_callback,
                )
                # The release is remote now: the regular (remote-backed) sync
                # is safe and refreshes episode counts against the manifest.
                await cls.sync_local_series_state(
                    library_type=scoped_type,
                    series_id=record.series_id,
                    release_id=record.release_id,
                )
                await asyncio.to_thread(
                    LibraryStateDb.upsert_operation,
                    library_type=scoped_type,
                    series_id=record.series_id,
                    operation_type=OPERATION_TYPE_PUBLISH,
                    status=OPERATION_COMPLETE,
                    progress=1.0,
                )
                await asyncio.to_thread(PendingPublishStore.delete, record.publish_id)
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record.attempts += 1
                record.last_error = str(exc)
                with suppress(Exception):
                    await asyncio.to_thread(PendingPublishStore.save, record)
                with suppress(Exception):
                    await asyncio.to_thread(
                        LibraryStateDb.upsert_operation,
                        library_type=scoped_type,
                        series_id=record.series_id,
                        operation_type=OPERATION_TYPE_PUBLISH,
                        status=OPERATION_ERROR,
                        error=str(exc),
                    )
                raise

    @classmethod
    async def publish_series_release(
        cls,
        *,
        library_type: LibraryType | str,
        display_name: str,
        series_id: str | None = None,
        already_locked: bool = False,
        expected_min_episodes: int | None = None,
        merge_existing_release: bool = False,
        progress_callback: ProgressCallback | None = None,
        hashing_progress_callback: HashingProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Synchronous publish: finalize + upload in one call.

        Kept for the legacy SSE ``/anime/index`` and ``/anime/update``
        routes. The queue-based flow uses :meth:`finalize_series_release`
        followed by a background :meth:`run_pending_upload`.
        """
        record = await cls.finalize_series_release(
            library_type=library_type,
            display_name=display_name,
            series_id=series_id,
            already_locked=already_locked,
            expected_min_episodes=expected_min_episodes,
            merge_existing_release=merge_existing_release,
            hashing_progress_callback=hashing_progress_callback,
        )
        result = await cls.run_pending_upload(
            record,
            progress_callback=progress_callback,
            already_locked=already_locked,
        )
        if result is None:
            raise RuntimeError(
                "Publish was superseded by a newer pending publish before "
                "its upload could run."
            )
        return result

    @classmethod
    async def delete_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        async with cls._series_lock(scoped_type, series_id):
            referencing_projects = [
                {
                    "project_id": project.id,
                    "anime_title": project.anime_name,
                    "phase": project.phase.value,
                    "scheduled_at": (
                        project.scheduled_at.isoformat() if project.scheduled_at else None
                    ),
                    "upload_completed_at": (
                        project.upload_completed_at.isoformat()
                        if project.upload_completed_at
                        else None
                    ),
                }
                for project in await asyncio.to_thread(
                    ProjectService.list_referencing_series,
                    library_type=scoped_type,
                    series_id=series_id,
                )
            ]
            if referencing_projects:
                raise SeriesDeleteBlockedError(
                    library_type=scoped_type,
                    series_id=series_id,
                    referencing_projects=referencing_projects,
                )

            # A pending (not yet uploaded) publish dies with the series: the
            # caller has already cancelled its upload job; reclaim the record,
            # staged artifacts, and any partial remote staging.
            pending = await asyncio.to_thread(
                PendingPublishStore.find_by_series,
                scoped_type.value,
                series_id,
            )
            if pending is not None:
                await StorageBoxRepository.abandon_prepared_release(pending)
                await asyncio.to_thread(PendingPublishStore.delete, pending.publish_id)

            state = await asyncio.to_thread(
                LibraryStateDb.get_series_state,
                scoped_type,
                series_id,
            )
            catalog_entry = None
            with suppress(Exception):
                catalog_entry = await StorageBoxRepository.find_catalog_entry_by_series_id(
                    scoped_type,
                    series_id,
                )
            manifest = None
            if isinstance(catalog_entry, dict):
                display_name = str(catalog_entry.get("name") or "").strip()
                storage_release_id = str(catalog_entry.get("storage_release_id") or "").strip()
                if display_name:
                    manifest = {
                        "series_id": series_id,
                        "display_name": display_name,
                        "release_id": storage_release_id,
                        "episode_count": int(catalog_entry.get("episode_count", 0) or 0),
                        "episodes": [],
                    }
            if manifest is None:
                with suppress(Exception):
                    manifest = await cls._load_or_fetch_manifest(scoped_type, series_id)

            if manifest is None and isinstance(catalog_entry, dict):
                display_name = str(catalog_entry.get("name") or "").strip()
            elif manifest is not None:
                display_name = str(manifest["display_name"])
            else:
                display_name = await asyncio.to_thread(
                    cls._resolve_local_display_name_sync,
                    scoped_type,
                    series_id,
                )

            async with cls._library_lock(scoped_type):
                await asyncio.to_thread(
                    cls._evict_local_series_sync,
                    scoped_type,
                    series_id,
                    manifest,
                )

            release_id = str(manifest["release_id"]) if manifest else (state.release_id if state else None)
            expected_episode_count = (
                int(manifest.get("episode_count", len(manifest.get("episodes", []))))
                if manifest
                else (state.expected_episode_count if state else 0)
            )
            permanent_pin = state.permanent_pin if state else False
            await asyncio.to_thread(
                LibraryStateDb.upsert_series_state,
                library_type=scoped_type,
                series_id=series_id,
                release_id=release_id,
                permanent_pin=permanent_pin,
                hydration_status=HYDRATION_STATUS_NOT_HYDRATED,
                local_episode_count=0,
                expected_episode_count=expected_episode_count,
                last_error=None,
            )
            await StorageBoxRepository.delete_series(
                library_type=scoped_type,
                series_id=series_id,
            )
            await asyncio.to_thread(
                LibraryStateDb.delete_series_records,
                library_type=scoped_type,
                series_id=series_id,
            )

        return {
            "status": "deleted",
            "series_id": series_id,
            "library_type": scoped_type.value,
            "display_name": display_name or None,
        }

    @classmethod
    def _rename_local_series_dir_sync(
        cls,
        old_dir: Path,
        new_dir: Path,
    ) -> None:
        if not old_dir.exists() or old_dir == new_dir:
            return
        same_location = False
        if new_dir.exists():
            with suppress(OSError):
                same_location = old_dir.resolve() == new_dir.resolve()
        if new_dir.exists() and not same_location:
            raise RuntimeError(f"Target series directory already exists: {new_dir}")
        if old_dir.name.casefold() == new_dir.name.casefold():
            temp_dir = old_dir.with_name(f".atr-rename-{uuid.uuid4().hex[:8]}")
            old_dir.rename(temp_dir)
            temp_dir.rename(new_dir)
            return
        old_dir.rename(new_dir)

    @classmethod
    def _rewrite_local_series_paths_in_place_sync(
        cls,
        *,
        library_type: LibraryType,
        series_id: str,
        old_display_name: str,
        new_display_name: str,
        release_id: str,
    ) -> None:
        library_root = AnimeLibraryService.get_library_path(library_type)
        old_dir = library_root / old_display_name
        new_dir = library_root / new_display_name
        same_location = False
        if new_dir.exists() and old_dir.exists():
            with suppress(OSError):
                same_location = old_dir.resolve() == new_dir.resolve()
        if new_dir.exists() and old_dir.exists() and old_dir != new_dir and not same_location:
            raise RuntimeError(f"Conflicting local series directories: {old_dir} and {new_dir}")

        if old_dir.exists() and old_dir != new_dir:
            cls._rename_local_series_dir_sync(old_dir, new_dir)

        if new_dir.exists():
            StorageBoxRepository.write_local_series_metadata(
                series_dir=new_dir,
                series_id=series_id,
                display_name=new_display_name,
                release_id=release_id,
            )

            for source_manifest_path in new_dir.rglob(
                f"*{AnimeLibraryService.SOURCE_IMPORT_MANIFEST_SUFFIX}"
            ):
                try:
                    payload = _json_load(source_manifest_path)
                except Exception:
                    continue
                rewritten = StorageBoxRepository._rewrite_source_import_payload_for_rename(
                    payload,
                    library_type=library_type,
                    old_display_name=old_display_name,
                    new_display_name=new_display_name,
                )
                _json_write_atomic(source_manifest_path, rewritten)

            torrents_path = new_dir / ".atr_torrents.json"
            if torrents_path.exists():
                try:
                    payload = _json_load(torrents_path)
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    rewritten = StorageBoxRepository._rewrite_torrent_metadata_for_rename(
                        payload,
                        library_type=library_type,
                        old_display_name=old_display_name,
                        new_display_name=new_display_name,
                    )
                    _json_write_atomic(torrents_path, rewritten)

            for sidecar_manifest_path in new_dir.rglob("manifest.json"):
                if not sidecar_manifest_path.parent.name.endswith(
                    AnimeLibraryService.SUBTITLE_SIDECAR_SUFFIX
                ):
                    continue
                try:
                    payload = _json_load(sidecar_manifest_path)
                except Exception:
                    continue
                rewritten = StorageBoxRepository._rewrite_subtitle_sidecar_manifest_for_rename(
                    payload,
                    library_type=library_type,
                    old_display_name=old_display_name,
                    new_display_name=new_display_name,
                )
                _json_write_atomic(sidecar_manifest_path, rewritten)

        index_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME
        manifest_path = index_dir / AnimeLibraryService.MANIFEST_FILE
        state_path = index_dir / AnimeLibraryService.STATE_FILE

        if manifest_path.exists():
            try:
                local_manifest = _json_load(manifest_path)
            except Exception:
                local_manifest = None
            if isinstance(local_manifest, dict):
                raw_series = local_manifest.get("series", {})
                if isinstance(raw_series, dict) and old_display_name in raw_series:
                    series_entry = raw_series.pop(old_display_name)
                    raw_series[new_display_name] = series_entry
                    _json_write_atomic(manifest_path, local_manifest)

        if state_path.exists():
            try:
                local_state = _json_load(state_path)
            except Exception:
                local_state = None
            if isinstance(local_state, dict):
                raw_files = local_state.get("files", {})
                if isinstance(raw_files, dict):
                    local_state["files"] = {
                        StorageBoxRepository._rewrite_local_relative_series_path(
                            path,
                            old_display_name=old_display_name,
                            new_display_name=new_display_name,
                        ): value
                        for path, value in raw_files.items()
                    }
                    _json_write_atomic(state_path, local_state)

    @classmethod
    async def rename_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        new_name: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        target_name = str(new_name or "").strip()
        if not target_name:
            raise ValueError("Le nouveau nom de la série ne peut pas être vide.")

        async with cls._series_lock(scoped_type, series_id):
            active_operation = await cls._selected_operation(
                library_type=scoped_type,
                series_id=series_id,
                preferred_types=("activate", "hydrate", "evict"),
            )
            if cls._operation_is_active(active_operation):
                raise SeriesRenameConflictError(
                    "Impossible de renommer la série pendant une activation, hydratation ou éviction en cours."
                )

            current = await StorageBoxRepository.get_current_release(scoped_type, series_id)
            current_release_id = str(current.get("release_id") or "").strip()
            manifest = await StorageBoxRepository.get_series_manifest(
                scoped_type,
                series_id,
                current_release_id or None,
            )
            old_name = str(manifest.get("display_name") or "").strip()
            if not old_name:
                raise RuntimeError(f"Series '{series_id}' is missing a display name.")
            if target_name == old_name:
                return {
                    "status": "renamed",
                    "series_id": series_id,
                    "library_type": scoped_type.value,
                    "old_name": old_name,
                    "new_name": old_name,
                    "storage_release_id": current_release_id,
                }

            existing_entry = await StorageBoxRepository.find_catalog_entry_by_name(
                scoped_type,
                target_name,
            )
            if existing_entry is not None:
                existing_series_id = str(existing_entry.get("series_id") or "").strip()
                if existing_series_id and existing_series_id != series_id:
                    raise SeriesRenameConflictError(
                        f"Une autre série existe déjà avec le nom '{target_name}'."
                    )
            else:
                remote_series_id = await StorageBoxRepository.find_remote_series_id_by_name(
                    scoped_type,
                    target_name,
                )
                if remote_series_id and str(remote_series_id).strip() != series_id:
                    raise SeriesRenameConflictError(
                        f"Une autre série existe déjà avec le nom '{target_name}'."
                    )

            library_root = AnimeLibraryService.get_library_path(scoped_type)
            old_dir = library_root / old_name
            target_dir = library_root / target_name
            if target_dir.exists() and target_dir != old_dir:
                same_location = False
                if old_dir.exists():
                    with suppress(OSError):
                        same_location = old_dir.resolve() == target_dir.resolve()
                if same_location:
                    target_dir = old_dir
                else:
                    target_metadata = await asyncio.to_thread(
                        StorageBoxRepository.read_local_series_metadata,
                        target_dir,
                    )
                    target_series_id = (
                        str(target_metadata.get("series_id") or "").strip()
                        if isinstance(target_metadata, dict)
                        else ""
                    )
                    if target_series_id != series_id:
                        raise SeriesRenameConflictError(
                            f"Un dossier local conflictuel existe déjà pour '{target_name}'."
                        )
                    if old_dir.exists():
                        raise SeriesRenameConflictError(
                            f"Deux dossiers locaux concurrents existent pour '{old_name}' et '{target_name}'."
                        )

            rename_result = await StorageBoxRepository.rename_series(
                library_type=scoped_type,
                series_id=series_id,
                new_display_name=target_name,
            )
            new_release_id = str(rename_result["release_id"])

            async with cls._library_lock(scoped_type):
                await asyncio.to_thread(
                    cls._rewrite_local_series_paths_in_place_sync,
                    library_type=scoped_type,
                    series_id=series_id,
                    old_display_name=old_name,
                    new_display_name=target_name,
                    release_id=new_release_id,
                )

            await cls._cache_manifest(scoped_type, dict(rename_result["manifest"]))
            await cls.sync_local_series_state(
                library_type=scoped_type,
                series_id=series_id,
                release_id=new_release_id,
            )
            await asyncio.to_thread(
                ProjectService.rename_series_references,
                library_type=scoped_type,
                series_id=series_id,
                new_name=target_name,
            )

            from .anime_matcher import AnimeMatcherService
            from .project_startup_service import project_startup_queue

            await project_startup_queue.rename_series_references(
                library_type=scoped_type,
                series_id=series_id,
                new_name=target_name,
            )
            AnimeMatcherService.mark_series_updated(scoped_type, old_name)
            AnimeMatcherService.mark_series_updated(scoped_type, target_name)
            await AnimeLibraryService.ensure_episode_manifest(
                force_refresh=True,
                library_type=scoped_type,
            )

            return {
                "status": "renamed",
                "series_id": series_id,
                "library_type": scoped_type.value,
                "old_name": old_name,
                "new_name": target_name,
                "storage_release_id": new_release_id,
            }

    @classmethod
    async def _cache_manifest(
        cls,
        library_type: LibraryType | str,
        manifest: dict[str, Any],
    ) -> None:
        path = cls._manifest_cache_path(
            library_type,
            str(manifest["series_id"]),
            str(manifest["release_id"]),
        )
        await asyncio.to_thread(_json_write_atomic, path, manifest)

    @classmethod
    async def _load_or_fetch_manifest(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        state = await asyncio.to_thread(LibraryStateDb.get_series_state, library_type, series_id)
        if state and state.release_id:
            cached_path = cls._manifest_cache_path(library_type, series_id, state.release_id)
            if cached_path.exists():
                return await asyncio.to_thread(_json_load, cached_path)
        manifest = await StorageBoxRepository.get_series_manifest(library_type, series_id)
        await cls._cache_manifest(library_type, manifest)
        return manifest

    @classmethod
    def _count_local_episodes_from_manifest(
        cls,
        library_type: LibraryType | str,
        manifest: dict[str, Any],
    ) -> int:
        library_path = AnimeLibraryService.get_library_path(library_type)
        total = 0
        for episode in manifest.get("episodes", []):
            if not isinstance(episode, dict):
                continue
            media = episode.get("media", {})
            media_rel = media.get("local_relative_path")
            if not isinstance(media_rel, str) or not media_rel:
                continue
            media_path = library_path / media_rel
            if not media_path.exists():
                continue
            sidecars = episode.get("sidecars", [])
            if not isinstance(sidecars, list):
                sidecars = []
            if all(
                isinstance(item, dict)
                and isinstance(item.get("local_relative_path"), str)
                and (library_path / str(item["local_relative_path"])).exists()
                for item in sidecars
            ):
                total += 1
        return total

    @classmethod
    def _local_index_ready_for_manifest_sync(
        cls,
        library_type: LibraryType | str,
        manifest: dict[str, Any],
    ) -> bool:
        display_name = str(manifest.get("display_name") or "").strip()
        series_id = str(manifest.get("series_id") or "").strip()
        release_id = str(manifest.get("release_id") or "").strip()
        if not display_name or not series_id or not release_id:
            return False

        library_path = AnimeLibraryService.get_library_path(library_type)
        local_series_dir = library_path / display_name
        local_metadata = StorageBoxRepository.read_local_series_metadata(local_series_dir)
        if not isinstance(local_metadata, dict):
            return False
        if str(local_metadata.get("series_id") or "").strip() != series_id:
            return False
        if str(local_metadata.get("release_id") or "").strip() != release_id:
            return False

        index_dir = library_path / AnimeLibraryService.INDEX_DIR_NAME
        manifest_path = index_dir / AnimeLibraryService.MANIFEST_FILE
        state_path = index_dir / AnimeLibraryService.STATE_FILE
        if not manifest_path.exists() or not state_path.exists():
            return False

        try:
            manifest_payload = _json_load(manifest_path)
            state_payload = _json_load(state_path)
        except Exception:
            return False

        if manifest_payload.get("version") != AnimeLibraryService.SEARCHER_INDEX_FORMAT_VERSION:
            return False
        if manifest_payload.get("engine_profile") != AnimeLibraryService.SEARCHER_ENGINE_PROFILE:
            return False

        series_map = manifest_payload.get("series", {})
        if not isinstance(series_map, dict):
            return False
        series_entry = series_map.get(display_name)
        if not isinstance(series_entry, dict):
            return False

        state_files = state_payload.get("files", {})
        if not isinstance(state_files, dict):
            return False

        shard_key = str(series_entry.get("key") or "").strip()
        if not shard_key:
            return False

        shard_dir = index_dir / "series" / shard_key
        return (
            shard_dir.is_dir()
            and (shard_dir / "faiss.index").is_file()
            and (shard_dir / "metadata.json").is_file()
        )

    @classmethod
    async def _hydrate_index_artifacts(
        cls,
        library_type: LibraryType,
        manifest: dict[str, Any],
        network_progress_callback: ProgressCallback | None = None,
    ) -> None:
        index_artifacts = [
            artifact
            for artifact in manifest.get("artifacts", [])
            if isinstance(artifact, dict) and artifact.get("artifact_type") == "index"
        ]
        if not index_artifacts:
            raise RuntimeError("No index artifacts found in the active release manifest.")

        temp_root = cls._temp_root() / library_type.value / str(manifest["series_id"]) / "index" / uuid.uuid4().hex[:8]
        release_root = StorageBoxRepository._release_root(
            library_type,
            str(manifest["series_id"]),
            str(manifest["release_id"]),
        )
        total_bytes = sum(
            int(artifact.get("size_bytes") or 0) for artifact in index_artifacts
        )
        items = [
            PurePosixPath(str(artifact["relative_path"]))
            for artifact in index_artifacts
        ]
        try:
            await StorageBoxRclone.download_batch(
                items,
                remote_base=release_root,
                dest_root=temp_root,
                total_bytes=total_bytes,
                progress_callback=network_progress_callback,
            )
            downloaded_paths = [temp_root / Path(*item.parts) for item in items]
            hashes = await _sha256_files_parallel(downloaded_paths)
            for artifact, path in zip(index_artifacts, downloaded_paths):
                relative_path = str(artifact["relative_path"])
                if not path.is_file():
                    raise RuntimeError(
                        f"Downloaded index artifact missing: {relative_path}"
                    )
                if hashes.get(path) != str(artifact["sha256"]):
                    raise RuntimeError(f"Checksum mismatch for {relative_path}")
            # The downloaded tree mirrors remote-relative paths, so the
            # payload the materializer expects lives under payload/index.
            await cls._materialize_local_matcher_cache(
                library_type, manifest, temp_root / "payload" / "index"
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, temp_root, True)

    @classmethod
    async def _materialize_local_matcher_cache(
        cls,
        library_type: LibraryType,
        manifest: dict[str, Any],
        temp_root: Path,
    ) -> None:
        async with cls._library_lock(library_type):
            await asyncio.to_thread(
                cls._materialize_local_matcher_cache_sync,
                library_type,
                manifest,
                temp_root,
            )

    @classmethod
    def _materialize_local_matcher_cache_sync(
        cls,
        library_type: LibraryType,
        manifest: dict[str, Any],
        temp_root: Path,
    ) -> None:
        display_name = str(manifest["display_name"])
        index_dir = AnimeLibraryService.get_library_path(library_type) / AnimeLibraryService.INDEX_DIR_NAME
        index_dir.mkdir(parents=True, exist_ok=True)
        series_dir = index_dir / "series"
        series_dir.mkdir(parents=True, exist_ok=True)

        fragment_root = temp_root / str(manifest["series_id"])
        manifest_fragment_path = fragment_root / "manifest.fragment.json"
        state_fragment_path = fragment_root / "state.fragment.json"
        manifest_fragment = _json_load(manifest_fragment_path)
        state_fragment = _json_load(state_fragment_path)

        local_manifest_path = index_dir / AnimeLibraryService.MANIFEST_FILE
        local_state_path = index_dir / AnimeLibraryService.STATE_FILE

        local_manifest = (
            _json_load(local_manifest_path)
            if local_manifest_path.exists()
            else {
                "version": manifest_fragment.get("version"),
                "engine_profile": manifest_fragment.get("engine_profile"),
                "config": manifest_fragment.get("config", {}),
                "series": {},
            }
        )
        local_state = (
            _json_load(local_state_path)
            if local_state_path.exists()
            else {"files": {}}
        )

        local_manifest["version"] = manifest_fragment.get("version")
        local_manifest["engine_profile"] = manifest_fragment.get("engine_profile")
        local_manifest["config"] = manifest_fragment.get("config", {})
        local_manifest.setdefault("series", {})
        local_manifest["series"][display_name] = manifest_fragment.get("series", {}).get(display_name, {})

        local_state.setdefault("files", {})
        prefix = f"{display_name}/"
        local_state["files"] = {
            path: value
            for path, value in dict(local_state["files"]).items()
            if not (path == display_name or str(path).startswith(prefix))
        }
        local_state["files"].update(state_fragment.get("files", {}))

        series_entry = manifest_fragment.get("series", {}).get(display_name, {})
        shard_key = str(series_entry.get("key") or "").strip()
        if not shard_key:
            raise RuntimeError(f"Missing shard key for {display_name}")
        shard_src_dir = fragment_root / "series" / shard_key
        shard_dst_dir = series_dir / shard_key
        if shard_dst_dir.exists():
            shutil.rmtree(shard_dst_dir, ignore_errors=True)
        shutil.copytree(shard_src_dir, shard_dst_dir, dirs_exist_ok=True)
        _json_write_atomic(local_manifest_path, local_manifest)
        _json_write_atomic(local_state_path, local_state)

        series_local_dir = AnimeLibraryService.get_library_path(library_type) / display_name
        series_local_dir.mkdir(parents=True, exist_ok=True)
        StorageBoxRepository.write_local_series_metadata(
            series_dir=series_local_dir,
            series_id=str(manifest["series_id"]),
            display_name=display_name,
            release_id=str(manifest["release_id"]),
        )

    @classmethod
    def _plan_episode_downloads(
        cls,
        library_type: LibraryType,
        manifest: dict[str, Any],
        episodes: list[dict[str, Any]],
    ) -> list[_EpisodeDownloadPlan]:
        """Collect the (remote, target) pairs for episodes not fully local."""
        library_root = AnimeLibraryService.get_library_path(library_type)
        plans: list[_EpisodeDownloadPlan] = []
        for episode in episodes:
            media = episode.get("media", {})
            if not isinstance(media, dict):
                raise RuntimeError("Invalid episode media payload")
            media_local_rel = str(media.get("local_relative_path") or "")
            if not media_local_rel:
                raise RuntimeError("Episode media is missing local_relative_path")

            media_target = library_root / media_local_rel
            sidecars = [
                item for item in episode.get("sidecars", []) if isinstance(item, dict)
            ]
            if media_target.exists() and all(
                (library_root / str(item.get("local_relative_path"))).exists()
                for item in sidecars
                if item.get("local_relative_path")
            ):
                continue

            items: list[_EpisodeDownloadItem] = []
            for item in [media, *sidecars]:
                remote_relative_path = str(item.get("relative_path") or "")
                local_relative_path = str(item.get("local_relative_path") or "")
                if not remote_relative_path or not local_relative_path:
                    raise RuntimeError("Episode artifact is missing relative paths")
                items.append(
                    _EpisodeDownloadItem(
                        remote_relative=PurePosixPath(remote_relative_path),
                        final_target=library_root / local_relative_path,
                        sha256=str(item.get("sha256") or ""),
                        size_bytes=int(item.get("size_bytes") or 0),
                    )
                )
            plans.append(
                _EpisodeDownloadPlan(
                    episode_key=str(episode.get("episode_key") or media_local_rel),
                    items=tuple(items),
                )
            )
        return plans

    @staticmethod
    def _verify_and_move_episode(
        plan: _EpisodeDownloadPlan,
        batch_temp_root: Path,
        hashes: dict[Path, str],
    ) -> None:
        """Checksum-gate then atomically move one episode's files.

        All of the episode's files are verified before ANY of them moves,
        so a failed episode leaves the library untouched.
        """
        moves: list[tuple[Path, Path]] = []
        for item in plan.items:
            temp_path = batch_temp_root / Path(*item.remote_relative.parts)
            if not temp_path.is_file():
                raise RuntimeError(
                    f"Downloaded file missing for {item.remote_relative.as_posix()}"
                )
            if item.sha256 and hashes.get(temp_path) != item.sha256:
                raise RuntimeError(
                    f"Checksum mismatch for {item.remote_relative.as_posix()}"
                )
            moves.append((temp_path, item.final_target))

        for _temp_path, target_path in moves:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        for temp_path, target_path in moves:
            if target_path.exists():
                target_path.unlink()
            temp_path.replace(target_path)

    @classmethod
    def _evict_local_series_sync(
        cls,
        library_type: LibraryType,
        series_id: str,
        manifest: dict[str, Any] | None,
    ) -> None:
        library_root = AnimeLibraryService.get_library_path(library_type)
        display_name = str(manifest["display_name"]) if manifest else cls._resolve_local_display_name_sync(
            library_type,
            series_id,
        )
        if display_name:
            series_dir = library_root / display_name
            if series_dir.exists():
                shutil.rmtree(series_dir, ignore_errors=True)

        index_dir = library_root / AnimeLibraryService.INDEX_DIR_NAME
        manifest_path = index_dir / AnimeLibraryService.MANIFEST_FILE
        state_path = index_dir / AnimeLibraryService.STATE_FILE
        if not manifest_path.exists() or not state_path.exists():
            return

        local_manifest = _json_load(manifest_path)
        local_state = _json_load(state_path)
        raw_series = local_manifest.get("series", {})
        shard_key = None
        if display_name and isinstance(raw_series, dict):
            series_entry = raw_series.pop(display_name, None)
            if isinstance(series_entry, dict):
                shard_key = str(series_entry.get("key") or "").strip() or None
        _json_write_atomic(manifest_path, local_manifest)

        if display_name:
            prefix = f"{display_name}/"
            raw_files = local_state.get("files", {})
            if isinstance(raw_files, dict):
                local_state["files"] = {
                    path: value
                    for path, value in raw_files.items()
                    if not (path == display_name or str(path).startswith(prefix))
                }
                _json_write_atomic(state_path, local_state)

        if shard_key:
            shutil.rmtree(index_dir / "series" / shard_key, ignore_errors=True)

        manifest_cache_dir = cls._storage_cache_root() / "manifests" / library_type.value / series_id
        if manifest_cache_dir.exists():
            shutil.rmtree(manifest_cache_dir, ignore_errors=True)

    @classmethod
    def _resolve_local_display_name_sync(
        cls,
        library_type: LibraryType,
        series_id: str,
    ) -> str | None:
        library_root = AnimeLibraryService.get_library_path(library_type)
        if not library_root.exists():
            return None
        for source_dir in library_root.iterdir():
            if not source_dir.is_dir() or source_dir.name.startswith("."):
                continue
            metadata = StorageBoxRepository.read_local_series_metadata(source_dir)
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("series_id") or "").strip() == series_id:
                return source_dir.name
        return None

    @classmethod
    def _state_payload(
        cls,
        *,
        series_state: SeriesStateRow | None,
        operation: OperationRow | None,
        project_pin_count: int,
    ) -> dict[str, Any]:
        return {
            "series_id": (
                series_state.series_id
                if series_state
                else (operation.series_id if operation is not None else None)
            ),
            "release_id": series_state.release_id if series_state else None,
            "hydration_status": (
                series_state.hydration_status
                if series_state
                else HYDRATION_STATUS_NOT_HYDRATED
            ),
            "local_episode_count": series_state.local_episode_count if series_state else 0,
            "expected_episode_count": series_state.expected_episode_count if series_state else 0,
            "is_fully_local": bool(
                series_state
                and series_state.expected_episode_count > 0
                and series_state.local_episode_count >= series_state.expected_episode_count
            ),
            "permanent_pin": bool(series_state.permanent_pin) if series_state else False,
            "project_pin_count": project_pin_count,
            "last_error": (
                series_state.last_error
                if series_state
                else (operation.error if operation is not None else None)
            ),
            "operation": (
                {
                    "type": operation.operation_type,
                    "status": operation.status,
                    "progress": operation.progress,
                    "error": operation.error,
                    "updated_at": operation.updated_at,
                    # Network bytes/speed persisted while a download runs
                    # (cleared on terminal upserts).
                    **(operation.detail or {}),
                }
                if operation is not None
                else None
            ),
            "updated_at": (
                series_state.updated_at
                if series_state
                else (operation.updated_at if operation is not None else None)
            ),
        }
