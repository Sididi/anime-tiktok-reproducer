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
                    "transferring": [{"name": "sources/episode.mkv"}],
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
