"""Torrent verification service — verifies replacement torrents against FAISS index."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import statistics
from pathlib import Path

import cv2

from ..config import settings
from ..library_types import LibraryType
from ..models.torrent import TorrentEntry, TorrentFileMapping, VerificationResult
from .anime_library import AnimeLibraryService
from .anime_matcher import AnimeMatcherService
from .qbittorrent import QBittorrentClient

logger = logging.getLogger("uvicorn.error")

VIDEO_EXTENSIONS = AnimeLibraryService.VIDEO_EXTENSIONS

# Episode number extraction patterns (ordered by specificity)
EPISODE_PATTERNS = [
    re.compile(r"S\d+E(\d+)", re.IGNORECASE),  # S01E01
    re.compile(r"- (\d{2,4})(?:\s|v\d|\[|\.)"),  # - 01, - 001
    re.compile(r"Episode\s*(\d+)", re.IGNORECASE),  # Episode 01
    re.compile(r"Ep\.?\s*(\d+)", re.IGNORECASE),  # Ep 01, Ep.01
    re.compile(r"E(\d{2,4})(?:\s|\[|\.)"),  # E01 (standalone)
    re.compile(r"(?:^|\s)(\d{2,3})(?:\s|v\d|\[|\.)"),  # bare 01/001 with boundaries
]

NUM_VERIFICATION_FRAMES = 50
STALL_TIMEOUT_SECONDS = 45
MAX_STALL_RETRIES = 3
DOWNLOAD_TIMEOUT_SECONDS = 600  # 10 min overall


def extract_episode_number(filename: str) -> int | None:
    """Extract episode number from a filename using common patterns."""
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(filename)
        if m:
            return int(m.group(1))
    return None


class TorrentVerificationService:
    """Verifies that a replacement torrent contains the same content as the original."""

    @staticmethod
    def pick_random_video_file(
        torrent_files: list[dict],
        torrent_mappings: list[TorrentFileMapping],
    ) -> tuple[int, str, TorrentFileMapping] | None:
        """Pick a random video file from the torrent that matches an indexed episode.

        Tries up to 3 random files to find one that matches an existing mapping.

        Args:
            torrent_files: File list from qBittorrent API.
            torrent_mappings: Existing TorrentFileMapping entries for this torrent.

        Returns:
            (file_index, file_name, matched_mapping) or None if no match found.
        """
        # Build lookup of indexed episode numbers -> mapping
        indexed_by_episode_num: dict[int, TorrentFileMapping] = {}
        for mapping in torrent_mappings:
            ep_num = extract_episode_number(mapping.torrent_filename)
            if ep_num is not None:
                indexed_by_episode_num[ep_num] = mapping

        # Filter video files from the torrent
        video_files = [
            (i, f)
            for i, f in enumerate(torrent_files)
            if Path(f.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
        ]

        if not video_files:
            return None

        # Try up to 3 random files
        random.shuffle(video_files)
        for file_index, file_info in video_files[:3]:
            filename = file_info.get("name", "")
            ep_num = extract_episode_number(Path(filename).name)
            if ep_num is not None and ep_num in indexed_by_episode_num:
                return file_index, filename, indexed_by_episode_num[ep_num]

        # Fallback: if we have exactly one mapping and one video, assume match
        if len(torrent_mappings) == 1 and len(video_files) == 1:
            file_index, file_info = video_files[0]
            return file_index, file_info.get("name", ""), torrent_mappings[0]

        return None

    @classmethod
    async def download_verification_file(
        cls,
        torrent_entry: TorrentEntry,
        new_magnet: str,
        qbt: QBittorrentClient,
        *,
        on_progress: object = None,
    ) -> tuple[Path, TorrentFileMapping, str]:
        """Download 1 random video file from a new magnet URI for verification.

        Args:
            torrent_entry: The existing TorrentEntry being replaced.
            new_magnet: The new magnet URI.
            qbt: qBittorrent client.
            on_progress: Optional async callback(progress_float, message_str).

        Returns:
            (downloaded_file_path, matched_mapping, new_info_hash)

        Raises:
            RuntimeError: If download fails or no matching file found.
            TimeoutError: If metadata or download times out.
        """
        # Extract info_hash from magnet URI
        hash_match = re.search(r"btih:([a-fA-F0-9]{40})", new_magnet)
        if not hash_match:
            # Try base32 encoded hash
            hash_match = re.search(r"btih:([A-Za-z2-7]{32})", new_magnet)
        if not hash_match:
            raise RuntimeError("Cannot extract info_hash from magnet URI")

        new_info_hash = hash_match.group(1).lower()
        tmp_dir = Path(f"/tmp/atr_verify_{torrent_entry.id}")
        tmp_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Add torrent to qBittorrent
            await qbt.add_torrent(new_magnet, str(tmp_dir))

            # Wait for metadata
            if on_progress:
                await on_progress(0.05, "Attente des métadonnées du torrent...")
            torrent_files = await qbt.wait_for_torrent_metadata(
                new_info_hash, timeout=60
            )

            # Pick a random indexed video file
            pick = cls.pick_random_video_file(torrent_files, torrent_entry.files)
            if pick is None:
                raise RuntimeError(
                    "Impossible de trouver un épisode indexé dans ce torrent."
                )

            file_index, file_name, matched_mapping = pick

            # Set priorities: only download the chosen file
            all_indices = list(range(len(torrent_files)))
            if all_indices:
                await qbt.set_file_priority(new_info_hash, all_indices, 0)
            await qbt.set_file_priority(new_info_hash, [file_index], 7)

            if on_progress:
                await on_progress(
                    0.1,
                    f"Téléchargement de {Path(file_name).name}...",
                )

            # Watch download with stall detection
            last_progress = 0.0
            stall_start = asyncio.get_event_loop().time()
            retries = 0
            deadline = asyncio.get_event_loop().time() + DOWNLOAD_TIMEOUT_SECONDS

            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2)
                files = await qbt.get_torrent_files(new_info_hash)
                if not files or file_index >= len(files):
                    continue

                current_progress = files[file_index].get("progress", 0.0)

                if current_progress >= 1.0:
                    # Download complete
                    downloaded_path = tmp_dir / file_name
                    if downloaded_path.exists():
                        return downloaded_path, matched_mapping, new_info_hash
                    # Try finding it with just the filename
                    for p in tmp_dir.rglob("*"):
                        if p.is_file() and p.name == Path(file_name).name:
                            return p, matched_mapping, new_info_hash
                    raise RuntimeError(
                        f"Download complete but file not found at {downloaded_path}"
                    )

                if current_progress > last_progress:
                    last_progress = current_progress
                    stall_start = asyncio.get_event_loop().time()
                    if on_progress:
                        dl_pct = 0.1 + current_progress * 0.4
                        await on_progress(
                            dl_pct,
                            f"Téléchargement: {current_progress * 100:.0f}%",
                        )
                elif (
                    asyncio.get_event_loop().time() - stall_start
                    >= STALL_TIMEOUT_SECONDS
                ):
                    retries += 1
                    if retries > MAX_STALL_RETRIES:
                        raise RuntimeError(
                            f"Téléchargement bloqué après {MAX_STALL_RETRIES} tentatives. "
                            "Vérifiez votre VPN ou le torrent."
                        )
                    # Retry: remove and re-add
                    logger.warning(
                        "Download stalled for %ds, retry %d/%d",
                        STALL_TIMEOUT_SECONDS,
                        retries,
                        MAX_STALL_RETRIES,
                    )
                    await qbt.delete_torrent(new_info_hash, delete_files=True)
                    await asyncio.sleep(1)
                    await qbt.add_torrent(new_magnet, str(tmp_dir))
                    await asyncio.sleep(3)
                    try:
                        await qbt.wait_for_torrent_metadata(new_info_hash, timeout=30)
                        await qbt.set_file_priority(
                            new_info_hash, all_indices, 0
                        )
                        await qbt.set_file_priority(
                            new_info_hash, [file_index], 7
                        )
                    except TimeoutError:
                        pass
                    stall_start = asyncio.get_event_loop().time()

            raise TimeoutError("Download timeout exceeded")
        except Exception:
            # Cleanup on failure
            await cls.cleanup_verification(new_info_hash, qbt, tmp_dir)
            raise

    @classmethod
    async def verify_file(
        cls,
        video_path: Path,
        series_name: str,
        expected_episode: str,
        library_type: LibraryType | str,
        *,
        on_progress: object = None,
    ) -> VerificationResult:
        """Verify a downloaded video against the existing FAISS index.

        Extracts 50 frames, searches each against the index, computes
        match_rate / avg_similarity / offset_median, and returns a verdict.

        Args:
            video_path: Path to the downloaded video file.
            series_name: Name of the series in the library.
            expected_episode: Episode identifier expected from TorrentFileMapping.
            library_type: Library type for the series.
            on_progress: Optional async callback(progress_float, message_str).

        Returns:
            VerificationResult with pass/warn/fail status.
        """
        loop = asyncio.get_event_loop()
        library_path = AnimeLibraryService.get_library_path(library_type)

        # Initialize FAISS searcher
        init_ok = await loop.run_in_executor(
            None,
            AnimeMatcherService._init_searcher,
            library_path,
            library_type,
            series_name,
        )
        if not init_ok:
            return VerificationResult(
                torrent_id="",
                status="fail",
                match_rate=0.0,
                avg_similarity=0.0,
                offset_median=0.0,
                message="Impossible d'initialiser l'index FAISS.",
            )

        # Get video duration
        cap = cv2.VideoCapture(str(video_path))
        try:
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0 or frame_count <= 0:
                return VerificationResult(
                    torrent_id="",
                    status="fail",
                    match_rate=0.0,
                    avg_similarity=0.0,
                    offset_median=0.0,
                    message="Impossible de lire la durée de la vidéo.",
                )
            duration = frame_count / fps
        finally:
            cap.release()

        # Compute evenly spaced timestamps
        timestamps = [
            duration * i / NUM_VERIFICATION_FRAMES
            for i in range(NUM_VERIFICATION_FRAMES)
        ]

        # Extract frames
        if on_progress:
            await on_progress(0.55, "Extraction des frames...")
        frames = await loop.run_in_executor(
            None,
            AnimeMatcherService.extract_frames,
            video_path,
            timestamps,
        )

        # Filter out failed extractions
        valid_pairs: list[tuple[int, object]] = [
            (i, frame) for i, frame in enumerate(frames) if frame is not None
        ]
        if len(valid_pairs) < 10:
            return VerificationResult(
                torrent_id="",
                status="fail",
                match_rate=0.0,
                avg_similarity=0.0,
                offset_median=0.0,
                message=f"Seulement {len(valid_pairs)} frames extraites sur {NUM_VERIFICATION_FRAMES}.",
            )

        valid_images = [img for _, img in valid_pairs]
        valid_indices = [i for i, _ in valid_pairs]

        # Search all frames in batch
        if on_progress:
            await on_progress(0.65, "Recherche dans l'index FAISS...")

        from functools import partial

        search_fn = partial(
            AnimeMatcherService._search_image_batch,
            valid_images,
            top_n=1,
            threshold=None,
            flip=False,
            series=series_name,
        )
        all_results = await loop.run_in_executor(None, search_fn)

        # Compute metrics
        # expected_episode is the episode stem (filename without extension) from the mapping
        expected_ep_stem = Path(expected_episode).stem
        matched_count = 0
        similarities: list[float] = []
        offsets: list[float] = []

        for result_idx, results in enumerate(all_results):
            if not results:
                continue

            top_result = results[0]
            frame_index = valid_indices[result_idx]
            source_timestamp = timestamps[frame_index]

            # Check if the result matches the expected episode
            result_episode = getattr(top_result, "episode", "")
            if result_episode == expected_ep_stem:
                matched_count += 1
                similarity = getattr(top_result, "similarity", 0.0)
                matched_timestamp = getattr(top_result, "timestamp", 0.0)
                similarities.append(similarity)
                offsets.append(abs(source_timestamp - matched_timestamp))

            if on_progress and result_idx % 10 == 0:
                pct = 0.65 + 0.3 * (result_idx / len(all_results))
                await on_progress(
                    pct,
                    f"Analyse des frames ({result_idx + 1}/{len(all_results)})",
                )

        total_valid = len(valid_pairs)
        match_rate = matched_count / total_valid if total_valid > 0 else 0.0
        avg_similarity = statistics.mean(similarities) if similarities else 0.0
        offset_median = statistics.median(offsets) if offsets else 999.0

        if on_progress:
            await on_progress(0.95, "Calcul du verdict...")

        # Decision: PASS first, then FAIL, then WARN (catch-all)
        if match_rate >= 0.85 and avg_similarity >= 0.75 and offset_median < 0.5:
            status = "pass"
            message = (
                f"Contenu identique confirmé. "
                f"Match: {match_rate:.0%}, Similarité: {avg_similarity:.2f}, "
                f"Offset: {offset_median:.2f}s"
            )
        elif match_rate < 0.60 or avg_similarity < 0.55:
            status = "fail"
            message = (
                f"Contenu différent détecté. "
                f"Match: {match_rate:.0%}, Similarité: {avg_similarity:.2f}"
            )
        else:
            status = "warn"
            message = (
                f"Même contenu mais décalage temporel détecté. "
                f"Match: {match_rate:.0%}, Similarité: {avg_similarity:.2f}, "
                f"Offset: {offset_median:.2f}s. Réindexation nécessaire."
            )

        return VerificationResult(
            torrent_id="",  # Will be set by caller
            status=status,
            match_rate=match_rate,
            avg_similarity=avg_similarity,
            offset_median=offset_median,
            message=message,
        )

    @staticmethod
    async def cleanup_verification(
        info_hash: str,
        qbt: QBittorrentClient,
        tmp_dir: Path,
    ) -> None:
        """Clean up verification torrent and temp files."""
        try:
            await qbt.delete_torrent(info_hash, delete_files=True)
        except Exception as e:
            logger.warning("Failed to delete verification torrent: %s", e)

        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
        except Exception as e:
            logger.warning("Failed to clean up temp dir %s: %s", tmp_dir, e)
