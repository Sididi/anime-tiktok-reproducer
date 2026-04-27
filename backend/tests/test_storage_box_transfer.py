from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from shlex import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, settings
from app.services.storage_box_lftp import StorageBoxLftpService
from app.services.storage_box_sftp_client import StorageBoxSftpClient
from app.services.storage_box_transfer import StorageBoxTransferService
from app.utils.subprocess_runner import CommandResult


@pytest.fixture(autouse=True)
def _reset_storage_box_state(monkeypatch: pytest.MonkeyPatch) -> None:
    StorageBoxLftpService._preflight_cache.clear()
    StorageBoxTransferService._preflight_cache.clear()
    monkeypatch.setattr(settings, "storage_box_host", "storage.example")
    monkeypatch.setattr(settings, "storage_box_port", 23)
    monkeypatch.setattr(settings, "storage_box_username", "storage-user")
    monkeypatch.setattr(settings, "storage_box_ssh_key_path", Path("/tmp/storage key"))
    monkeypatch.setattr(settings, "storage_box_password", None)
    monkeypatch.setattr(settings, "storage_box_known_hosts_path", None)
    monkeypatch.setattr(settings, "storage_box_root", "")
    monkeypatch.setattr(settings, "storage_box_enabled", True)
    monkeypatch.setattr(settings, "storage_box_max_connections", 8)
    monkeypatch.setattr(settings, "storage_box_upload_max_parallel", 6)
    monkeypatch.setattr(settings, "storage_box_download_max_parallel", 6)
    monkeypatch.setattr(settings, "storage_box_rsync_timeout_seconds", 30)
    monkeypatch.setattr(settings, "storage_box_rsync_min_file_size_mb", 1)
    monkeypatch.setattr(settings, "storage_box_lftp_segments", 4)
    monkeypatch.setattr(settings, "storage_box_lftp_min_file_size_mb", 50)
    yield
    StorageBoxLftpService._preflight_cache.clear()
    StorageBoxTransferService._preflight_cache.clear()


def test_storage_box_transfer_mode_accepts_lftp() -> None:
    configured = Settings(_env_file=None, storage_box_transfer_mode="lftp")
    assert configured.storage_box_transfer_mode == "lftp"


@pytest.mark.asyncio
async def test_lftp_preflight_uses_script_open_and_strict_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_command(cmd: list[str], *, cwd=None, timeout_seconds: float | None = None) -> CommandResult:
        captured["cmd"] = cmd
        captured["timeout_seconds"] = timeout_seconds
        captured["script"] = Path(cmd[2]).read_text(encoding="utf-8")
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        StorageBoxLftpService,
        "_capability",
        classmethod(lambda cls: (True, "ready")),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "_lftp_binary",
        classmethod(lambda cls: "/usr/bin/lftp"),
    )
    monkeypatch.setattr("app.services.storage_box_lftp.run_command", fake_run_command)

    result = await StorageBoxLftpService.preflight()

    assert result == {"available": True, "reason": "ready", "cached": False}
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:2] == ["/usr/bin/lftp", "-f"]
    assert len(cmd) == 3
    assert captured["timeout_seconds"] == 30
    script = str(captured["script"])
    assert "set cmd:fail-exit yes" in script
    assert "open sftp://storage-user@storage.example" in script
    assert "\npwd\n" in script


@pytest.mark.asyncio
async def test_auto_single_file_selection_skips_lftp_for_upload_and_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "Episode 01.mkv"
    local_path.write_bytes(b"episode")

    async def fake_rsync_preflight(cls) -> dict[str, object]:
        return {"available": True, "reason": "ready", "cached": False}

    async def fake_remote_size(cls, remote_path: PurePosixPath) -> int:
        return 7

    def fail_if_lftp_checked(cls):
        raise AssertionError("auto mode should not consult lftp for single-file transfers")

    monkeypatch.setattr(
        StorageBoxTransferService,
        "_configured_mode",
        classmethod(lambda cls: "auto"),
    )
    monkeypatch.setattr(
        StorageBoxTransferService,
        "_rsync_min_size_bytes",
        classmethod(lambda cls: 1),
    )
    monkeypatch.setattr(
        StorageBoxTransferService,
        "_rsync_capability",
        classmethod(lambda cls: (True, "ready")),
    )
    monkeypatch.setattr(
        StorageBoxTransferService,
        "preflight",
        classmethod(fake_rsync_preflight),
    )
    monkeypatch.setattr(
        StorageBoxTransferService,
        "_remote_size",
        classmethod(fake_remote_size),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "is_available",
        classmethod(fail_if_lftp_checked),
    )

    upload_mode, _, upload_size = await StorageBoxTransferService._select_upload_mode(local_path)
    download_mode, _, download_size = await StorageBoxTransferService._select_download_mode(
        PurePosixPath("payload/library/Episode 01.mkv")
    )

    assert upload_mode == "rsync"
    assert upload_size == local_path.stat().st_size
    assert download_mode == "rsync"
    assert download_size == 7


@pytest.mark.asyncio
async def test_explicit_lftp_mode_still_selects_lftp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_path = tmp_path / "Episode 01.mkv"
    local_path.write_bytes(b"episode")

    async def fake_lftp_preflight(cls) -> dict[str, object]:
        return {"available": True, "reason": "ready", "cached": False}

    monkeypatch.setattr(
        StorageBoxTransferService,
        "_configured_mode",
        classmethod(lambda cls: "lftp"),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "is_available",
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "preflight",
        classmethod(fake_lftp_preflight),
    )

    mode, reason, size_bytes = await StorageBoxTransferService._select_upload_mode(local_path)

    assert mode == "lftp"
    assert reason == "lftp_ready"
    assert size_bytes == local_path.stat().st_size


