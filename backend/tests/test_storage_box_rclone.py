from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.storage_box_progress import ProgressSnapshot
from app.services.storage_box_rclone import (
    StorageBoxRclone,
    StorageBoxRcloneError,
)


_FAKE_RCLONE = """#!/usr/bin/env python3
import json, os, sys

if len(sys.argv) > 1 and sys.argv[1] == "obscure":
    sys.stdout.write("OBSCURED:" + sys.stdin.read().strip())
    sys.exit(0)

capture = os.environ.get("FAKE_RCLONE_CAPTURE")
if capture:
    src = sys.argv[2]
    tree = []
    for root, dirs, files in os.walk(src):
        for name in files:
            path = os.path.join(root, name)
            tree.append(
                {
                    "rel": os.path.relpath(path, src),
                    "target": os.path.realpath(path),
                    "is_symlink": os.path.islink(path),
                }
            )
    with open(capture, "w") as fh:
        json.dump(
            {
                "argv": sys.argv[1:],
                "env_pass": os.environ.get("RCLONE_SFTP_PASS"),
                "tree": sorted(tree, key=lambda item: item["rel"]),
            },
            fh,
        )

for line in os.environ.get("FAKE_RCLONE_STDERR", "").split("\\x1e"):
    if line:
        sys.stderr.write(line + "\\n")
sys.exit(int(os.environ.get("FAKE_RCLONE_EXIT", "0")))
"""


@pytest.fixture(autouse=True)
def _storage_box_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(settings, "storage_box_enabled", True)
    monkeypatch.setattr(settings, "storage_box_host", "storage.example")
    monkeypatch.setattr(settings, "storage_box_port", 23)
    monkeypatch.setattr(settings, "storage_box_username", "storage-user")
    monkeypatch.setattr(settings, "storage_box_ssh_key_path", Path("/tmp/storage key"))
    monkeypatch.setattr(settings, "storage_box_password", None)
    monkeypatch.setattr(settings, "storage_box_known_hosts_path", None)
    monkeypatch.setattr(settings, "storage_box_root", "/home/box/root")
    monkeypatch.setattr(settings, "storage_box_rclone_transfers", 4)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(settings, "cache_dir", cache_dir)
    yield


@pytest.fixture
def fake_rclone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    binary = tmp_path / "fake-rclone"
    binary.write_text(_FAKE_RCLONE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        StorageBoxRclone, "binary", classmethod(lambda cls: str(binary))
    )
    return binary


def _stats_line(
    *,
    bytes_done: int,
    total_bytes: int,
    speed: float,
    eta: float | None = None,
    transferring: int = 0,
) -> str:
    return json.dumps(
        {
            "level": "notice",
            "msg": "stats",
            "stats": {
                "bytes": bytes_done,
                "totalBytes": total_bytes,
                "speed": speed,
                "eta": eta,
                "transferring": [{"name": f"f{i}"} for i in range(transferring)],
            },
        }
    )


