"""Deferred download: recover missing episodes from original source or via qBittorrent."""

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..config import settings
from ..models.torrent import TorrentEntry, TorrentFileMapping
from .torrent_linker import TorrentLinkerService

logger = logging.getLogger("uvicorn.error")

STALL_TIMEOUT_SECONDS = 45


@dataclass
class MissingEpisode:
    episode_path: str
    source_name: str
    torrent_entry: TorrentEntry | None = None
    file_mapping: TorrentFileMapping | None = None
    original_source_path: str | None = None  # from .atr_source.json


@dataclass
class DownloadPlan:
    torrent_entry: TorrentEntry
    needed_files: list[TorrentFileMapping]
    temp_download_dir: str  # temp dir for torrent download
    library_source_dir: str  # where remuxed .mp4 should end up
    source_name: str = ""


class DeferredDownloadService:

    @classmethod
    def _resolve_episode_path(
        cls,
        episode_ref: str,
        source_dir: Path,
    ) -> Path | None:
        """Resolve a match episode reference to a full library path.

        ``episode_ref`` may be an absolute path, a relative path with extension,
        or a bare stem like ``"Chio's School Road - S01E02"``.  We try, in order:
        1. Absolute path that exists on disk.
        2. Direct file inside ``source_dir`` (with and without ``.mp4``).
        3. Lookup via ``.atr_source.json`` sidecar — even if the ``.mp4`` was
           purged the sidecar's ``prepared_path`` tells us what the canonical
           library path *should* be.
        """
        from .anime_library import AnimeLibraryService

        p = Path(episode_ref)

        # 1. Already an absolute, existing path
        if p.is_absolute() and p.exists():
            return p

        # 2. Direct lookup in source_dir
        for candidate in (source_dir / p.name, source_dir / f"{p.stem}.mp4"):
            if candidate.exists():
                return candidate

        # 3. Sidecar-based resolution (works even after purge)
        for suffix in (".mp4", ""):
            stem = p.stem if p.suffix else str(p)
            sidecar = source_dir / f"{stem}{suffix}{AnimeLibraryService.SOURCE_IMPORT_MANIFEST_SUFFIX}"
            if sidecar.exists():
                try:
                    payload = json.loads(sidecar.read_text(encoding="utf-8"))
                    prepared = payload.get("prepared_path")
                    if prepared:
                        return Path(prepared)
                except (json.JSONDecodeError, OSError):
                    pass

        # 4. Match against .atr_torrents.json library_path entries by stem
        metadata = TorrentLinkerService.load_metadata(source_dir)
        if metadata:
            target_stem = p.stem if p.suffix else str(p)
            for te in metadata.torrents:
                for fm in te.files:
                    if Path(fm.library_path).stem == target_stem:
                        return Path(fm.library_path)

        return None

    @staticmethod
    def check_missing_sources(
        match_episodes: list[str],
        library_root: Path,
        source_name: str,
    ) -> list[MissingEpisode]:
        """Check which matched episodes are missing from disk.

        Handles both absolute paths and bare episode names (e.g.
        ``"Chio's School Road - S01E02"``).  Reads ``.atr_source.json``
        sidecars to find original source paths for recovery without
        re-downloading.
        """
        from .anime_library import AnimeLibraryService

        missing = []
        source_dir = library_root / source_name
        metadata = TorrentLinkerService.load_metadata(source_dir)

        for ep_ref in match_episodes:
            # Resolve bare name / relative ref to an absolute library path
            resolved = DeferredDownloadService._resolve_episode_path(
                ep_ref, source_dir
            )
            # Fall back to constructing a plausible path
            if resolved is None:
                stem = Path(ep_ref).stem if Path(ep_ref).suffix else ep_ref
                resolved = source_dir / f"{stem}.mp4"

            if resolved.exists():
                continue

            torrent_entry = None
            file_mapping = None
            original_source_path = None

            # Try to find original source via .atr_source.json sidecar
            manifest = AnimeLibraryService._load_source_import_manifest_sync(resolved)
            if manifest and manifest.get("source_path"):
                src = Path(manifest["source_path"])
                if src.exists():
                    original_source_path = str(src)

            # Look up torrent info from .atr_torrents.json
            if metadata:
                resolved_stem = resolved.stem
                for te in metadata.torrents:
                    for fm in te.files:
                        if Path(fm.library_path).stem == resolved_stem:
                            torrent_entry = te
                            file_mapping = fm
                            break
                    if torrent_entry:
                        break

            missing.append(
                MissingEpisode(
                    episode_path=str(resolved),
                    source_name=source_name,
                    torrent_entry=torrent_entry,
                    file_mapping=file_mapping,
                    original_source_path=original_source_path,
                )
            )
        return missing

    @staticmethod
    async def recover_from_sources(
        recoverable: list[MissingEpisode],
    ) -> AsyncIterator[dict]:
        """Copy+remux episodes from their original source location."""
        from .anime_library import AnimeLibraryService

        total = len(recoverable)
        for i, m in enumerate(recoverable):
            ep_name = Path(m.episode_path).stem
            yield {
                "status": "recovering",
                "phase": "recover",
                "message": f"Recovering {ep_name}...",
                "episode": ep_name,
                "progress": i / max(total, 1),
            }
            try:
                dest_dir = Path(m.episode_path).parent
                dest_dir.mkdir(parents=True, exist_ok=True)
                actual_dest, action, _changed = (
                    await AnimeLibraryService._prepare_single_source_for_library(
                        source_path=Path(m.original_source_path),
                        dest_dir=dest_dir,
                    )
                )
                logger.info(
                    "Recovered %s via %s -> %s",
                    ep_name,
                    action,
                    actual_dest,
                )
            except Exception as exc:
                logger.error("Failed to recover %s: %s", ep_name, exc)
                yield {
                    "status": "error",
                    "phase": "recover",
                    "message": f"Failed to recover {ep_name}: {exc}",
                    "episode": ep_name,
                    "error": str(exc),
                }
                return

        yield {
            "status": "recovering",
            "phase": "recover",
            "message": f"Recovered {total} episode(s)",
            "progress": 1.0,
        }

    @staticmethod
    def plan_downloads(missing: list[MissingEpisode]) -> list[DownloadPlan]:
        """Group missing episodes by torrent for efficient downloading.

        Downloads go to a temp directory to avoid double-nesting from the
        torrent's internal folder structure.
        """
        by_torrent: dict[str, DownloadPlan] = {}
        for m in missing:
            if not m.torrent_entry or not m.file_mapping:
                continue
            key = m.torrent_entry.info_hash
            if key not in by_torrent:
                temp_dir = settings.cache_dir / "deferred_downloads" / key
                by_torrent[key] = DownloadPlan(
                    torrent_entry=m.torrent_entry,
                    needed_files=[],
                    temp_download_dir=str(temp_dir),
                    library_source_dir=str(Path(m.episode_path).parent),
                    source_name=m.source_name,
                )
            by_torrent[key].needed_files.append(m.file_mapping)
        return list(by_torrent.values())

    @staticmethod
    async def execute_downloads(
        plans: list[DownloadPlan], qbt: "QBittorrentClient"
    ) -> None:
        """Add torrents to qBittorrent, select only needed files."""
        for plan in plans:
            temp_dir = Path(plan.temp_download_dir)
            temp_dir.mkdir(parents=True, exist_ok=True)

            await qbt.add_torrent(
                plan.torrent_entry.magnet_uri, plan.temp_download_dir
            )
            await asyncio.sleep(3)
            files = await qbt.get_torrent_files(
                plan.torrent_entry.info_hash
            )
            needed_indices = {
                fm.torrent_file_index for fm in plan.needed_files
            }
            skip_indices = [
                i for i in range(len(files)) if i not in needed_indices
            ]
            if skip_indices:
                await qbt.set_file_priority(
                    plan.torrent_entry.info_hash, skip_indices, 0
                )

    @staticmethod
    async def watch_downloads(
        plans: list[DownloadPlan], qbt: "QBittorrentClient"
    ) -> AsyncIterator[dict]:
        """Poll until all needed files are downloaded. Yields progress events.

        Detects stalled torrents (no progress for STALL_TIMEOUT_SECONDS) and
        yields a ``torrent_failed`` event so the frontend can offer replacement.
        """
        pending_hashes = {
            p.torrent_entry.info_hash: p for p in plans
        }
        last_progress: dict[str, float] = {h: 0.0 for h in pending_hashes}
        last_change: dict[str, float] = {h: time.monotonic() for h in pending_hashes}
        failed_hashes: set[str] = set()

        while pending_hashes:
            for info_hash, plan in list(pending_hashes.items()):
                files = await qbt.get_torrent_files(info_hash)
                needed_indices = {
                    fm.torrent_file_index for fm in plan.needed_files
                }
                all_done = all(
                    files[idx].get("progress", 0) >= 1.0
                    for idx in needed_indices
                    if idx < len(files)
                )
                total_progress = sum(
                    files[idx].get("progress", 0)
                    for idx in needed_indices
                    if idx < len(files)
                ) / max(len(needed_indices), 1)

                now = time.monotonic()
                if total_progress > last_progress[info_hash]:
                    last_progress[info_hash] = total_progress
                    last_change[info_hash] = now

                if (
                    not all_done
                    and info_hash not in failed_hashes
                    and (now - last_change[info_hash]) >= STALL_TIMEOUT_SECONDS
                ):
                    failed_hashes.add(info_hash)
                    logger.warning(
                        "Torrent stalled for %ss: %s (%s)",
                        STALL_TIMEOUT_SECONDS,
                        plan.torrent_entry.torrent_name,
                        info_hash,
                    )
                    yield {
                        "status": "torrent_failed",
                        "phase": "download",
                        "torrent": plan.torrent_entry.torrent_name,
                        "torrent_id": plan.torrent_entry.id,
                        "source_name": plan.source_name,
                        "info_hash": info_hash,
                        "message": f"Torrent stalled ({STALL_TIMEOUT_SECONDS}s sans progrès)",
                    }
                    del pending_hashes[info_hash]
                    continue

                yield {
                    "status": "downloading",
                    "phase": "download",
                    "torrent": plan.torrent_entry.torrent_name,
                    "progress": total_progress,
                    "complete": all_done,
                    "message": f"Downloading {plan.torrent_entry.torrent_name}... {int(total_progress * 100)}%",
                }
                if all_done:
                    del pending_hashes[info_hash]
            if pending_hashes:
                await asyncio.sleep(2)

    @staticmethod
    async def remux_downloaded_files(
        plans: list[DownloadPlan],
    ) -> AsyncIterator[dict]:
        """Remux downloaded .mkv files from temp dir to library as .mp4."""
        from .anime_library import AnimeLibraryService

        total_files = sum(len(p.needed_files) for p in plans)
        done = 0

        for plan in plans:
            temp_dir = Path(plan.temp_download_dir)
            library_dir = Path(plan.library_source_dir)
            library_dir.mkdir(parents=True, exist_ok=True)

            for fm in plan.needed_files:
                downloaded_path = temp_dir / fm.torrent_filename
                ep_name = Path(fm.torrent_filename).stem

                yield {
                    "status": "remuxing",
                    "phase": "remux",
                    "message": f"Preparing {ep_name}...",
                    "episode": ep_name,
                    "progress": done / max(total_files, 1),
                }

                if not downloaded_path.exists():
                    logger.error(
                        "Downloaded file not found: %s", downloaded_path
                    )
                    yield {
                        "status": "error",
                        "phase": "remux",
                        "message": f"Downloaded file not found: {ep_name}",
                        "error": f"File missing after download: {downloaded_path}",
                    }
                    return

                try:
                    actual_dest, action, _changed = (
                        await AnimeLibraryService._prepare_single_source_for_library(
                            source_path=downloaded_path,
                            dest_dir=library_dir,
                        )
                    )
                    logger.info(
                        "Remuxed %s via %s -> %s", ep_name, action, actual_dest
                    )
                    done += 1
                except Exception as exc:
                    logger.error("Failed to remux %s: %s", ep_name, exc)
                    yield {
                        "status": "error",
                        "phase": "remux",
                        "message": f"Failed to prepare {ep_name}: {exc}",
                        "error": str(exc),
                    }
                    return

            # Clean up temp download directory
            try:
                await asyncio.to_thread(shutil.rmtree, temp_dir, ignore_errors=True)
            except Exception:
                pass

        yield {
            "status": "remuxing",
            "phase": "remux",
            "message": f"Prepared {done} episode(s)",
            "progress": 1.0,
        }

    @classmethod
    async def recover_missing_episodes(
        cls,
        match_episodes: list[str],
        library_root: Path,
        source_name: str,
    ) -> AsyncIterator[dict]:
        """Top-level orchestrator: check, recover from source, download, remux, verify.

        Yields SSE-friendly dicts with ``status`` and ``phase`` fields.
        """
        # --- Phase: check ---
        yield {
            "status": "checking",
            "phase": "check",
            "message": "Checking source files...",
            "progress": 0,
        }

        missing = cls.check_missing_sources(match_episodes, library_root, source_name)

        if not missing:
            yield {
                "status": "complete",
                "phase": "check",
                "message": "All source files present",
                "progress": 1.0,
            }
            return

        logger.info(
            "Deferred download: %d missing episode(s) for %s",
            len(missing),
            source_name,
        )

        # Partition: recoverable from source vs needs torrent download
        recoverable = [m for m in missing if m.original_source_path]
        needs_download = [m for m in missing if not m.original_source_path]

        # --- Phase: recover ---
        if recoverable:
            logger.info(
                "%d episode(s) recoverable from original source", len(recoverable)
            )
            async for event in cls.recover_from_sources(recoverable):
                yield event
                if event.get("status") == "error":
                    return

        # --- Phase: download ---
        if needs_download:
            no_torrent = [m for m in needs_download if not m.torrent_entry]
            if no_torrent:
                names = [Path(m.episode_path).name for m in no_torrent]
                yield {
                    "status": "error",
                    "phase": "download",
                    "message": f"{len(no_torrent)} missing file(s) without torrent link: {', '.join(names)}",
                    "error": f"No torrent link for: {', '.join(names)}",
                }
                return

            plans = cls.plan_downloads(needs_download)
            if plans:
                from .qbittorrent import QBittorrentClient

                qbt = QBittorrentClient()
                try:
                    yield {
                        "status": "downloading",
                        "phase": "download",
                        "message": f"Downloading {len(needs_download)} episode(s)...",
                        "progress": 0,
                    }

                    await cls.execute_downloads(plans, qbt)

                    has_failure = False
                    async for event in cls.watch_downloads(plans, qbt):
                        yield event
                        if event.get("status") == "torrent_failed":
                            has_failure = True

                    if has_failure:
                        return

                    # --- Phase: remux ---
                    async for event in cls.remux_downloaded_files(plans):
                        yield event
                        if event.get("status") == "error":
                            return
                finally:
                    await qbt.close()

        # --- Phase: verify ---
        still_missing = [
            ep for ep in match_episodes if not Path(ep).exists()
        ]
        if still_missing:
            names = [Path(p).name for p in still_missing]
            yield {
                "status": "error",
                "phase": "verify",
                "message": f"Still missing after recovery: {', '.join(names)}",
                "error": f"Files still missing: {', '.join(names)}",
            }
            return

        yield {
            "status": "complete",
            "phase": "verify",
            "message": f"All {len(match_episodes)} episode(s) ready",
            "progress": 1.0,
        }
