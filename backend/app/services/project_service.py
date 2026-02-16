import json
import re
from pathlib import Path
from datetime import datetime

from ..config import settings
from ..models import Project, ProjectPhase, SceneList

_PROJECT_ID_RE = re.compile(r"[a-zA-Z0-9_-]+$")


def _validate_project_id(project_id: str) -> None:
    """Reject project IDs that could escape the projects directory."""
    if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(
            f"Invalid project id: must be non-empty alphanumeric/hyphen/underscore, got {project_id!r}"
        )


class ProjectService:
    """Service for managing projects."""

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
    def create(cls, tiktok_url: str | None = None, source_path: str | None = None, anime_name: str | None = None) -> Project:
        """Create a new project."""
        source_paths = []
        if source_path:
            source_paths.append(source_path)
        
        project = Project(tiktok_url=tiktok_url, source_paths=source_paths, anime_name=anime_name)
        project_dir = cls.get_project_dir(project.id)
        project_dir.mkdir(parents=True, exist_ok=True)

        cls.save(project)
        return project

    @classmethod
    def save(cls, project: Project) -> None:
        """Save a project to disk."""
        project.updated_at = datetime.now()
        project_file = cls.get_project_file(project.id)
        project_file.write_text(project.model_dump_json(indent=2))

    @classmethod
    def load(cls, project_id: str) -> Project | None:
        """Load a project from disk."""
        project_file = cls.get_project_file(project_id)
        if not project_file.exists():
            return None
        return Project.model_validate_json(project_file.read_text())

    @classmethod
    def delete(cls, project_id: str) -> bool:
        """Delete a project and all its data."""
        project_dir = cls.get_project_dir(project_id)
        if not project_dir.exists():
            return False

        import shutil

        shutil.rmtree(project_dir)
        return True

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
        scenes_file.write_text(scenes.model_dump_json(indent=2))

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
        from ..models import MatchList
        matches_file = cls.get_matches_file(project_id)
        matches_file.write_text(matches.model_dump_json(indent=2))

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
        from ..models import Transcription
        transcription_file = cls.get_transcription_file(project_id)
        transcription_file.write_text(transcription.model_dump_json(indent=2))

    @classmethod
    def load_transcription(cls, project_id: str) -> "Transcription | None":
        """Load transcription for a project."""
        from ..models import Transcription
        transcription_file = cls.get_transcription_file(project_id)
        if not transcription_file.exists():
            return None
        return Transcription.model_validate_json(transcription_file.read_text())
