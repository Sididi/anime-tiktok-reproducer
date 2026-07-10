from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.google_drive_service import GoogleDriveService


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
        return SimpleNamespace(execute=lambda: self._response)


def _patch_client(monkeypatch, drive):
    monkeypatch.setattr(
        GoogleDriveService, "_client", classmethod(lambda cls: drive)
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


def test_api_error_returns_none(monkeypatch):
    _patch_client(monkeypatch, _FakeDrive(error=RuntimeError("boom")))
    assert GoogleDriveService.get_video_duration_seconds("f1") is None
