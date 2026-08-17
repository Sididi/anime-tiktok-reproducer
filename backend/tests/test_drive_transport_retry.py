from __future__ import annotations

import ssl
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.google_drive_service import GoogleDriveService


class _FakeHttp:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeDrive:
    """Drive stub whose first call dies the way a stale keep-alive socket does."""

    def __init__(self, errors: list[Exception], response=None) -> None:
        self._errors = list(errors)
        self._response = response if response is not None else {"id": "p1"}
        self._http = _FakeHttp()
        self.calls = 0
        self.page_tokens: list[str | None] = []

    def permissions(self):
        return self

    def files(self):
        return self

    def create(self, **kwargs):
        return SimpleNamespace(execute=self._execute)

    def get(self, **kwargs):
        return SimpleNamespace(execute=self._execute)

    def list(self, **kwargs):
        self.page_tokens.append(kwargs.get("pageToken"))
        return SimpleNamespace(execute=self._execute)

    def _execute(self, **kwargs):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._response


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(
        "app.services.google_drive_service.time.sleep", lambda _seconds: None
    )
    monkeypatch.setattr(GoogleDriveService, "reset_client", classmethod(lambda cls: None))


def _ssl_eof() -> ssl.SSLError:
    return ssl.SSLError(
        "[SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof while reading"
    )


def test_set_public_read_reconnects_after_tls_eof():
    drive = _FakeDrive([_ssl_eof()])

    GoogleDriveService.set_public_read("video1", drive=drive)

    assert drive.calls == 2
    assert drive._http.closed == 1


def test_set_public_read_reconnects_after_broken_pipe():
    drive = _FakeDrive([BrokenPipeError(32, "Broken pipe")])

    GoogleDriveService.set_public_read("video1", drive=drive)

    assert drive.calls == 2


def test_set_public_read_gives_up_after_max_attempts():
    drive = _FakeDrive([_ssl_eof() for _ in range(5)])

    with pytest.raises(ssl.SSLError):
        GoogleDriveService.set_public_read("video1", drive=drive)

    assert drive.calls == 5


def test_get_web_view_url_reconnects_after_tls_eof(monkeypatch):
    drive = _FakeDrive([_ssl_eof()], response={"webViewLink": "https://drive/v1"})
    monkeypatch.setattr(GoogleDriveService, "_client", classmethod(lambda cls: drive))

    assert GoogleDriveService.get_web_view_url("video1") == "https://drive/v1"
    assert drive.calls == 2


def test_query_files_retries_the_same_page_after_connection_loss():
    drive = _FakeDrive([ConnectionResetError(104, "Connection reset by peer")],
                       response={"files": [{"id": "f1"}]})

    files = GoogleDriveService._query_files("trashed=false", drive=drive)

    assert files == [{"id": "f1"}]
    # The failed page is replayed with its own token, not skipped.
    assert drive.page_tokens == [None, None]


def test_non_transport_errors_are_not_retried_as_connection_loss():
    drive = _FakeDrive([ValueError("bad request")])

    with pytest.raises(ValueError):
        GoogleDriveService.set_public_read("video1", drive=drive)

    assert drive.calls == 1
