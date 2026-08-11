from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.downloader import (  # noqa: E402
    DownloadProgress,
    DownloaderService,
    _DownloadCommandResult,
)
from app.services.downloader import settings  # noqa: E402


@pytest.mark.asyncio
async def test_download_retries_transient_tiktok_rehydration_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project" / "tiktok.mp4"
    attempts = 0

    async def fake_stream(cls, cmd, *, progress_message_prefix, activity_path=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield _DownloadCommandResult(
                returncode=1,
                stderr=(
                    "ERROR: [TikTok] 123: Unable to extract universal data "
                    "for rehydration"
                ),
            )
            return

        output_path.write_bytes(b"video")
        yield _DownloadCommandResult(returncode=0)

    async def fake_has_audio(video_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        DownloaderService,
        "get_output_path",
        staticmethod(lambda project_id: output_path),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(fake_has_audio),
    )
    monkeypatch.setattr(
        DownloaderService,
        "EXTRACTION_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )

    events = [
        event
        async for event in DownloaderService.download(
            "https://www.tiktok.com/@demo/video/123",
            "project",
        )
    ]

    assert attempts == 2
    assert any(
        event.status == "downloading" and "retrying (1/2)" in event.message
        for event in events
    )
    assert events[-1].status == "complete"


@pytest.mark.asyncio
async def test_download_retries_unexpected_tiktok_response_without_impersonation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project" / "tiktok.mp4"
    commands: list[list[str]] = []

    async def fake_stream(cls, cmd, *, progress_message_prefix, activity_path=None):
        commands.append(cmd)
        if len(commands) == 1:
            yield _DownloadCommandResult(
                returncode=1,
                stderr="ERROR: [TikTok] 123: Unexpected response from webpage request",
            )
            return

        output_path.write_bytes(b"video")
        yield _DownloadCommandResult(returncode=0)

    async def fake_has_audio(video_path: Path) -> bool:
        return True

    monkeypatch.setattr(
        DownloaderService,
        "get_output_path",
        staticmethod(lambda project_id: output_path),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(fake_has_audio),
    )
    monkeypatch.setattr(
        DownloaderService,
        "EXTRACTION_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )

    events = [
        event
        async for event in DownloaderService.download(
            "https://www.tiktok.com/@demo/video/123",
            "project",
        )
    ]

    assert len(commands) == 2
    assert commands[0][0].endswith("yt-dlp")
    assert commands[1][:3] == [
        sys.executable,
        "-c",
        DownloaderService.YTDLP_NO_IMPERSONATION_WRAPPER,
    ]
    assert commands[1][3:] == commands[0][1:]
    assert any(
        event.status == "downloading" and "retrying (1/2)" in event.message
        for event in events
    )
    assert events[-1].status == "complete"


@pytest.mark.asyncio
async def test_download_retries_transient_audio_recovery_extraction_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project" / "tiktok.mp4"
    recovery_path = tmp_path / "project" / "tiktok.recovery.mp4"
    info_json_path = tmp_path / "project" / "tiktok.info.json"
    attempts = 0

    async def fake_stream(cls, cmd, *, progress_message_prefix, activity_path=None):
        nonlocal attempts
        attempts += 1
        command_output = Path(cmd[cmd.index("-o") + 1])
        if attempts == 1:
            assert "--write-info-json" in cmd
            info_json_path.write_text("{}", encoding="utf-8")
        else:
            info_index = cmd.index("--load-info-json")
            assert Path(cmd[info_index + 1]) == info_json_path
            assert not any(argument.startswith("https://www.tiktok.com/") for argument in cmd)
        if attempts == 2:
            yield _DownloadCommandResult(
                returncode=1,
                stderr=(
                    "ERROR: [TikTok] 123: Unable to extract universal data "
                    "for rehydration"
                ),
            )
            return

        command_output.write_bytes(b"video")
        yield _DownloadCommandResult(returncode=0)

    audio_results = iter((False, True, True))

    async def fake_has_audio(video_path: Path) -> bool:
        return next(audio_results)

    async def fake_can_mux(cls, primary_path: Path, recovered_path: Path) -> bool:
        return False

    monkeypatch.setattr(
        DownloaderService,
        "get_output_path",
        staticmethod(lambda project_id: output_path),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_has_audio_stream",
        staticmethod(fake_has_audio),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_can_mux_recovered_audio",
        classmethod(fake_can_mux),
    )
    monkeypatch.setattr(
        DownloaderService,
        "EXTRACTION_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )

    events = [
        event
        async for event in DownloaderService.download(
            "https://www.tiktok.com/@demo/video/123",
            "project",
        )
    ]

    assert attempts == 3
    assert not recovery_path.exists()
    assert not info_json_path.exists()
    assert any(
        event.status == "downloading"
        and "did not return audio data; retrying (1/2)" in event.message
        for event in events
    )
    assert events[-1].status == "complete"


@pytest.mark.asyncio
async def test_download_does_not_retry_non_transient_extraction_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "project" / "tiktok.mp4"
    attempts = 0

    async def fake_stream(cls, cmd, *, progress_message_prefix, activity_path=None):
        nonlocal attempts
        attempts += 1
        yield _DownloadCommandResult(
            returncode=1,
            stderr="ERROR: [TikTok] 123: This video is private",
        )

    monkeypatch.setattr(
        DownloaderService,
        "get_output_path",
        staticmethod(lambda project_id: output_path),
    )
    monkeypatch.setattr(
        DownloaderService,
        "_stream_download_command",
        classmethod(fake_stream),
    )

    events = [
        event
        async for event in DownloaderService.download(
            "https://www.tiktok.com/@demo/video/123",
            "project",
        )
    ]

    assert attempts == 1
    assert events[-1].status == "error"
    assert "This video is private" in (events[-1].error or "")


def test_download_command_uses_configured_binary_and_browser_cookies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "chrome")
    monkeypatch.setattr(
        "app.services.downloader.get_ytdlp_binary",
        lambda: "/opt/yt-dlp",
    )
    monkeypatch.setattr(
        "app.services.downloader.get_ytdlp_ffmpeg_location",
        lambda: None,
    )

    command = DownloaderService._build_primary_download_command(
        "https://www.tiktok.com/@demo/video/123",
        tmp_path / "tiktok.mp4",
    )

    assert command[0] == "/opt/yt-dlp"
    cookie_index = command.index("--cookies-from-browser")
    assert command[cookie_index + 1] == "chrome"
