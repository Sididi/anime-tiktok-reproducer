from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.match_playback_service import MatchPlaybackService, _ClipPlan


@pytest.fixture(autouse=True)
def reset_capability_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class-level probe caches must not leak between tests."""
    monkeypatch.setattr(MatchPlaybackService, "_nvenc_checked", False)
    monkeypatch.setattr(MatchPlaybackService, "_nvenc_available", False)
    monkeypatch.setattr(MatchPlaybackService, "_full_gpu_checked", False)
    monkeypatch.setattr(MatchPlaybackService, "_full_gpu_available", False)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capability_fake_run(*, encoders: str, decoders: str, filters: str, calls: list):
    """Return a fake subprocess.run serving ffmpeg capability listings."""

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-encoders" in cmd:
            return _FakeCompleted(stdout=encoders)
        if "-decoders" in cmd:
            return _FakeCompleted(stdout=decoders)
        if "-filters" in cmd:
            return _FakeCompleted(stdout=filters)
        raise AssertionError(f"unexpected command: {cmd}")

    return _fake_run


def test_full_gpu_probe_true_when_all_capabilities_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264_cuvid ... hevc_cuvid ...",
            filters="... scale_cuda ...",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is True
    first_call_count = len(calls)

    # Second call must be served from the cache: no new subprocess calls.
    assert MatchPlaybackService._is_full_gpu_available_sync() is True
    assert len(calls) == first_call_count


def test_full_gpu_probe_false_without_scale_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264_cuvid ... hevc_cuvid ...",
            filters="... scale ... (no cuda resizer)",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def test_full_gpu_probe_false_without_cuvid_decoders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264 ... hevc ...",
            filters="... scale_cuda ...",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def test_full_gpu_probe_false_when_nvenc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: False)
    )

    def _explode(cmd, **kwargs):
        raise AssertionError("no capability listing should run when nvenc is absent")

    monkeypatch.setattr("app.services.match_playback_service.subprocess.run", _explode)
    assert MatchPlaybackService._is_full_gpu_available_sync() is False
