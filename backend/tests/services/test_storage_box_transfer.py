from __future__ import annotations

import asyncio
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.services.storage_box_sftp_client import StorageBoxSftpClient
from app.services.storage_box_transfer import StorageBoxTransferService
from app.utils.subprocess_runner import CommandResult


def _reset_transfer_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(StorageBoxTransferService, "_preflight_cache", {}, raising=False)


def _configure_rsync_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    key_path = tmp_path / "keys" / "storage box key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("dummy-key", encoding="utf-8")
    known_hosts = tmp_path / "known_hosts storage"
    known_hosts.write_text("host-key", encoding="utf-8")

    monkeypatch.setattr(settings, "storage_box_enabled", True)
    monkeypatch.setattr(settings, "storage_box_host", "storage.example.test")
    monkeypatch.setattr(settings, "storage_box_port", 23)
    monkeypatch.setattr(settings, "storage_box_username", "demo-user")
    monkeypatch.setattr(settings, "storage_box_password", None)
    monkeypatch.setattr(settings, "storage_box_ssh_key_path", key_path)
    monkeypatch.setattr(settings, "storage_box_known_hosts_path", known_hosts)
    monkeypatch.setattr(settings, "storage_box_root", "root with spaces")
    monkeypatch.setattr(settings, "storage_box_transfer_mode", "auto")
    monkeypatch.setattr(settings, "storage_box_rsync_min_file_size_mb", 16)
    monkeypatch.setattr(settings, "storage_box_rsync_timeout_seconds", 120)
    monkeypatch.setattr(StorageBoxSftpClient, "is_configured", classmethod(lambda cls: True))
    monkeypatch.setattr("app.services.storage_box_transfer.shutil.which", lambda name: f"/usr/bin/{name}")
    _reset_transfer_state(monkeypatch)
    return key_path, known_hosts


def _create_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(size_bytes)


@pytest.mark.asyncio
async def test_auto_upload_uses_rsync_for_large_file_and_preserves_spaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_rsync_ready(monkeypatch, tmp_path)
    local_path = tmp_path / "video file.mp4"
    _create_file(local_path, 17 * 1024 * 1024)
    commands: list[list[str]] = []

    async def fake_run_command(cmd, *, cwd=None, timeout_seconds=None):
        commands.append(list(cmd))
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    async def unexpected_upload(cls, local_path, remote_path):
        raise AssertionError("SFTP upload should not run when rsync is available")

    async def fake_exists(cls, remote_path):
        return False

    monkeypatch.setattr("app.services.storage_box_transfer.run_command", fake_run_command)
    monkeypatch.setattr(StorageBoxSftpClient, "upload_file", classmethod(unexpected_upload))
    monkeypatch.setattr(StorageBoxSftpClient, "exists", classmethod(fake_exists))

    await StorageBoxTransferService.upload_file(
        local_path,
        PurePosixPath("folder with spaces/output clip.mp4"),
    )

    assert len(commands) == 2
    preflight_cmd, upload_cmd = commands
    assert "--list-only" in preflight_cmd
    assert "--mkpath" in upload_cmd
    assert "--whole-file" in upload_cmd
    assert "--protect-args" in upload_cmd
    assert "--info=stats2" in upload_cmd
    assert "--append-verify" not in upload_cmd
    assert upload_cmd[-2] == str(local_path)
    assert upload_cmd[-1] == "demo-user@storage.example.test:root with spaces/folder with spaces/output clip.mp4"

    ssh_arg = upload_cmd[upload_cmd.index("-e") + 1]
    assert "IdentitiesOnly=yes" in ssh_arg
    assert "BatchMode=yes" in ssh_arg
    assert "StrictHostKeyChecking=yes" in ssh_arg
    assert "UserKnownHostsFile=" in ssh_arg
    assert str(settings.storage_box_ssh_key_path) in ssh_arg


