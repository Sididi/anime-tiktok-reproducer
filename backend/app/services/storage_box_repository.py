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


logger = logging.getLogger("uvicorn.error")


SCHEMA_VERSION = 1
REPOSITORY_VERSION = "v1"
LOCAL_STORAGE_BOX_METADATA = ".atr_storage_box.json"
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

        normalized_items = [dict(item) for item in items if isinstance(item, dict)]
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

        payload = {
            "schema_version": SCHEMA_VERSION,
            "library_type": scoped_type.value,
            "updated_at": _utc_now_iso(),
            "items": sorted(
                items,
                key=lambda item: str(item.get("name", "")).casefold(),
            ),
        }

        catalog_path = cls._catalog_path(scoped_type)
        tmp_path = catalog_path.with_name(f"catalog.{uuid.uuid4().hex[:8]}.tmp")
        await cls._write_remote_json(tmp_path, payload)
        await StorageBoxSftpClient.rename(tmp_path, catalog_path)
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
    async def _verify_remote_artifacts(
        cls,
        *,
        staging_root: PurePosixPath,
        artifacts: list[LocalArtifact],
    ) -> None:
        for artifact in artifacts:
            remote_path = staging_root / artifact.remote_relative_path
            stat_result = await StorageBoxSftpClient.stat(remote_path)
            remote_size = int(getattr(stat_result, "size", 0))
            if remote_size != artifact.size_bytes:
                raise RuntimeError(
                    f"Remote size mismatch for {artifact.remote_relative_path.as_posix()}: "
                    f"expected {artifact.size_bytes}, got {remote_size}"
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

        return _uuid7_string()

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

            for artifact in artifacts:
                await StorageBoxSftpClient.upload_file(
                    artifact.local_path,
                    staging_root / artifact.remote_relative_path,
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
            await StorageBoxSftpClient.rename(tmp_current, current_path)
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
