import asyncio
import json
import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from datetime import datetime, timezone

from ..config import settings
from ..library_types import DEFAULT_LIBRARY_TYPE, LibraryType, coerce_library_type
from ..models import Project, ProjectPhase, SceneList
from ..models.temporary_source import TemporarySource, TemporarySourceInput
from .atomic_files import write_text_atomic
from .library_state_db import LibraryStateDb
from .project_locks import ProjectLocks

_PROJECT_ID_RE = re.compile(r"[a-zA-Z0-9_-]+$")


class ArchivedProjectError(ValueError):
    def __init__(self):
        super().__init__("This project is archive-only and is disabled. Its read-only information is available in Project Manager.")


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
        if project.archived_at or not project.series_id:
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
        temporary_source: TemporarySourceInput | None = None,
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
            temporary_source=(TemporarySource(**temporary_source.model_dump()) if temporary_source else None),
        )
        project_dir = cls.get_project_dir(project.id)
        project_dir.mkdir(parents=True, exist_ok=True)

        cls.save(project)
        return project

    @classmethod
    def save(cls, project: Project) -> None:
        """Save a project to disk."""
        cls.require_active(project.id)
        if project.archived_at:
            raise ArchivedProjectError()
        if project.temporary_source is not None:
            if not cls.get_project_dir(project.id).exists():
                raise RuntimeError("Project no longer exists")
        project.updated_at = datetime.now()
        project_file = cls.get_project_file(project.id)
        write_text_atomic(project_file, project.model_dump_json(indent=2), create_parent=project.temporary_source is None)
        cls.sync_project_pin(project)

    @classmethod
    def load(cls, project_id: str) -> Project | None:
        """Load a project from disk."""
        project_file = cls.get_project_file(project_id)
        if not project_file.exists():
            return None
        return Project.model_validate_json(project_file.read_text())

    @classmethod
    def require_active(cls, project_id: str) -> None:
        """Reject stale writers as well as direct access to archived projects."""
        project = cls.load(project_id)
        if project is not None and project.archived_at:
            raise ArchivedProjectError()

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
    async def alist_all(cls, *, include_archived: bool = False) -> list[Project]:
        if include_archived:
            return await asyncio.to_thread(cls.list_all, include_archived=True)
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
        cls.require_active(project_id)
        project_dir = cls.get_project_dir(project_id)
        if not project_dir.exists():
            return False

        import shutil

        LibraryStateDb.remove_project_pins(project_id)
        shutil.rmtree(project_dir)
        return True

    @classmethod
    def archive(
        cls,
        project: Project,
        *,
        drive_archive: dict | None,
        uploaded: dict,
        cancellations: dict[str, str],
    ) -> bool:
        """Keep one small project.json; discard all generation data and assets.

        Persist the record before removing any files, so a failed metadata
        write cannot destroy the only local copy of the publishing history.
        This allowlist deliberately does not grow with the Project model.
        """
        import shutil

        cls.require_active(project.id)
        project_dir = cls.get_project_dir(project.id)
        if not project_dir.exists():
            return False
        retained = project.model_dump(mode="json", include={
            "id", "anime_name", "library_type", "project_mode", "output_language",
            "created_at", "updated_at", "tiktok_url", "mother_project_id", "source_project_id",
            "scheduled_at", "scheduled_account_id", "scheduled_slot", "platform_schedules",
            "upload_completed_at",
        }, exclude_none=True)
        upload_result = project.upload_last_result or {}
        # Remote video/cache references are obsolete after Drive deletion.
        # Platform outcomes and public post links remain useful history.
        retained["upload_last_result"] = {
            key: upload_result[key]
            for key in ("platforms", "requested_platforms")
            if key in upload_result
        }
        if project.temporary_source is not None and project.temporary_source.kind == "url":
            retained["source_url"] = project.temporary_source.value
        retained.update(
            archived_at=datetime.now(timezone.utc).isoformat(),
            archived_upload=uploaded,
            archive_cancellations=cancellations,
            drive_folder_id=drive_archive.get("folder_id") if drive_archive else None,
            drive_folder_url=(
                drive_archive.get("folder_url")
                or f"https://drive.google.com/drive/folders/{drive_archive['folder_id']}"
            ) if drive_archive else None,
        )
        project_file = cls.get_project_file(project.id)
        write_text_atomic(project_file, json.dumps(retained, ensure_ascii=False, indent=2), create_parent=False)
        LibraryStateDb.remove_project_pins(project.id)
        for child in project_dir.iterdir():
            if child == project_file:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
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
    def list_all(cls, *, include_archived: bool = False) -> list[Project]:
        """List active projects; history consumers can explicitly include archives."""
        projects = []
        for project_dir in settings.projects_dir.iterdir():
            if project_dir.is_dir():
                project = cls.load(project_dir.name)
                if project and (include_archived or not project.archived_at):
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
            if not isinstance(raw, dict) or raw.get("archived_at") or not raw.get("reschedule_pending"):
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
        cls.require_active(project_id)
        scenes_file = cls.get_scenes_file(project_id)
        write_text_atomic(scenes_file, scenes.model_dump_json(indent=2), create_parent=False)

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
        cls.require_active(project_id)
        matches_file = cls.get_matches_file(project_id)
        write_text_atomic(matches_file, matches.model_dump_json(indent=2), create_parent=False)

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
        cls.require_active(project_id)
        transcription_file = cls.get_transcription_file(project_id)
        write_text_atomic(transcription_file, transcription.model_dump_json(indent=2), create_parent=False)

    @classmethod
    def load_transcription(cls, project_id: str) -> "Transcription | None":
        """Load transcription for a project."""
        from ..models import Transcription
        transcription_file = cls.get_transcription_file(project_id)
        if not transcription_file.exists():
            return None
        return Transcription.model_validate_json(transcription_file.read_text())

    @classmethod
    def validate_transcription_scene_alignment(
        cls, project_id: str, transcription: "Transcription"
    ) -> None:
        """Reject a transcript left over from a different source scene layout.

        Matching can merge, split, or move cuts while keeping consecutive scene
        indices. Comparing indices/counts alone cannot detect stale pairings.
        This compares original-video bounds, never generated TTS timings.
        """
        scenes = cls.load_scenes(project_id)
        aligned = scenes is not None and len(scenes.scenes) == len(transcription.scenes)
        if aligned:
            aligned = all(
                scene.index == transcript.scene_index
                and abs(scene.start_time - transcript.start_time) <= 1e-4
                and abs(scene.end_time - transcript.end_time) <= 1e-4
                for scene, transcript in zip(scenes.scenes, transcription.scenes)
            )
        if not aligned:
            raise ValueError(
                "The transcription no longer matches the current video scene boundaries. "
                "Re-run transcription, then regenerate the script, TTS audio, and export."
            )
