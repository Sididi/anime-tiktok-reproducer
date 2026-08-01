from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.google_drive_service import (
    DriveVideoMetadataLookupError,
    GoogleDriveService,
)


class _FakeDrive:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def files(self):
        return self

    def get(self, **kwargs):
        assert kwargs["fields"] == "videoMediaMetadata(durationMillis)"
        assert kwargs["supportsAllDrives"] is True
        if self._error:
            raise self._error
        return SimpleNamespace(execute=lambda **execute_kwargs: self._response)


def _patch_client(monkeypatch, drive):
    monkeypatch.setattr(
        GoogleDriveService, "_video_metadata_client", classmethod(lambda cls: drive)
    )


@pytest.fixture(autouse=True)
def clear_duration_cache(monkeypatch):
    monkeypatch.setattr(GoogleDriveService, "_video_duration_cache", {})
    monkeypatch.setattr(
        GoogleDriveService,
        "_reset_video_metadata_client",
        classmethod(lambda cls: None),
    )


def test_duration_from_metadata(monkeypatch):
    _patch_client(
        monkeypatch,
        _FakeDrive({"videoMediaMetadata": {"durationMillis": "95500"}}),
    )
    assert GoogleDriveService.get_video_duration_seconds("f1") == 95.5


def test_missing_metadata_returns_none(monkeypatch):
    _patch_client(monkeypatch, _FakeDrive({}))
    assert GoogleDriveService.get_video_duration_seconds("f1") is None


def test_unparsable_duration_returns_none(monkeypatch):
    _patch_client(
        monkeypatch,
        _FakeDrive({"videoMediaMetadata": {"durationMillis": "abc"}}),
    )
    assert GoogleDriveService.get_video_duration_seconds("f1") is None


def test_api_error_is_not_reported_as_missing_metadata(monkeypatch):
    _patch_client(monkeypatch, _FakeDrive(error=RuntimeError("boom")))
    monkeypatch.setattr(GoogleDriveService, "_VIDEO_METADATA_MAX_ATTEMPTS", 1)
    with pytest.raises(DriveVideoMetadataLookupError):
        GoogleDriveService.get_video_duration_seconds("f1")


def test_successful_duration_is_cached(monkeypatch):
    drive = _FakeDrive({"videoMediaMetadata": {"durationMillis": "95500"}})
    calls = 0
    original_get = drive.get

    def counted_get(**kwargs):
        nonlocal calls
        calls += 1
        return original_get(**kwargs)

    drive.get = counted_get
    _patch_client(monkeypatch, drive)

    assert GoogleDriveService.get_video_duration_seconds("f1") == 95.5
    assert GoogleDriveService.get_video_duration_seconds("f1") == 95.5
    assert calls == 1