def _set_stderr_lines(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    # Record separator keeps JSON payloads (which may contain \n escapes)
    # unambiguous in a single env var.
    monkeypatch.setenv("FAKE_RCLONE_STDERR", "\x1e".join(lines))


def _make_items(tmp_path: Path) -> list[tuple[Path, PurePosixPath]]:
    src_dir = tmp_path / "src"
    (src_dir / "nested").mkdir(parents=True)
    file_a = src_dir / "episode.mkv"
    file_a.write_bytes(b"video-bytes")
    file_b = src_dir / "nested" / "manifest.json"
    file_b.write_text("{}", encoding="utf-8")
    return [
        (file_a, PurePosixPath("library/Series Name/episode.mkv")),
        (file_b, PurePosixPath("index/manifest.json")),
    ]


@pytest.mark.asyncio
async def test_snapshot_mapping_and_total_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    _set_stderr_lines(
        monkeypatch,
        [
            _stats_line(bytes_done=100, total_bytes=500, speed=1048576.0, eta=10, transferring=2),
            _stats_line(bytes_done=5000, total_bytes=500, speed=2097152.0, eta=None, transferring=1),
        ],
    )

    snapshots: list[ProgressSnapshot] = []

    await StorageBoxRclone.upload_batch(
        _make_items(tmp_path),
        remote_base="staging/pub-1",
        total_bytes=2000,
        progress_callback=snapshots.append,
    )

    assert len(snapshots) == 3
    first, second, final = snapshots
    assert first.bytes_transferred == 100
    assert first.bytes_total == 2000  # exact precomputed total wins over rclone's
    assert first.mib_per_sec == 1.0
    assert first.eta_seconds == 10.0
    assert first.active_transfers == 2
    assert second.bytes_transferred == 2000  # clamped to total
    assert second.eta_seconds is None
    assert final.bytes_transferred == 2000
    assert final.bytes_total == 2000
    assert final.eta_seconds == 0.0
    assert final.active_transfers == 0


@pytest.mark.asyncio
async def test_error_exit_raises_with_rclone_error_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    _set_stderr_lines(
        monkeypatch,
        [
            json.dumps({"level": "error", "msg": "sftp: connection lost"}),
            json.dumps({"level": "error", "msg": "Failed to copy: episode.mkv"}),
        ],
    )
    monkeypatch.setenv("FAKE_RCLONE_EXIT", "3")

    with pytest.raises(StorageBoxRcloneError) as excinfo:
        await StorageBoxRclone.upload_batch(
            _make_items(tmp_path), remote_base="staging/pub-1"
        )

    message = str(excinfo.value)
    assert "code 3" in message
    assert "sftp: connection lost" in message
    assert "Failed to copy: episode.mkv" in message


@pytest.mark.asyncio
async def test_command_flags_symlink_tree_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    capture_path = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_RCLONE_CAPTURE", str(capture_path))
    items = _make_items(tmp_path)

    await StorageBoxRclone.upload_batch(items, remote_base="staging/pub-1")

    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    argv = captured["argv"]
    assert argv[0] == "copy"
    assert argv[2] == ":sftp:/home/box/root/staging/pub-1"

    def flag(name: str) -> str:
        return argv[argv.index(name) + 1]

    assert flag("--sftp-host") == "storage.example"
    assert flag("--sftp-user") == "storage-user"
    assert flag("--sftp-port") == "23"
    assert flag("--transfers") == "4"
    assert flag("--sftp-key-file") == "/tmp/storage key"
    assert "--copy-links" in argv
    assert "--use-json-log" in argv
    assert captured["env_pass"] is None

    assert captured["tree"] == [
        {
            "rel": "index/manifest.json",
            "target": str(items[1][0].resolve()),
            "is_symlink": True,
        },
        {
            "rel": "library/Series Name/episode.mkv",
            "target": str(items[0][0].resolve()),
            "is_symlink": True,
        },
    ]

    leftovers = list(settings.cache_dir.glob("atr-rclone-*"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_password_auth_uses_obscured_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    monkeypatch.setattr(settings, "storage_box_ssh_key_path", None)
    monkeypatch.setattr(settings, "storage_box_password", "secret-pass")
    capture_path = tmp_path / "capture.json"
    monkeypatch.setenv("FAKE_RCLONE_CAPTURE", str(capture_path))

    await StorageBoxRclone.upload_batch(
        _make_items(tmp_path), remote_base="staging/pub-1"
    )

    captured = json.loads(capture_path.read_text(encoding="utf-8"))
    assert captured["env_pass"] == "OBSCURED:secret-pass"
    assert "--sftp-key-file" not in captured["argv"]


@pytest.mark.asyncio
async def test_empty_batch_spawns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(StorageBoxRclone, "binary", classmethod(lambda cls: None))
    # Must not raise even though rclone is unavailable: nothing to do.
    await StorageBoxRclone.upload_batch([], remote_base="staging/pub-1")


@pytest.mark.asyncio
async def test_missing_binary_raises_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(StorageBoxRclone, "binary", classmethod(lambda cls: None))
    with pytest.raises(StorageBoxRcloneError, match="pacman -S rclone"):
        await StorageBoxRclone.upload_batch(
            _make_items(tmp_path), remote_base="staging/pub-1"
        )


@pytest.mark.asyncio
async def test_garbage_stderr_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    _set_stderr_lines(
        monkeypatch,
        [
            "not json at all",
            json.dumps(["a", "list"]),
            json.dumps({"level": "notice", "msg": "no stats here"}),
            _stats_line(bytes_done=10, total_bytes=100, speed=0.0),
        ],
    )

    snapshots: list[ProgressSnapshot] = []
    await StorageBoxRclone.upload_batch(
        _make_items(tmp_path),
        remote_base="staging/pub-1",
        progress_callback=snapshots.append,
    )

    # One real stats line + the terminal snapshot; without an explicit
    # total_bytes the reported total is used.
    assert len(snapshots) == 2
    assert snapshots[0].bytes_transferred == 10
    assert snapshots[0].bytes_total == 100
    assert snapshots[0].mib_per_sec == 0.0


@pytest.mark.asyncio
async def test_absolute_remote_relative_path_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_rclone: Path
) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"x")
    with pytest.raises(StorageBoxRcloneError, match="relative"):
        await StorageBoxRclone.upload_batch(
            [(source, PurePosixPath("/absolute/path.bin"))],
            remote_base="staging/pub-1",
        )
