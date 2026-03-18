"""Torrent replacement orchestrator — manages the full replace + verify + reindex pipeline."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import AsyncIterator

from ..config import settings
from ..library_types import LibraryType, coerce_library_type
from ..models.torrent import (
    ConfirmReindexRequest,
    ReplacementProgress,
    ReplaceTorrentsRequest,
    TorrentEntry,
    TorrentFileMapping,
    VerificationResult,
)
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService
from .qbittorrent import QBittorrentClient
from .torrent_linker import TorrentLinkerService
from .torrent_verification import (
    TorrentVerificationService,
    extract_episode_number,
)

logger = logging.getLogger("uvicorn.error")

# How long verification results are kept before expiring (seconds).
_REPLACEMENT_STATE_TTL = 600  # 10 minutes


class _ReplacementState:
    """Transient state persisted between verify and confirm-reindex requests."""

    def __init__(
        self,
        source_name: str,
        library_type: LibraryType,
        results: list[VerificationResult],
        new_magnets: dict[str, str],  # torrent_id -> new_magnet_uri
        new_info_hashes: dict[str, str],  # torrent_id -> new_info_hash
    ) -> None:
        self.source_name = source_name
        self.library_type = library_type
        self.results = results
        self.new_magnets = new_magnets
        self.new_info_hashes = new_info_hashes
        self.created_at = time.monotonic()

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > _REPLACEMENT_STATE_TTL


class TorrentReplacerService:
    """Orchestrates torrent replacement: verify, save metadata, and reindex."""

    # Per-source locks to prevent concurrent replacements.
    _locks: dict[str, asyncio.Lock] = {}
    # Transient state between verify and confirm-reindex endpoints.
    _replacement_state: dict[str, _ReplacementState] = {}

    @classmethod
    def _get_lock(cls, source_name: str) -> asyncio.Lock:
        if source_name not in cls._locks:
            cls._locks[source_name] = asyncio.Lock()
        return cls._locks[source_name]

    @classmethod
    def _cleanup_expired_states(cls) -> None:
        expired = [k for k, v in cls._replacement_state.items() if v.expired]
        for k in expired:
            del cls._replacement_state[k]

    @classmethod
    async def replace_torrents(
        cls,
        request: ReplaceTorrentsRequest,
        qbt: QBittorrentClient,
    ) -> AsyncIterator[ReplacementProgress]:
        """Run the verification pipeline for torrent replacements.

        Yields ReplacementProgress events for SSE streaming. On completion,
        yields a 'results' phase event with verification_results. PASS torrents
        have their metadata saved immediately. WARN torrent state is persisted
        for a subsequent confirm-reindex call.
        """
        cls._cleanup_expired_states()
        source_name = request.source_name
        library_type = coerce_library_type(request.library_type)
        library_path = AnimeLibraryService.get_library_path(library_type)
        source_dir = library_path / source_name

        # Load existing metadata
        metadata = TorrentLinkerService.load_metadata(source_dir)
        if not metadata:
            yield ReplacementProgress(
                phase="error",
                error="Aucune métadonnée torrent trouvée pour cette source.",
            )
            return

        torrent_by_id: dict[str, TorrentEntry] = {t.id: t for t in metadata.torrents}
        results: list[VerificationResult] = []
        new_magnets: dict[str, str] = {}
        new_info_hashes: dict[str, str] = {}

        for replacement in request.replacements:
            tid = replacement.torrent_id
            new_magnet = replacement.new_magnet_uri
            torrent_entry = torrent_by_id.get(tid)

            if not torrent_entry:
                results.append(
                    VerificationResult(
                        torrent_id=tid,
                        status="fail",
                        match_rate=0.0,
                        avg_similarity=0.0,
                        offset_median=0.0,
                        message=f"Torrent ID '{tid}' introuvable.",
                    )
                )
                continue

            # --- Phase: downloading_verification ---
            yield ReplacementProgress(
                phase="downloading_verification",
                torrent_id=tid,
                progress=0.0,
                message=f"Téléchargement du fichier de test pour {torrent_entry.torrent_name}...",
            )

            try:

                async def _dl_progress(pct: float, msg: str) -> None:
                    pass  # Progress is streamed at phase level

                downloaded_path, matched_mapping, new_hash = (
                    await TorrentVerificationService.download_verification_file(
                        torrent_entry,
                        new_magnet,
                        qbt,
                        on_progress=_dl_progress,
                    )
                )
                new_info_hashes[tid] = new_hash
            except Exception as e:
                logger.exception("Verification download failed for torrent %s", tid)
                results.append(
                    VerificationResult(
                        torrent_id=tid,
                        status="fail",
                        match_rate=0.0,
                        avg_similarity=0.0,
                        offset_median=0.0,
                        message=f"Échec du téléchargement: {e}",
                    )
                )
                continue

            # --- Phase: verifying ---
            yield ReplacementProgress(
                phase="verifying",
                torrent_id=tid,
                progress=0.5,
                message=f"Vérification de {Path(downloaded_path).name}...",
            )

            try:
                # The expected episode is identified by the mapping's library_path stem
                expected_episode = Path(matched_mapping.library_path).stem

                result = await TorrentVerificationService.verify_file(
                    downloaded_path,
                    source_name,
                    expected_episode,
                    library_type,
                )
                result.torrent_id = tid
                results.append(result)
                new_magnets[tid] = new_magnet
            except Exception as e:
                logger.exception("Verification failed for torrent %s", tid)
                results.append(
                    VerificationResult(
                        torrent_id=tid,
                        status="fail",
                        match_rate=0.0,
                        avg_similarity=0.0,
                        offset_median=0.0,
                        message=f"Erreur de vérification: {e}",
                    )
                )
            finally:
                # Cleanup verification files
                info_hash = new_info_hashes.get(tid, "")
                if info_hash:
                    tmp_dir = Path(f"/tmp/atr_verify_{tid}")
                    await TorrentVerificationService.cleanup_verification(
                        info_hash, qbt, tmp_dir
                    )

        # --- Phase: saving (for PASS torrents) ---
        pass_ids = [r.torrent_id for r in results if r.status == "pass"]
        if pass_ids:
            yield ReplacementProgress(
                phase="saving",
                progress=0.0,
                message="Sauvegarde des torrents validés...",
            )
            await cls._save_torrent_metadata(
                source_dir,
                metadata,
                pass_ids,
                new_magnets,
                new_info_hashes,
                qbt,
            )

        # Persist state for WARN torrents
        warn_ids = [r.torrent_id for r in results if r.status == "warn"]
        if warn_ids:
            cls._replacement_state[source_name] = _ReplacementState(
                source_name=source_name,
                library_type=library_type,
                results=results,
                new_magnets=new_magnets,
                new_info_hashes=new_info_hashes,
            )

        # --- Phase: results ---
        yield ReplacementProgress(
            phase="results",
            progress=1.0,
            message="Vérification terminée.",
            verification_results=results,
        )

    @classmethod
    async def execute_reindex(
        cls,
        request: ConfirmReindexRequest,
        qbt: QBittorrentClient,
    ) -> AsyncIterator[ReplacementProgress]:
        """Execute reindex for WARN torrents after user confirmation."""
        cls._cleanup_expired_states()
        source_name = request.source_name
        library_type = coerce_library_type(request.library_type)

        state = cls._replacement_state.get(source_name)
        if not state or state.expired:
            yield ReplacementProgress(
                phase="error",
                error="État de remplacement expiré. Veuillez relancer la vérification.",
            )
            return

        library_path = AnimeLibraryService.get_library_path(library_type)
        source_dir = library_path / source_name
        metadata = TorrentLinkerService.load_metadata(source_dir)
        if not metadata:
            yield ReplacementProgress(
                phase="error",
                error="Métadonnées torrent introuvables.",
            )
            return

        torrent_by_id = {t.id: t for t in metadata.torrents}

        # First, save metadata for WARN torrents too
        yield ReplacementProgress(
            phase="saving",
            progress=0.0,
            message="Sauvegarde des métadonnées torrents...",
        )
        await cls._save_torrent_metadata(
            source_dir,
            metadata,
            request.torrent_ids,
            state.new_magnets,
            state.new_info_hashes,
            qbt,
        )
        # Reload metadata after save
        metadata = TorrentLinkerService.load_metadata(source_dir)
        torrent_by_id = {t.id: t for t in metadata.torrents}

        # --- Phase: cache_cleanup (before reindex) ---
        yield ReplacementProgress(
            phase="cache_cleanup",
            progress=0.0,
            message="Nettoyage du cache...",
        )
        await cls._purge_caches(library_type, source_name)

        for tid in request.torrent_ids:
            torrent_entry = torrent_by_id.get(tid)
            if not torrent_entry:
                continue

            # Collect library paths for this torrent's episodes
            episode_library_paths = [
                Path(fm.library_path) for fm in torrent_entry.files
            ]

            if not episode_library_paths:
                continue

            # --- Phase: downloading_reindex ---
            yield ReplacementProgress(
                phase="downloading_reindex",
                torrent_id=tid,
                progress=0.0,
                message=f"Téléchargement de {len(episode_library_paths)} épisode(s)...",
            )

            try:
                await cls._download_episodes_for_reindex(
                    torrent_entry, qbt, source_dir
                )
            except Exception as e:
                logger.exception("Download for reindex failed for %s", tid)
                yield ReplacementProgress(
                    phase="error",
                    torrent_id=tid,
                    error=f"Échec du téléchargement pour réindexation: {e}",
                )
                return

            # --- Phase: removing_old_index ---
            yield ReplacementProgress(
                phase="removing_old_index",
                torrent_id=tid,
                progress=0.3,
                message=f"Suppression des anciens index pour {torrent_entry.torrent_name}...",
            )

            try:
                async for progress in AnimeLibraryService.remove_anime_files(
                    library_type=library_type,
                    anime_name=source_name,
                    library_paths=episode_library_paths,
                ):
                    if progress.status == "error":
                        yield ReplacementProgress(
                            phase="error",
                            torrent_id=tid,
                            error=f"Erreur suppression index: {progress.error}",
                        )
                        return
            except Exception as e:
                logger.exception("Remove index failed for %s", tid)
                yield ReplacementProgress(
                    phase="error",
                    torrent_id=tid,
                    error=f"Erreur suppression index: {e}",
                )
                return

            # --- Phase: reindexing ---
            yield ReplacementProgress(
                phase="reindexing",
                torrent_id=tid,
                progress=0.5,
                message=f"Réindexation de {len(episode_library_paths)} épisode(s)...",
            )

            try:
                source_paths = [
                    source_dir / Path(fm.library_path).name
                    for fm in torrent_entry.files
                ]
                async for progress in AnimeLibraryService.update_anime(
                    library_type=library_type,
                    anime_name=source_name,
                    source_paths=source_paths,
                ):
                    if progress.status == "error":
                        yield ReplacementProgress(
                            phase="error",
                            torrent_id=tid,
                            error=f"Erreur réindexation: {progress.error}",
                        )
                        return
            except Exception as e:
                logger.exception("Reindex failed for %s", tid)
                yield ReplacementProgress(
                    phase="error",
                    torrent_id=tid,
                    error=f"Erreur réindexation: {e}",
                )
                return

        # Invalidate matcher cache
        AnimeMatcherService.mark_series_updated(library_type, source_name)

        # Clean up transient state
        cls._replacement_state.pop(source_name, None)

        yield ReplacementProgress(
            phase="complete",
            progress=1.0,
            message="Remplacement et réindexation terminés.",
        )

    @classmethod
    async def _save_torrent_metadata(
        cls,
        source_dir: Path,
        metadata: object,
        torrent_ids: list[str],
        new_magnets: dict[str, str],
        new_info_hashes: dict[str, str],
        qbt: QBittorrentClient,
    ) -> None:
        """Update TorrentEntry metadata for replaced torrents and save."""
        torrent_by_id = {t.id: t for t in metadata.torrents}

        for tid in torrent_ids:
            torrent_entry = torrent_by_id.get(tid)
            if not torrent_entry or tid not in new_magnets:
                continue

            new_magnet = new_magnets[tid]
            new_hash = new_info_hashes.get(tid, "")

            # Get file list from the new torrent to update mappings
            new_files: list[dict] = []
            if new_hash:
                try:
                    # Temporarily add to get metadata (may already be gone after cleanup)
                    await qbt.add_torrent(new_magnet, str(source_dir))
                    new_files = await qbt.wait_for_torrent_metadata(
                        new_hash, timeout=30
                    )
                except Exception:
                    pass
                finally:
                    try:
                        await qbt.delete_torrent(new_hash, delete_files=False)
                    except Exception:
                        pass

            # Update the entry
            torrent_entry.magnet_uri = new_magnet
            torrent_entry.info_hash = new_hash if new_hash else torrent_entry.info_hash

            # Try to extract torrent name from magnet
            dn_match = re.search(r"dn=([^&]+)", new_magnet)
            if dn_match:
                from urllib.parse import unquote
                torrent_entry.torrent_name = unquote(dn_match.group(1))

            # Update file mappings if we got the new file list
            if new_files:
                cls._update_file_mappings(torrent_entry, new_files)

        TorrentLinkerService.save_metadata(source_dir, metadata)

    @staticmethod
    def _update_file_mappings(
        torrent_entry: TorrentEntry,
        new_files: list[dict],
    ) -> None:
        """Map new torrent files to existing episode mappings by episode number."""
        # Build episode number -> new file mapping
        new_by_episode: dict[int, tuple[int, dict]] = {}
        for i, f in enumerate(new_files):
            name = f.get("name", "")
            ep_num = extract_episode_number(Path(name).name)
            if ep_num is not None:
                new_by_episode[ep_num] = (i, f)

        # Also build alphabetically sorted video lists for positional fallback
        from .anime_library import AnimeLibraryService

        video_exts = AnimeLibraryService.VIDEO_EXTENSIONS
        new_videos_sorted = sorted(
            [(i, f) for i, f in enumerate(new_files) if Path(f.get("name", "")).suffix.lower() in video_exts],
            key=lambda x: x[1].get("name", ""),
        )
        old_mappings_sorted = sorted(
            torrent_entry.files,
            key=lambda m: m.torrent_filename,
        )

        for map_idx, mapping in enumerate(torrent_entry.files):
            old_ep_num = extract_episode_number(
                Path(mapping.torrent_filename).name
            )

            matched = False
            # Strategy 1: match by episode number
            if old_ep_num is not None and old_ep_num in new_by_episode:
                new_idx, new_file = new_by_episode[old_ep_num]
                mapping.torrent_file_index = new_idx
                mapping.torrent_filename = new_file.get("name", "")
                mapping.file_size = new_file.get("size", mapping.file_size)
                matched = True

            # Strategy 2: fallback to positional matching
            if not matched:
                # Find position of this mapping in the sorted old list
                sorted_pos = next(
                    (i for i, m in enumerate(old_mappings_sorted) if m is mapping),
                    None,
                )
                if sorted_pos is not None and sorted_pos < len(new_videos_sorted):
                    new_idx, new_file = new_videos_sorted[sorted_pos]
                    mapping.torrent_file_index = new_idx
                    mapping.torrent_filename = new_file.get("name", "")
                    mapping.file_size = new_file.get("size", mapping.file_size)

    @classmethod
    async def _download_episodes_for_reindex(
        cls,
        torrent_entry: TorrentEntry,
        qbt: QBittorrentClient,
        source_dir: Path,
    ) -> None:
        """Download only the episodes referenced in TorrentFileMapping."""
        await qbt.add_torrent(torrent_entry.magnet_uri, str(source_dir))
        await asyncio.sleep(3)

        try:
            files = await qbt.wait_for_torrent_metadata(
                torrent_entry.info_hash, timeout=60
            )
        except TimeoutError:
            raise RuntimeError("Metadata timeout for reindex download")

        # Set priorities: only needed files
        all_indices = list(range(len(files)))
        needed_indices = [fm.torrent_file_index for fm in torrent_entry.files]

        if all_indices:
            await qbt.set_file_priority(torrent_entry.info_hash, all_indices, 0)
        if needed_indices:
            await qbt.set_file_priority(torrent_entry.info_hash, needed_indices, 7)

        # Wait for downloads to complete
        from .torrent_verification import (
            DOWNLOAD_TIMEOUT_SECONDS,
            MAX_STALL_RETRIES,
            STALL_TIMEOUT_SECONDS,
        )

        last_progress = 0.0
        stall_start = asyncio.get_event_loop().time()
        deadline = asyncio.get_event_loop().time() + DOWNLOAD_TIMEOUT_SECONDS

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            try:
                current_files = await qbt.get_torrent_files(torrent_entry.info_hash)
            except Exception:
                continue

            # Check if all needed files are complete
            all_done = True
            total_progress = 0.0
            for idx in needed_indices:
                if idx < len(current_files):
                    fp = current_files[idx].get("progress", 0.0)
                    total_progress += fp
                    if fp < 1.0:
                        all_done = False

            if all_done and needed_indices:
                return

            avg = total_progress / max(len(needed_indices), 1)
            if avg > last_progress:
                last_progress = avg
                stall_start = asyncio.get_event_loop().time()
            elif (
                asyncio.get_event_loop().time() - stall_start
                >= STALL_TIMEOUT_SECONDS
            ):
                raise RuntimeError(
                    "Téléchargement bloqué. Vérifiez votre VPN ou le torrent."
                )

        raise TimeoutError("Download timeout for reindex")

    @staticmethod
    async def _purge_caches(
        library_type: LibraryType,
        source_name: str,
    ) -> None:
        """Clear caches relevant to a specific source."""
        cache_dir = settings.cache_dir

        # Episode manifest for this library type
        manifest = cache_dir / f"episodes_manifest__{library_type.value}.json"
        if manifest.exists():
            manifest.unlink()
        # Also the unscoped one
        default_manifest = cache_dir / "episodes_manifest.json"
        if default_manifest.exists():
            default_manifest.unlink()

        # Source-specific cache subdirectories
        for subdir_name in ["source_previews", "source_stream_chunks_v1"]:
            source_cache = cache_dir / subdir_name / source_name
            if source_cache.exists():
                shutil.rmtree(source_cache)
