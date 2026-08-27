"""Regression tests for the 2026-08-27 upload crash:

* Drive resumable chunk transport errors (socket read timeout) are retried
  instead of killing the upload on attempt 1;
* the transfer client no longer uses googleapiclient's 60s socket timeout;
* an aborted upload settles platform jobs that were already running and
  rolls back what they published (orphan Facebook reel);
* the VPS client logs the response body of an HTTP error.
"""
from __future__ import annotations

import logging
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import local

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

from app.services import google_drive_service as gds
from app.services.discord_service import DiscordService, _swallow
from app.services.google_drive_service import GoogleDriveService
from app.services.social_upload_service import PlatformUploadResult
from app.services.upload_phase import UploadPhaseService


class _FakeHttp:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _FakeResumableRequest:
    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.http = _FakeHttp()
        self.calls = 0

    def next_chunk(self, num_retries=0):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gds.time, "sleep", lambda *_: None)


def test_resumable_upload_retries_transport_error_after_reconnect():
    request = _FakeResumableRequest(
        [TimeoutError("The read operation timed out"), (None, {"id": "file-1"})]
    )
    response = GoogleDriveService._upload_resumable_request(
        request, file_size=10, operation="test"
    )
    assert response == {"id": "file-1"}
    assert request.calls == 2
    assert request.http.closed == 1  # dead socket dropped before the retry


def test_resumable_upload_gives_up_after_max_attempts():
    request = _FakeResumableRequest([ssl.SSLError("EOF")] * 3)
    with pytest.raises(ssl.SSLError):
        GoogleDriveService._upload_resumable_request(
            request, file_size=10, operation="test", max_attempts=3
        )
    assert request.calls == 3


def test_resumable_upload_still_raises_non_retryable_errors():
    request = _FakeResumableRequest([ValueError("bad media")])
    with pytest.raises(ValueError):
        GoogleDriveService._upload_resumable_request(
            request, file_size=10, operation="test"
        )
    assert request.calls == 1


def test_download_file_retries_transport_error(monkeypatch, tmp_path):
    class _Downloader:
        instances = []

        def __init__(self, fd, request):
            self.request = request
            self.calls = 0
            _Downloader.instances.append(self)

        def next_chunk(self):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("The read operation timed out")
            return None, True

    class _Files:
        def get_media(self, **_):
            return _FakeResumableRequest([])

    class _Drive:
        def files(self):
            return _Files()

    monkeypatch.setattr(gds, "MediaIoBaseDownload", _Downloader)
    monkeypatch.setattr(GoogleDriveService, "_client", classmethod(lambda cls: _Drive()))
    GoogleDriveService.download_file("file-1", tmp_path / "out.mp4")
    downloader = _Downloader.instances[-1]
    assert downloader.calls == 2
    assert downloader.request.http.closed == 1


def test_transfer_client_uses_long_socket_timeout(monkeypatch):
    class _Creds:
        token = "t"

    monkeypatch.setattr(GoogleDriveService, "_client_local", local())
    monkeypatch.setattr(GoogleDriveService, "_credentials", classmethod(lambda cls: _Creds()))
    client = GoogleDriveService.client()
    assert client._http.http.timeout == GoogleDriveService._TRANSFER_HTTP_TIMEOUT_SECONDS
    assert GoogleDriveService._TRANSFER_HTTP_TIMEOUT_SECONDS > 60


def test_swallow_logs_http_error_response_body(monkeypatch, caplog):
    monkeypatch.setattr(DiscordService, "is_configured", classmethod(lambda cls: True))

    @_swallow("Discord create_job")
    def boom():
        request = httpx.Request("POST", "https://vps.example/api/internal/jobs")
        response = httpx.Response(
            400, request=request, json={"detail": "Unknown account 'x_tmp'"}
        )
        raise httpx.HTTPStatusError("400", request=request, response=response)

    with caplog.at_level(logging.WARNING):
        assert boom() is None
    assert "Unknown account 'x_tmp'" in caplog.text


def _uploaded(platform: str, rid: str) -> PlatformUploadResult:
    return PlatformUploadResult(
        platform=platform, status="uploaded", url=f"https://x/{rid}", resource_id=rid
    )


