from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import shutil
import uuid
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from .anime_library import AnimeLibraryService
from .library_state_db import LibraryStateDb, OperationRow, SeriesStateRow
from .project_service import ProjectService
from .storage_box_progress import (
    ProgressCallback,
    StorageBoxTransferProgress,
    TransferSession,
)
from .storage_box_repository import StorageBoxRepository
from .storage_box_transfer import StorageBoxTransferService


logger = logging.getLogger("uvicorn.error")


HYDRATION_STATUS_NOT_HYDRATED = "not_hydrated"
HYDRATION_STATUS_HYDRATING_INDEX = "hydrating_index"
HYDRATION_STATUS_INDEX_READY = "index_ready"
HYDRATION_STATUS_HYDRATING_EPISODES = "hydrating_episodes"
HYDRATION_STATUS_FULLY_LOCAL = "fully_local"
HYDRATION_STATUS_ERROR = "error"

OPERATION_PENDING = "pending"
OPERATION_RUNNING = "running"
OPERATION_INTERRUPTED = "interrupted"
OPERATION_COMPLETE = "complete"
OPERATION_ERROR = "error"


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
ArtifactProgressCallback = Callable[[int, int, str], Awaitable[None] | None]


async def _call_progress_callback(callback, *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


async def _run_bounded(items: list[Any], limit: int, worker) -> None:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def _run_one(item: Any) -> None:
        async with semaphore:
            await worker(item)

    async with asyncio.TaskGroup() as task_group:
        for item in items:
            task_group.create_task(_run_one(item))


class LibraryHydrationService:
    """Owns activation, local matcher cache materialization, hydration, and eviction."""

    _series_locks: dict[tuple[str, str], asyncio.Lock] = {}
    _library_locks: dict[str, asyncio.Lock] = {}
    _background_tasks: set[asyncio.Task[Any]] = set()

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

    @classmethod
    async def ensure_catalog_available(cls, library_type: LibraryType | str) -> None:
        if not StorageBoxRepository.is_enabled():
            return
        try:
            await StorageBoxRepository.list_catalog(library_type)
        except Exception:
            await StorageBoxRepository.rebuild_catalog(library_type)

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
            current = await StorageBoxRepository.get_current_release(scoped_type, series_id)
            manifest = await StorageBoxRepository.get_series_manifest(
                scoped_type,
                series_id,
                str(current["release_id"]),
            )
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
            try:
                current = await StorageBoxRepository.get_current_release(scoped_type, series_id)
                manifest = await StorageBoxRepository.get_series_manifest(
                    scoped_type,
                    series_id,
                    str(current["release_id"]),
                )
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

                    async def _artifact_progress(
                        completed: int,
                        total: int,
                        relative_path: str,
                    ) -> None:
                        artifact_progress = completed / max(total, 1)
                        activation_progress = 0.15 + (0.75 * artifact_progress)
                        await asyncio.to_thread(
                            LibraryStateDb.upsert_operation,
                            library_type=scoped_type,
                            series_id=series_id,
                            operation_type="activate",
                            status=OPERATION_RUNNING,
                            progress=activation_progress,
                            error=None,
                        )
                        await _call_progress_callback(
                            progress_callback,
                            activation_progress,
                            (
                                "Hydrating matcher cache "
                                f"({completed}/{total}): {Path(relative_path).name}"
                            ),
                        )

                    await cls._hydrate_index_artifacts(
                        scoped_type,
                        manifest,
                        progress_callback=_artifact_progress,
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

                total = len(selected_episodes)
                completed = 0
                progress_lock = asyncio.Lock()

                total_bytes = 0
                for episode_entry in selected_episodes:
                    media = episode_entry.get("media") or {}
                    if isinstance(media, dict):
                        total_bytes += int(media.get("size_bytes") or 0)
                    sidecars = episode_entry.get("sidecars") or []
                    if isinstance(sidecars, list):
                        for sidecar in sidecars:
                            if isinstance(sidecar, dict):
                                total_bytes += int(sidecar.get("size_bytes") or 0)

                session = await StorageBoxTransferProgress.open_session(
                    f"hydrate-series:{scoped_type.value}:{series_id}:{uuid.uuid4().hex[:8]}",
                    direction="download",
                    total_bytes=total_bytes,
                    on_update=progress_callback,
                ) if progress_callback is not None else None

                async def _hydrate_with_progress(episode: dict[str, Any]) -> None:
                    nonlocal completed
                    await cls._hydrate_episode(scoped_type, manifest, episode, session)
                    async with progress_lock:
                        completed += 1
                        await asyncio.to_thread(
                            LibraryStateDb.upsert_operation,
                            library_type=scoped_type,
                            series_id=series_id,
                            operation_type="hydrate",
                            status=OPERATION_RUNNING,
                            progress=completed / max(total, 1),
                            error=None,
                        )

                try:
                    await _run_bounded(
                        selected_episodes,
                        settings.storage_box_download_max_parallel,
                        _hydrate_with_progress,
                    )
                finally:
                    if session is not None:
                        await StorageBoxTransferProgress.close_session(session)
                    # Newly hydrated episode files must become visible to
                    # AnimeLibraryService.resolve_episode_path consumers
                    # (processing pipeline, gap resolution, playback).
                    await AnimeLibraryService.ensure_episode_manifest(
                        force_refresh=True,
                        library_type=scoped_type,
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
        catalog = await StorageBoxRepository.list_catalog(scoped_type)
        library_path = AnimeLibraryService.get_library_path(scoped_type)
        state_by_series = await asyncio.to_thread(LibraryStateDb.list_series_states, scoped_type)
        pin_counts = await asyncio.to_thread(
            LibraryStateDb.get_project_pin_counts,
            [str(entry.get("series_id")) for entry in catalog],
        )
        results: list[dict[str, Any]] = []
        for entry in catalog:
            series_id = str(entry.get("series_id"))
            state = state_by_series.get(series_id)
            storage_release_id = str(entry.get("storage_release_id", ""))
            if state is None or (storage_release_id and state.release_id != storage_release_id):
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
                    "updated_at": str(
                        (state.updated_at if state else None)
                        or entry.get("updated_at")
                        or ""
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
        torrent_metadata = await StorageBoxRepository.read_remote_torrent_metadata(
            scoped_type,
            series_id,
            str(manifest["release_id"]),
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
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)

        async def _publish() -> dict[str, Any]:
            publish_result = await StorageBoxRepository.publish_series(
                library_type=scoped_type,
                display_name=display_name,
                series_id=series_id,
                expected_min_episodes=expected_min_episodes,
                merge_existing_release=merge_existing_release,
                progress_callback=progress_callback,
            )
            await cls.sync_local_series_state(
                library_type=scoped_type,
                series_id=str(publish_result["series_id"]),
                release_id=str(publish_result["release_id"]),
            )
            return publish_result

        if series_id and not already_locked:
            async with cls._series_lock(scoped_type, series_id):
                return await _publish()
        return await _publish()

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

            state = await asyncio.to_thread(
                LibraryStateDb.get_series_state,
                scoped_type,
                series_id,
            )
            manifest = None
            with suppress(Exception):
                manifest = await cls._load_or_fetch_manifest(scoped_type, series_id)

            display_name = (
                str(manifest["display_name"])
                if manifest
                else await asyncio.to_thread(
                    cls._resolve_local_display_name_sync,
                    scoped_type,
                    series_id,
                )
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
        progress_callback: ArtifactProgressCallback | None = None,
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
        temp_root.mkdir(parents=True, exist_ok=True)
        release_root = StorageBoxRepository._release_root(
            library_type,
            str(manifest["series_id"]),
            str(manifest["release_id"]),
        )
        total_bytes = sum(
            int(artifact.get("size_bytes") or 0) for artifact in index_artifacts
        )
        session = await StorageBoxTransferProgress.open_session(
            f"hydrate-index:{manifest['series_id']}:{uuid.uuid4().hex[:8]}",
            direction="download",
            total_bytes=total_bytes,
            on_update=network_progress_callback,
        ) if network_progress_callback is not None else None
        try:
            completed = 0
            total = len(index_artifacts)
            progress_lock = asyncio.Lock()

            async def _download_artifact(artifact: dict[str, Any]) -> None:
                nonlocal completed
                relative_path = str(artifact["relative_path"])
                local_relative_path = Path(relative_path).relative_to("payload/index")
                download_path = temp_root / local_relative_path
                await StorageBoxTransferService.download_file(
                    release_root / relative_path,
                    download_path,
                    session=session,
                )
                actual_sha = await asyncio.to_thread(_sha256_file, download_path)
                if actual_sha != str(artifact["sha256"]):
                    raise RuntimeError(f"Checksum mismatch for {relative_path}")
                async with progress_lock:
                    completed += 1
                    await _call_progress_callback(
                        progress_callback,
                        completed,
                        total,
                        relative_path,
                    )

            await _run_bounded(
                index_artifacts,
                settings.storage_box_download_max_parallel,
                _download_artifact,
            )
            await cls._materialize_local_matcher_cache(library_type, manifest, temp_root)
        finally:
            if session is not None:
                await StorageBoxTransferProgress.close_session(session)
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
    async def _hydrate_episode(
        cls,
        library_type: LibraryType,
        manifest: dict[str, Any],
        episode: dict[str, Any],
        session: TransferSession | None = None,
    ) -> None:
        media = episode.get("media", {})
        if not isinstance(media, dict):
            raise RuntimeError("Invalid episode media payload")

        library_root = AnimeLibraryService.get_library_path(library_type)
        media_local_rel = str(media.get("local_relative_path") or "")
        if not media_local_rel:
            raise RuntimeError("Episode media is missing local_relative_path")

        media_target = library_root / media_local_rel
        sidecars = [item for item in episode.get("sidecars", []) if isinstance(item, dict)]
        if media_target.exists() and all(
            (library_root / str(item.get("local_relative_path"))).exists()
            for item in sidecars
            if item.get("local_relative_path")
        ):
            return

        temp_root = cls._temp_root() / library_type.value / str(manifest["series_id"]) / "episodes" / str(episode.get("episode_key") or uuid.uuid4().hex[:8])
        if temp_root.exists():
            await asyncio.to_thread(shutil.rmtree, temp_root, True)
        temp_root.mkdir(parents=True, exist_ok=True)
        release_root = StorageBoxRepository._release_root(
            library_type,
            str(manifest["series_id"]),
            str(manifest["release_id"]),
        )

        try:
            downloaded: list[tuple[Path, Path]] = []
            for item in [media, *sidecars]:
                remote_relative_path = str(item.get("relative_path") or "")
                local_relative_path = str(item.get("local_relative_path") or "")
                if not remote_relative_path or not local_relative_path:
                    raise RuntimeError("Episode artifact is missing relative paths")
                temp_path = temp_root / local_relative_path
                await StorageBoxTransferService.download_file(
                    release_root / remote_relative_path,
                    temp_path,
                    session=session,
                )
                expected_sha = str(item.get("sha256") or "")
                if expected_sha and (await asyncio.to_thread(_sha256_file, temp_path)) != expected_sha:
                    raise RuntimeError(f"Checksum mismatch for {remote_relative_path}")
                downloaded.append((temp_path, library_root / local_relative_path))

            for _temp_path, target_path in downloaded:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            for temp_path, target_path in downloaded:
                if target_path.exists():
                    target_path.unlink()
                temp_path.replace(target_path)
        finally:
            await asyncio.to_thread(shutil.rmtree, temp_root, True)

        display_name = str(manifest["display_name"])
        StorageBoxRepository.write_local_series_metadata(
            series_dir=AnimeLibraryService.get_library_path(library_type) / display_name,
            series_id=str(manifest["series_id"]),
            display_name=display_name,
            release_id=str(manifest["release_id"]),
        )

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
