"""Service for managing the anime library (indexing, listing, copying)."""

import asyncio
import json
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from ..config import settings
from ..utils.subprocess_runner import CommandTimeoutError, run_command, terminate_process


@dataclass
class IndexProgress:
    """Progress information for anime indexing."""

    status: str  # starting, copying, indexing, complete, error
    progress: float = 0.0  # 0-1
    message: str = ""
    current_file: str = ""
    total_files: int = 0
    completed_files: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "current_file": self.current_file,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "error": self.error,
        }


class AnimeLibraryService:
    """Service for managing the anime library."""

    VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v"}
    INDEX_DIR_NAME = ".index"
    MANIFEST_FILE = "manifest.json"
    LEGACY_METADATA_FILE = "metadata.json"
    STATE_FILE = "state.json"
    LIST_TIMEOUT_SECONDS = 120.0
    SEARCH_TIMEOUT_SECONDS = 120.0
    REMUX_TIMEOUT_SECONDS = 600.0
    INDEX_TIMEOUT_SECONDS = 7200.0

    _episode_manifest_cache: dict | None = None
    _episode_manifest_lock: asyncio.Lock | None = None

    @staticmethod
    def get_library_path() -> Path:
        """Get the anime library path from settings."""
        return settings.anime_library_path

    @staticmethod
    def get_anime_searcher_path() -> Path:
        """Get the anime_searcher module path."""
        return settings.anime_searcher_path

    @staticmethod
    def _coerce_fps(value: object) -> float | None:
        """Convert candidate FPS value to a positive float when possible."""
        try:
            fps = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if fps <= 0:
            return None
        return fps

    @classmethod
    def _get_indexed_series_fps_sync(cls, anime_name: str) -> float | None:
        """Read FPS for an already indexed series from index metadata."""
        index_dir = cls.get_library_path() / cls.INDEX_DIR_NAME
        manifest_path = index_dir / cls.MANIFEST_FILE
        if manifest_path.exists():
            try:
                payload = json.loads(manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = None

            if isinstance(payload, dict):
                series_map = payload.get("series", {})
                if isinstance(series_map, dict):
                    entry = series_map.get(anime_name)
                    if entry is not None:
                        if isinstance(entry, dict):
                            series_fps = cls._coerce_fps(entry.get("fps"))
                            if series_fps is not None:
                                return series_fps
                        config = payload.get("config", {})
                        if isinstance(config, dict):
                            return cls._coerce_fps(
                                config.get("default_fps", config.get("fps"))
                            )

        # Legacy fallback: single global FPS in metadata config.
        legacy_metadata_path = index_dir / cls.LEGACY_METADATA_FILE
        if not legacy_metadata_path.exists():
            return None

        # Legacy index is global; ensure this series actually exists in state.
        state_path = index_dir / cls.STATE_FILE
        if state_path.exists():
            try:
                state_payload = json.loads(state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state_payload = {}
            files = state_payload.get("files", {}) if isinstance(state_payload, dict) else {}
            if isinstance(files, dict):
                has_series_state = any(
                    path == anime_name or path.startswith(f"{anime_name}/")
                    for path in files
                )
                if not has_series_state:
                    return None

        try:
            legacy_payload = json.loads(legacy_metadata_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(legacy_payload, dict):
            return None
        config = legacy_payload.get("config", {})
        if not isinstance(config, dict):
            return None
        return cls._coerce_fps(config.get("fps"))

    @classmethod
    def get_episode_manifest_path(cls) -> Path:
        """Get path for cached episode index manifest."""
        return settings.cache_dir / "episodes_manifest.json"

    @classmethod
    def _get_manifest_lock(cls) -> asyncio.Lock:
        if cls._episode_manifest_lock is None:
            cls._episode_manifest_lock = asyncio.Lock()
        return cls._episode_manifest_lock

    @classmethod
    def _scan_library_episodes_sync(cls) -> dict:
        """Scan library once and build fast stem -> path index."""
        library_path = cls.get_library_path()
        episodes: list[str] = []
        by_stem: dict[str, list[str]] = {}

        if library_path.exists():
            for entry in library_path.rglob("*"):
                if not entry.is_file() or entry.suffix.lower() not in cls.VIDEO_EXTENSIONS:
                    continue
                resolved = str(entry.resolve())
                episodes.append(resolved)

                stem = entry.stem
                for key in (stem, stem.lower()):
                    by_stem.setdefault(key, []).append(resolved)

        episodes = sorted(set(episodes))
        by_stem = {k: sorted(set(v)) for k, v in by_stem.items()}
        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "library_root": str(library_path.resolve()),
            "episodes": episodes,
            "by_stem": by_stem,
        }

        manifest_path = cls.get_episode_manifest_path()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        cls._episode_manifest_cache = manifest
        return manifest

    @classmethod
    def _load_episode_manifest_sync(cls) -> dict | None:
        """Load cached episode manifest if present."""
        if cls._episode_manifest_cache is not None:
            return cls._episode_manifest_cache

        manifest_path = cls.get_episode_manifest_path()
        if not manifest_path.exists():
            return None

        try:
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest.get("by_stem"), dict):
                return None
            cls._episode_manifest_cache = manifest
            return manifest
        except (json.JSONDecodeError, OSError):
            return None

    @classmethod
    async def ensure_episode_manifest(cls, *, force_refresh: bool = False) -> dict:
        """Ensure episode manifest exists, rebuilding if needed."""
        if not force_refresh:
            manifest = await asyncio.to_thread(cls._load_episode_manifest_sync)
            if manifest is not None:
                return manifest

        async with cls._get_manifest_lock():
            if not force_refresh:
                manifest = await asyncio.to_thread(cls._load_episode_manifest_sync)
                if manifest is not None:
                    return manifest
            return await asyncio.to_thread(cls._scan_library_episodes_sync)

    @classmethod
    def resolve_episode_path(cls, episode_name: str, manifest: dict | None = None) -> Path | None:
        """Resolve an episode path using cached manifest (no recursive scan)."""
        candidate = Path(episode_name)
        if candidate.is_absolute() and candidate.exists():
            return candidate

        library_path = cls.get_library_path()
        if candidate.suffix and not candidate.is_absolute():
            full = (library_path / candidate).resolve()
            if full.exists():
                return full

        manifest_data = manifest or cls._load_episode_manifest_sync()
        if manifest_data is None:
            return None

        lookup_keys = []
        stem = Path(episode_name).stem if Path(episode_name).suffix else episode_name
        for key in (episode_name, stem):
            lookup_keys.append(key)
            lookup_keys.append(key.lower())

        seen: set[str] = set()
        by_stem = manifest_data.get("by_stem", {})
        for key in lookup_keys:
            for raw_path in by_stem.get(key, []):
                if raw_path in seen:
                    continue
                seen.add(raw_path)
                path = Path(raw_path)
                if path.exists():
                    return path
        return None

    @classmethod
    def list_episode_paths(cls, manifest: dict | None = None) -> list[str]:
        """Return known episode absolute paths from manifest."""
        manifest_data = manifest or cls._load_episode_manifest_sync()
        if manifest_data is None:
            return []
        return [p for p in manifest_data.get("episodes", []) if Path(p).exists()]

    @classmethod
    async def list_indexed_anime(cls) -> list[str]:
        """
        List all indexed anime in the library.

        Returns:
            Sorted list of anime series names.
        """
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        # Call anime_searcher directly via pixi to avoid task-shell quoting issues.
        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "list", str(library_path), "--json",
        ]

        try:
            result = await run_command(
                cmd,
                cwd=searcher_path,
                timeout_seconds=cls.LIST_TIMEOUT_SECONDS,
            )
        except CommandTimeoutError as exc:
            raise RuntimeError(str(exc)) from exc

        if result.returncode != 0:
            # If index doesn't exist yet, return empty list
            if b"does not exist" in result.stderr or b"empty" in result.stderr.lower():
                return []
            raise RuntimeError(f"Failed to list anime: {result.stderr.decode()}")

        try:
            payload = json.loads(result.stdout.decode())
            series = payload.get("series", [])
            # CLI returns objects with {name, frames}, extract just names
            if series and isinstance(series[0], dict):
                return [s["name"] for s in series]
            return series
        except json.JSONDecodeError:
            return []

    @classmethod
    async def get_available_folders(cls, source_path: Path) -> list[str]:
        """
        List folders in a source path that could be indexed.

        Args:
            source_path: Path to scan for anime folders.

        Returns:
            List of folder names.
        """
        if not source_path.exists() or not source_path.is_dir():
            return []

        def _scan_folders() -> list[str]:
            folders = []
            for item in source_path.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    folders.append(item.name)
            return sorted(folders)

        return await asyncio.to_thread(_scan_folders)

    @classmethod
    async def index_anime(
        cls,
        source_folder: Path,
        anime_name: str | None = None,
        fps: float = 2.0,
        batch_size: int = 64,
        prefetch_batches: int = 3,
        transform_workers: int = 4,
        require_gpu: bool = True,
    ) -> AsyncIterator[IndexProgress]:
        """
        Copy anime folder to library and index it.

        This method ensures all file copying/remuxing operations complete
        and files are verified before starting the indexing process to
        prevent race conditions.

        Args:
            source_folder: Path to folder containing episodes.
            anime_name: Name for the anime (default: folder name).
            fps: Requested FPS for indexing (used for new series).
            batch_size: Embedding batch size.
            prefetch_batches: Pipeline prefetch queue size.
            transform_workers: CPU worker count for image transforms.
            require_gpu: Fail if CUDA is unavailable.

        Yields:
            Progress updates during copying and indexing.
        """
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        # Ensure library directory exists
        library_path.mkdir(parents=True, exist_ok=True)

        # Determine anime name
        if anime_name is None:
            anime_name = source_folder.name

        requested_fps = fps
        effective_fps = fps
        existing_series_fps = await asyncio.to_thread(
            cls._get_indexed_series_fps_sync,
            anime_name,
        )
        is_existing_series = existing_series_fps is not None
        if existing_series_fps is not None:
            effective_fps = existing_series_fps

        dest_path = library_path / anime_name

        yield IndexProgress(
            status="starting",
            message=f"Preparing to index {anime_name}",
        )

        # Check if source folder exists
        if not source_folder.exists():
            yield IndexProgress(
                status="error",
                error=f"Source folder not found: {source_folder}",
            )
            return

        # Count video files for progress
        def _collect_video_files() -> list[Path]:
            return [
                f for f in source_folder.iterdir()
                if f.is_file() and f.suffix.lower() in cls.VIDEO_EXTENSIONS
            ]

        video_files = await asyncio.to_thread(_collect_video_files)

        if not video_files:
            yield IndexProgress(
                status="error",
                error=f"No video files found in {source_folder}",
            )
            return

        total_files = len(video_files)

        # Copy files if not already in library
        if dest_path != source_folder:
            yield IndexProgress(
                status="copying",
                message=f"Copying {total_files} files to library",
                total_files=total_files,
            )

            # Create destination directory
            dest_path.mkdir(parents=True, exist_ok=True)

            # Copy/remux each file
            for i, video_file in enumerate(video_files):
                is_mkv = video_file.suffix.lower() == ".mkv"
                dest_file = dest_path / (video_file.stem + ".mp4" if is_mkv else video_file.name)

                if not dest_file.exists():
                    if is_mkv:
                        # Remux MKV to MP4 (no re-encoding, fast)
                        yield IndexProgress(
                            status="copying",
                            message=f"Remuxing {video_file.name} → .mp4",
                            progress=(i + 0.5) / total_files * 0.3,
                            current_file=video_file.name,
                            total_files=total_files,
                            completed_files=i,
                        )
                        try:
                            remux_result = await run_command(
                                [
                                    "ffmpeg", "-y", "-i", str(video_file),
                                    "-c", "copy", "-movflags", "+faststart",
                                    str(dest_file),
                                ],
                                timeout_seconds=cls.REMUX_TIMEOUT_SECONDS,
                            )
                            if remux_result.returncode != 0:
                                import sys
                                print(
                                    f"[WARNING] ffmpeg remux failed for {video_file.name}: "
                                    f"{remux_result.stderr.decode()[:200]}",
                                    file=sys.stderr,
                                )
                                # Fallback: copy as-is
                                fallback_dest = dest_path / video_file.name
                                if not fallback_dest.exists():
                                    await asyncio.to_thread(shutil.copy2, video_file, fallback_dest)
                        except CommandTimeoutError:
                            import sys
                            print(f"[WARNING] ffmpeg remux timed out for {video_file.name}, falling back to copy", file=sys.stderr)
                            fallback_dest = dest_path / video_file.name
                            if not fallback_dest.exists():
                                await asyncio.to_thread(shutil.copy2, video_file, fallback_dest)
                        except FileNotFoundError:
                            import sys
                            print("[WARNING] ffmpeg not found, falling back to copy", file=sys.stderr)
                            fallback_dest = dest_path / video_file.name
                            if not fallback_dest.exists():
                                await asyncio.to_thread(shutil.copy2, video_file, fallback_dest)
                    else:
                        await asyncio.to_thread(shutil.copy2, video_file, dest_file)

                yield IndexProgress(
                    status="copying",
                    message=f"{'Remuxing' if is_mkv else 'Copying'} {video_file.name}",
                    progress=(i + 1) / total_files * 0.3,  # Copying is 30% of progress
                    current_file=video_file.name,
                    total_files=total_files,
                    completed_files=i + 1,
                )

            # Verify all copied files are accessible and complete before indexing
            yield IndexProgress(
                status="copying",
                message="Verifying copied files before indexing...",
                progress=0.3,
                total_files=total_files,
                completed_files=total_files,
            )

            # Small delay to ensure filesystem has flushed all writes
            await asyncio.sleep(1.0)

            # Verify each file is readable and has valid size
            for video_file in video_files:
                is_mkv = video_file.suffix.lower() == ".mkv"
                dest_file = dest_path / (video_file.stem + ".mp4" if is_mkv else video_file.name)

                # Check if file exists and has size > 0
                if not dest_file.exists():
                    yield IndexProgress(
                        status="error",
                        error=f"Copied file missing after copy: {dest_file.name}",
                    )
                    return

                file_stat = await asyncio.to_thread(dest_file.stat)
                if file_stat.st_size == 0:
                    yield IndexProgress(
                        status="error",
                        error=f"Copied file is empty: {dest_file.name}",
                    )
                    return

        if is_existing_series and abs(effective_fps - requested_fps) > 1e-9:
            yield IndexProgress(
                status="indexing",
                message=(
                    f"{anime_name} already indexed at {effective_fps:g} fps; "
                    f"keeping existing FPS (requested {requested_fps:g} ignored)"
                ),
                progress=0.3,
                total_files=total_files,
            )

        # Run indexing with pixi run
        yield IndexProgress(
            status="indexing",
            message=f"Indexing {anime_name} at {effective_fps:g} fps",
            progress=0.3,
            total_files=total_files,
        )

        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "index", str(library_path),
            "--fps", str(effective_fps),
            "--series", anime_name,
            "--batch-size", str(batch_size),
            "--prefetch-batches", str(prefetch_batches),
            "--transform-workers", str(transform_workers),
        ]
        if require_gpu:
            cmd.append("--require-gpu")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(searcher_path),
        )

        # Read output progressively
        stdout_lines = []
        stderr_task = asyncio.create_task(
            process.stderr.read() if process.stderr is not None else asyncio.sleep(0, result=b"")
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cls.INDEX_TIMEOUT_SECONDS
        aborted = False

        try:
            assert process.stdout is not None
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError

                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                if not line:
                    break
                stdout_lines.append(line.decode())

                # Try to parse progress from output
                line_str = line.decode().strip()
                if line_str:
                    # Estimate progress based on output
                    yield IndexProgress(
                        status="indexing",
                        message=line_str[:100],  # Truncate long messages
                        progress=0.3 + 0.65 * (len(stdout_lines) / (total_files * 100)),  # Rough estimate
                        total_files=total_files,
                    )

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(process.wait(), timeout=remaining)
        except asyncio.CancelledError:
            aborted = True
            await terminate_process(process)
            raise
        except asyncio.TimeoutError:
            aborted = True
            await terminate_process(process)
            yield IndexProgress(
                status="error",
                error=(
                    f"Indexing timed out after {int(cls.INDEX_TIMEOUT_SECONDS)} seconds. "
                    "Try reducing library size or retrying."
                ),
            )
            return
        finally:
            if aborted and not stderr_task.done():
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task

        stderr = await stderr_task
        if process.returncode != 0:
            yield IndexProgress(
                status="error",
                error=f"Indexing failed: {stderr.decode()}",
            )
            return

        await cls.ensure_episode_manifest(force_refresh=True)

        yield IndexProgress(
            status="complete",
            message=f"Successfully indexed {anime_name}",
            progress=1.0,
            total_files=total_files,
            completed_files=total_files,
        )

    @classmethod
    async def search_frame(
        cls,
        image_path: Path,
        anime_name: str | None = None,
        flip: bool = True,
        top_n: int = 5,
    ) -> list[dict]:
        """
        Search for a frame in the indexed library.

        Args:
            image_path: Path to the query image.
            anime_name: Filter to specific anime (optional).
            flip: Also search flipped image.
            top_n: Number of results to return.

        Returns:
            List of search results.
        """
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        cmd = [
            "pixi", "run", "--locked",
            "python", "-m", "anime_searcher.cli",
            "search", str(image_path),
            "--library", str(library_path),
            "--top-n", str(top_n),
            "--json",
        ]

        if flip:
            cmd.append("--flip")

        if anime_name:
            cmd.extend(["--series", anime_name])

        try:
            result = await run_command(
                cmd,
                cwd=searcher_path,
                timeout_seconds=cls.SEARCH_TIMEOUT_SECONDS,
            )
        except CommandTimeoutError as exc:
            raise RuntimeError(str(exc)) from exc

        if result.returncode != 0:
            raise RuntimeError(f"Search failed: {result.stderr.decode()}")

        try:
            payload = json.loads(result.stdout.decode())
            return payload.get("results", [])
        except json.JSONDecodeError:
            return []
