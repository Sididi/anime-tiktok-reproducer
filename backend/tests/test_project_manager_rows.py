# backend/tests/test_project_manager_rows.py
"""Project Manager row listing: Drive lookups are skipped for upload-locked
projects (posted / scheduled+dispatched) and served from persisted data."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.models import Project
from app.services import upload_phase as up
from app.services.upload_phase import UploadPhaseService, _persisted_drive_video


PAST = datetime.now(tz=timezone.utc) - timedelta(days=2)
FUTURE = datetime.now(tz=timezone.utc) + timedelta(days=2)


@pytest.fixture
def drive_calls(tmp_path, monkeypatch):
    """Hermetic list_manager_rows environment recording Drive batch calls."""
    calls: dict = {"batch_parent_ids": None, "folder_listing": 0}

    metadata_file = tmp_path / "metadata.json"
    metadata_file.write_text("{}")

    monkeypatch.setattr(
        up.ProjectService, "get_metadata_file", classmethod(lambda cls, pid: metadata_file)
    )
    monkeypatch.setattr(
        up.ProjectService,
        "get_project_dir",
        classmethod(lambda cls, pid: tmp_path / "missing" / pid),
    )

    monkeypatch.setattr(up.GoogleDriveService, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr(up.GoogleDriveService, "client", classmethod(lambda cls: object()))
    monkeypatch.setattr(up.GoogleDriveService, "reset_client", classmethod(lambda cls: None))

    calls["folders_by_name"] = {}

    def _list_folders(cls, *, drive=None):
        calls["folder_listing"] += 1
        return calls["folders_by_name"]

    monkeypatch.setattr(
        up.GoogleDriveService, "list_project_folders_under_parent", classmethod(_list_folders)
    )

    calls["batch_result"] = {}

    def _batch(cls, parent_ids, extensions, *, drive=None):
        calls["batch_parent_ids"] = list(parent_ids)
        return calls["batch_result"]

    monkeypatch.setattr(
        up.GoogleDriveService, "list_root_video_files_by_parent_ids", classmethod(_batch)
    )

    def _boom(cls, *a, **kw):
        raise AssertionError("per-project Drive lookup must not run")

    monkeypatch.setattr(up.GoogleDriveService, "find_project_folder_by_name", classmethod(_boom))

    monkeypatch.setattr(
        up.ExportService, "output_folder_name", classmethod(lambda cls, project: f"OUT_{project.id}")
    )
    monkeypatch.setattr(
        up.ExportService, "filter_upload_video_candidates", classmethod(lambda cls, files: files)
    )

    monkeypatch.setattr(Project, "resolved_llm_preset_key", lambda self: "preset")
    monkeypatch.setattr(Project, "resolved_template_key", lambda self: "template")
    monkeypatch.setattr(Project, "resolved_min_playback_speed", lambda self: 1.0)

    monkeypatch.setattr(UploadPhaseService, "_drive_video_cache", {})

    def _set_projects(projects):
        monkeypatch.setattr(up.ProjectService, "list_all", classmethod(lambda cls: projects))

    calls["set_projects"] = _set_projects
    return calls


def _rows_by_id(rows):
    return {row["project_id"]: row for row in rows}


def test_posted_project_skips_drive_batch_and_uses_persisted_video(drive_calls):
    posted = Project(
        id="posted1",
        anime_name="A",
        scheduled_at=PAST,
        drive_folder_id="fposted",
        drive_folder_url="https://drive.google.com/drive/folders/fposted",
        upload_last_result={
            "drive_video_id": "vid123",
            "drive_video_name": "output.mp4",
            "drive_video_url": "https://drive.google.com/file/d/vid123/view",
        },
    )
    active = Project(id="active1", anime_name="B", drive_folder_id="factive")
    drive_calls["set_projects"]([posted, active])
    drive_calls["batch_result"] = {
        "factive": [
            {"id": "va", "name": "output.mp4", "webViewLink": "https://x/va"}
        ]
    }

    rows = _rows_by_id(UploadPhaseService.list_manager_rows())

    # Only the active (upload-enabled) project hits the Drive video lookup.
    assert drive_calls["batch_parent_ids"] == ["factive"]

    assert rows["posted1"]["uploaded"] is True
    assert rows["posted1"]["drive_video_id"] == "vid123"
    assert rows["posted1"]["drive_video_name"] == "output.mp4"
    assert rows["posted1"]["drive_folder_url"] == "https://drive.google.com/drive/folders/fposted"

    assert rows["active1"]["drive_video_id"] == "va"
    assert rows["active1"]["can_upload_status"] == "green"


def test_all_projects_locked_makes_batch_lookup_a_noop(drive_calls):
    posted = Project(id="posted1", scheduled_at=PAST, drive_folder_id="fposted")
    scheduled = Project(
        id="sched1",
        scheduled_at=FUTURE,
        final_upload_discord_message_id="123",
        drive_folder_id="fsched",
    )
    drive_calls["set_projects"]([posted, scheduled])

    rows = _rows_by_id(UploadPhaseService.list_manager_rows())

    assert drive_calls["batch_parent_ids"] == []
    # Without persisted video info there is nothing to preview, but the rows
    # still carry their persisted folder link.
    assert rows["posted1"]["drive_video_id"] is None
    assert rows["posted1"]["drive_folder_id"] == "fposted"
    assert rows["sched1"]["uploaded_status"] == "orange"


def test_scheduled_without_dispatch_is_still_checked(drive_calls):
    # scheduled_at in the future with no Discord message = reservation only;
    # the Upload button stays enabled, so Drive readiness must stay live.
    reserved = Project(id="res1", scheduled_at=FUTURE, drive_folder_id="fres")
    drive_calls["set_projects"]([reserved])
    drive_calls["batch_result"] = {"fres": []}

    rows = _rows_by_id(UploadPhaseService.list_manager_rows())

    assert drive_calls["batch_parent_ids"] == ["fres"]
    assert rows["res1"]["uploaded_status"] == "red"


def test_empty_folder_listing_success_avoids_per_project_search(drive_calls):
    # Parent folder legitimately empty: no per-project find_project_folder_by_name
    # fallback (the fixture's _boom would raise).
    active = Project(id="active1")  # no persisted drive folder
    drive_calls["set_projects"]([active])
    drive_calls["folders_by_name"] = {}

    rows = _rows_by_id(UploadPhaseService.list_manager_rows())

    assert rows["active1"]["drive_folder_id"] is None
    assert "no output video found" in rows["active1"]["can_upload_reasons"]


def test_persisted_drive_video_recovery_variants():
    explicit = Project(upload_last_result={"drive_video_id": "abc", "drive_video_name": "n.mp4"})
    assert _persisted_drive_video(explicit) == {
        "id": "abc",
        "name": "n.mp4",
        "webViewLink": None,
    }

    legacy_view = Project(
        upload_last_result={"drive_video_url": "https://drive.google.com/file/d/legacy-1/view?usp=sharing"}
    )
    recovered = _persisted_drive_video(legacy_view)
    assert recovered is not None
    assert recovered["id"] == "legacy-1"
    assert recovered["webViewLink"] == "https://drive.google.com/file/d/legacy-1/view?usp=sharing"

    legacy_download = Project(
        upload_last_result={
            "direct_drive_download": "https://drive.usercontent.google.com/download?id=dl_2&export=download&confirm=t"
        }
    )
    recovered = _persisted_drive_video(legacy_download)
    assert recovered is not None and recovered["id"] == "dl_2"

    assert _persisted_drive_video(Project(upload_last_result=None)) is None
    assert _persisted_drive_video(Project(upload_last_result={"platforms": []})) is None


def _set_single(monkeypatch, project):
    """get_manager_row loads one project by id rather than listing all."""
    monkeypatch.setattr(
        up.ProjectService,
        "load",
        classmethod(lambda cls, pid: project if pid == project.id else None),
    )


def test_get_manager_row_for_locked_project_issues_no_drive_call(
    drive_calls, monkeypatch
):
    # The row refreshed after an upload settles is upload-locked by then, so
    # the single-row path must cost zero Drive requests.
    posted = Project(
        id="posted1",
        anime_name="A",
        scheduled_at=PAST,
        drive_folder_id="fposted",
        drive_folder_url="https://drive.google.com/drive/folders/fposted",
        upload_last_result={
            "drive_video_id": "vid123",
            "drive_video_name": "output.mp4",
        },
    )
    _set_single(monkeypatch, posted)
    monkeypatch.setattr(
        up.GoogleDriveService,
        "list_project_folders_under_parent",
        classmethod(lambda cls, **kw: (_ for _ in ()).throw(AssertionError("no Drive"))),
    )
    monkeypatch.setattr(
        up.GoogleDriveService,
        "list_root_video_files_by_parent_ids",
        classmethod(lambda cls, *a, **kw: (_ for _ in ()).throw(AssertionError("no Drive"))),
    )

    row = UploadPhaseService.get_manager_row("posted1")

    assert row is not None
    assert row["uploaded"] is True
    assert row["drive_video_id"] == "vid123"
    assert row["drive_folder_url"] == "https://drive.google.com/drive/folders/fposted"


def test_get_manager_row_matches_the_list_row(drive_calls, monkeypatch):
    posted = Project(
        id="posted1",
        anime_name="A",
        scheduled_at=PAST,
        drive_folder_id="fposted",
        upload_last_result={"drive_video_id": "vid123", "drive_video_name": "o.mp4"},
    )
    drive_calls["set_projects"]([posted])
    listed = _rows_by_id(UploadPhaseService.list_manager_rows())["posted1"]

    _set_single(monkeypatch, posted)
    assert UploadPhaseService.get_manager_row("posted1") == listed


def test_get_manager_row_unknown_project_is_none(drive_calls, monkeypatch):
    monkeypatch.setattr(up.ProjectService, "load", classmethod(lambda cls, pid: None))
    assert UploadPhaseService.get_manager_row("nope") is None
