from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from .anime_library import AnimeLibraryService
from .library_state_db import LibraryStateDb, OperationRow, SeriesStateRow
from .storage_box_repository import StorageBoxRepository


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


class LibraryHydrationService:
    """Owns activation, local matcher cache materialization, hydration, and eviction."""

    _series_locks: dict[tuple[str, str], asyncio.Lock] = {}
    _library_locks: dict[str, asyncio.Lock] = {}

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
        scoped_type = coerce_library_type(library_type)
        series_state = await asyncio.to_thread(
            LibraryStateDb.get_series_state,
            scoped_type,
            series_id,
        )
        operation = await asyncio.to_thread(
            LibraryStateDb.get_operation,
            scoped_type,
            series_id,
            "activate",
        )
        project_pin_count = await asyncio.to_thread(
            LibraryStateDb.count_project_pins,
            series_id,
        )
        return cls._state_payload(
            series_state=series_state,
            operation=operation,
            project_pin_count=project_pin_count,
        )

    @classmethod
    async def ensure_index_ready(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> bool:
        state = await asyncio.to_thread(LibraryStateDb.get_series_state, library_type, series_id)
        return bool(
            state
            and state.hydration_status in {HYDRATION_STATUS_INDEX_READY, HYDRATION_STATUS_FULLY_LOCAL}
        )

    @classmethod
    async def activate_project_series(
        cls,
        *,
        project_id: str,
        library_type: LibraryType | str,
        series_id: str,
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
                await asyncio.to_thread(
                    LibraryStateDb.upsert_series_state,
                    library_type=scoped_type,
                    series_id=series_id,
                    release_id=str(manifest["release_id"]),
                    hydration_status=HYDRATION_STATUS_HYDRATING_INDEX,
                    local_episode_count=0,
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
                await cls._cache_manifest(scoped_type, manifest)
                await cls._hydrate_index_artifacts(scoped_type, manifest)
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
    async def hydrate_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        episode_keys: list[str] | None = None,
        full_series: bool = False,
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
                for index, episode in enumerate(selected_episodes, start=1):
                    await asyncio.to_thread(
                        LibraryStateDb.upsert_operation,
                        library_type=scoped_type,
                        series_id=series_id,
                        operation_type="hydrate",
                        status=OPERATION_RUNNING,
                        progress=(index - 1) / max(total, 1),
                        error=None,
                    )
                    await cls._hydrate_episode(scoped_type, manifest, episode)

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
                hydration_started = True
                asyncio.create_task(
                    cls._run_background_full_hydration(
                        library_type=scoped_type,
                        series_id=series_id,
                    )
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
    async def describe_series(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        state = await asyncio.to_thread(LibraryStateDb.get_series_state, scoped_type, series_id)
        operation = await asyncio.to_thread(
            LibraryStateDb.get_operation,
            scoped_type,
            series_id,
            "hydrate",
        )
        project_pin_count = await asyncio.to_thread(LibraryStateDb.count_project_pins, series_id)
        return cls._state_payload(
            series_state=state,
            operation=operation,
            project_pin_count=project_pin_count,
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
        hydration_status = (
            HYDRATION_STATUS_FULLY_LOCAL
            if expected_episode_count > 0 and local_episode_count >= expected_episode_count
            else HYDRATION_STATUS_INDEX_READY
            if has_local_index_metadata or local_episode_count > 0
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
            await cls.activate_project_series(
                project_id=project_id,
                library_type=library_type,
                series_id=series_id,
            )

    @classmethod
    async def _run_background_full_hydration(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> None:
        try:
            await cls.hydrate_series(
                library_type=library_type,
                series_id=series_id,
                full_series=True,
            )
        except Exception:
            logger.exception("Background full hydration failed for %s/%s", library_type, series_id)

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
    async def _hydrate_index_artifacts(
        cls,
        library_type: LibraryType,
        manifest: dict[str, Any],
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
        try:
            for artifact in index_artifacts:
                relative_path = str(artifact["relative_path"])
                local_relative_path = Path(relative_path).relative_to("payload/index")
                download_path = temp_root / local_relative_path
                from .storage_box_sftp_client import StorageBoxSftpClient

                await StorageBoxSftpClient.download_file(
                    release_root / relative_path,
                    download_path,
                )
                if _sha256_file(download_path) != str(artifact["sha256"]):
                    raise RuntimeError(f"Checksum mismatch for {relative_path}")
            await cls._materialize_local_matcher_cache(library_type, manifest, temp_root)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

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
            shutil.rmtree(temp_root, ignore_errors=True)
        temp_root.mkdir(parents=True, exist_ok=True)
        release_root = StorageBoxRepository._release_root(
            library_type,
            str(manifest["series_id"]),
            str(manifest["release_id"]),
        )

        from .storage_box_sftp_client import StorageBoxSftpClient

        try:
            items = [media, *sidecars]
            downloaded: list[tuple[Path, Path]] = []
            for item in items:
                remote_relative_path = str(item.get("relative_path") or "")
                local_relative_path = str(item.get("local_relative_path") or "")
                if not remote_relative_path or not local_relative_path:
                    raise RuntimeError("Episode artifact is missing relative paths")
                temp_path = temp_root / local_relative_path
                await StorageBoxSftpClient.download_file(
                    release_root / remote_relative_path,
                    temp_path,
                )
                expected_sha = str(item.get("sha256") or "")
                if expected_sha and _sha256_file(temp_path) != expected_sha:
                    raise RuntimeError(f"Checksum mismatch for {remote_relative_path}")
                downloaded.append((temp_path, library_root / local_relative_path))

            for _temp_path, target_path in downloaded:
                target_path.parent.mkdir(parents=True, exist_ok=True)
            for temp_path, target_path in downloaded:
                if target_path.exists():
                    target_path.unlink()
                temp_path.replace(target_path)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

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
        display_name = str(manifest["display_name"]) if manifest else None
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

    @classmethod
    def _state_payload(
        cls,
        *,
        series_state: SeriesStateRow | None,
        operation: OperationRow | None,
        project_pin_count: int,
    ) -> dict[str, Any]:
        return {
            "series_id": series_state.series_id if series_state else None,
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
            "last_error": series_state.last_error if series_state else None,
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
            "updated_at": series_state.updated_at if series_state else None,
        }