@pytest.mark.asyncio
async def test_auto_upload_falls_back_to_sftp_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_rsync_ready(monkeypatch, tmp_path)
    local_path = tmp_path / "big.bin"
    _create_file(local_path, 17 * 1024 * 1024)
    sftp_calls: list[tuple[Path, PurePosixPath]] = []

    async def fake_preflight(cls):
        return {"available": False, "reason": "rsync preflight failed", "cached": False}

    async def fake_sftp_upload(cls, local_path: Path, remote_path: PurePosixPath):
        sftp_calls.append((local_path, remote_path))

    monkeypatch.setattr(StorageBoxTransferService, "preflight", classmethod(fake_preflight))
    monkeypatch.setattr(
        "app.services.storage_box_transfer.run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rsync transfer should not start after failed preflight")),
    )
    monkeypatch.setattr(StorageBoxSftpClient, "upload_file", classmethod(fake_sftp_upload))

    await StorageBoxTransferService.upload_file(local_path, PurePosixPath("bulk/object.bin"))

    assert sftp_calls == [(local_path, PurePosixPath("root with spaces/bulk/object.bin"))]


@pytest.mark.asyncio
async def test_rsync_mode_rejects_missing_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_rsync_ready(monkeypatch, tmp_path)
    local_path = tmp_path / "strict.bin"
    _create_file(local_path, 1024)
    monkeypatch.setattr(settings, "storage_box_transfer_mode", "rsync")
    monkeypatch.setattr(settings, "storage_box_port", 22)

    with pytest.raises(RuntimeError, match="ATR_STORAGE_BOX_PORT=23"):
        await StorageBoxTransferService.upload_file(local_path, PurePosixPath("strict/object.bin"))


@pytest.mark.asyncio
async def test_auto_download_uses_rsync_for_large_remote_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_rsync_ready(monkeypatch, tmp_path)
    local_path = tmp_path / "downloads" / "local file.mp4"
    commands: list[list[str]] = []

    class _Stat:
        size = 17 * 1024 * 1024

    async def fake_run_command(cmd, *, cwd=None, timeout_seconds=None):
        commands.append(list(cmd))
        if "--list-only" not in cmd:
            Path(cmd[-1]).write_bytes(b"downloaded")
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    async def fake_stat(cls, remote_path):
        return _Stat()

    async def unexpected_download(cls, remote_path, local_path):
        raise AssertionError("SFTP download should not run when rsync is available")

    monkeypatch.setattr("app.services.storage_box_transfer.run_command", fake_run_command)
    monkeypatch.setattr(StorageBoxSftpClient, "stat", classmethod(fake_stat))
    monkeypatch.setattr(StorageBoxSftpClient, "download_file", classmethod(unexpected_download))

    await StorageBoxTransferService.download_file(
        PurePosixPath("folder with spaces/remote clip.mp4"),
        local_path,
    )

    assert len(commands) == 2
    assert "--whole-file" in commands[-1]
    assert "--append-verify" not in commands[-1]
    assert commands[-1][-2] == "demo-user@storage.example.test:root with spaces/folder with spaces/remote clip.mp4"
    assert commands[-1][-1] == str(local_path)
    assert local_path.read_bytes() == b"downloaded"


@pytest.mark.asyncio
async def test_download_rsync_resumes_existing_local_partial_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_rsync_ready(monkeypatch, tmp_path)
    local_path = tmp_path / "downloads" / "resume.bin"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"partial")
    commands: list[list[str]] = []

    class _Stat:
        size = 17 * 1024 * 1024

    async def fake_run_command(cmd, *, cwd=None, timeout_seconds=None):
        commands.append(list(cmd))
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    async def fake_stat(cls, remote_path):
        return _Stat()

    monkeypatch.setattr("app.services.storage_box_transfer.run_command", fake_run_command)
    monkeypatch.setattr(StorageBoxSftpClient, "stat", classmethod(fake_stat))

    await StorageBoxTransferService.download_file(
        PurePosixPath("resume/object.bin"),
        local_path,
    )

    assert len(commands) == 2
    assert "--append-verify" in commands[-1]
    assert "--whole-file" not in commands[-1]
