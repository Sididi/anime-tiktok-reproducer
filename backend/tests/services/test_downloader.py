from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.services.downloader import DownloadProgress, DownloaderService, _DownloadCommandResult


async def _collect(stream) -> list[DownloadProgress]:
    return [event async for event in stream]


def _output_path_from_command(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("-o") + 1])


async def _has_audio_for_payload(path: Path) -> bool | None:
    if not path.exists():
        return False
    payload = path.read_bytes()
    if payload in {b"primary-audio", b"recovery-audio", b"muxed-audio"}:
        return True
    if payload in {b"primary-silent", b"recovery-silent"}:
        return False
    return None


@pytest.mark.asyncio
async def test_download_primary_audio_no_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    commands: list[tuple[list[str], str]] = []

    async def fake_stream_download_command(cls, cmd, *, progress_message_prefix):
        commands.append((list(cmd), progress_message_prefix))
        output_path = _output_path_from_command(cmd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"primary-audio")
        yield DownloadProgress("downloading", 0.5, f"{progress_message_prefix}: 50%")
        yield _DownloadCommandResult(returncode=0, stderr="")

    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream_download_command),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(_has_audio_for_payload),
    )

    events = await _collect(DownloaderService.download("https://example.com/video", "project-1"))

    output_path = tmp_path / "project-1" / "tiktok.mp4"
    assert [event.status for event in events] == ["starting", "downloading", "complete"]
    assert commands == [
        (DownloaderService._build_primary_download_command("https://example.com/video", output_path), "Downloading")
    ]
    assert output_path.read_bytes() == b"primary-audio"


@pytest.mark.asyncio
async def test_download_recovers_audio_and_muxes_high_quality_video(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    commands: list[tuple[list[str], str]] = []
    mux_calls: list[tuple[Path, Path, Path]] = []

    async def fake_stream_download_command(cls, cmd, *, progress_message_prefix):
        commands.append((list(cmd), progress_message_prefix))
        output_path = _output_path_from_command(cmd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.name == "tiktok.mp4":
            output_path.write_bytes(b"primary-silent")
        else:
            output_path.write_bytes(b"recovery-audio")
        yield _DownloadCommandResult(returncode=0, stderr="")

    async def fake_get_video_info(video_path: Path) -> dict:
        durations = {
            b"primary-silent": 123.12,
            b"recovery-audio": 123.18,
            b"muxed-audio": 123.12,
        }
        if not video_path.exists():
            return {}
        return {"duration": durations.get(video_path.read_bytes())}

    async def fake_mux_recovered_audio(cls, *, video_path: Path, audio_source_path: Path, output_path: Path) -> str | None:
        mux_calls.append((video_path, audio_source_path, output_path))
        output_path.write_bytes(b"muxed-audio")
        return None

    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream_download_command),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(_has_audio_for_payload),
    )
    monkeypatch.setattr(DownloaderService, "get_video_info", staticmethod(fake_get_video_info))
    monkeypatch.setattr(
        DownloaderService,
        "_mux_recovered_audio",
        classmethod(fake_mux_recovered_audio),
    )

    events = await _collect(DownloaderService.download("https://example.com/video", "project-2"))

    output_path = tmp_path / "project-2" / "tiktok.mp4"
    recovery_path = tmp_path / "project-2" / "tiktok.recovery.mp4"
    mux_path = tmp_path / "project-2" / "tiktok.muxed.mp4"
    assert output_path.read_bytes() == b"muxed-audio"
    assert not recovery_path.exists()
    assert not mux_path.exists()
    assert any(event.message == "Recovering audio track..." for event in events)
    assert any(event.message == "Merging recovered audio..." for event in events)
    assert events[-1].status == "complete"
    assert mux_calls == [(output_path, recovery_path, mux_path)]
    assert [message for _cmd, message in commands] == ["Downloading", "Recovering audio"]


@pytest.mark.asyncio
async def test_download_uses_audio_recovery_file_when_duration_delta_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "projects_dir", tmp_path)

    async def fake_stream_download_command(cls, cmd, *, progress_message_prefix):
        output_path = _output_path_from_command(cmd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.name == "tiktok.mp4":
            output_path.write_bytes(b"primary-silent")
        else:
            output_path.write_bytes(b"recovery-audio")
        yield _DownloadCommandResult(returncode=0, stderr="")

    async def fake_get_video_info(video_path: Path) -> dict:
        durations = {
            b"primary-silent": 123.12,
            b"recovery-audio": 123.60,
        }
        if not video_path.exists():
            return {}
        return {"duration": durations.get(video_path.read_bytes())}

    async def unexpected_mux(cls, *, video_path: Path, audio_source_path: Path, output_path: Path) -> str | None:
        raise AssertionError("Mux should not run when duration delta exceeds the tolerance")

    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream_download_command),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(_has_audio_for_payload),
    )
    monkeypatch.setattr(DownloaderService, "get_video_info", staticmethod(fake_get_video_info))
    monkeypatch.setattr(DownloaderService, "_mux_recovered_audio", classmethod(unexpected_mux))

    events = await _collect(DownloaderService.download("https://example.com/video", "project-3"))

    output_path = tmp_path / "project-3" / "tiktok.mp4"
    assert output_path.read_bytes() == b"recovery-audio"
    assert events[-1].status == "complete"


@pytest.mark.asyncio
async def test_download_errors_when_primary_and_recovery_files_lack_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "projects_dir", tmp_path)

    async def fake_stream_download_command(cls, cmd, *, progress_message_prefix):
        output_path = _output_path_from_command(cmd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.name == "tiktok.mp4":
            output_path.write_bytes(b"primary-silent")
        else:
            output_path.write_bytes(b"recovery-silent")
        yield _DownloadCommandResult(returncode=0, stderr="")

    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream_download_command),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(_has_audio_for_payload),
    )

    events = await _collect(DownloaderService.download("https://example.com/video", "project-4"))

    output_path = tmp_path / "project-4" / "tiktok.mp4"
    assert events[-1].status == "error"
    assert events[-1].error == DownloaderService.AUDIO_REQUIRED_ERROR_MESSAGE
    assert not output_path.exists()


@pytest.mark.asyncio
async def test_download_falls_back_to_audio_recovery_file_when_mux_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "projects_dir", tmp_path)

    async def fake_stream_download_command(cls, cmd, *, progress_message_prefix):
        output_path = _output_path_from_command(cmd)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.name == "tiktok.mp4":
            output_path.write_bytes(b"primary-silent")
        else:
            output_path.write_bytes(b"recovery-audio")
        yield _DownloadCommandResult(returncode=0, stderr="")

    async def fake_get_video_info(video_path: Path) -> dict:
        durations = {
            b"primary-silent": 123.12,
            b"recovery-audio": 123.18,
        }
        if not video_path.exists():
            return {}
        return {"duration": durations.get(video_path.read_bytes())}

    async def fake_mux_recovered_audio(cls, *, video_path: Path, audio_source_path: Path, output_path: Path) -> str | None:
        return "mux failed"

    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream_download_command),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(_has_audio_for_payload),
    )
    monkeypatch.setattr(DownloaderService, "get_video_info", staticmethod(fake_get_video_info))
    monkeypatch.setattr(
        DownloaderService,
        "_mux_recovered_audio",
        classmethod(fake_mux_recovered_audio),
    )

    events = await _collect(DownloaderService.download("https://example.com/video", "project-5"))

    output_path = tmp_path / "project-5" / "tiktok.mp4"
    assert output_path.read_bytes() == b"recovery-audio"
    assert events[-1].status == "complete"
