"""Service for managing the anime library (indexing, listing, copying)."""

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..config import settings


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

    @staticmethod
    def get_library_path() -> Path:
        """Get the anime library path from settings."""
        return settings.anime_library_path

    @staticmethod
    def get_anime_searcher_path() -> Path:
        """Get the anime_searcher module path."""
        return settings.anime_searcher_path

    @classmethod
    async def list_indexed_anime(cls) -> list[str]:
        """
        List all indexed anime in the library.

        Returns:
            Sorted list of anime series names.
        """
        library_path = cls.get_library_path()
        searcher_path = cls.get_anime_searcher_path()

        # Use uv run to call anime-search list --json
        cmd = [
            "uv", "run", "--project", str(searcher_path),
            "anime-search", "list", str(library_path), "--json"
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(searcher_path),
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            # If index doesn't exist yet, return empty list
            if b"does not exist" in stderr or b"empty" in stderr.lower():
                return []
            raise RuntimeError(f"Failed to list anime: {stderr.decode()}")

        try:
            result = json.loads(stdout.decode())
            series = result.get("series", [])
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

        folders = []
        for item in source_path.iterdir():
            if item.is_dir() and not item.name.startswith("."):
                folders.append(item.name)
        return sorted(folders)

    @classmethod
    async def index_anime(
        cls,
        source_folder: Path,
        anime_name: str | None = None,
        fps: float = 2.0,
    ) -> AsyncIterator[IndexProgress]:
        """
        Copy anime folder to library and index it.

        Args:
            source_folder: Path to folder containing episodes.
            anime_name: Name for the anime (default: folder name).
            fps: Frames per second for indexing.

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
        video_extensions = {".mkv", ".mp4", ".avi", ".webm"}
        video_files = [
            f for f in source_folder.iterdir()
            if f.is_file() and f.suffix.lower() in video_extensions
        ]

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

            # Copy each file
            for i, video_file in enumerate(video_files):
                dest_file = dest_path / video_file.name
                if not dest_file.exists():
                    shutil.copy2(video_file, dest_file)

                yield IndexProgress(
                    status="copying",
                    message=f"Copying {video_file.name}",
                    progress=(i + 1) / total_files * 0.3,  # Copying is 30% of progress
                    current_file=video_file.name,
                    total_files=total_files,
                    completed_files=i + 1,
                )

        # Run indexing with uv run
        yield IndexProgress(
            status="indexing",
            message=f"Indexing {anime_name} at {fps} fps",
            progress=0.3,
            total_files=total_files,
        )

        cmd = [
            "uv", "run", "--project", str(searcher_path),
            "anime-search", "index", str(library_path),
            "--fps", str(fps),
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(searcher_path),
        )

        # Read output progressively
        stdout_lines = []
        assert process.stdout is not None
        assert process.stderr is not None
        while True:
            line = await process.stdout.readline()
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

        await process.wait()

        if process.returncode != 0:
            stderr = await process.stderr.read()
            yield IndexProgress(
                status="error",
                error=f"Indexing failed: {stderr.decode()}",
            )
            return

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
            "uv", "run", "--project", str(searcher_path),
            "anime-search", "search", str(image_path),
            "--library", str(library_path),
            "--top-n", str(top_n),
            "--json",
        ]

        if flip:
            cmd.append("--flip")

        if anime_name:
            cmd.extend(["--series", anime_name])

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(searcher_path),
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Search failed: {stderr.decode()}")

        try:
            result = json.loads(stdout.decode())
            return result.get("results", [])
        except json.JSONDecodeError:
            return []
