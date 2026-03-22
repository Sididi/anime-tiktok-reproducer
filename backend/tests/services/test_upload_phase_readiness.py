from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.models import VideoMetadataPayload
from app.models.project import Project
from app.services.account_service import AccountService
from app.services.discord_service import DiscordService
from app.services.export_service import ExportService
from app.services.google_drive_service import GoogleDriveService
from app.services.metadata import MetadataService
from app.services.project_service import ProjectService
from app.services.upload_phase import UploadPhaseService
from app.config import settings


def _metadata_payload() -> VideoMetadataPayload:
    return VideoMetadataPayload.model_validate(
        {
            "facebook": {
                "title": "Demo title",
                "description": "Demo description",
                "tags": ["demo"],
            },
            "instagram": {
                "caption": "Demo caption",
            },
            "youtube": {
                "title": "Demo title",
                "description": "Demo description",
                "tags": ["demo"],
            },
            "tiktok": {
                "description": "Demo description",
            },
        }
    )


def test_project_manager_row_can_seed_drive_video_for_upload_when_direct_lookup_flaps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project(
        id="project-1",
        anime_name="Demo",
        output_language="fr",
        drive_folder_id="folder-1",
        drive_folder_url="https://drive.google.com/drive/folders/folder-1",
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text("{}", encoding="utf-8")
    subtitle_path = tmp_path / "subtitles.srt"
    subtitle_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nDemo\n",
        encoding="utf-8",
    )
    drive_video = {
        "id": "drive-video-1",
        "name": "output.mp4",
        "webViewLink": "https://drive.google.com/file/d/drive-video-1/view",
    }

    monkeypatch.setattr(UploadPhaseService, "_drive_video_cache", {}, raising=False)
    monkeypatch.setattr(
        UploadPhaseService,
        "_cross_overdue_upload_messages",
        classmethod(lambda cls, projects: None),
    )
    monkeypatch.setattr(ProjectService, "list_all", classmethod(lambda cls: [project]))
    monkeypatch.setattr(ProjectService, "load", classmethod(lambda cls, project_id: project))
    monkeypatch.setattr(ProjectService, "get_project_dir", classmethod(lambda cls, project_id: tmp_path))
    monkeypatch.setattr(
        ProjectService,
        "get_metadata_file",
        classmethod(lambda cls, project_id: metadata_path),
    )
    monkeypatch.setattr(ProjectService, "save", classmethod(lambda cls, project: None))
    monkeypatch.setattr(GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(GoogleDriveService, "client", classmethod(lambda cls: object()))
    monkeypatch.setattr(
        GoogleDriveService,
        "list_project_folders_under_parent",
        classmethod(lambda cls, drive=None: {}),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "list_root_video_files_by_parent_ids",
        classmethod(lambda cls, parent_ids, extensions, drive=None: {"folder-1": [drive_video]}),
    )
    monkeypatch.setattr("app.services.upload_phase._dir_size", lambda path: 0)

    rows = UploadPhaseService.list_manager_rows()

    assert len(rows) == 1
    assert rows[0]["can_upload_status"] == "green"
    assert rows[0]["drive_video_id"] == "drive-video-1"
    assert rows[0]["drive_video_name"] == "output.mp4"

    monkeypatch.setattr(
        ExportService,
        "detect_upload_video_in_drive_root",
        classmethod(
            lambda cls, folder_id: (_ for _ in ()).throw(RuntimeError("transient Drive failure"))
        ),
    )
    monkeypatch.setattr(AccountService, "list_accounts", classmethod(lambda cls: []))
    monkeypatch.setattr(
        ExportService,
        "subtitle_path",
        classmethod(lambda cls, _project: subtitle_path),
    )
    monkeypatch.setattr(
        MetadataService,
        "load",
        classmethod(lambda cls, project_id: _metadata_payload()),
    )
    monkeypatch.setattr(GoogleDriveService, "set_public_read", classmethod(lambda cls, file_id: None))
    monkeypatch.setattr(
        GoogleDriveService,
        "get_direct_download_url",
        classmethod(lambda cls, file_id: f"https://download/{file_id}"),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "download_file",
        classmethod(lambda cls, file_id, destination: destination.write_bytes(b"video")),
    )
    monkeypatch.setattr(
        DiscordService,
        "delete_message",
        classmethod(lambda cls, message_id: None),
    )
    monkeypatch.setattr(
        DiscordService,
        "post_message",
        classmethod(lambda cls, message: SimpleNamespace(id="discord-1")),
    )
    monkeypatch.setattr(settings, "n8n_webhook_url", None)

    result = UploadPhaseService.execute_upload(project.id, platforms=["instagram"])

    assert result["requested_platforms"] == ["instagram"]
    assert result["direct_drive_download"] == "https://download/drive-video-1"


def test_list_manager_rows_retries_drive_batch_lookup_after_transient_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project(
        id="project-1",
        anime_name="Demo",
        output_language="fr",
        drive_folder_id="folder-1",
        drive_folder_url="https://drive.google.com/drive/folders/folder-1",
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text("{}", encoding="utf-8")
    drive_video = {
        "id": "drive-video-1",
        "name": "output.mp4",
        "webViewLink": "https://drive.google.com/file/d/drive-video-1/view",
    }

    monkeypatch.setattr(UploadPhaseService, "_drive_video_cache", {}, raising=False)
    monkeypatch.setattr(
        UploadPhaseService,
        "_cross_overdue_upload_messages",
        classmethod(lambda cls, projects: None),
    )
    monkeypatch.setattr(ProjectService, "list_all", classmethod(lambda cls: [project]))
    monkeypatch.setattr(ProjectService, "get_project_dir", classmethod(lambda cls, project_id: tmp_path))
    monkeypatch.setattr(
        ProjectService,
        "get_metadata_file",
        classmethod(lambda cls, project_id: metadata_path),
    )
    monkeypatch.setattr(GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(GoogleDriveService, "client", classmethod(lambda cls: object()))
    monkeypatch.setattr("app.services.upload_phase._dir_size", lambda path: 0)

    attempts = {"folders": 0, "reset": 0}

    def _list_folders(cls, drive=None):
        attempts["folders"] += 1
        if attempts["folders"] == 1:
            raise BrokenPipeError("Broken pipe")
        return {}

    monkeypatch.setattr(
        GoogleDriveService,
        "list_project_folders_under_parent",
        classmethod(_list_folders),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "list_root_video_files_by_parent_ids",
        classmethod(lambda cls, parent_ids, extensions, drive=None: {"folder-1": [drive_video]}),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "reset_client",
        classmethod(lambda cls: attempts.__setitem__("reset", attempts["reset"] + 1)),
        raising=False,
    )

    rows = UploadPhaseService.list_manager_rows()

    assert len(rows) == 1
    assert rows[0]["drive_video_id"] == "drive-video-1"
    assert rows[0]["can_upload_status"] == "green"
    assert attempts["folders"] == 2
    assert attempts["reset"] == 1


def test_compute_readiness_reports_drive_verification_failure_when_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = Project(
        id="project-1",
        anime_name="Demo",
        output_language="fr",
        drive_folder_id="folder-1",
        drive_folder_url="https://drive.google.com/drive/folders/folder-1",
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(UploadPhaseService, "_drive_video_cache", {}, raising=False)
    monkeypatch.setattr(
        ProjectService,
        "get_metadata_file",
        classmethod(lambda cls, project_id: metadata_path),
    )
    monkeypatch.setattr(
        ExportService,
        "detect_upload_video_in_drive_root",
        classmethod(
            lambda cls, folder_id: (_ for _ in ()).throw(RuntimeError("transient Drive failure"))
        ),
    )

    readiness = UploadPhaseService.compute_readiness(project)

    assert "unable to verify output video in Drive" in readiness.reasons
    assert "no output video found" not in readiness.reasons
