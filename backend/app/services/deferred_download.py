"""Deferred download: detect missing episodes after matching, download via qBittorrent."""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..models.torrent import TorrentEntry, TorrentFileMapping
from .qbittorrent import QBittorrentClient
from .torrent_linker import TorrentLinkerService

logger = logging.getLogger("uvicorn.error")

STALL_TIMEOUT_SECONDS = 45


@dataclass
class MissingEpisode:
    episode_path: str
    source_name: str
    torrent_entry: TorrentEntry | None = None
    file_mapping: TorrentFileMapping | None = None


@dataclass
class DownloadPlan:
    torrent_entry: TorrentEntry
    needed_files: list[TorrentFileMapping]
    save_path: str
    source_name: str = ""


class DeferredDownloadService:

    @staticmethod
    def check_missing_sources(
        match_episodes: list[str],
        library_root: Path,
        source_name: str,
    ) -> list[MissingEpisode]:
        """Check which matched episodes are missing from disk."""
        missing = []
        metadata = TorrentLinkerService.load_metadata(library_root / source_name)

        for ep_path in match_episodes:
            if not Path(ep_path).exists():
                torrent_entry = None
                file_mapping = None
                if metadata:
                    for te in metadata.torrents:
                        for fm in te.files:
                            if Path(fm.library_path).name == Path(ep_path).name:
                                torrent_entry = te
                                file_mapping = fm
                                break
                        if torrent_entry:
                            break
                missing.append(
                    MissingEpisode(
                        episode_path=ep_path,
                        source_name=source_name,
                        torrent_entry=torrent_entry,
                        file_mapping=file_mapping,
                    )
                )
        return missing

    @staticmethod
    def plan_downloads(missing: list[MissingEpisode]) -> list[DownloadPlan]:
        """Group missing episodes by torrent for efficient downloading."""
        by_torrent: dict[str, DownloadPlan] = {}
        for m in missing:
            if not m.torrent_entry or not m.file_mapping:
                continue
            key = m.torrent_entry.info_hash
            if key not in by_torrent:
                by_torrent[key] = DownloadPlan(
                    torrent_entry=m.torrent_entry,
                    needed_files=[],
                    save_path=str(Path(m.episode_path).parent),
                    source_name=m.source_name,
                )
            by_torrent[key].needed_files.append(m.file_mapping)
        return list(by_torrent.values())

    @staticmethod
    async def execute_downloads(
        plans: list[DownloadPlan], qbt: QBittorrentClient
    ) -> None:
        """Add torrents to qBittorrent, select only needed files."""
        for plan in plans:
            await qbt.add_torrent(
                plan.torrent_entry.magnet_uri, plan.save_path
            )
            # Wait for torrent metadata to load in qBittorrent
            await asyncio.sleep(3)
            # Set priorities: skip files we don't need
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
        plans: list[DownloadPlan], qbt: QBittorrentClient
    ) -> AsyncIterator[dict]:
        """Poll until all needed files are downloaded. Yields progress events.

        Detects stalled torrents (no progress for STALL_TIMEOUT_SECONDS) and
        yields a ``torrent_failed`` event so the frontend can offer replacement.
        """
        pending_hashes = {
            p.torrent_entry.info_hash: p for p in plans
        }
        # Track last-seen progress per torrent for stall detection
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

                # Stall detection
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
                        "type": "torrent_failed",
                        "torrent": plan.torrent_entry.torrent_name,
                        "torrent_id": plan.torrent_entry.id,
                        "source_name": plan.source_name,
                        "info_hash": info_hash,
                        "message": f"Torrent stalled ({STALL_TIMEOUT_SECONDS}s sans progrès)",
                    }
                    del pending_hashes[info_hash]
                    continue

                yield {
                    "type": "download_progress",
                    "torrent": plan.torrent_entry.torrent_name,
                    "progress": total_progress,
                    "complete": all_done,
                }
                if all_done:
                    del pending_hashes[info_hash]
            if pending_hashes:
                await asyncio.sleep(2)
