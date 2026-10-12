"""Local deletion keeps publishing history, never an executable project."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PlatformSchedule, Project
from app.services.project_service import ArchivedProjectError, ProjectService
from app.services.upload_phase import UploadPhaseService, _uploaded_fields


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr("app.services.project_service.settings.projects_dir", projects)
    monkeypatch.setattr("app.services.project_service.LibraryStateDb.remove_project_pins", MagicMock())
    monkeypatch.setattr("app.services.project_service.LibraryStateDb.add_project_pin", MagicMock())
    future = datetime.now(timezone.utc) + timedelta(days=2)
    project = Project(
        id="archivable", anime_name="Example", library_type="anime",
        project_mode="facecam", source_project_id="source", mother_project_id="mother",
        tiktok_url="https://www.tiktok.com/@source/video/123", output_language="fr",
        source_paths=["/large/library"], video_path="/local/source.mp4", series_id="series",
        music_key="music", template="custom", facecam_delivery={"status": "complete"},
        scheduled_account_id="account", scheduled_at=future,
        platform_schedules={"youtube": PlatformSchedule(slot=future, scheduled_at=future)},
        final_upload_discord_message_id="discord", upload_completed_at=datetime.now(timezone.utc),
        upload_last_result={
            "platforms": [{"platform": "youtube", "status": "uploaded", "url": "https://youtu.be/example"}],
            "requested_platforms": ["youtube"], "drive_video_id": "old-video",
            "direct_drive_download": "https://drive.example/old",
        },
        drive_folder_id="original", drive_folder_url="https://drive.example/original",
        reschedule_pending={"youtube": {"retries": 1}},
        cep_launch_request={"project_id": "archivable"},
    )
    ProjectService.save(project)
    project_dir = ProjectService.get_project_dir(project.id)
    (project_dir / "matches.json").write_text('{"heavy": true}')
    (project_dir / "metadata.json").write_text('{"title": "Title"}')
    (project_dir / "output").mkdir()
    (project_dir / "output" / "video.mp4").write_bytes(b"assets")
    return project


def archive(project, *, drive_archive=None):
    return ProjectService.archive(
        project,
        drive_archive=drive_archive,
        uploaded=_uploaded_fields(project),
        cancellations={"youtube": "ok"},
    )


def test_archive_keeps_only_small_publishing_record(project):
    assert archive(project, drive_archive={"folder_id": "archive-folder"})
    project_dir = ProjectService.get_project_dir(project.id)
    assert [p.name for p in project_dir.iterdir()] == ["project.json"]
    raw = json.loads((project_dir / "project.json").read_text())
    assert set(raw) <= {
        "id", "anime_name", "library_type", "project_mode", "output_language",
        "created_at", "updated_at", "tiktok_url", "mother_project_id", "source_project_id",
        "scheduled_at", "scheduled_account_id", "scheduled_slot", "platform_schedules",
        "upload_completed_at", "upload_last_result", "archived_at", "archived_upload",
        "archive_cancellations", "drive_folder_id", "drive_folder_url",
    }
    assert raw["tiktok_url"] == project.tiktok_url
    assert raw["mother_project_id"] == "mother"
    assert raw["source_project_id"] == "source"
    assert raw["upload_last_result"] == {
        "platforms": project.upload_last_result["platforms"], "requested_platforms": ["youtube"],
    }
    assert raw["drive_folder_url"] == "https://drive.google.com/drive/folders/archive-folder"
    stored = ProjectService.load(project.id)
    assert stored.archived_at is not None
    assert stored.platform_schedules == project.platform_schedules
    assert stored.scheduled_account_id == "account"
    assert stored.archived_upload == {"uploaded": False, "uploaded_status": "orange", "publish_error_detail": None}
    assert not ProjectService.should_keep_project_pin(stored)


def test_archive_without_drive_copy_drops_dead_link(project):
    archive(project)
    stored = ProjectService.load(project.id)
    assert stored.drive_folder_id is None
    assert stored.drive_folder_url is None


def test_temporary_source_url_survives_without_index_state(project):
    from app.models.temporary_source import TemporarySource

    project.library_type = "pure"
    project.series_id = None
    project.tiktok_url = None
    project.temporary_source = TemporarySource(kind="url", value="https://example.com/source.mp4", status="ready")
    archive(project)
    raw = json.loads(ProjectService.get_project_file(project.id).read_text())
    assert raw["source_url"] == "https://example.com/source.mp4"
    assert "temporary_source" not in raw
    assert ProjectService.load(project.id).source_url == raw["source_url"]


def test_history_excluded_from_active_lists_and_retry_work(project):
    archive(project)
    assert ProjectService.list_all() == []
    assert ProjectService.list_with_reschedule_pending() == []
    assert [p.id for p in ProjectService.list_all(include_archived=True)] == [project.id]


def test_archive_is_immutable_even_to_stale_writers(project):
    archive(project)
    before = ProjectService.get_project_file(project.id).read_bytes()
    for operation in (
        lambda: ProjectService.save(project),
        lambda: ProjectService.delete(project.id),
        lambda: ProjectService.save_matches(project.id, None),
        lambda: archive(project),
    ):
        with pytest.raises(ArchivedProjectError):
            operation()
    assert ProjectService.get_project_file(project.id).read_bytes() == before


def test_archive_metadata_write_failure_preserves_project(project, monkeypatch):
    monkeypatch.setattr("app.services.project_service.write_text_atomic", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        archive(project)
    assert ProjectService.load(project.id).archived_at is None
    assert ProjectService.get_matches_file(project.id).exists()
    assert (ProjectService.get_project_dir(project.id) / "output/video.mp4").exists()


def test_archived_manager_uses_no_drive_or_generation_work(project, monkeypatch):
    archive(project, drive_archive={"folder_id": "archive", "folder_url": "https://drive.example/archive"})
    forbidden = MagicMock(side_effect=AssertionError("Archive requested external/generation work"))
    monkeypatch.setattr("app.services.upload_phase.GoogleDriveService.is_configured", forbidden)
    monkeypatch.setattr(UploadPhaseService, "_resolve_drive_folder", forbidden)
    monkeypatch.setattr(UploadPhaseService, "_facecam_readiness", forbidden)
    monkeypatch.setattr(Project, "resolved_llm_preset_key", forbidden)
    monkeypatch.setattr(Project, "resolved_template_key", forbidden)
    monkeypatch.setattr(Project, "resolved_min_playback_speed", forbidden)
    monkeypatch.setattr("app.services.upload_phase._dir_size", forbidden)
    rows = UploadPhaseService.list_manager_rows()
    assert len(rows) == 1
    assert UploadPhaseService.get_manager_row(project.id) == rows[0]
    row = rows[0]
    assert row["archived_at"]
    assert row["can_upload_status"] == "red"
    assert row["drive_video_id"] is None
    assert row["drive_folder_url"] == "https://drive.example/archive"
    assert row["local_size_bytes"] == ProjectService.get_project_file(project.id).stat().st_size
    assert row["archive_cancellations"] == {"youtube": "ok"}
    # Time advancing must not turn a cancelled scheduled upload into "posted".
    stored = ProjectService.load(project.id)
    stored.scheduled_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert _uploaded_fields(stored)["uploaded"] is False


def test_archives_do_not_reserve_slots_or_accept_vps_updates(project, monkeypatch):
    from app.services.scheduling_service import SchedulingService
    from app.services.vps_status_sync_service import VpsStatusSyncService

    archive(project)
    monkeypatch.setattr("app.services.scheduling_service.AccountService.all_accounts", lambda: {})
    assert SchedulingService._collect_pool_reservations("account:account:youtube", "youtube") == {}
    before = ProjectService.get_project_file(project.id).read_bytes()
    VpsStatusSyncService._persist_terminal_outcomes({project.id: {"youtube": {"status": "uploaded"}}})
    assert ProjectService.get_project_file(project.id).read_bytes() == before


def test_managed_delete_replaces_drive_link_and_prevents_repeat(project, monkeypatch):
    past = datetime.now(timezone.utc) - timedelta(days=2)
    project.scheduled_at = past
    project.platform_schedules["youtube"] = PlatformSchedule(slot=past, scheduled_at=past)
    ProjectService.save(project)
    drive_archive = MagicMock(return_value={"folder_id": "archive-copy", "files_copied": 3})
    drive_delete = MagicMock()
    monkeypatch.setattr("app.services.upload_phase.GoogleDriveService.is_configured", lambda: True)
    monkeypatch.setattr("app.services.upload_phase.GoogleDriveService.archive_project_folder", drive_archive)
    monkeypatch.setattr("app.services.upload_phase.GoogleDriveService.delete_folder", drive_delete)
    monkeypatch.setattr("app.services.cep_link_service.CepLinkService.delete_launch", MagicMock())
    monkeypatch.setattr("app.services.upload_phase.DiscordService.delete_job", MagicMock())
    monkeypatch.setattr(UploadPhaseService, "cleanup_project_caches", MagicMock())
    result = UploadPhaseService.managed_delete(project.id)
    assert result["local_deleted"] is True
    assert ProjectService.load(project.id).drive_folder_id == "archive-copy"
    assert [p.name for p in ProjectService.get_project_dir(project.id).iterdir()] == ["project.json"]
    with pytest.raises(ArchivedProjectError):
        UploadPhaseService.managed_delete(project.id, confirmed=True)
    drive_archive.assert_called_once_with("original")
    drive_delete.assert_called_once_with("original")


def test_deletion_removes_external_preview_and_upload_caches(project, tmp_path, monkeypatch):
    from app.services.thumbnail_service import ThumbnailService

    roots = []
    for owner, name in [
        (UploadPhaseService, "_SOURCE_CACHE_DIR"),
        (UploadPhaseService, "_COPYRIGHT_AUDIO_CACHE_DIR"),
        (UploadPhaseService, "_FACEBOOK_PREP_CACHE_DIR"),
        (UploadPhaseService, "_YOUTUBE_PREP_CACHE_DIR"),
        (UploadPhaseService, "_LEGACY_FACEBOOK_PREP_CACHE_DIR"),
        (UploadPhaseService, "_LEGACY_YOUTUBE_PREP_CACHE_DIR"),
        (ThumbnailService, "_THUMBS_CACHE_DIR"),
    ]:
        root = tmp_path / name
        monkeypatch.setattr(owner, name, root)
        (root / project.id).mkdir(parents=True)
        (root / project.id / "asset.mp4").write_bytes(b"asset")
        (root / "other-project").mkdir()
        roots.append(root)
    archive(project)
    UploadPhaseService.cleanup_project_caches(project.id)
    assert all(not (root / project.id).exists() for root in roots)
    assert all((root / "other-project").exists() for root in roots)


@pytest.mark.parametrize("method,path", [
    ("GET", "/projects/archivable"),
    ("GET", "/projects/archivable/matches"),
    ("PATCH", "/projects/archivable"),
    ("DELETE", "/projects/archivable"),
    ("DELETE", "/project-manager/projects/archivable"),
    ("POST", "/project-manager/projects/archivable/upload"),
    ("GET", "/project-manager/projects/archivable/upload-source-preview"),
    ("POST", "/project-manager/projects/archivable/facebook-check"),
    ("DELETE", "/scheduling/projects/archivable/all"),
    ("GET", "/projects/archivable/facecam"),
])
def test_archived_project_apis_are_disabled(project, method, path):
    from app.api.routes import api_router

    archive(project)
    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as client:
        response = client.request(method, f"/api{path}")
        assert response.status_code == 410, response.text
        assert response.json()["detail"]["code"] == "project_archived"
        row = client.get(f"/api/project-manager/projects/{project.id}/row")
        assert row.status_code == 200, row.text
        assert row.json()["project"]["archived_at"]
