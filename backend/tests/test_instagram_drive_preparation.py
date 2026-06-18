from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.export_service import ExportService
from app.services.google_drive_service import GoogleDriveService
from app.services.social_upload_service import LimitedDurationVideoPreparation, SocialUploadService
from app.services.upload_phase import UploadPhaseService


def test_drive_readiness_ignores_prepared_instagram_artifact(monkeypatch) -> None:
    monkeypatch.setattr(
        GoogleDriveService,
        "list_root_video_files",
        lambda folder_id, extensions: [
            {"id": "main", "name": "final_reel.mp4"},
            {"id": "ig", "name": "output_instagram.mp4"},
            {"id": "ig-upper", "name": "OUTPUT_INSTAGRAM.MP4"},
        ],
    )

    assert ExportService.detect_upload_video_in_drive_root("folder_1") == [
        {"id": "main", "name": "final_reel.mp4"}
    ]


def test_drive_upsert_deletes_existing_file_before_upload(monkeypatch, tmp_path: Path) -> None:
    deleted: list[str] = []
    upload_args: dict[str, object] = {}
    local_path = tmp_path / "output_instagram.mp4"
    local_path.write_bytes(b"mp4")

    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_named",
        lambda parent_id, filename, *, drive=None: [
            {"id": "old_1", "name": filename},
            {"id": "old_2", "name": filename},
        ],
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "delete_file",
        lambda file_id, *, drive=None: deleted.append(file_id),
    )

    def fake_upload_local_file(**kwargs):
        upload_args.update(kwargs)
        return {"id": "new_file", "name": kwargs["filename"]}

    monkeypatch.setattr(GoogleDriveService, "upload_local_file", fake_upload_local_file)

    uploaded = GoogleDriveService.upsert_local_file(
        parent_id="folder_1",
        filename="output_instagram.mp4",
        local_path=local_path,
        drive=object(),
    )

    assert deleted == ["old_1", "old_2"]
    assert uploaded == {"id": "new_file", "name": "output_instagram.mp4"}
    assert upload_args["parent_id"] == "folder_1"
    assert upload_args["filename"] == "output_instagram.mp4"
    assert upload_args["local_path"] == local_path


def test_upload_phase_prepares_instagram_artifact_and_returns_drive_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    prepared = tmp_path / "output_instagram.mp4"
    calls: dict[str, object] = {}

    def prepare(**kwargs):
        calls["prepare"] = kwargs
        prepared.write_bytes(b"prepared")
        return LimitedDurationVideoPreparation(status="ready", video_path=prepared)

    monkeypatch.setattr(SocialUploadService, "prepare_instagram_video_for_drive", prepare)
    monkeypatch.setattr(GoogleDriveService, "client", lambda: "drive")

    def upsert(**kwargs):
        calls["upsert"] = kwargs
        return {"id": "ig_file", "webViewLink": "https://drive.google.com/file/d/ig_file"}

    monkeypatch.setattr(GoogleDriveService, "upsert_local_file", upsert)
    monkeypatch.setattr(
        GoogleDriveService,
        "set_public_read",
        lambda file_id, *, drive=None: calls.setdefault("public", (file_id, drive)),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "get_direct_download_url",
        lambda file_id: f"https://drive.usercontent.google.com/download?id={file_id}",
    )

    result, metadata = UploadPhaseService._prepare_instagram_drive_video(
        project_id="p1",
        source_video_path=source,
        drive_folder_id="folder_1",
        facebook_strategy="cut",
        work_dir=tmp_path,
    )

    assert result is None
    assert metadata == {
        "instagram_drive_file_id": "ig_file",
        "instagram_drive_video_url": "https://drive.usercontent.google.com/download?id=ig_file",
        "instagram_drive_web_url": "https://drive.google.com/file/d/ig_file",
        "instagram_drive_filename": "output_instagram.mp4",
    }
    assert calls["prepare"]["source_video_path"] == source
    assert calls["prepare"]["output_path"] == tmp_path / "output_instagram.mp4"
    assert calls["prepare"]["facebook_strategy"] == "cut"
    assert calls["upsert"]["parent_id"] == "folder_1"
    assert calls["upsert"]["filename"] == "output_instagram.mp4"
    assert calls["upsert"]["local_path"] == prepared
    assert calls["public"] == ("ig_file", "drive")


def test_upload_phase_instagram_preparation_failure_is_not_schedulable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    monkeypatch.setattr(
        SocialUploadService,
        "prepare_instagram_video_for_drive",
        lambda **kwargs: LimitedDurationVideoPreparation(
            status="error",
            detail="Instagram prepared media validation failed",
        ),
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "upsert_local_file",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("failed prep must not upload")
        ),
    )

    result, metadata = UploadPhaseService._prepare_instagram_drive_video(
        project_id="p1",
        source_video_path=source,
        drive_folder_id="folder_1",
        facebook_strategy="auto",
        work_dir=tmp_path,
    )

    assert result is not None
    assert result.platform == "instagram"
    assert result.status == "failed"
    assert result.detail == "Instagram prepared media validation failed"
    assert metadata == {}
