from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.google_drive_rclone import (
    GoogleDriveRclone,
    GoogleDriveRcloneError,
)
from app.services.google_drive_service import GoogleDriveService
from app.services.rclone_runner import RcloneStats

_FAKE_RCLONE = """#!/usr/bin/env python3
import json, os, sys

capture = os.environ.get("FAKE_RCLONE_CAPTURE")
if capture:
    with open(capture, "w") as fh:
        json.dump(
            {
                "argv": sys.argv[1:],
                "env": {
                    key: os.environ.get(key)
                    for key in (
                        "RCLONE_DRIVE_CLIENT_ID",
                        "RCLONE_DRIVE_CLIENT_SECRET",
                        "RCLONE_DRIVE_TOKEN",
                        "RCLONE_DRIVE_ROOT_FOLDER_ID",
                        "RCLONE_DRIVE_TEAM_DRIVE",
                    )
                },
            },
            fh,
        )

for line in os.environ.get("FAKE_RCLONE_STDERR", "").split("\\x1e"):
    if line:
        sys.stderr.write(line + "\\n")
sys.exit(int(os.environ.get("FAKE_RCLONE_EXIT", "0")))
"""


@pytest.fixture(autouse=True)
def _drive_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "google_drive_refresh_token", "refresh-token")
    monkeypatch.setattr(settings, "google_drive_team_drive_id", None)
    monkeypatch.setattr(settings, "drive_rclone_transfers", 4)
    monkeypatch.setattr(settings, "drive_rclone_chunk_mb", 128)
    monkeypatch.setattr(
        GoogleDriveService,
        "rclone_token_json",
        classmethod(
            lambda cls: json.dumps(
                {
                    "access_token": "fresh-access",
                    "token_type": "Bearer",
                    "refresh_token": "refresh-token",
                    "expiry": "2026-01-01T00:00:00+00:00",
                }
            )
        ),
    )
    yield


@pytest.fixture
def fake_rclone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    binary = tmp_path / "fake-rclone"
    binary.write_text(_FAKE_RCLONE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        GoogleDriveRclone, "binary", classmethod(lambda cls: str(binary))
    )
    return binary


@pytest.mark.asyncio
async def test_sync_command_flags_and_env_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    capture_path = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_RCLONE_CAPTURE", str(capture_path))
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    await GoogleDriveRclone.sync_tree(stage_dir, folder_id="folder-123")

    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    argv = captured["argv"]
    assert argv[0] == "sync"
    assert argv[1] == str(stage_dir)
    assert argv[2] == ":drive:"
    assert "--checksum" in argv
    assert "--copy-links" in argv
    assert "--delete-during" in argv

    def flag(name: str) -> str:
        return argv[argv.index(name) + 1]

    assert flag("--transfers") == "4"
    assert flag("--drive-chunk-size") == "128M"
    assert flag("--drive-upload-cutoff") == "64M"

    env = captured["env"]
    assert env["RCLONE_DRIVE_CLIENT_ID"] == "client-id"
    assert env["RCLONE_DRIVE_CLIENT_SECRET"] == "client-secret"
    assert env["RCLONE_DRIVE_ROOT_FOLDER_ID"] == "folder-123"
    assert env["RCLONE_DRIVE_TEAM_DRIVE"] is None
    token = json.loads(env["RCLONE_DRIVE_TOKEN"])
    assert token["refresh_token"] == "refresh-token"
    assert token["access_token"] == "fresh-access"

    # No secrets on argv.
    joined = " ".join(argv)
    assert "client-secret" not in joined
    assert "refresh-token" not in joined


@pytest.mark.asyncio
async def test_copy_command_is_additive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    capture_path = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_RCLONE_CAPTURE", str(capture_path))
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    await GoogleDriveRclone.copy_tree(stage_dir, folder_id="shared-1")

    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    argv = captured["argv"]
    assert argv[0] == "copy"
    assert argv[1] == str(stage_dir)
    assert argv[2] == ":drive:"
    assert "--checksum" in argv
    assert "--copy-links" in argv
    # The shared folder has other writers' files: copy must never delete.
    assert "--delete-during" not in argv
    assert captured["env"]["RCLONE_DRIVE_ROOT_FOLDER_ID"] == "shared-1"


