from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import Project
from app.services.google_drive_service import GoogleDriveService
from app.services.music_config_service import MusicConfigService, MusicEntry
from app.services.project_service import ProjectService
from app.services.upload_phase import (
    NO_MUSIC_WAV_FILENAME,
    UploadPhaseService,
    UploadReadiness,
)


def _readiness(folder_id: str | None) -> UploadReadiness:
    return UploadReadiness(
        status="green",
        metadata_exists=True,
        drive_video_count=1,
        drive_video_id="vid-1",
        drive_video_name="output.mp4",
        drive_video_web_url=None,
        reasons=[],
        drive_folder_id=folder_id,
        drive_folder_url=None,
    )


def _music(copyright_flag: bool) -> MusicEntry:
    return MusicEntry(
        key="track",
        display_name="Track",
        file_path="/nonexistent/track.wav",
        volume_db=-12,
        copyright=copyright_flag,
    )


@pytest.fixture
def _project(monkeypatch: pytest.MonkeyPatch) -> Project:
    project = Project(anime_name="Test Anime", music_key="track")
    monkeypatch.setattr(
        ProjectService, "load", classmethod(lambda cls, pid: project)
    )
    monkeypatch.setattr(
        MusicConfigService, "list_non_copyrighted", classmethod(lambda cls: [])
    )
    monkeypatch.setattr(
        UploadPhaseService,
        "compute_readiness",
        classmethod(lambda cls, p: _readiness("folder-1")),
    )
    return project


def test_non_copyrighted_music_short_circuits(
    monkeypatch: pytest.MonkeyPatch, _project: Project
) -> None:
    monkeypatch.setattr(
        MusicConfigService, "get_music", classmethod(lambda cls, key: _music(False))
    )

    def _no_drive(cls, folder_id, drive=None):
        raise AssertionError("Drive must not be touched for non-copyrighted music")

    monkeypatch.setattr(
        GoogleDriveService, "list_children", classmethod(_no_drive)
    )

    assert UploadPhaseService.check_copyright(_project.id) == {"copyrighted": False}


def test_copyrighted_with_wav_on_drive(
    monkeypatch: pytest.MonkeyPatch, _project: Project
) -> None:
    monkeypatch.setattr(
        MusicConfigService, "get_music", classmethod(lambda cls, key: _music(True))
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children",
        classmethod(
            lambda cls, folder_id, drive=None: [
                {"id": "wav-1", "name": NO_MUSIC_WAV_FILENAME},
                {"id": "vid-1", "name": "output.mp4"},
            ]
        ),
    )

    result = UploadPhaseService.check_copyright(_project.id)
    assert result["copyrighted"] is True
    assert result["no_music_available"] is True
    assert result["no_music_file_id"] == "wav-1"


def test_local_only_wav_is_not_reported_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _project: Project
) -> None:
    # Regression for the removed local-path branch: a wav in the project's
    # local output dir must NOT set no_music_available without a Drive file
    # id (the modal requires both and the session used to hang).
    projects_dir = tmp_path / "projects"
    output_dir = projects_dir / _project.id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / NO_MUSIC_WAV_FILENAME).write_bytes(b"wav")
    monkeypatch.setattr(settings, "projects_dir", projects_dir)

    monkeypatch.setattr(
        MusicConfigService, "get_music", classmethod(lambda cls, key: _music(True))
    )
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children",
        classmethod(lambda cls, folder_id, drive=None: []),
    )

    result = UploadPhaseService.check_copyright(_project.id)
    assert result["no_music_available"] is False
    assert result["no_music_file_id"] is None


def test_drive_listing_failure_degrades_to_unavailable(
    monkeypatch: pytest.MonkeyPatch, _project: Project
) -> None:
    monkeypatch.setattr(
        MusicConfigService, "get_music", classmethod(lambda cls, key: _music(True))
    )

    def _boom(cls, folder_id, drive=None):
        raise RuntimeError("drive down")

    monkeypatch.setattr(GoogleDriveService, "list_children", classmethod(_boom))

    result = UploadPhaseService.check_copyright(_project.id)
    assert result["copyrighted"] is True
    assert result["no_music_available"] is False
    assert result["no_music_file_id"] is None
