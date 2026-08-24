import asyncio
import json
import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from datetime import datetime

from ..config import settings
from ..library_types import DEFAULT_LIBRARY_TYPE, LibraryType, coerce_library_type
from ..models import Project, ProjectPhase, SceneList
from .atomic_files import write_text_atomic
from .library_state_db import LibraryStateDb
from .project_locks import ProjectLocks

_PROJECT_ID_RE = re.compile(r"[a-zA-Z0-9_-]+$")


def _validate_project_id(project_id: str) -> None:
    """Reject project IDs that could escape the projects directory."""
    if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(
            f"Invalid project id: must be non-empty alphanumeric/hyphen/underscore, got {project_id!r}"
        )


class ProjectService:
    """Service for managing projects.

    Every ``load_*`` / ``save_*`` here is synchronous disk I/O (``matches.json``
    reaches 14 MB; ``list_all`` parses ~400 files).  From ``async`` code use
    the ``a``-prefixed twins (``aload``, ``asave_matches``, ``alist_all``, …),
    which run on the light thread pool, and wrap any load → mutate → save
    sequence in ``async with ProjectService.edit_lock(project_id):`` — moving
    the I/O off the loop introduces await points, and without the lock two
    concurrent edits of the same project could lose one update.  Rules for
    the lock live in :mod:`app.services.project_locks`.

    Saves are atomic (temp file + rename): a crash mid-write can no longer
    truncate a project file.
    """

    @staticmethod
    def should_keep_project_pin(project: Project) -> bool:
        """Keep local residency only while a project is still actively generating."""
        if not project.series_id:
            return False
        if project.upload_completed_at is not None:
            return False
        if project.scheduled_at is not None:
            return False
        if project.phase == ProjectPhase.COMPLETE:
            return False
        return True

    @staticmethod
    def get_project_dir(project_id: str) -> Path:
        """Get the directory for a project."""
        _validate_project_id(project_id)
        return settings.projects_dir / project_id

    @staticmethod
    def get_project_file(project_id: str) -> Path:
        """Get the project.json file path."""
        return ProjectService.get_project_dir(project_id) / "project.json"

    @staticmethod
    def get_scenes_file(project_id: str) -> Path:
        """Get the scenes.json file path."""
        return ProjectService.get_project_dir(project_id) / "scenes.json"

    @classmethod
    def create(
        cls,
        tiktok_url: str | None = None,
        source_path: str | None = None,
        anime_name: str | None = None,
        series_id: str | None = None,
        library_type: LibraryType = DEFAULT_LIBRARY_TYPE,
    ) -> Project:
        """Create a new project."""
        source_paths = []
        if source_path:
            source_paths.append(source_path)

        project = Project(
            tiktok_url=tiktok_url,
            source_paths=source_paths,
            anime_name=anime_name,
            series_id=series_id,
            library_type=library_type,
        )
        project_dir = cls.get_project_dir(project.id)
        project_dir.mkdir(parents=True, exist_ok=True)

        cls.save(project)
        return project

    @classmethod
    def save(cls, project: Project) -> None:
        """Save a project to disk."""
        project.updated_at = datetime.now()
        project_file = cls.get_project_file(project.id)
        write_text_atomic(project_file, project.model_dump_json(indent=2))
        cls.sync_project_pin(project)

    @classmethod
    def load(cls, project_id: str) -> Project | None:
        """Load a project from disk."""
        project_file = cls.get_project_file(project_id)
        if not project_file.exists():
            return None
        return Project.model_validate_json(project_file.read_text())

    # ------------------------------------------------------------------
    # event-loop friendly twins (light thread pool) + per-project edit lock

    @classmethod
    def edit_lock(cls, project_id: str) -> AbstractAsyncContextManager[None]:
        """``async with`` guard for a load → mutate → save section."""
        return ProjectLocks.hold(project_id)

    @classmethod
    async def aload(cls, project_id: str) -> Project | None:
        return await asyncio.to_thread(cls.load, project_id)

    @classmethod
    async def asave(cls, project: Project) -> None:
        await asyncio.to_thread(cls.save, project)

    @classmethod
    async def alist_all(cls) -> list[Project]:
        return await asyncio.to_thread(cls.list_all)

    @classmethod
    async def aload_scenes(cls, project_id: str) -> SceneList | None:
        return await asyncio.to_thread(cls.load_scenes, project_id)

    @classmethod
    async def asave_scenes(cls, project_id: str, scenes: SceneList) -> None:
        await asyncio.to_thread(cls.save_scenes, project_id, scenes)

    @classmethod
    async def aload_matches(cls, project_id: str) -> "MatchList | None":
        return await asyncio.to_thread(cls.load_matches, project_id)

    @classmethod
    async def asave_matches(cls, project_id: str, matches: "MatchList") -> None:
        await asyncio.to_thread(cls.save_matches, project_id, matches)

    @classmethod
    async def aload_transcription(cls, project_id: str) -> "Transcription | None":
        return await asyncio.to_thread(cls.load_transcription, project_id)

    @classmethod
    async def asave_transcription(
        cls, project_id: str, transcription: "Transcription"
    ) -> None:
        await asyncio.to_thread(cls.save_transcription, project_id, transcription)

    @classmethod
    def delete(cls, project_id: str) -> bool:
        """Delete a project and all its data."""
        project_dir = cls.get_project_dir(project_id)
        if not project_dir.exists():
            return False

        import shutil

        LibraryStateDb.remove_project_pins(project_id)
        shutil.rmtree(project_dir)
        return True

    @classmethod
    def sync_project_pin(cls, project: Project) -> None:
        LibraryStateDb.remove_project_pins(project.id)
        if cls.should_keep_project_pin(project):
            LibraryStateDb.add_project_pin(project.id, project.series_id)

    @classmethod
    def sync_all_project_pins(cls) -> None:
        """Rebuild project pins from saved projects using current pin rules."""
        LibraryStateDb.clear_all_project_pins()
        for project in cls.list_all():
            if cls.should_keep_project_pin(project):
                LibraryStateDb.add_project_pin(project.id, project.series_id)

    @classmethod
    def list_referencing_series(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
    ) -> list[Project]:
        scoped_type = coerce_library_type(library_type)
        return [
            project
            for project in cls.list_all()
            if project.series_id == series_id and project.library_type == scoped_type
        ]

    @classmethod
    def rename_series_references(
        cls,
        *,
        library_type: LibraryType | str,
        series_id: str,
        new_name: str,
    ) -> list[Project]:
        scoped_type = coerce_library_type(library_type)
        renamed_projects: list[Project] = []
        for project in cls.list_all():
            if project.series_id != series_id or project.library_type != scoped_type:
                continue
            if project.anime_name == new_name:
                renamed_projects.append(project)
                continue
            project.anime_name = new_name
            cls.save(project)
            renamed_projects.append(project)
        return renamed_projects

    @classmethod
    def list_all(cls) -> list[Project]:
        """List all projects."""
        projects = []
        for project_dir in settings.projects_dir.iterdir():
            if project_dir.is_dir():
                project = cls.load(project_dir.name)
                if project:
                    projects.append(project)
        return sorted(projects, key=lambda p: p.created_at, reverse=True)

    @classmethod
    def list_with_reschedule_pending(cls) -> list[Project]:
        """Only projects with a non-empty ``reschedule_pending``.

        Same one pass over the project files as :meth:`list_all`, but the
        (rare) matching projects are the only ones validated into models —
        cheap enough to run every minute from the retry loop.
        """
        projects: list[Project] = []
        for project_dir in settings.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            project_file = project_dir / "project.json"
            if not project_file.exists():
                continue
            try:
                raw = json.loads(project_file.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(raw, dict) or not raw.get("reschedule_pending"):
                continue
            projects.append(Project.model_validate(raw))
        return sorted(projects, key=lambda p: p.created_at, reverse=True)

    @classmethod
    def update_phase(cls, project_id: str, phase: ProjectPhase) -> Project | None:
        """Update the project phase."""
        project = cls.load(project_id)
        if not project:
            return None
        project.phase = phase
        cls.save(project)
        return project

    @classmethod
    def save_scenes(cls, project_id: str, scenes: SceneList) -> None:
        """Save scenes for a project."""
        scenes_file = cls.get_scenes_file(project_id)
        write_text_atomic(scenes_file, scenes.model_dump_json(indent=2))

    @classmethod
    def load_scenes(cls, project_id: str) -> SceneList | None:
        """Load scenes for a project."""
        scenes_file = cls.get_scenes_file(project_id)
        if not scenes_file.exists():
            return None
        return SceneList.model_validate_json(scenes_file.read_text())

    @classmethod
    def get_matches_file(cls, project_id: str) -> Path:
        """Get the matches.json file path."""
        return cls.get_project_dir(project_id) / "matches.json"

    @classmethod
    def get_metadata_file(cls, project_id: str) -> Path:
        """Get the metadata.json file path."""
        return cls.get_project_dir(project_id) / "metadata.json"

    @classmethod
    def get_metadata_html_file(cls, project_id: str) -> Path:
        """Get the metadata.html file path."""
        return cls.get_project_dir(project_id) / "metadata.html"

    @classmethod
    def save_matches(cls, project_id: str, matches: "MatchList") -> None:
        """Save matches for a project."""
        matches_file = cls.get_matches_file(project_id)
        write_text_atomic(matches_file, matches.model_dump_json(indent=2))

    @classmethod
    def load_matches(cls, project_id: str) -> "MatchList | None":
        """Load matches for a project."""
        from ..models import MatchList
        matches_file = cls.get_matches_file(project_id)
        if not matches_file.exists():
            return None
        return MatchList.model_validate_json(matches_file.read_text())

    @classmethod
    def get_transcription_file(cls, project_id: str) -> Path:
        """Get the transcription.json file path."""
        return cls.get_project_dir(project_id) / "transcription.json"

    @classmethod
    def save_transcription(cls, project_id: str, transcription: "Transcription") -> None:
        """Save transcription for a project."""
        transcription_file = cls.get_transcription_file(project_id)
        write_text_atomic(transcription_file, transcription.model_dump_json(indent=2))

    @classmethod
    def load_transcription(cls, project_id: str) -> "Transcription | None":
        """Load transcription for a project."""
        from ..models import Transcription
        transcription_file = cls.get_transcription_file(project_id)
        if not transcription_file.exists():
            return None
        return Transcription.model_validate_json(transcription_file.read_text())