@pytest.mark.asyncio
async def test_sync_team_drive_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    monkeypatch.setattr(settings, "google_drive_team_drive_id", "team-9")
    capture_path = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_RCLONE_CAPTURE", str(capture_path))
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    await GoogleDriveRclone.sync_tree(stage_dir, folder_id="folder-123")

    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    assert captured["env"]["RCLONE_DRIVE_TEAM_DRIVE"] == "team-9"


@pytest.mark.asyncio
async def test_sync_stats_reach_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    monkeypatch.setenv(
        "FAKE_RCLONE_STDERR",
        json.dumps(
            {
                "level": "notice",
                "msg": "stats",
                "stats": {
                    "bytes": 512,
                    "totalBytes": 2048,
                    "speed": 1048576.0,
                    "eta": 7,
                    "transferring": [
                        {"name": "sources/episode.mkv", "bytes": 300, "size": 1000, "speed": 2048.0}
                    ],
                    "transfers": 1,
                    "totalTransfers": 3,
                    "checks": 5,
                    "totalChecks": 9,
                },
            }
        ),
    )
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    seen: list[RcloneStats] = []

    await GoogleDriveRclone.sync_tree(
        stage_dir, folder_id="folder-123", stats_callback=seen.append
    )

    assert len(seen) == 1
    stats = seen[0]
    assert stats.bytes_transferred == 512
    assert stats.bytes_total == 2048
    assert stats.transfers == 1
    assert stats.total_transfers == 3
    assert stats.checks == 5
    assert stats.total_checks == 9
    assert stats.transferring_names == ["sources/episode.mkv"]
    assert stats.transferring[0].bytes_done == 300
    assert stats.transferring[0].size == 1000
    assert stats.transferring[0].speed_bytes_per_sec == 2048.0


@pytest.mark.asyncio
async def test_sync_error_exit_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    monkeypatch.setenv(
        "FAKE_RCLONE_STDERR",
        json.dumps({"level": "error", "msg": "googleapi: rate limit"}),
    )
    monkeypatch.setenv("FAKE_RCLONE_EXIT", "7")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    with pytest.raises(GoogleDriveRcloneError) as excinfo:
        await GoogleDriveRclone.sync_tree(stage_dir, folder_id="folder-123")
    assert "code 7" in str(excinfo.value)
    assert "googleapi: rate limit" in str(excinfo.value)


@pytest.mark.asyncio
async def test_missing_binary_raises_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(GoogleDriveRclone, "binary", classmethod(lambda cls: None))
    with pytest.raises(GoogleDriveRcloneError, match="pacman -S rclone"):
        await GoogleDriveRclone.sync_tree(tmp_path, folder_id="folder-123")


# --------------------------------------------------------------------------- #
# Throttled-session guard                                                     #
# --------------------------------------------------------------------------- #

from app.services.google_drive_rclone import _SlowStreamGuard  # noqa: E402
from app.services import google_drive_rclone as gdr_module  # noqa: E402
from app.services.rclone_runner import RcloneRestartRequested  # noqa: E402

_MIB = 1024 * 1024


