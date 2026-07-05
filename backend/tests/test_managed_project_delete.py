from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.services.google_drive_service import FOLDER_MIME, GoogleDriveService
from app.services.platform_reschedule_service import NotificationResult
from app.services.upload_phase import (
    PendingProjectDeletionRequiresConfirmation,
    UploadPhaseService,
)


def _scheduled_project() -> Project:
    future = datetime.now(timezone.utc) + timedelta(days=1)
    return Project(
        id="project1",
        drive_folder_id="drive-project",
        platform_schedules={
            platform: PlatformSchedule(slot=future, scheduled_at=future)
            for platform in ("youtube", "facebook", "instagram", "tiktok")
        },
    )


def test_managed_delete_rejects_unconfirmed_scheduled_project(monkeypatch):
    project = _scheduled_project()
    monkeypatch.setattr("app.services.upload_phase.ProjectService.load", lambda _id: project)
    archive = MagicMock()
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.archive_project_folder", archive
    )

    with pytest.raises(PendingProjectDeletionRequiresConfirmation) as exc_info:
        UploadPhaseService.managed_delete(project.id)

    assert exc_info.value.platforms == ["facebook", "instagram", "tiktok", "youtube"]
    archive.assert_not_called()


def test_legacy_aggregate_schedule_still_requires_confirmation(monkeypatch):
    future = datetime.now(timezone.utc) + timedelta(days=1)
    project = Project(
        id="legacy1",
        scheduled_at=future,
        upload_last_result={
            "platforms": [
                {"platform": "youtube", "url": "https://youtu.be/abc12345"},
                {"platform": "tiktok", "status": "scheduled"},
            ]
        },
    )
    monkeypatch.setattr("app.services.upload_phase.ProjectService.load", lambda _id: project)

    with pytest.raises(PendingProjectDeletionRequiresConfirmation) as exc_info:
        UploadPhaseService.managed_delete(project.id)

    assert exc_info.value.platforms == ["tiktok", "youtube"]


def test_confirmed_managed_delete_archives_unschedules_then_deletes(monkeypatch):
    project = _scheduled_project()
    calls: list[str] = []
    monkeypatch.setattr("app.services.upload_phase.ProjectService.load", lambda _id: project)
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.archive_project_folder",
        lambda _id: calls.append("archive") or {"folder_id": "archive1", "files_copied": 8},
    )
    monkeypatch.setattr(
        "app.services.upload_phase.PlatformRescheduleService.delete_server_job",
        lambda _project: calls.append("server") or NotificationResult(status="ok"),
    )
    monkeypatch.setattr(
        "app.services.upload_phase.PlatformRescheduleService.cancel",
        lambda _project, platform: calls.append(f"cancel:{platform}") or NotificationResult(status="ok"),
    )
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.delete_folder",
        lambda _id: calls.append("drive-delete"),
    )
    monkeypatch.setattr(
        "app.services.upload_phase.ProjectService.delete",
        lambda _id: calls.append("local-delete") or True,
    )

    result = UploadPhaseService.managed_delete(project.id, confirmed=True)

    assert calls == [
        "archive",
        "server",
        "cancel:facebook",
        "cancel:youtube",
        "drive-delete",
        "local-delete",
    ]
    assert result["status"] == "deleted"
    assert result["unscheduled"] == {
        "facebook": "ok",
        "instagram": "ok",
        "tiktok": "ok",
        "youtube": "ok",
    }


def test_archive_failure_preserves_original_and_local_project(monkeypatch):
    project = _scheduled_project()
    monkeypatch.setattr("app.services.upload_phase.ProjectService.load", lambda _id: project)
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.is_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.archive_project_folder",
        MagicMock(side_effect=RuntimeError("copy failed")),
    )
    drive_delete = MagicMock()
    local_delete = MagicMock()
    monkeypatch.setattr(
        "app.services.upload_phase.GoogleDriveService.delete_folder", drive_delete
    )
    monkeypatch.setattr("app.services.upload_phase.ProjectService.delete", local_delete)

    with pytest.raises(RuntimeError, match="copy failed"):
        UploadPhaseService.managed_delete(project.id, confirmed=True)

    drive_delete.assert_not_called()
    local_delete.assert_not_called()


def test_drive_archive_copies_only_reconstructable_files(monkeypatch):
    children = {
        "source": [
            {"id": "jsx", "name": "import_project.jsx", "mimeType": "text/plain"},
            {"id": "tts", "name": "tts_edited.wav", "mimeType": "audio/wav"},
            {"id": "video", "name": "output.mp4", "mimeType": "video/mp4"},
            {"id": "assets", "name": "assets", "mimeType": FOLDER_MIME},
            {"id": "subtitles", "name": "subtitles", "mimeType": FOLDER_MIME},
            {"id": "raw", "name": "raw_scene_subtitles", "mimeType": FOLDER_MIME},
            {"id": "sources", "name": "sources", "mimeType": FOLDER_MIME},
        ],
        "assets": [{"id": "preset", "name": "template.mogrt", "mimeType": "application/octet-stream"}],
        "subtitles": [{"id": "subs", "name": "subtitles.zip", "mimeType": "application/zip"}],
        "raw": [{"id": "raw-srt", "name": "text_subtitles.srt", "mimeType": "text/plain"}],
        "sources": [
            {"id": "title", "name": "title_overlay.png", "mimeType": "image/png"},
            {"id": "category", "name": "category_overlay.png", "mimeType": "image/png"},
            {"id": "anime", "name": "episode.mkv", "mimeType": "video/x-matroska"},
            {"id": "music", "name": "music.mp3", "mimeType": "audio/mpeg"},
        ],
    }
    copied: list[str] = []

    class Request:
        def __init__(self, value):
            self.value = value

        def execute(self):
            return self.value

    class Files:
        def get(self, **_kwargs):
            return Request({"id": "source", "name": "Original Project uuid"})

        def copy(self, *, fileId, **_kwargs):
            copied.append(fileId)
            return Request({"id": f"copy-{fileId}"})

    drive = MagicMock()
    drive.files.return_value = Files()
    monkeypatch.setattr(GoogleDriveService, "_client", classmethod(lambda cls: drive))
    monkeypatch.setattr(
        GoogleDriveService,
        "_ensure_child_folder",
        classmethod(
            lambda cls, name, _parent, **_kwargs: {
                "id": f"dest-{name}",
                "webViewLink": "url",
            }
        ),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children",
        classmethod(lambda cls, folder_id, **_kwargs: children.get(folder_id, [])),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "clear_folder",
        classmethod(lambda cls, *_args, **_kwargs: 0),
    )
    monkeypatch.setattr(
        "app.services.google_drive_service.settings.google_drive_parent_folder_id",
        "main",
    )

    result = GoogleDriveService.archive_project_folder("source")

    assert set(copied) == {"jsx", "tts", "preset", "subs", "raw-srt", "title", "category"}
    assert "video" not in copied
    assert "anime" not in copied
    assert "music" not in copied
    assert result["files_copied"] == 7