def test_settle_aborted_jobs_rolls_back_running_and_cancels_queued(monkeypatch):
    rolled_back = []
    monkeypatch.setattr(
        UploadPhaseService,
        "_rollback_platform_upload",
        classmethod(lambda cls, account_id, result: rolled_back.append((account_id, result.resource_id)) or True),
    )
    emitted = []

    def emit(result, *, update_discord=True):
        emitted.append((result, update_discord))

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        def running_job():
            time.sleep(0.2)
            return _uploaded("facebook", "reel-1")

        queued_job_ran = []

        def queued_job():
            queued_job_ran.append(True)
            return _uploaded("youtube", "yt-1")

        futures = {
            executor.submit(running_job): "facebook",
            executor.submit(queued_job): "youtube",  # max_workers=1 → still queued
        }
        leftovers = UploadPhaseService._settle_aborted_platform_jobs(
            futures, account_id="acc", emit_platform_result=emit
        )
    finally:
        executor.shutdown(wait=True)

    assert leftovers == []
    assert rolled_back == [("acc", "reel-1")]
    assert queued_job_ran == []
    assert len(emitted) == 1
    row, update_discord = emitted[0]
    assert row.platform == "facebook" and row.status == "failed"
    assert row.resource_id is None and "removed" in (row.detail or "")
    assert update_discord is False


def test_settle_aborted_jobs_reports_leftover_when_rollback_fails(monkeypatch):
    monkeypatch.setattr(
        UploadPhaseService, "_rollback_platform_upload", classmethod(lambda cls, a, r: False)
    )
    emitted = []
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        futures = {executor.submit(lambda: _uploaded("facebook", "reel-2")): "facebook"}
        time.sleep(0.05)
        leftovers = UploadPhaseService._settle_aborted_platform_jobs(
            futures, account_id="acc", emit_platform_result=lambda r, **kw: emitted.append(r)
        )
    finally:
        executor.shutdown(wait=True)
    assert leftovers == ["facebook: https://x/reel-2"]
    assert emitted[0].resource_id == "reel-2"
    assert "could NOT be removed" in (emitted[0].detail or "")


def test_settle_aborted_jobs_ignores_failed_and_skipped_results(monkeypatch):
    calls = []
    monkeypatch.setattr(
        UploadPhaseService, "_rollback_platform_upload", classmethod(lambda cls, a, r: calls.append(r) or True)
    )
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        def failing():
            raise RuntimeError("upload failed on its own")

        futures = {
            executor.submit(failing): "youtube",
            executor.submit(lambda: PlatformUploadResult(platform="facebook", status="skipped")): "facebook",
        }
        time.sleep(0.05)
        leftovers = UploadPhaseService._settle_aborted_platform_jobs(
            futures, account_id="acc", emit_platform_result=lambda r, **kw: None
        )
    finally:
        executor.shutdown(wait=True)
    assert leftovers == [] and calls == []


def test_rollback_facebook_deletes_reel_via_graph(monkeypatch):
    from app.services import upload_phase as up
    from app.services.account_service import AccountService

    class _Creds:
        facebook_page_access_token = "page-token"

    monkeypatch.setattr(AccountService, "get_meta_credentials", classmethod(lambda cls, a: _Creds()))
    seen = {}

    def fake_delete(url, *, params, timeout):
        seen["url"] = url
        seen["params"] = params
        return httpx.Response(200, json={"success": True}, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(up.httpx, "delete", fake_delete)
    ok = UploadPhaseService._rollback_platform_upload("acc", _uploaded("facebook", "reel-9"))
    assert ok is True
    assert seen["url"].endswith("/reel-9")
    assert seen["params"] == {"access_token": "page-token"}


def test_rollback_returns_false_for_unsupported_platform_or_missing_id():
    assert UploadPhaseService._rollback_platform_upload("acc", _uploaded("instagram", "m-1")) is False
    assert UploadPhaseService._rollback_platform_upload("acc", PlatformUploadResult(platform="facebook", status="uploaded")) is False
    assert UploadPhaseService._rollback_platform_upload(None, _uploaded("facebook", "r")) is False
