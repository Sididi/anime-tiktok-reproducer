"""Link source video files to their original torrent downloads."""

import logging
from pathlib import Path

from ..config import settings
from ..models.torrent import (
    SourceTorrentMetadata,
    TorrentEntry,
    TorrentFileMapping,
)

logger = logging.getLogger("uvicorn.error")

TORRENT_METADATA_FILENAME = ".atr_torrents.json"


class TorrentLinkerService:

    @staticmethod
    async def link_files_to_torrents(
        source_files: list[Path],
        qbt: "QBittorrentClient",
    ) -> tuple[SourceTorrentMetadata, list[Path]]:
        """
        Match source files to torrents.

        Returns (metadata with matched torrents, list of unmatched files).
        """
        source_names: dict[str, Path] = {f.name: f for f in source_files}
        unmatched = set(source_names.keys())
        torrent_entries: dict[str, TorrentEntry] = {}

        # Strategy 1: qBittorrent API
        try:
            torrents = await qbt.list_torrents()
            for t in torrents:
                t_hash = t["hash"]
                t_files = await qbt.get_torrent_files(t_hash)
                matched_files = []
                for idx, tf in enumerate(t_files):
                    tf_name = Path(tf["name"]).name
                    if tf_name in unmatched:
                        matched_files.append(
                            TorrentFileMapping(
                                torrent_file_index=idx,
                                torrent_filename=tf["name"],
                                library_path=str(source_names[tf_name]),
                                file_size=tf.get("size", 0),
                            )
                        )
                        unmatched.discard(tf_name)
                if matched_files:
                    torrent_entries[t_hash] = TorrentEntry(
                        info_hash=t_hash,
                        magnet_uri=f"magnet:?xt=urn:btih:{t_hash}",
                        torrent_name=t.get("name", ""),
                        files=matched_files,
                    )
        except Exception:
            logger.debug("qBittorrent API unavailable for torrent linking")

        # Strategy 2: Parse .torrent files from .complete directory
        if unmatched and settings.torrent_complete_dir.exists():
            try:
                import torf
            except ImportError:
                logger.debug("torf not installed, skipping .torrent fallback")
            else:
                for torrent_path in settings.torrent_complete_dir.glob(
                    "*.torrent"
                ):
                    try:
                        t = torf.Torrent.read(torrent_path)
                        t_hash = t.infohash
                        if t_hash in torrent_entries:
                            # Already matched via API, just store torrent_file_path
                            torrent_entries[t_hash].torrent_file_path = str(
                                torrent_path
                            )
                            continue
                        matched_files = []
                        for idx, tf in enumerate(t.files):
                            tf_name = Path(tf).name
                            if tf_name in unmatched:
                                matched_files.append(
                                    TorrentFileMapping(
                                        torrent_file_index=idx,
                                        torrent_filename=str(tf),
                                        library_path=str(
                                            source_names[tf_name]
                                        ),
                                        file_size=0,
                                    )
                                )
                                unmatched.discard(tf_name)
                        if matched_files:
                            torrent_entries[t_hash] = TorrentEntry(
                                info_hash=t_hash,
                                magnet_uri=t.magnet(),
                                torrent_name=t.name or torrent_path.stem,
                                torrent_file_path=str(torrent_path),
                                files=matched_files,
                            )
                    except Exception:
                        continue

        metadata = SourceTorrentMetadata(
            torrents=list(torrent_entries.values()),
        )
        unmatched_files = [source_names[name] for name in unmatched]
        return metadata, unmatched_files

    @staticmethod
    def save_metadata(
        library_source_dir: Path, metadata: SourceTorrentMetadata
    ) -> None:
        path = library_source_dir / TORRENT_METADATA_FILENAME
        path.write_text(metadata.model_dump_json(indent=2))

    @staticmethod
    def load_metadata(
        library_source_dir: Path,
    ) -> SourceTorrentMetadata | None:
        path = library_source_dir / TORRENT_METADATA_FILENAME
        if not path.exists():
            return None
        return SourceTorrentMetadata.model_validate_json(path.read_text())
