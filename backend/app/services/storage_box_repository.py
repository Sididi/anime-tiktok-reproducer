from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import shutil
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from .anime_library import AnimeLibraryService
from .storage_box_sftp_client import StorageBoxSftpClient
from .storage_box_transfer import StorageBoxTransferService


logger = logging.getLogger("uvicorn.error")


SCHEMA_VERSION = 1
REPOSITORY_VERSION = "v1"
LOCAL_STORAGE_BOX_METADATA = ".atr_storage_box.json"
TORRENT_METADATA_FILENAME = ".atr_torrents.json"
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _uuid7_string() -> str:
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = 0
    value |= ts_ms << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return str(uuid.UUID(int=value))


def _safe_json_loads(raw: str, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON for {context}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid JSON payload for {context}: expected object")
    return payload


def _series_state_file_prefix(display_name: str) -> str:
    return f"{display_name}/"


def _human_size(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(max(0, size_bytes))
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    decimals = 0 if unit == "B" else 1
    return f"{size:.{decimals}f} {unit}"


async def _run_bounded(items: list[Any], limit: int, worker) -> None:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def _run_one(item: Any) -> None:
        async with semaphore:
            await worker(item)

    async with asyncio.TaskGroup() as task_group:
        for item in items:
            task_group.create_task(_run_one(item))


@dataclass(frozen=True)
class LocalArtifact:
    local_path: Path
    remote_relative_path: PurePosixPath
    size_bytes: int
    sha256: str
    artifact_type: str
    episode_key: str | None = None
    local_relative_path: str | None = None


class StorageBoxRepository:
    """Owns release publication and remote catalog/current/manifest reads."""

    _catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _catalog_cache_ttl_seconds = 30.0

    @classmethod
    def is_enabled(cls) -> bool:
        return StorageBoxSftpClient.is_configured()

    @staticmethod
    def local_series_metadata_path(series_dir: Path) -> Path:
        return series_dir / LOCAL_STORAGE_BOX_METADATA

    @classmethod
    def read_local_series_metadata(cls, series_dir: Path) -> dict[str, Any] | None:
        metadata_path = cls.local_series_metadata_path(series_dir)
        if not metadata_path.exists():
            return None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @classmethod
    def write_local_series_metadata(
        cls,
        *,
        series_dir: Path,
        series_id: str,
        display_name: str,
        release_id: str | None = None,
    ) -> None:
        metadata_path = cls.local_series_metadata_path(series_dir)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "series_id": series_id,
            "display_name": display_name,
            "release_id": release_id,
            "updated_at": _utc_now_iso(),
        }
        metadata_path.write_text(_json_dumps(payload), encoding="utf-8")

    @classmethod
    def _type_root(cls, library_type: LibraryType | str) -> PurePosixPath:
        scoped_type = coerce_library_type(library_type)
        return PurePosixPath(REPOSITORY_VERSION) / scoped_type.value

    @classmethod
    def _catalog_path(cls, library_type: LibraryType | str) -> PurePosixPath:
        return cls._type_root(library_type) / "catalog.json"

    @classmethod
    def _series_root(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> PurePosixPath:
        return cls._type_root(library_type) / "series" / series_id

    @classmethod
    def _current_path(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> PurePosixPath:
        return cls._series_root(library_type, series_id) / "current.json"

    @classmethod
    def _releases_root(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> PurePosixPath:
        return cls._series_root(library_type, series_id) / "releases"

    @classmethod
    def _release_root(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str,
    ) -> PurePosixPath:
        return cls._releases_root(library_type, series_id) / release_id

    @classmethod
    def _staging_root(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        publish_id: str,
    ) -> PurePosixPath:
        return cls._series_root(library_type, series_id) / "staging" / publish_id

    @classmethod
    def _payload_library_root(
        cls,
        display_name: str,
    ) -> PurePosixPath:
        return PurePosixPath("payload") / "library" / display_name

    @classmethod
    def _payload_index_root(
        cls,
        series_id: str,
    ) -> PurePosixPath:
        return PurePosixPath("payload") / "index" / series_id

    @classmethod
    def _invalidate_catalog_cache(cls, library_type: LibraryType | str | None = None) -> None:
        if library_type is None:
            cls._catalog_cache.clear()
            return
        scoped_type = coerce_library_type(library_type).value
        cls._catalog_cache.pop(scoped_type, None)

    @staticmethod
    def _normalized_catalog_name(value: Any) -> str:
        return str(value or "").strip().casefold()

    @classmethod
    def _catalog_entry_priority(cls, entry: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(entry.get("updated_at") or ""),
            str(entry.get("storage_release_id") or ""),
            str(entry.get("series_id") or ""),
        )

    @classmethod
    def _dedupe_catalog_entries(cls, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}

        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            series_id = str(entry.get("series_id") or "").strip()
            name_key = cls._normalized_catalog_name(entry.get("name"))
            dedupe_key = name_key or series_id
            if not dedupe_key:
                continue

            existing = deduped.get(dedupe_key)
            if existing is None:
                deduped[dedupe_key] = entry
                continue

            if cls._catalog_entry_priority(entry) >= cls._catalog_entry_priority(existing):
                if str(existing.get("series_id") or "") != series_id:
                    logger.warning(
                        "Duplicate catalog entry for '%s'; keeping series_id=%s over series_id=%s",
                        entry.get("name") or existing.get("name") or dedupe_key,
                        series_id,
                        existing.get("series_id"),
                    )
                deduped[dedupe_key] = entry

        return sorted(
            deduped.values(),
            key=lambda item: str(item.get("name", "")).casefold(),
        )

    @classmethod
    async def _read_remote_json(
        cls,
        remote_path: PurePosixPath,
        *,
        context: str,
    ) -> dict[str, Any]:
        payload = await StorageBoxSftpClient.read_text(remote_path)
        return _safe_json_loads(payload, context=context)

    @classmethod
    async def _write_remote_json(cls, remote_path: PurePosixPath, payload: dict[str, Any]) -> None:
        await StorageBoxSftpClient.write_text(remote_path, _json_dumps(payload))

    @classmethod
    async def list_catalog(cls, library_type: LibraryType | str) -> list[dict[str, Any]]:
        scoped_type = coerce_library_type(library_type).value
        cached = cls._catalog_cache.get(scoped_type)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < cls._catalog_cache_ttl_seconds:
            return [dict(entry) for entry in cached[1]]

        try:
            payload = await cls._read_remote_json(
                cls._catalog_path(scoped_type),
                context=f"{scoped_type} catalog",
            )
        except Exception:
            payload = await cls.rebuild_catalog(scoped_type)

        items = payload.get("items", [])
        if not isinstance(items, list):
            items = []

        normalized_items = cls._dedupe_catalog_entries(
            [dict(item) for item in items if isinstance(item, dict)]
        )
        cls._catalog_cache[scoped_type] = (now, normalized_items)
        return [dict(entry) for entry in normalized_items]

    @classmethod
    async def find_catalog_entry_by_name(
        cls,
        library_type: LibraryType | str,
        display_name: str,
    ) -> dict[str, Any] | None:
        entries = await cls.list_catalog(library_type)
        normalized = display_name.strip().casefold()
        for entry in entries:
            if str(entry.get("name", "")).strip().casefold() == normalized:
                return entry
        return None

    @classmethod
    async def get_current_release(
        cls,
        library_type: LibraryType | str,
        series_id: str,
    ) -> dict[str, Any]:
        payload = await cls._read_remote_json(
            cls._current_path(library_type, series_id),
            context=f"current release for {series_id}",
        )
        if int(payload.get("schema_version", 0) or 0) != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported current.json schema for {series_id}: {payload.get('schema_version')}"
            )
        return payload

    @classmethod
    async def find_remote_series_id_by_name(
        cls,
        library_type: LibraryType | str,
        display_name: str,
    ) -> str | None:
        normalized_name = cls._normalized_catalog_name(display_name)
        if not normalized_name:
            return None

        series_root = cls._type_root(library_type) / "series"
        try:
            series_ids = await StorageBoxSftpClient.listdir(series_root)
        except Exception:
            return None

        best_priority: tuple[str, str, str] | None = None
        best_series_id: str | None = None

        for candidate_series_id in series_ids:
            try:
                current = await cls.get_current_release(library_type, candidate_series_id)
            except Exception:
                continue

            if cls._normalized_catalog_name(current.get("display_name")) != normalized_name:
                continue

            candidate_priority = (
                str(current.get("published_at") or ""),
                str(current.get("release_id") or ""),
                str(candidate_series_id),
            )
            if best_priority is None or candidate_priority >= best_priority:
                best_priority = candidate_priority
                best_series_id = str(candidate_series_id)

        return best_series_id

    @classmethod
    async def get_series_manifest(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        effective_release_id = release_id
        if effective_release_id is None:
            current = await cls.get_current_release(library_type, series_id)
            effective_release_id = str(current["release_id"])
        manifest = await cls._read_remote_json(
            cls._release_root(library_type, series_id, effective_release_id) / "series_manifest.json",
            context=f"series manifest for {series_id}/{effective_release_id}",
        )
        if int(manifest.get("schema_version", 0) or 0) != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported series_manifest schema for {series_id}: {manifest.get('schema_version')}"
            )
        return manifest

    @classmethod
    async def read_remote_torrent_metadata(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        manifest = await cls.get_series_manifest(library_type, series_id, release_id)
        relative = manifest.get("torrent_metadata_relative_path")
        if not isinstance(relative, str) or not relative:
            return {"torrents": [], "purge_protection": False}
        remote_path = cls._release_root(
            library_type,
            series_id,
            str(manifest["release_id"]),
        ) / PurePosixPath(relative)
        try:
            return await cls._read_remote_json(remote_path, context=f"torrent metadata for {series_id}")
        except Exception:
            return {"torrents": [], "purge_protection": False}

    @classmethod
    async def list_release_episodes(
        cls,
        library_type: LibraryType | str,
        series_id: str,
        release_id: str | None = None,
    ) -> list[dict[str, Any]]:
        manifest = await cls.get_series_manifest(library_type, series_id, release_id)
        episodes = manifest.get("episodes", [])
        return [dict(item) for item in episodes if isinstance(item, dict)]

    @classmethod
    async def rebuild_catalog(cls, library_type: LibraryType | str) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        series_root = cls._type_root(scoped_type) / "series"
        items: list[dict[str, Any]] = []

        try:
            series_ids = await StorageBoxSftpClient.listdir(series_root)
        except Exception:
            series_ids = []

        for series_id in sorted(series_ids):
            try:
                current = await cls.get_current_release(scoped_type, series_id)
                manifest = await cls.get_series_manifest(scoped_type, series_id, str(current["release_id"]))
            except Exception as exc:
                logger.warning("Skipping catalog entry rebuild for %s/%s: %s", scoped_type.value, series_id, exc)
                continue

            items.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "series_id": str(manifest["series_id"]),
                    "name": str(manifest["display_name"]),
                    "storage_release_id": str(manifest["release_id"]),
                    "episode_count": int(manifest.get("episode_count", len(manifest.get("episodes", [])))),
                    "total_size_bytes": int(manifest.get("total_size_bytes", 0)),
                    "fps": float(manifest.get("fps", 0.0) or 0.0),
                    "torrent_count": int(manifest.get("torrent_count", 0)),
                    "updated_at": str(current.get("published_at") or manifest.get("created_at") or _utc_now_iso()),
                }
            )

        items = cls._dedupe_catalog_entries(items)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "library_type": scoped_type.value,
            "updated_at": _utc_now_iso(),
            "items": items,
        }

        catalog_path = cls._catalog_path(scoped_type)
        tmp_path = catalog_path.with_name(f"catalog.{uuid.uuid4().hex[:8]}.tmp")
        await cls._write_remote_json(tmp_path, payload)
        await StorageBoxSftpClient.replace_file(tmp_path, catalog_path)
        cls._invalidate_catalog_cache(scoped_type)
        return payload

    @classmethod
    def _read_local_index_series_payload(
        cls,
        *,
        library_type: LibraryType,
        display_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        library_path = AnimeLibraryService.get_library_path(library_type)
        index_dir = library_path / AnimeLibraryService.INDEX_DIR_NAME
        manifest_path = index_dir / AnimeLibraryService.MANIFEST_FILE
        state_path = index_dir / AnimeLibraryService.STATE_FILE

        manifest_payload = _safe_json_loads(
            manifest_path.read_text(encoding="utf-8"),
            context=f"local manifest for {display_name}",
        )
        state_payload = _safe_json_loads(
            state_path.read_text(encoding="utf-8"),
            context=f"local state for {display_name}",
        )

        raw_series = manifest_payload.get("series", {})
        if not isinstance(raw_series, dict):
            raise RuntimeError("Local index manifest is missing its series map")
        series_entry = raw_series.get(display_name)
        if not isinstance(series_entry, dict):
            raise RuntimeError(f"Series '{display_name}' is missing from the local index manifest")

        shard_key = str(series_entry.get("key") or "").strip()
        if not shard_key:
            raise RuntimeError(f"Series '{display_name}' is missing a shard key in the local index manifest")

        state_files = state_payload.get("files", {})
        if not isinstance(state_files, dict):
            raise RuntimeError("Local index state is missing its file map")

        prefix = _series_state_file_prefix(display_name)
        filtered_state = {
            path: value
            for path, value in state_files.items()
            if isinstance(path, str) and (path == display_name or path.startswith(prefix))
        }

        manifest_fragment = {
            "schema_version": SCHEMA_VERSION,
            "version": manifest_payload.get("version"),
            "engine_profile": manifest_payload.get("engine_profile"),
            "config": manifest_payload.get("config", {}),
            "series": {display_name: series_entry},
        }
        state_fragment = {
            "schema_version": SCHEMA_VERSION,
            "files": filtered_state,
        }
        return manifest_fragment, state_fragment, shard_key

    @classmethod
    def _collect_series_artifacts(
        cls,
        *,
        library_type: LibraryType,
        series_dir: Path,
        display_name: str,
        series_id: str,
    ) -> tuple[list[LocalArtifact], list[dict[str, Any]], Path]:
        payload_library_root = cls._payload_library_root(display_name)
        payload_index_root = cls._payload_index_root(series_id)

        local_artifacts: list[LocalArtifact] = []
        episode_entries: list[dict[str, Any]] = []

        for path in sorted(series_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name == LOCAL_STORAGE_BOX_METADATA:
                continue
            relative = path.relative_to(series_dir)
            remote_relative = payload_library_root / PurePosixPath(relative.as_posix())
            local_artifacts.append(
                LocalArtifact(
                    local_path=path,
                    remote_relative_path=remote_relative,
                    size_bytes=path.stat().st_size,
                    sha256=_sha256_file(path),
                    artifact_type="library",
                    local_relative_path=f"{display_name}/{relative.as_posix()}",
                )
            )

        video_files = [
            path
            for path in sorted(series_dir.iterdir())
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
        ]
        by_local_path = {
            artifact.local_path.resolve(): artifact
            for artifact in local_artifacts
        }

        for media_path in video_files:
            media_artifact = by_local_path.get(media_path.resolve())
            if media_artifact is None:
                continue
            episode_key = media_path.stem
            source_sidecar = series_dir / f"{media_path.name}{AnimeLibraryService.SOURCE_IMPORT_MANIFEST_SUFFIX}"
            subtitle_dir = AnimeLibraryService.get_subtitle_sidecar_dir(media_path)
            sidecars: list[dict[str, Any]] = []

            if source_sidecar.exists():
                source_artifact = by_local_path.get(source_sidecar.resolve())
                if source_artifact is not None:
                    sidecars.append(
                        {
                            "relative_path": source_artifact.remote_relative_path.as_posix(),
                            "local_relative_path": source_artifact.local_relative_path,
                            "size_bytes": source_artifact.size_bytes,
                            "sha256": source_artifact.sha256,
                            "artifact_type": "source_metadata",
                        }
                    )
            if subtitle_dir.exists():
                for subtitle_file in sorted(subtitle_dir.rglob("*")):
                    if not subtitle_file.is_file():
                        continue
                    subtitle_artifact = by_local_path.get(subtitle_file.resolve())
                    if subtitle_artifact is None:
                        continue
                    sidecars.append(
                        {
                            "relative_path": subtitle_artifact.remote_relative_path.as_posix(),
                            "local_relative_path": subtitle_artifact.local_relative_path,
                            "size_bytes": subtitle_artifact.size_bytes,
                            "sha256": subtitle_artifact.sha256,
                            "artifact_type": "subtitle_sidecar",
                        }
                    )

            episode_entries.append(
                {
                    "episode_key": episode_key,
                    "media": {
                        "relative_path": media_artifact.remote_relative_path.as_posix(),
                        "local_relative_path": media_artifact.local_relative_path,
                        "size_bytes": media_artifact.size_bytes,
                        "sha256": media_artifact.sha256,
                    },
                    "sidecars": sidecars,
                }
            )

        manifest_fragment, state_fragment, shard_key = cls._read_local_index_series_payload(
            library_type=library_type,
            display_name=display_name,
        )

        temp_root = Path(tempfile.mkdtemp(prefix="storage_box_release_", dir=settings.cache_dir))
        index_root = temp_root / "index" / series_id
        shard_src_dir = (
            AnimeLibraryService.get_library_path(library_type)
            / AnimeLibraryService.INDEX_DIR_NAME
            / "series"
            / shard_key
        )
        shard_dst_dir = index_root / "series" / shard_key
        shard_dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(shard_src_dir, shard_dst_dir, dirs_exist_ok=True)
        (index_root / "manifest.fragment.json").write_text(
            _json_dumps(manifest_fragment),
            encoding="utf-8",
        )
        (index_root / "state.fragment.json").write_text(
            _json_dumps(state_fragment),
            encoding="utf-8",
        )

        for path in sorted(index_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(index_root)
            remote_relative = payload_index_root / PurePosixPath(relative.as_posix())
            local_artifacts.append(
                LocalArtifact(
                    local_path=path,
                    remote_relative_path=remote_relative,
                    size_bytes=path.stat().st_size,
                    sha256=_sha256_file(path),
                    artifact_type="index",
                )
            )

        return local_artifacts, episode_entries, temp_root

    @classmethod
    async def _try_hardlink_first(
        cls,
        src: PurePosixPath,
        dst: PurePosixPath,
    ) -> bool:
        """Attempt a single hardlink to probe server support.

        Returns ``True`` if the hardlink succeeded, ``False`` otherwise.
        """
        try:
            await StorageBoxSftpClient.hardlink(src, dst)
            return True
        except Exception:
            return False

    @classmethod
    async def _verify_remote_artifacts(
        cls,
        *,
        staging_root: PurePosixPath,
        artifacts: list[LocalArtifact],
    ) -> None:
        async def _verify(artifact: LocalArtifact) -> None:
            remote_path = staging_root / artifact.remote_relative_path
            stat_result = await StorageBoxSftpClient.stat(remote_path)
            remote_size = int(getattr(stat_result, "size", 0))
            if remote_size != artifact.size_bytes:
                raise RuntimeError(
                    f"Remote size mismatch for {artifact.remote_relative_path.as_posix()}: "
                    f"expected {artifact.size_bytes}, got {remote_size}"
                )

        await _run_bounded(
            artifacts,
            settings.storage_box_upload_max_parallel,
            _verify,
        )

    @classmethod
    async def _resolve_or_create_series_id(
        cls,
        *,
        library_type: LibraryType,
        display_name: str,
        series_dir: Path,
        requested_series_id: str | None = None,
    ) -> str:
        if requested_series_id:
            return requested_series_id

        local_metadata = cls.read_local_series_metadata(series_dir)
        if local_metadata:
            series_id = str(local_metadata.get("series_id") or "").strip()
            if series_id:
                return series_id

        with suppress(Exception):
            entry = await cls.find_catalog_entry_by_name(library_type, display_name)
            if entry is not None:
                series_id = str(entry.get("series_id") or "").strip()
                if series_id:
                    return series_id

        with suppress(Exception):
            series_id = await cls.find_remote_series_id_by_name(library_type, display_name)
            if series_id:
                return series_id

        return _uuid7_string()

    @classmethod
    def _rewrite_local_relative_series_path(
        cls,
        value: Any,
        *,
        old_display_name: str,
        new_display_name: str,
    ) -> Any:
        if not isinstance(value, str) or not value:
            return value
        if value == old_display_name:
            return new_display_name
        old_prefix = _series_state_file_prefix(old_display_name)
        if value.startswith(old_prefix):
            return f"{new_display_name}/{value[len(old_prefix):]}"
        return value

    @classmethod
    def _rewrite_payload_library_relative_path(
        cls,
        value: Any,
        *,
        old_display_name: str,
        new_display_name: str,
    ) -> Any:
        if not isinstance(value, str) or not value:
            return value
        old_prefix = cls._payload_library_root(old_display_name).as_posix()
        new_prefix = cls._payload_library_root(new_display_name).as_posix()
        if value == old_prefix:
            return new_prefix
        if value.startswith(f"{old_prefix}/"):
            return f"{new_prefix}{value[len(old_prefix):]}"
        return value

    @classmethod
    def _rewrite_local_absolute_series_path(
        cls,
        value: Any,
        *,
        library_type: LibraryType | str,
        old_display_name: str,
        new_display_name: str,
    ) -> Any:
        if not isinstance(value, str) or not value:
            return value
        library_root = AnimeLibraryService.get_library_path(library_type)
        old_prefix = str((library_root / old_display_name).resolve())
        new_prefix = str((library_root / new_display_name).resolve())
        if value == old_prefix:
            return new_prefix
        for separator in ("/", "\\"):
            prefix = f"{old_prefix}{separator}"
            if value.startswith(prefix):
                return f"{new_prefix}{value[len(old_prefix):]}"
        return value

    @classmethod
    def _rename_artifact_needs_content_rewrite(
        cls,
        relative_path: str,
    ) -> bool:
        path = PurePosixPath(relative_path)
        if path.name in {"manifest.fragment.json", "state.fragment.json"}:
            return True
        if path.name.endswith(AnimeLibraryService.SOURCE_IMPORT_MANIFEST_SUFFIX):
            return True
        if path.name == TORRENT_METADATA_FILENAME:
            return True
        if (
            path.name == "manifest.json"
            and any(part.endswith(AnimeLibraryService.SUBTITLE_SIDECAR_SUFFIX) for part in path.parts)
        ):
            return True
        return False

    @classmethod
    def _rewrite_manifest_fragment_for_rename(
        cls,
        payload: dict[str, Any],
        *,
        old_display_name: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        rewritten = json.loads(json.dumps(payload))
        raw_series = rewritten.get("series", {})
        if isinstance(raw_series, dict):
            entry = raw_series.pop(old_display_name, None)
            if entry is not None:
                raw_series[new_display_name] = entry
        return rewritten

    @classmethod
    def _rewrite_state_fragment_for_rename(
        cls,
        payload: dict[str, Any],
        *,
        old_display_name: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        rewritten = json.loads(json.dumps(payload))
        raw_files = rewritten.get("files", {})
        if isinstance(raw_files, dict):
            rewritten["files"] = {
                cls._rewrite_local_relative_series_path(
                    path,
                    old_display_name=old_display_name,
                    new_display_name=new_display_name,
                ): value
                for path, value in raw_files.items()
            }
        return rewritten

    @classmethod
    def _rewrite_source_import_payload_for_rename(
        cls,
        payload: dict[str, Any],
        *,
        library_type: LibraryType | str,
        old_display_name: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        rewritten = json.loads(json.dumps(payload))
        for key in ("prepared_path", "sidecar_source_path"):
            rewritten[key] = cls._rewrite_local_absolute_series_path(
                rewritten.get(key),
                library_type=library_type,
                old_display_name=old_display_name,
                new_display_name=new_display_name,
            )
        return rewritten

    @classmethod
    def _rewrite_subtitle_sidecar_manifest_for_rename(
        cls,
        payload: dict[str, Any],
        *,
        library_type: LibraryType | str,
        old_display_name: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        rewritten = json.loads(json.dumps(payload))
        rewritten["source_path"] = cls._rewrite_local_absolute_series_path(
            rewritten.get("source_path"),
            library_type=library_type,
            old_display_name=old_display_name,
            new_display_name=new_display_name,
        )
        return rewritten

    @classmethod
    def _rewrite_torrent_metadata_for_rename(
        cls,
        payload: dict[str, Any],
        *,
        library_type: LibraryType | str,
        old_display_name: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        rewritten = json.loads(json.dumps(payload))
        torrents = rewritten.get("torrents", [])
        if not isinstance(torrents, list):
            return rewritten
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            files = torrent.get("files", [])
            if not isinstance(files, list):
                continue
            for file_mapping in files:
                if not isinstance(file_mapping, dict):
                    continue
                file_mapping["library_path"] = cls._rewrite_local_absolute_series_path(
                    file_mapping.get("library_path"),
                    library_type=library_type,
                    old_display_name=old_display_name,
                    new_display_name=new_display_name,
                )
        return rewritten

    @classmethod
    def _rewrite_remote_json_artifact_for_rename(
        cls,
        payload: dict[str, Any],
        *,
        library_type: LibraryType | str,
        relative_path: str,
        old_display_name: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        path = PurePosixPath(relative_path)
        if path.name == "manifest.fragment.json":
            return cls._rewrite_manifest_fragment_for_rename(
                payload,
                old_display_name=old_display_name,
                new_display_name=new_display_name,
            )
        if path.name == "state.fragment.json":
            return cls._rewrite_state_fragment_for_rename(
                payload,
                old_display_name=old_display_name,
                new_display_name=new_display_name,
            )
        if path.name.endswith(AnimeLibraryService.SOURCE_IMPORT_MANIFEST_SUFFIX):
            return cls._rewrite_source_import_payload_for_rename(
                payload,
                library_type=library_type,
                old_display_name=old_display_name,
                new_display_name=new_display_name,
            )
        if path.name == TORRENT_METADATA_FILENAME:
            return cls._rewrite_torrent_metadata_for_rename(
                payload,
                library_type=library_type,
                old_display_name=old_display_name,
                new_display_name=new_display_name,
            )
        if (
            path.name == "manifest.json"
            and any(part.endswith(AnimeLibraryService.SUBTITLE_SIDECAR_SUFFIX) for part in path.parts)
        ):
            return cls._rewrite_subtitle_sidecar_manifest_for_rename(
                payload,
                library_type=library_type,
                old_display_name=old_display_name,
                new_display_name=new_display_name,
            )
        raise RuntimeError(f"Unsupported rename rewrite target: {relative_path}")

    @classmethod
    def _rewrite_series_manifest_for_rename(
        cls,
        manifest: dict[str, Any],
        *,
        old_display_name: str,
        new_display_name: str,
        new_release_id: str,
    ) -> dict[str, Any]:
        rewritten = json.loads(json.dumps(manifest))
        rewritten["display_name"] = new_display_name
        rewritten["release_id"] = new_release_id
        rewritten["created_at"] = _utc_now_iso()
        rewritten["torrent_metadata_relative_path"] = cls._rewrite_payload_library_relative_path(
            rewritten.get("torrent_metadata_relative_path"),
            old_display_name=old_display_name,
            new_display_name=new_display_name,
        )

        episodes = rewritten.get("episodes", [])
        if isinstance(episodes, list):
            for episode in episodes:
                if not isinstance(episode, dict):
                    continue
                media = episode.get("media", {})
                if isinstance(media, dict):
                    media["relative_path"] = cls._rewrite_payload_library_relative_path(
                        media.get("relative_path"),
                        old_display_name=old_display_name,
                        new_display_name=new_display_name,
                    )
                    media["local_relative_path"] = cls._rewrite_local_relative_series_path(
                        media.get("local_relative_path"),
                        old_display_name=old_display_name,
                        new_display_name=new_display_name,
                    )
                sidecars = episode.get("sidecars", [])
                if not isinstance(sidecars, list):
                    continue
                for sidecar in sidecars:
                    if not isinstance(sidecar, dict):
                        continue
                    sidecar["relative_path"] = cls._rewrite_payload_library_relative_path(
                        sidecar.get("relative_path"),
                        old_display_name=old_display_name,
                        new_display_name=new_display_name,
                    )
                    sidecar["local_relative_path"] = cls._rewrite_local_relative_series_path(
                        sidecar.get("local_relative_path"),
                        old_display_name=old_display_name,
                        new_display_name=new_display_name,
                    )

        artifacts = rewritten.get("artifacts", [])
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                artifact["relative_path"] = cls._rewrite_payload_library_relative_path(
                    artifact.get("relative_path"),
                    old_display_name=old_display_name,
                    new_display_name=new_display_name,
                )
                artifact["local_relative_path"] = cls._rewrite_local_relative_series_path(
                    artifact.get("local_relative_path"),
                    old_display_name=old_display_name,
                    new_display_name=new_display_name,
                )
        return rewritten

    @classmethod
    def _refresh_manifest_episode_artifact_metadata(
        cls,
        manifest: dict[str, Any],
        artifact_entries_by_relative_path: dict[str, dict[str, Any]],
    ) -> None:
        episodes = manifest.get("episodes", [])
        if not isinstance(episodes, list):
            return
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            media = episode.get("media", {})
            if isinstance(media, dict):
                relative_path = str(media.get("relative_path") or "")
                artifact_entry = artifact_entries_by_relative_path.get(relative_path)
                if artifact_entry is not None:
                    media["local_relative_path"] = artifact_entry.get("local_relative_path")
                    media["size_bytes"] = artifact_entry.get("size_bytes")
                    media["sha256"] = artifact_entry.get("sha256")
            sidecars = episode.get("sidecars", [])
            if not isinstance(sidecars, list):
                continue
            for sidecar in sidecars:
                if not isinstance(sidecar, dict):
                    continue
                relative_path = str(sidecar.get("relative_path") or "")
                artifact_entry = artifact_entries_by_relative_path.get(relative_path)
                if artifact_entry is None:
                    continue
                sidecar["local_relative_path"] = artifact_entry.get("local_relative_path")
                sidecar["size_bytes"] = artifact_entry.get("size_bytes")
                sidecar["sha256"] = artifact_entry.get("sha256")

    @classmethod
    async def rename_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        new_display_name: str,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        if not cls.is_enabled():
            raise RuntimeError("Storage Box is not configured.")

        current = await cls.get_current_release(scoped_type, series_id)
        current_release_id = str(current["release_id"])
        manifest = await cls.get_series_manifest(
            scoped_type,
            series_id,
            current_release_id,
        )
        old_display_name = str(manifest.get("display_name") or "").strip()
        target_display_name = str(new_display_name or "").strip()
        if not old_display_name:
            raise RuntimeError(f"Series '{series_id}' is missing a display name.")
        if not target_display_name:
            raise ValueError("Series rename requires a non-empty target name.")
        if target_display_name == old_display_name:
            return {
                "series_id": series_id,
                "release_id": current_release_id,
                "old_name": old_display_name,
                "new_name": old_display_name,
                "manifest": manifest,
                "current": current,
            }

        publish_id = uuid.uuid4().hex[:12]
        new_release_id = _uuid7_string()
        staging_root = cls._staging_root(scoped_type, series_id, publish_id)
        release_root = cls._release_root(scoped_type, series_id, new_release_id)
        previous_release_root = cls._release_root(scoped_type, series_id, current_release_id)
        renamed_manifest = cls._rewrite_series_manifest_for_rename(
            manifest,
            old_display_name=old_display_name,
            new_display_name=target_display_name,
            new_release_id=new_release_id,
        )

        temp_root = Path(tempfile.mkdtemp(prefix="storage_box_rename_", dir=settings.cache_dir))
        upload_artifacts: list[LocalArtifact] = []
        verify_artifacts: list[LocalArtifact] = []
        hardlink_artifacts: list[tuple[LocalArtifact, PurePosixPath]] = []
        artifact_entries_by_relative_path: dict[str, dict[str, Any]] = {}

        try:
            rewritten_artifacts: list[dict[str, Any]] = []
            for raw_artifact in manifest.get("artifacts", []):
                if not isinstance(raw_artifact, dict):
                    continue
                old_relative_path = str(raw_artifact.get("relative_path") or "").strip()
                if not old_relative_path:
                    continue
                new_relative_path = str(
                    cls._rewrite_payload_library_relative_path(
                        old_relative_path,
                        old_display_name=old_display_name,
                        new_display_name=target_display_name,
                    )
                )
                local_relative_path = cls._rewrite_local_relative_series_path(
                    raw_artifact.get("local_relative_path"),
                    old_display_name=old_display_name,
                    new_display_name=target_display_name,
                )
                artifact_entry = {
                    "relative_path": new_relative_path,
                    "local_relative_path": local_relative_path,
                    "size_bytes": int(raw_artifact.get("size_bytes", 0) or 0),
                    "sha256": str(raw_artifact.get("sha256") or ""),
                    "artifact_type": str(raw_artifact.get("artifact_type") or ""),
                }

                if cls._rename_artifact_needs_content_rewrite(old_relative_path):
                    payload = await cls._read_remote_json(
                        previous_release_root / old_relative_path,
                        context=f"rename source artifact {old_relative_path}",
                    )
                    rewritten_payload = cls._rewrite_remote_json_artifact_for_rename(
                        payload,
                        library_type=scoped_type,
                        relative_path=old_relative_path,
                        old_display_name=old_display_name,
                        new_display_name=target_display_name,
                    )
                    temp_path = temp_root / PurePosixPath(new_relative_path)
                    temp_path.parent.mkdir(parents=True, exist_ok=True)
                    temp_path.write_text(_json_dumps(rewritten_payload), encoding="utf-8")
                    artifact_entry["size_bytes"] = temp_path.stat().st_size
                    artifact_entry["sha256"] = _sha256_file(temp_path)
                    local_artifact = LocalArtifact(
                        local_path=temp_path,
                        remote_relative_path=PurePosixPath(new_relative_path),
                        size_bytes=int(artifact_entry["size_bytes"]),
                        sha256=str(artifact_entry["sha256"]),
                        artifact_type=str(artifact_entry["artifact_type"]),
                        local_relative_path=(
                            str(local_relative_path) if isinstance(local_relative_path, str) else None
                        ),
                    )
                    upload_artifacts.append(local_artifact)
                    verify_artifacts.append(local_artifact)
                else:
                    placeholder_path = temp_root / PurePosixPath(new_relative_path)
                    local_artifact = LocalArtifact(
                        local_path=placeholder_path,
                        remote_relative_path=PurePosixPath(new_relative_path),
                        size_bytes=int(artifact_entry["size_bytes"]),
                        sha256=str(artifact_entry["sha256"]),
                        artifact_type=str(artifact_entry["artifact_type"]),
                        local_relative_path=(
                            str(local_relative_path) if isinstance(local_relative_path, str) else None
                        ),
                    )
                    verify_artifacts.append(local_artifact)
                    hardlink_artifacts.append(
                        (
                            local_artifact,
                            previous_release_root / old_relative_path,
                        )
                    )

                rewritten_artifacts.append(artifact_entry)
                artifact_entries_by_relative_path[new_relative_path] = artifact_entry

            renamed_manifest["artifacts"] = rewritten_artifacts
            cls._refresh_manifest_episode_artifact_metadata(
                renamed_manifest,
                artifact_entries_by_relative_path,
            )
            manifest_text = _json_dumps(renamed_manifest)

            async def _upload_artifact(artifact: LocalArtifact) -> None:
                await StorageBoxTransferService.upload_file(
                    artifact.local_path,
                    staging_root / artifact.remote_relative_path,
                )

            await _run_bounded(
                upload_artifacts,
                settings.storage_box_upload_max_parallel,
                _upload_artifact,
            )

            if hardlink_artifacts:
                hardlink_ok = await cls._try_hardlink_first(
                    hardlink_artifacts[0][1],
                    staging_root / hardlink_artifacts[0][0].remote_relative_path,
                )
                if hardlink_ok:
                    remaining = hardlink_artifacts[1:]
                    if remaining:
                        async def _hardlink_artifact(
                            item: tuple[LocalArtifact, PurePosixPath],
                        ) -> None:
                            artifact, source_remote = item
                            await StorageBoxSftpClient.hardlink(
                                source_remote,
                                staging_root / artifact.remote_relative_path,
                            )

                        await _run_bounded(
                            remaining,
                            settings.storage_box_upload_max_parallel,
                            _hardlink_artifact,
                        )
                else:
                    logger.warning(
                        "Hardlink unsupported on this Storage Box during rename; "
                        "falling back to upload for %d artifact(s)",
                        len(hardlink_artifacts),
                    )
                    async def _copy_remote_artifact(
                        item: tuple[LocalArtifact, PurePosixPath],
                    ) -> None:
                        artifact, source_remote = item
                        artifact.local_path.parent.mkdir(parents=True, exist_ok=True)
                        await StorageBoxTransferService.download_file(
                            source_remote,
                            artifact.local_path,
                        )
                        await StorageBoxTransferService.upload_file(
                            artifact.local_path,
                            staging_root / artifact.remote_relative_path,
                        )

                    await _run_bounded(
                        hardlink_artifacts,
                        settings.storage_box_upload_max_parallel,
                        _copy_remote_artifact,
                    )

            await cls._verify_remote_artifacts(
                staging_root=staging_root,
                artifacts=verify_artifacts,
            )
            await StorageBoxSftpClient.write_text(
                staging_root / "series_manifest.json",
                manifest_text,
            )
            await StorageBoxSftpClient.rename(staging_root, release_root)

            current_payload = {
                "schema_version": SCHEMA_VERSION,
                "series_id": series_id,
                "release_id": new_release_id,
                "published_at": _utc_now_iso(),
                "display_name": target_display_name,
                "manifest_checksum": _sha256_text(manifest_text),
            }
            current_path = cls._current_path(scoped_type, series_id)
            tmp_current = current_path.with_name(f"current.{publish_id}.tmp")
            await cls._write_remote_json(tmp_current, current_payload)
            await StorageBoxSftpClient.replace_file(tmp_current, current_path)
            await cls.rebuild_catalog(scoped_type)
            return {
                "series_id": series_id,
                "release_id": new_release_id,
                "old_name": old_display_name,
                "new_name": target_display_name,
                "manifest": renamed_manifest,
                "current": current_payload,
            }
        except Exception:
            with suppress(Exception):
                await StorageBoxSftpClient.remove_tree(staging_root)
            raise
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    @classmethod
    async def publish_series(
        cls,
        *,
        library_type: LibraryType | str,
        display_name: str,
        series_id: str | None = None,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        if not cls.is_enabled():
            raise RuntimeError("Storage Box is not configured.")

        series_dir = AnimeLibraryService.get_library_path(scoped_type) / display_name
        if not series_dir.exists():
            raise RuntimeError(f"Local series directory not found: {series_dir}")

        effective_series_id = await cls._resolve_or_create_series_id(
            library_type=scoped_type,
            display_name=display_name,
            series_dir=series_dir,
            requested_series_id=series_id,
        )
        publish_id = uuid.uuid4().hex[:12]
        release_id = _uuid7_string()
        staging_root = cls._staging_root(scoped_type, effective_series_id, publish_id)
        release_root = cls._release_root(scoped_type, effective_series_id, release_id)

        # -- Fetch previous release manifest for incremental diffing ----------
        previous_sha_to_remote: dict[str, PurePosixPath] = {}
        try:
            prev_current = await cls.get_current_release(scoped_type, effective_series_id)
            prev_release_id = str(prev_current["release_id"])
            prev_release_root = cls._release_root(
                scoped_type, effective_series_id, prev_release_id,
            )
            prev_manifest = await cls.get_series_manifest(
                scoped_type, effective_series_id, prev_release_id,
            )
            for art in prev_manifest.get("artifacts", []):
                sha = art.get("sha256")
                rel = art.get("relative_path")
                if sha and rel:
                    previous_sha_to_remote[sha] = prev_release_root / PurePosixPath(rel)
        except Exception:
            logger.debug(
                "No previous release found for %s; will do full upload",
                effective_series_id,
            )

        artifacts, episodes, temp_root = await asyncio.to_thread(
            cls._collect_series_artifacts,
            library_type=scoped_type,
            series_dir=series_dir,
            display_name=display_name,
            series_id=effective_series_id,
        )

        try:
            torrent_metadata_relative_path: str | None = None
            torrent_count = 0
            for artifact in artifacts:
                if artifact.remote_relative_path.name == ".atr_torrents.json":
                    torrent_metadata_relative_path = artifact.remote_relative_path.as_posix()
                    try:
                        raw_torrents = json.loads(artifact.local_path.read_text(encoding="utf-8"))
                        if isinstance(raw_torrents, dict):
                            torrent_count = len(raw_torrents.get("torrents", []))
                    except (OSError, json.JSONDecodeError):
                        torrent_count = 0
                    break

            manifest_payload = {
                "schema_version": SCHEMA_VERSION,
                "library_type": scoped_type.value,
                "series_id": effective_series_id,
                "release_id": release_id,
                "display_name": display_name,
                "fps": float(
                    (
                        cls._read_local_index_series_payload(
                            library_type=scoped_type,
                            display_name=display_name,
                        )[0]
                        .get("series", {})
                        .get(display_name, {})
                        .get("fps")
                    )
                    or 0.0
                ),
                "created_at": _utc_now_iso(),
                "episode_count": len(episodes),
                "total_size_bytes": sum(item["media"]["size_bytes"] for item in episodes),
                "torrent_count": torrent_count,
                "torrent_metadata_relative_path": torrent_metadata_relative_path,
                "episodes": episodes,
                "artifacts": [
                    {
                        "relative_path": artifact.remote_relative_path.as_posix(),
                        "local_relative_path": artifact.local_relative_path,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "artifact_type": artifact.artifact_type,
                    }
                    for artifact in artifacts
                ],
            }
            manifest_text = _json_dumps(manifest_payload)

            # -- Partition artifacts: upload new/changed, hardlink unchanged ----
            to_upload: list[LocalArtifact] = []
            to_hardlink: list[tuple[LocalArtifact, PurePosixPath]] = []
            for artifact in artifacts:
                prev_remote = previous_sha_to_remote.get(artifact.sha256)
                if prev_remote is not None:
                    to_hardlink.append((artifact, prev_remote))
                else:
                    to_upload.append(artifact)

            if to_hardlink:
                upload_bytes = sum(a.size_bytes for a in to_upload)
                link_bytes = sum(a.size_bytes for a, _ in to_hardlink)
                logger.info(
                    "Publish %s: %d artifact(s) to upload (%s), "
                    "%d unchanged artifact(s) to hardlink (%s)",
                    display_name,
                    len(to_upload),
                    _human_size(upload_bytes),
                    len(to_hardlink),
                    _human_size(link_bytes),
                )

            async def _upload_artifact(artifact: LocalArtifact) -> None:
                await StorageBoxTransferService.upload_file(
                    artifact.local_path,
                    staging_root / artifact.remote_relative_path,
                )

            await _run_bounded(
                to_upload,
                settings.storage_box_upload_max_parallel,
                _upload_artifact,
            )

            # Hardlink unchanged artifacts from previous release.
            # If hardlinks fail (server doesn't support them), fall back to
            # a regular upload for the remaining artifacts.
            if to_hardlink:
                hardlink_ok = await cls._try_hardlink_first(
                    to_hardlink[0][1],
                    staging_root / to_hardlink[0][0].remote_relative_path,
                )
                if hardlink_ok:
                    # First hardlink succeeded — do the rest in parallel.
                    remaining = to_hardlink[1:]
                    if remaining:
                        async def _hardlink_artifact(
                            item: tuple[LocalArtifact, PurePosixPath],
                        ) -> None:
                            artifact, source_remote = item
                            await StorageBoxSftpClient.hardlink(
                                source_remote,
                                staging_root / artifact.remote_relative_path,
                            )

                        await _run_bounded(
                            remaining,
                            settings.storage_box_upload_max_parallel,
                            _hardlink_artifact,
                        )
                else:
                    # Hardlinks not supported — upload all unchanged artifacts.
                    logger.warning(
                        "Hardlink unsupported on this Storage Box; "
                        "falling back to upload for %d artifact(s)",
                        len(to_hardlink),
                    )
                    await _run_bounded(
                        [a for a, _ in to_hardlink],
                        settings.storage_box_upload_max_parallel,
                        _upload_artifact,
                    )

            await cls._verify_remote_artifacts(staging_root=staging_root, artifacts=artifacts)
            await StorageBoxSftpClient.write_text(
                staging_root / "series_manifest.json",
                manifest_text,
            )
            await StorageBoxSftpClient.rename(staging_root, release_root)

            current_payload = {
                "schema_version": SCHEMA_VERSION,
                "series_id": effective_series_id,
                "release_id": release_id,
                "published_at": _utc_now_iso(),
                "display_name": display_name,
                "manifest_checksum": _sha256_text(manifest_text),
            }
            current_path = cls._current_path(scoped_type, effective_series_id)
            tmp_current = current_path.with_name(f"current.{publish_id}.tmp")
            with suppress(Exception):
                existing_current = await StorageBoxSftpClient.read_text(current_path)
                backup_path = series_dir / ".atr_storage_box.current.backup.json"
                backup_path.write_text(existing_current, encoding="utf-8")
            await cls._write_remote_json(tmp_current, current_payload)
            await StorageBoxSftpClient.replace_file(tmp_current, current_path)
            await cls.rebuild_catalog(scoped_type)
            await asyncio.to_thread(
                cls.write_local_series_metadata,
                series_dir=series_dir,
                series_id=effective_series_id,
                display_name=display_name,
                release_id=release_id,
            )
            return {
                "series_id": effective_series_id,
                "release_id": release_id,
                "manifest": manifest_payload,
                "current": current_payload,
            }
        except Exception:
            with suppress(Exception):
                await StorageBoxSftpClient.remove_tree(staging_root)
            raise
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    @classmethod
    async def delete_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> None:
        scoped_type = coerce_library_type(library_type)
        if not cls.is_enabled():
            raise RuntimeError("Storage Box is not configured.")

        series_root = cls._series_root(scoped_type, series_id)
        if await StorageBoxSftpClient.exists(series_root):
            await StorageBoxSftpClient.remove_tree(series_root)
        await cls.rebuild_catalog(scoped_type)
