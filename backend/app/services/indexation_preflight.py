from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..library_types import LibraryType, coerce_library_type
from .anime_library import AnimeLibraryService
from .storage_box_repository import StorageBoxRepository


logger = logging.getLogger("uvicorn.error")


RESOLUTION_NEW = "new"
RESOLUTION_EXACT_MATCH = "exact_match"
RESOLUTION_UPDATE_REQUIRED = "update_required"
RESOLUTION_NEEDS_FIX = "needs_fix"
RESOLUTION_BLOCKED_ORPHAN = "blocked_orphan"


class IndexationPreflightService:
    """Remote-authoritative indexing/update preflight decisions."""

    @staticmethod
    def normalize_target_name(value: Any) -> str:
        return str(value or "").strip().casefold()

    @staticmethod
    def _batch_episode_stems(video_names: list[str]) -> set[str]:
        return {Path(video_name).stem for video_name in video_names}

    @classmethod
    def _find_first_video_dir(cls, root: Path) -> str | None:
        from collections import deque

        queue = deque([root])
        while queue:
            current = queue.popleft()
            try:
                children = sorted(current.iterdir())
            except PermissionError:
                continue
            for child in children:
                if not child.is_dir() or child.name.startswith("."):
                    continue
                try:
                    has_videos = any(
                        file.suffix.lower() in AnimeLibraryService.VIDEO_EXTENSIONS
                        for file in child.iterdir()
                        if file.is_file()
                    )
                except (PermissionError, OSError):
                    continue
                if has_videos:
                    return str(child)
                queue.append(child)
        return None

    @classmethod
    async def _resolve_remote_series(
        cls,
        *,
        library_type: LibraryType | str,
        display_name: str,
    ) -> dict[str, Any] | None:
        entry = None
        try:
            entry = await StorageBoxRepository.find_catalog_entry_by_name(
                library_type,
                display_name,
            )
        except Exception:
            logger.debug(
                "Catalog lookup failed during preflight for %s/%s",
                library_type,
                display_name,
                exc_info=True,
            )

        series_id = str(entry.get("series_id") or "").strip() if isinstance(entry, dict) else ""
        if not series_id:
            try:
                series_id = str(
                    await StorageBoxRepository.find_remote_series_id_by_name(
                        library_type,
                        display_name,
                    )
                    or ""
                ).strip()
            except Exception:
                logger.debug(
                    "Remote series lookup failed during preflight for %s/%s",
                    library_type,
                    display_name,
                    exc_info=True,
                )
                series_id = ""

        if not series_id:
            return None

        current = await StorageBoxRepository.get_current_release(library_type, series_id)
        manifest = await StorageBoxRepository.get_series_manifest(
            library_type,
            series_id,
            str(current["release_id"]),
        )
        return {
            "series_id": series_id,
            "release_id": str(current["release_id"]),
            "current": current,
            "manifest": manifest,
            "catalog_entry": entry if isinstance(entry, dict) else None,
        }

    @classmethod
    def _remote_episode_stems(cls, manifest: dict[str, Any]) -> set[str]:
        episode_stems: set[str] = set()
        for episode in manifest.get("episodes", []):
            if not isinstance(episode, dict):
                continue
            episode_key = str(episode.get("episode_key") or "").strip()
            if episode_key:
                episode_stems.add(episode_key)
                continue
            media = episode.get("media", {})
            if not isinstance(media, dict):
                continue
            local_relative_path = str(media.get("local_relative_path") or "").strip()
            if local_relative_path:
                episode_stems.add(Path(local_relative_path).stem)
        return episode_stems

    @classmethod
    def _local_collision_reason(
        cls,
        *,
        library_type: LibraryType | str,
        display_name: str,
        remote_series_id: str | None,
    ) -> str | None:
        local_series_dir = AnimeLibraryService.get_library_path(library_type) / display_name
        if not local_series_dir.exists() or not local_series_dir.is_dir():
            return None

        metadata = StorageBoxRepository.read_local_series_metadata(local_series_dir)
        local_series_id = (
            str(metadata.get("series_id") or "").strip()
            if isinstance(metadata, dict)
            else ""
        )

        if remote_series_id:
            if local_series_id == remote_series_id:
                return None
            return (
                "Un dossier local orphelin ou incohérent existe déjà pour cette série. "
                "Nettoyez-le avant de continuer."
            )

        return (
            "Un dossier local orphelin existe déjà pour cette série. "
            "Nettoyez-le avant d'indexer une nouvelle source."
        )

    @classmethod
    async def preflight_source(
        cls,
        *,
        source_path: str | Path,
        library_type: LibraryType | str,
        anime_name: str | None = None,
    ) -> dict[str, Any]:
        scoped_type = coerce_library_type(library_type)
        folder = Path(source_path)
        display_name = (anime_name or folder.name).strip()

        result: dict[str, Any] = {
            "path": str(folder),
            "name": display_name,
            "has_videos": False,
            "suggested_path": None,
            "resolution": RESOLUTION_NEW,
            "series_id": None,
            "storage_release_id": None,
            "conflict_details": None,
            "orphan_reason": None,
            "invalid_video_files": [],
        }

        source_scan = await asyncio.to_thread(
            AnimeLibraryService.scan_direct_video_files_sync,
            folder,
        )
        source_video_names = [path.name for path in source_scan.readable_files]
        invalid_video_files = [path.name for path in source_scan.invalid_files]
        has_videos = source_scan.has_direct_videos
        suggested_path = None if has_videos else cls._find_first_video_dir(folder)

        result["has_videos"] = has_videos
        result["suggested_path"] = suggested_path
        result["invalid_video_files"] = invalid_video_files

        if not source_video_names:
            if invalid_video_files:
                result["resolution"] = RESOLUTION_NEEDS_FIX
                return result
            result["resolution"] = RESOLUTION_NEEDS_FIX
            return result

        remote = await cls._resolve_remote_series(
            library_type=scoped_type,
            display_name=display_name,
        )
        remote_series_id = str(remote["series_id"]) if remote else None
        result["series_id"] = remote_series_id
        result["storage_release_id"] = str(remote["release_id"]) if remote else None

        orphan_reason = cls._local_collision_reason(
            library_type=scoped_type,
            display_name=display_name,
            remote_series_id=remote_series_id,
        )
        if orphan_reason:
            result["resolution"] = RESOLUTION_BLOCKED_ORPHAN
            result["orphan_reason"] = orphan_reason
            return result

        if remote is None:
            result["resolution"] = RESOLUTION_NEW
            return result

        manifest = remote["manifest"]
        remote_episode_stems = cls._remote_episode_stems(manifest)
        source_episode_stems = cls._batch_episode_stems(source_video_names)
        new_episodes = sorted(source_episode_stems - remote_episode_stems)
        removed_episodes = sorted(remote_episode_stems - source_episode_stems)

        if not new_episodes and not removed_episodes:
            result["resolution"] = RESOLUTION_EXACT_MATCH
            return result

        result["resolution"] = RESOLUTION_UPDATE_REQUIRED
        result["conflict_details"] = {
            "new_episodes": new_episodes,
            "removed_episodes": removed_episodes,
            "existing_episode_count": int(
                manifest.get("episode_count", len(manifest.get("episodes", [])))
            ),
            "existing_torrent_count": int(manifest.get("torrent_count", 0) or 0),
        }
        return result

    @classmethod
    async def validate_batch_items(
        cls,
        *,
        items: list[dict[str, Any]],
        library_type: LibraryType | str,
    ) -> list[dict[str, Any]]:
        seen_targets: set[str] = set()
        results: list[dict[str, Any]] = []

        for item in items:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            display_name = str(item.get("name") or Path(path).name).strip()
            target_key = cls.normalize_target_name(display_name)
            if target_key and target_key in seen_targets:
                results.append(
                    {
                        "path": path,
                        "name": display_name,
                        "has_videos": False,
                        "suggested_path": None,
                        "resolution": RESOLUTION_BLOCKED_ORPHAN,
                        "series_id": None,
                        "storage_release_id": None,
                        "conflict_details": None,
                        "orphan_reason": (
                            "Plusieurs dossiers du batch ciblent la même série. "
                            "Gardez un seul dossier par série."
                        ),
                        "invalid_video_files": [],
                    }
                )
                continue

            if target_key:
                seen_targets.add(target_key)

            result = await cls.preflight_source(
                source_path=path,
                library_type=library_type,
                anime_name=display_name,
            )
            results.append(result)

        return results
