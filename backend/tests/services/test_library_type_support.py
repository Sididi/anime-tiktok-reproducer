from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.library_types import LibraryType
from app.models import Project
from app.services.account_service import AccountConfig, AccountService
from app.services.project_service import ProjectService
from app.services.upload_phase import UploadPhaseService


def test_project_service_load_defaults_library_type_to_anime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", tmp_path)
    project_dir = tmp_path / "legacy-project"
    project_dir.mkdir(parents=True)
    project_dir.joinpath("project.json").write_text(
        json.dumps(
            {
                "id": "legacy-project",
                "tiktok_url": "https://example.com/video",
                "anime_name": "Demo",
                "source_paths": [],
                "phase": "setup",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
        ),
        encoding="utf-8",
    )

    project = ProjectService.load("legacy-project")

    assert project is not None
    assert project.library_type == LibraryType.ANIME


def test_account_service_defaults_supported_types_to_anime() -> None:
    account = AccountService._parse_account(
        "legacy",
        {
            "name": "Legacy",
            "language": "fr",
            "slots": ["14:00"],
        },
    )

    assert account.supported_types == [LibraryType.ANIME]


def test_execute_upload_rejects_unsupported_account_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(
        id="project-1",
        anime_name="Demo",
        library_type=LibraryType.SIMPSONS,
        output_language="fr",
    )
    account = AccountConfig(
        id="account-1",
        name="Anime FR",
        language="fr",
        supported_types=[LibraryType.ANIME],
    )

    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, project_id: project))
    monkeypatch.setattr(AccountService, "list_accounts", classmethod(lambda cls: [{"id": account.id}]))
    monkeypatch.setattr(AccountService, "get_account", classmethod(lambda cls, account_id: account))

    with pytest.raises(ValueError, match="Project type 'simpsons'"):
        UploadPhaseService.execute_upload(project.id, account_id=account.id)
