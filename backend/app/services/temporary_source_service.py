"""Prepare a project's private library without catalog, hydration or publishing."""
from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from ..library_types import LocalLibrary
from ..models.temporary_source import TemporarySourceInput
from ..utils.file_tasks import file_io
from ..utils.subprocess_runner import run_command
from .anime_library import AnimeLibraryService
from .atomic_files import write_text_atomic
from .downloader import DownloaderService
from .project_service import ProjectService


class TemporarySourceService:
    _deleting: set[str] = set()

    @staticmethod
    def root(project_id: str) -> Path:
        return ProjectService.get_project_dir(project_id) / "temporary_source"

    @staticmethod
    def validate_input(source: TemporarySourceInput) -> None:
        if source.kind == "folder":
            folder = Path(source.value).expanduser().resolve()
            if not folder.is_dir():
                raise ValueError("Le dossier source est introuvable sur la machine du backend.")
            if not AnimeLibraryService._list_direct_video_files_sync(folder):
                raise ValueError("Le dossier ne contient aucune vidéo compatible (les sous-dossiers ne sont pas inclus).")
            source.value = str(folder)

    @classmethod
    def _ready_files(cls, project) -> dict[str, list[int]]:
        library = AnimeLibraryService.get_library_path(project.media_library)
        files = sorted((library / "source").glob("*.mp4"))
        if not files:
            return {}
        files += sorted(p for p in (library / ".index").rglob("*") if p.is_file() and p.name != ".write.lock")
        if not files or not (library / ".index" / "manifest.json").is_file():
            return {}
        return {str(p.relative_to(library)): [p.stat().st_size, p.stat().st_mtime_ns] for p in files}

    @classmethod
    def is_ready(cls, project) -> bool:
        if project.temporary_source is None:
            return True
        if project.id in cls._deleting:
            return False
        if project.temporary_source.status != "ready":
            return False
        try:
            marker = json.loads((cls.root(project.id) / "ready.json").read_text())
            return bool(marker) and marker == cls._ready_files(project)
        except (OSError, ValueError):
            return False

    @classmethod
    def require_ready(cls, project) -> None:
        if not cls.is_ready(project):
            raise RuntimeError("La source temporaire n’est pas prête. Relance la préparation depuis la liste des projets en cours.")

    @classmethod
    async def _save_state(cls, project_id: str, status: str, error: str | None = None):
        async with ProjectService.edit_lock(project_id):
            project = await ProjectService.aload(project_id)
            if project is None or project.temporary_source is None:
                raise RuntimeError("Project not found")
            project.temporary_source.status = status
            project.temporary_source.error = error
            await file_io(ProjectService.save, project)
            return project

    @classmethod
    async def _validate_single_video_url(cls, url: str, destination: Path) -> bool:
        """Validate one video; return whether its download should use cookies."""
        # --no-playlist selects a video from a watch URL with a list parameter,
        # but a playlist-only URL still describes a collection: reject that
        # explicitly before downloading anything.
        command = DownloaderService._build_download_command(
            url, destination, format_selector=DownloaderService.SOURCE_FORMAT_SELECTOR,
        )
        command[1:1] = ["--dump-single-json", "--skip-download", "--flat-playlist", "--playlist-end", "1"]
        result = await run_command(command, timeout_seconds=120)
        use_browser_cookies = "--no-cookies-from-browser" not in command
        if result.returncode and DownloaderService._needs_anonymous_youtube_fallback(
            command, result.stderr.decode(errors="replace"),
        ):
            command = DownloaderService._build_anonymous_command(command)
            result = await run_command(command, timeout_seconds=120)
            use_browser_cookies = False
        if result.returncode:
            raise ValueError(result.stderr.decode(errors="replace")[-1500:] or "Impossible de lire la source vidéo.")
        try:
            info = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError("Impossible de lire les informations de la source vidéo.") from exc
        if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
            raise ValueError("Une seule vidéo est acceptée : utilise son URL directe, pas une playlist ou une chaîne.")
        return use_browser_cookies

    @classmethod
    def _discard_uncommitted_files(cls, project) -> None:
        """Remove only unfinished private imports/checkpoints before a retry.

        A completed import has a sidecar; a completed index checkpoint has a
        manifest. Keep both whenever possible so Retry reuses finished work.
        """
        library = AnimeLibraryService.get_library_path(project.media_library)
        for video in (library / "source").glob("*.mp4"):
            if (AnimeLibraryService.is_transient_library_video_path(video)
                    or AnimeLibraryService._load_source_import_manifest_sync(video) is None):
                video.unlink()
                AnimeLibraryService.get_source_import_manifest_path(video).unlink(missing_ok=True)
        index = library / ".index"
        if not index.exists():
            return
        try:
            manifest = json.loads((index / "manifest.json").read_text())
            valid_checkpoint = isinstance(manifest, dict) and isinstance(manifest.get("series"), dict)
        except (OSError, ValueError):
            valid_checkpoint = False
        # The indexer explicitly diagnoses corrupt shards/old formats this
        # way. Rebuild those on the user's next retry, preserving the videos.
        requires_rebuild = "rebuild the index with" in (project.temporary_source.error or "")
        if not valid_checkpoint or requires_rebuild:
            shutil.rmtree(index)

    @classmethod
    async def prepare(cls, project_id: str, progress_callback) -> None:
        from .indexation_queue import indexation_queue

        project = await ProjectService.aload(project_id)
        if project is None or project.temporary_source is None:
            raise RuntimeError("Temporary source not found")
        if project_id in cls._deleting:
            raise RuntimeError("Project is being deleted")
        if await asyncio.to_thread(cls.is_ready, project):
            await progress_callback(1.0, "Source locale déjà prête.")
            return
        source = project.temporary_source
        root = cls.root(project_id)
        root.mkdir(parents=True, exist_ok=True)
        await cls._save_state(project_id, "preparing")
        try:
            if source.kind == "url":
                source_folder = root / "download"
                downloaded = source_folder / "source.mp4"
                marker = root / "download.json"
                reusable = False
                try:
                    stamp = json.loads(marker.read_text())
                    stat = downloaded.stat()
                    reusable = stamp == [stat.st_size, stat.st_mtime_ns]
                except (OSError, ValueError):
                    pass
                if not reusable:
                    await progress_callback(0.01, "Vérification de l’URL source…")
                    use_browser_cookies = await cls._validate_single_video_url(source.value, downloaded)
                    async for progress in DownloaderService.download(
                        source.value, project_id, output_path=downloaded,
                        require_audio=False, format_selector=DownloaderService.SOURCE_FORMAT_SELECTOR,
                        use_browser_cookies=use_browser_cookies,
                    ):
                        if progress.status == "error":
                            raise RuntimeError(progress.error or "Source download failed")
                        await progress_callback(0.2 * progress.progress, "Source : " + progress.message)
                    info = await DownloaderService.get_video_info(downloaded)
                    if not info.get("width") or not info.get("duration"):
                        raise ValueError("La source téléchargée n’est pas une vidéo lisible.")
                    stat = downloaded.stat()
                    await file_io(write_text_atomic, marker, json.dumps([stat.st_size, stat.st_mtime_ns]))
            else:
                source_folder = Path(source.value)

            await progress_callback(0.2, "En attente d’un créneau pour l’indexation locale…")
            await cls._save_state(project_id, "indexing")
            complete = False
            async with indexation_queue.heavy_slot("temporary_indexation"):
                await file_io(cls._discard_uncommitted_files, project)
                async for progress in AnimeLibraryService.index_anime(
                    source_folder=source_folder,
                    library_type=project.media_library,
                    anime_name=project.media_series_name,
                    fps=source.fps,
                ):
                    if progress.status == "error":
                        raise RuntimeError(progress.error or "Local indexation failed")
                    complete = progress.status == "complete"
                    await progress_callback(0.2 + 0.8 * progress.progress, progress.message)
            if not complete:
                raise RuntimeError("L’indexation locale ne s’est pas terminée.")
            signatures = await asyncio.to_thread(cls._ready_files, project)
            if not signatures:
                raise RuntimeError("L’index local est incomplet.")
            await file_io(write_text_atomic, root / "ready.json", json.dumps(signatures))
            async with ProjectService.edit_lock(project_id):
                project = await ProjectService.aload(project_id)
                if project is None:
                    raise RuntimeError("Project not found")
                project.source_paths = [str(AnimeLibraryService.get_library_path(project.media_library) / "source")]
                project.temporary_source.status = "ready"
                project.temporary_source.error = None
                await file_io(ProjectService.save, project)
        except Exception as exc:
            await cls._save_state(project_id, "error", str(exc))
            raise

    @classmethod
    def forget_library(cls, project) -> None:
        """Release this library's in-memory manifest under the matcher lock."""
        from .anime_matcher import AnimeMatcherService

        library = project.media_library
        if not isinstance(library, LocalLibrary):
            return
        key = AnimeLibraryService._manifest_cache_key(library)
        AnimeLibraryService._episode_manifest_cache.pop(key, None)
        AnimeLibraryService._episode_manifest_locks.pop(key, None)
        with AnimeLibraryService._preview_proxy_locks_guard:
            for source_key in list(AnimeLibraryService._preview_proxy_locks):
                if Path(source_key).is_relative_to(cls.root(project.id)):
                    AnimeLibraryService._preview_proxy_locks.pop(source_key, None)
        if AnimeMatcherService._loaded_library_path == library.path:
            AnimeMatcherService.release_matching_resources(reason="temporary_project_deleted")

    @classmethod
    @asynccontextmanager
    async def deleting_project(cls, project_id: str):
        """Stop source writers before either project deletion route removes data."""
        from .project_startup_service import project_startup_queue
        from .indexation_queue import indexation_queue
        from .zoom_search_service import zoom_search_service
        from .match_playback_service import MatchPlaybackService
        from .progress_jobs import progress_jobs

        project = await ProjectService.aload(project_id)
        if project is None or project.temporary_source is None:
            if project_id in cls._deleting:
                raise RuntimeError("Project deletion is already running")
            cls._deleting.add(project_id)
            try:
                await progress_jobs.cancel_project(project_id)
                await project_startup_queue.cancel_project(project_id)
                yield
            finally:
                cls._deleting.discard(project_id)
            return
        if project_id in cls._deleting:
            raise RuntimeError("Project deletion is already running")
        cls._deleting.add(project_id)
        try:
            await progress_jobs.cancel_project(project_id)
            await project_startup_queue.cancel_project(project_id)
            zoom_search_service.invalidate_project(project_id, reason="project deleted")
            root = cls.root(project_id).resolve()
            previews = [task for key, task in list(AnimeLibraryService._preview_generation_tasks.items()) if Path(key).is_relative_to(root)]
            for task in previews:
                task.cancel()
            if previews:
                await asyncio.gather(*previews, return_exceptions=True)
            async with MatchPlaybackService._get_prepare_lock(project_id):
                # Clip encoders run in threads and may outlive a disconnected
                # playback stream. Let their writes settle before deletion.
                clips = list(MatchPlaybackService._clip_tasks.get(project_id, ()))
                if clips:
                    await asyncio.gather(*clips, return_exceptions=True)
                async with indexation_queue.matching_lock():
                    await file_io(cls.forget_library, project)
                    yield
        finally:
            cls._deleting.discard(project_id)