@pytest.mark.asyncio
async def test_directory_transfers_prefer_lftp_mirror_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    upload_calls: list[tuple[Path, PurePosixPath, int | None, bool]] = []
    download_calls: list[tuple[PurePosixPath, Path, int | None, bool]] = []

    async def fake_lftp_preflight(cls) -> dict[str, object]:
        return {"available": True, "reason": "ready", "cached": False}

    async def fake_upload_directory(
        cls,
        local_dir: Path,
        remote_dir: PurePosixPath,
        *,
        parallel_files: int | None = None,
        delete_remote: bool = False,
    ) -> None:
        upload_calls.append((local_dir, remote_dir, parallel_files, delete_remote))

    async def fake_download_directory(
        cls,
        remote_dir: PurePosixPath,
        local_dir: Path,
        *,
        parallel_files: int | None = None,
        delete_local: bool = False,
    ) -> None:
        download_calls.append((remote_dir, local_dir, parallel_files, delete_local))

    async def fail_upload_fallback(cls, local_path: Path, remote_path: PurePosixPath) -> None:
        raise AssertionError("unexpected file upload fallback")

    async def fail_download_fallback(cls, remote_path: PurePosixPath):
        raise AssertionError("unexpected directory download fallback")

    monkeypatch.setattr(
        StorageBoxLftpService,
        "is_available",
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "preflight",
        classmethod(fake_lftp_preflight),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "upload_directory",
        classmethod(fake_upload_directory),
    )
    monkeypatch.setattr(
        StorageBoxLftpService,
        "download_directory",
        classmethod(fake_download_directory),
    )
    monkeypatch.setattr(
        StorageBoxTransferService,
        "upload_file",
        classmethod(fail_upload_fallback),
    )
    monkeypatch.setattr(
        StorageBoxSftpClient,
        "scandir",
        classmethod(fail_download_fallback),
    )

    upload_dir = tmp_path / "Series Name"
    upload_dir.mkdir()
    local_download_dir = tmp_path / "Download Target"

    await StorageBoxTransferService.upload_directory(
        upload_dir,
        PurePosixPath("payload/library/Series Name"),
        parallel_files=5,
        delete_remote=True,
    )
    await StorageBoxTransferService.download_directory(
        PurePosixPath("payload/library/Series Name"),
        local_download_dir,
        parallel_files=4,
        delete_local=True,
    )

    assert upload_calls == [
        (upload_dir, PurePosixPath("payload/library/Series Name"), 5, True)
    ]
    assert download_calls == [
        (PurePosixPath("payload/library/Series Name"), local_download_dir, 4, True)
    ]


@pytest.mark.asyncio
async def test_lftp_single_file_scripts_quote_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_scripts: list[str] = []

    async def fake_run_lftp_script(
        cls,
        script: str,
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        captured_scripts.append(script)
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        StorageBoxLftpService,
        "_run_lftp_script",
        classmethod(fake_run_lftp_script),
    )

    source_dir = tmp_path / "Season 1"
    source_dir.mkdir()
    local_source = source_dir / "Episode 01.mkv"
    local_source.write_bytes(b"episode")
    local_target = source_dir / "Episode 01 copy.mkv"

    await StorageBoxLftpService.upload_file(
        local_source,
        PurePosixPath("payload/library/Series Name/Episode 01.mkv"),
    )
    await StorageBoxLftpService.download_file(
        PurePosixPath("payload/library/Series Name/Episode 01.mkv"),
        local_target,
    )

    assert "mkdir -p 'payload/library/Series Name'" in captured_scripts[0]
    assert "put -c -O 'payload/library/Series Name'" in captured_scripts[0]
    assert quote(str(local_source)) in captured_scripts[0]
    assert "pget -c -n 4 -O " in captured_scripts[1]
    assert quote("payload/library/Series Name/Episode 01.mkv") in captured_scripts[1]
    assert f"!mv {quote(str(source_dir / 'Episode 01.mkv'))} {quote(str(local_target))}" in captured_scripts[1]


@pytest.mark.asyncio
async def test_lftp_mirror_scripts_quote_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_scripts: list[str] = []

    async def fake_run_lftp_script(
        cls,
        script: str,
        *,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        captured_scripts.append(script)
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(
        StorageBoxLftpService,
        "_run_lftp_script",
        classmethod(fake_run_lftp_script),
    )

    local_upload_dir = tmp_path / "Series Name"
    local_upload_dir.mkdir()
    local_download_dir = tmp_path / "Series Download"

    await StorageBoxLftpService.upload_directory(
        local_upload_dir,
        PurePosixPath("payload/library/Series Name"),
        parallel_files=3,
        delete_remote=True,
    )
    await StorageBoxLftpService.download_directory(
        PurePosixPath("payload/library/Series Name"),
        local_download_dir,
        parallel_files=2,
        delete_local=True,
    )

    assert (
        f"mirror --reverse --continue --parallel=3 --delete {quote(str(local_upload_dir))} "
        f"{quote('payload/library/Series Name')}"
    ) in captured_scripts[0]
    assert (
        f"mirror --continue --parallel=2 --delete {quote('payload/library/Series Name')} "
        f"{quote(str(local_download_dir))}"
    ) in captured_scripts[1]
