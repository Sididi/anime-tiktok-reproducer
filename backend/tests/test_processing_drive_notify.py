"""The Drive-export completion hook posts to Discord AND requests a Premiere Link launch."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.api.routes import processing as processing_routes
from app.api.routes.processing import _notify_drive_upload_complete
from app.models import Project
from app.services.discord_service import DiscordMessage
from app.services.project_service import ProjectService


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch):
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", pdir)
    project = Project(id="p1")
    project.anime_name = "One Piece"
    ProjectService.get_project_dir(project.id).mkdir(parents=True, exist_ok=True)
    ProjectService.save(project)
    return pdir


@pytest.fixture
def launch_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def _request_launch(cls, project, **kwargs):
        calls.append((project.id, kwargs))
        return True

    monkeypatch.setattr(
        processing_routes.CepLinkService, "request_launch", classmethod(_request_launch)
    )
    return calls


def _mock_post(monkeypatch, result):
    monkeypatch.setattr(
        processing_routes.DiscordService,
        "post_message",
        classmethod(lambda cls, content: result(content) if callable(result) else result),
    )


def test_notify_requests_launch_with_discord_message(project_dir, launch_calls, monkeypatch):
    _mock_post(monkeypatch, lambda content: DiscordMessage(id="m1", content=content))

    _notify_drive_upload_complete("p1", "https://drive.example/folder")

    assert len(launch_calls) == 1
    project_id, kwargs = launch_calls[0]
    assert project_id == "p1"
    assert kwargs["discord_message_id"] == "m1"
    assert "http://localhost:48653/p/p1" in kwargs["discord_content"]
    assert "**One Piece**" in kwargs["discord_content"]
    assert ProjectService.load("p1").generation_discord_message_id == "m1"


def test_notify_requests_launch_even_without_discord(project_dir, launch_calls, monkeypatch):
    _mock_post(monkeypatch, None)

    _notify_drive_upload_complete("p1", "https://drive.example/folder")

    assert launch_calls[0][1]["discord_message_id"] is None
    assert ProjectService.load("p1").generation_discord_message_id is None


def test_notify_never_raises_when_launch_request_explodes(project_dir, monkeypatch):
    _mock_post(monkeypatch, lambda content: DiscordMessage(id="m1", content=content))

    def _boom(cls, project, **kwargs):
        raise RuntimeError("vps exploded")

    monkeypatch.setattr(processing_routes.CepLinkService, "request_launch", classmethod(_boom))
    _notify_drive_upload_complete("p1", "https://drive.example/folder")  # must not raise


def test_notify_replaces_previous_generation_message(project_dir, launch_calls, monkeypatch):
    project = ProjectService.load("p1")
    project.generation_discord_message_id = "old"
    ProjectService.save(project)
    deleted: list[str] = []
    monkeypatch.setattr(
        processing_routes.DiscordService,
        "delete_message",
        classmethod(lambda cls, message_id: deleted.append(message_id) or True),
    )
    _mock_post(monkeypatch, lambda content: DiscordMessage(id="new", content=content))

    _notify_drive_upload_complete("p1", "https://drive.example/folder")

    assert deleted == ["old"]
    assert launch_calls[0][1]["discord_message_id"] == "new"
    assert ProjectService.load("p1").generation_discord_message_id == "new"
