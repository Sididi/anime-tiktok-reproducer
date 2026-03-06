from pathlib import Path
import os
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.utils import media_binaries


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_get_ffmpeg_binary_uses_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_ffmpeg = _make_executable(tmp_path / "custom" / "ffmpeg")

    monkeypatch.setattr(settings, "ffmpeg_binary", str(custom_ffmpeg))

    assert media_binaries.get_ffmpeg_binary() == str(custom_ffmpeg.resolve())


def test_get_ffmpeg_binary_rejects_invalid_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ffmpeg_binary", "missing-ffmpeg-override")
    monkeypatch.setattr(media_binaries, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(media_binaries.sys, "executable", str(tmp_path / "env" / "bin" / "python"))
    monkeypatch.setattr(media_binaries.shutil, "which", lambda name: None)

    with pytest.raises(FileNotFoundError, match="ATR_FFMPEG_BINARY"):
        media_binaries.get_ffmpeg_binary()


def test_get_ffmpeg_binary_prefers_current_python_env_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_binary = _make_executable(tmp_path / "env" / "bin" / "python")
    current_env_ffmpeg = _make_executable(tmp_path / "env" / "bin" / "ffmpeg")

    monkeypatch.setattr(settings, "ffmpeg_binary", None)
    monkeypatch.setattr(media_binaries, "PROJECT_ROOT", tmp_path / "repo")
    monkeypatch.setattr(media_binaries.sys, "executable", str(python_binary))
    monkeypatch.setattr(media_binaries.shutil, "which", lambda name: None)

    assert media_binaries.get_ffmpeg_binary() == str(current_env_ffmpeg)


def test_get_ffprobe_binary_falls_back_to_repo_pixi_default_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repo"
    repo_pixi_ffprobe = _make_executable(
        repo_root / ".pixi" / "envs" / "default" / "bin" / "ffprobe"
    )
    python_binary = _make_executable(tmp_path / "other-env" / "bin" / "python")

    monkeypatch.setattr(settings, "ffprobe_binary", None)
    monkeypatch.setattr(media_binaries, "PROJECT_ROOT", repo_root)
    monkeypatch.setattr(media_binaries.sys, "executable", str(python_binary))
    monkeypatch.setattr(media_binaries.shutil, "which", lambda name: None)

    assert media_binaries.get_ffprobe_binary() == str(repo_pixi_ffprobe)


def test_rewrite_media_command_only_rewrites_ffmpeg_and_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_binaries, "get_ffmpeg_binary", lambda: "/opt/bin/ffmpeg")
    monkeypatch.setattr(media_binaries, "get_ffprobe_binary", lambda: "/opt/bin/ffprobe")

    assert media_binaries.rewrite_media_command(["ffmpeg", "-version"]) == [
        "/opt/bin/ffmpeg",
        "-version",
    ]
    assert media_binaries.rewrite_media_command(["ffprobe", "-version"]) == [
        "/opt/bin/ffprobe",
        "-version",
    ]
    assert media_binaries.rewrite_media_command(["yt-dlp", "--help"]) == [
        "yt-dlp",
        "--help",
    ]


def test_get_media_subprocess_env_sanitizes_ld_library_path_for_system_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_binaries,
        "PROCESS_START_ENV",
        {
            "CONDA_PREFIX": "/repo/.pixi/envs/dev",
            "LD_LIBRARY_PATH": "/repo/.pixi/envs/dev/lib:/usr/lib",
        },
    )
    monkeypatch.setattr(
        media_binaries.sys,
        "executable",
        "/repo/.pixi/envs/dev/bin/python",
    )
    monkeypatch.setenv("CONDA_PREFIX", "/repo/.pixi/envs/dev")
    monkeypatch.setenv(
        "LD_LIBRARY_PATH",
        "/repo/.pixi/envs/dev/lib:/usr/lib:/custom/lib",
    )

    env = media_binaries.get_media_subprocess_env(["/usr/bin/ffmpeg", "-version"])

    assert env is not None
    assert env["LD_LIBRARY_PATH"] == os.pathsep.join(["/usr/lib", "/custom/lib"])


def test_get_media_subprocess_env_returns_none_for_managed_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        media_binaries,
        "PROCESS_START_ENV",
        {"CONDA_PREFIX": "/repo/.pixi/envs/dev"},
    )
    monkeypatch.setattr(
        media_binaries.sys,
        "executable",
        "/repo/.pixi/envs/dev/bin/python",
    )
    monkeypatch.setenv("CONDA_PREFIX", "/repo/.pixi/envs/dev")

    assert media_binaries.get_media_subprocess_env(
        ["/repo/.pixi/envs/dev/bin/ffmpeg", "-version"]
    ) is None
