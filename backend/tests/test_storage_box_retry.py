from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncssh

from app.config import settings
from app.services.storage_box_sftp_client import _is_transient_error, _retry_transient


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "storage_box_retry_max_attempts", 4)
    monkeypatch.setattr(settings, "storage_box_retry_base_delay_seconds", 0.001)
    monkeypatch.setattr(settings, "storage_box_retry_max_delay_seconds", 0.01)


def test_is_transient_error_recognises_connection_drops() -> None:
    assert _is_transient_error(asyncssh.misc.ConnectionLost("x"))
    assert _is_transient_error(ConnectionResetError(104, "x"))
    assert _is_transient_error(BrokenPipeError(32, "x"))
    assert _is_transient_error(EOFError())


def test_is_transient_error_rejects_permission_errors() -> None:
    assert not _is_transient_error(PermissionError(13, "denied"))
    assert not _is_transient_error(ValueError("bad value"))
    # FileNotFoundError uses errno=ENOENT which is not in the transient set.
    assert not _is_transient_error(FileNotFoundError(2, "missing"))


@pytest.mark.asyncio
async def test_retry_transient_succeeds_after_recoverable_failures() -> None:
    calls = {"count": 0}

    async def factory() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise asyncssh.misc.ConnectionLost("transient")
        return "ok"

    result = await _retry_transient("op", factory)
    assert result == "ok"
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_retry_transient_propagates_non_transient_immediately() -> None:
    calls = {"count": 0}

    async def factory() -> str:
        calls["count"] += 1
        raise PermissionError(13, "denied")

    with pytest.raises(PermissionError):
        await _retry_transient("op", factory)
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_retry_transient_gives_up_after_max_attempts() -> None:
    calls = {"count": 0}

    async def factory() -> str:
        calls["count"] += 1
        raise ConnectionResetError(104, "transient")

    with pytest.raises(ConnectionResetError):
        await _retry_transient("op", factory)
    assert calls["count"] == settings.storage_box_retry_max_attempts