def _batch_stats(done: int, total: int) -> RcloneStats:
    return RcloneStats(
        bytes_transferred=done, bytes_total=total, speed_bytes_per_sec=0.0, eta_seconds=None,
        transferring_names=[], transfers=0, total_transfers=1, checks=0, total_checks=0,
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _guard(clock: _Clock) -> _SlowStreamGuard:
    return _SlowStreamGuard(
        floor_bytes_per_sec=1 * _MIB, window_seconds=60, min_remaining_bytes=64 * _MIB, clock=clock
    )


def test_guard_restarts_a_session_crawling_for_a_full_window() -> None:
    clock = _Clock()
    guard = _guard(clock)
    total = 1400 * _MIB
    # 0.5 MB/s for 70 s: under the floor once the window is full.
    for second in range(0, 71):
        clock.now = 1000.0 + second
        reason = guard.check(_batch_stats(done=second * _MIB // 2, total=total))
        if second < 60:
            assert reason is None, second
    assert reason is not None and "throttled" in reason


def test_guard_keeps_a_healthy_session_and_small_remainders() -> None:
    clock = _Clock()
    guard = _guard(clock)
    total = 1400 * _MIB
    for second in range(0, 91):  # 8 MB/s
        clock.now = 1000.0 + second
        assert guard.check(_batch_stats(done=second * 8 * _MIB, total=total)) is None
    # Crawling, but only 10 MB left: finishing beats restarting.
    guard = _guard(clock)
    for second in range(0, 91):
        clock.now = 2000.0 + second
        assert guard.check(_batch_stats(done=total - 10 * _MIB + second, total=total)) is None
    # Still scanning (no total yet) never trips.
    guard = _guard(clock)
    for second in range(0, 91):
        clock.now = 3000.0 + second
        assert guard.check(_batch_stats(done=0, total=0)) is None


def test_guard_window_is_trailing_not_lifetime() -> None:
    clock = _Clock()
    guard = _guard(clock)
    total = 1400 * _MIB
    # 8 MB/s for 100 s (800 MB in), then the session degrades to 0.3 MB/s.
    done = 0
    for second in range(0, 100):
        clock.now = 1000.0 + second
        done = second * 8 * _MIB
        assert guard.check(_batch_stats(done=done, total=total)) is None
    for second in range(100, 151):
        clock.now = 1000.0 + second
        done += int(0.3 * _MIB)
        # The trailing window still holds enough of the fast phase.
        assert guard.check(_batch_stats(done=done, total=total)) is None, second
    clock.now = 1000.0 + 162
    done += int(0.3 * _MIB)
    # A lifetime average (~5 MB/s) would never trip; the trailing window does.
    assert guard.check(_batch_stats(done=done, total=total)) is not None


@pytest.mark.asyncio
async def test_run_restarts_then_lets_the_last_attempt_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "drive_slow_stream_restart_enabled", True)
    monkeypatch.setattr(settings, "drive_slow_stream_max_restarts", 2)
    monkeypatch.setattr(GoogleDriveRclone, "binary", classmethod(lambda cls: "/bin/true"))

    async def _env(cls, folder_id):
        return {"FOLDER": folder_id}

    monkeypatch.setattr(GoogleDriveRclone, "_build_env", classmethod(_env))
    calls: list[object] = []

    async def _fake_run(argv, *, env=None, stats_callback=None, error_prefix="rclone", should_restart=None):
        calls.append(should_restart)
        if len(calls) <= 2:
            raise RcloneRestartRequested("upload session throttled", None)
        return None

    monkeypatch.setattr(gdr_module, "run_rclone", _fake_run)
    restarts: list[str] = []
    await GoogleDriveRclone.copy_tree(tmp_path, folder_id="f", on_restart=restarts.append)

    assert len(calls) == 3
    assert calls[0] is not None and calls[1] is not None
    assert calls[2] is None  # last allowed attempt runs unguarded
    assert restarts == ["upload session throttled", "upload session throttled"]


@pytest.mark.asyncio
async def test_run_without_guard_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "drive_slow_stream_restart_enabled", False)
    monkeypatch.setattr(GoogleDriveRclone, "binary", classmethod(lambda cls: "/bin/true"))

    async def _env(cls, folder_id):
        return {}

    monkeypatch.setattr(GoogleDriveRclone, "_build_env", classmethod(_env))
    seen: list[object] = []

    async def _fake_run(argv, *, env=None, stats_callback=None, error_prefix="rclone", should_restart=None):
        seen.append(should_restart)

    monkeypatch.setattr(gdr_module, "run_rclone", _fake_run)
    await GoogleDriveRclone.sync_tree(tmp_path, folder_id="f")
    assert seen == [None]
