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


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "The page needs to be reloaded.", "Sign in to confirm your age."])
async def test_youtube_download_never_authenticates_and_caps_quality(monkeypatch, tmp_path, error):
    output = tmp_path / "source.mp4"
    commands = []
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "firefox")
    monkeypatch.setattr(DownloaderService, "EXTRACTION_RETRY_DELAYS_SECONDS", (0, 0))
    async def stream(cls, cmd, **kwargs):
        commands.append(cmd)
        if error:
            yield _DownloadCommandResult(returncode=1, stderr=f"ERROR: [youtube] video: {error}")
        else:
            output.write_bytes(b"video")
            yield _DownloadCommandResult(returncode=0)
    async def audio(_path):
        return True
    monkeypatch.setattr(DownloaderService, "_stream_download_command", classmethod(stream))
    monkeypatch.setattr(DownloaderService, "_has_audio_stream", audio)
    events = [event async for event in DownloaderService.download(
        "https://www.youtube.com/watch?v=video", "project",
        output_path=output, require_audio=False, format_selector="bv*+ba/b",
    )]
    assert len(commands) == 1
    command = commands[0]
    assert "--cookies-from-browser" not in command
    assert "--no-cookies" in command and "--no-cookies-from-browser" in command
    assert "--ignore-config" in command
    assert command[command.index("-f") + 1] == DownloaderService.SOURCE_FORMAT_SELECTOR
    assert events[-1].status == ("error" if error else "complete")
    assert settings.ytdlp_cookies_from_browser == "firefox"


@pytest.mark.parametrize("url", ["https://youtube.com/watch?v=v", "https://youtu.be/v", "https://m.youtube.com/watch?v=v"])
def test_youtube_command_uses_managed_javascript_runtime(monkeypatch, tmp_path, url):
    monkeypatch.setattr("app.services.downloader.get_ytdlp_js_runtime", lambda: "deno:/managed/bin/deno")
    cmd = DownloaderService._build_primary_download_command(url, tmp_path / "source.mp4")
    assert cmd[cmd.index("--js-runtimes") + 1] == "deno:/managed/bin/deno"
    tiktok = DownloaderService._build_primary_download_command("https://www.tiktok.com/@test/video/1", tmp_path / "tiktok.mp4")
    assert "--js-runtimes" not in tiktok


def test_javascript_runtime_resolves_managed_deno_before_node(monkeypatch):
    from app.utils import media_binaries
    binaries = {"deno": "/managed/bin/deno", "node": "/usr/bin/node"}
    monkeypatch.setattr(media_binaries, "_find_default_binary", binaries.get)
    assert media_binaries.get_ytdlp_js_runtime() == "deno:/managed/bin/deno"
    binaries.pop("deno")
    assert media_binaries.get_ytdlp_js_runtime() == "node:/usr/bin/node"


def test_youtube_audio_recovery_keeps_anonymous_policy_with_saved_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "firefox")
    command = DownloaderService._build_audio_recovery_command(
        "https://youtu.be/video", tmp_path / "audio.mp4", info_json_path=tmp_path / "video.info.json",
    )
    assert "--load-info-json" in command
    assert "--no-cookies-from-browser" in command and "--ignore-config" in command
    assert "--cookies-from-browser" not in command


@pytest.mark.parametrize("available,expected", [
    (["4k_av1", "1080_av1", "1080_h264", "720_h264"], "1080_h264"),
    (["4k_av1", "1080_av1", "720_h264"], "1080_av1"),
    (["4k_av1", "720_h264"], "720_h264"),
    (["4k_av1"], None),
])
def test_actual_ytdlp_selection_caps_every_fallback_and_prefers_native_codecs(available, expected):
    from yt_dlp import YoutubeDL
    video_specs = {
        "4k_av1": (2160, "av01.0.13M.08"),
        "1080_av1": (1080, "av01.0.08M.08"),
        "1080_h264": (1080, "avc1.64002A"),
        "720_h264": (720, "avc1.640020"),
    }
    formats = [dict(format_id=key, height=video_specs[key][0], width=video_specs[key][0]*16//9,
                    vcodec=video_specs[key][1], acodec="none", ext="mp4", url=f"https://example.test/{key}.mp4")
               for key in available]
    formats += [dict(format_id="aac", vcodec="none", acodec="mp4a.40.2", ext="m4a", url="https://example.test/audio.m4a"),
                dict(format_id="opus", vcodec="none", acodec="opus", ext="webm", url="https://example.test/audio.webm")]
    with YoutubeDL({"quiet": True, "format_sort": DownloaderService.SOURCE_FORMAT_SORT.split(",")}) as ydl:
        info = {"formats": formats}
        ydl.sort_formats(info)
        selected = ydl._select_formats(info["formats"], ydl.build_format_selector(DownloaderService.SOURCE_FORMAT_SELECTOR))
    if expected is None:
        assert selected == []  # Never fall back to an uncapped 4K format.
    else:
        assert selected[0]["format_id"] == f"{expected}+aac"


def test_non_youtube_hosts_keep_authentication(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ytdlp_cookies_from_browser", "firefox")
    for url in ("https://www.tiktok.com/@demo/video/1", "https://youtube.com.example.test/video"):
        command = DownloaderService._build_primary_download_command(url, tmp_path / "video.mp4")
        assert "--cookies-from-browser" in command
        assert "--no-cookies-from-browser" not in command
        assert command[command.index("-f") + 1] == DownloaderService.PRIMARY_FORMAT_SELECTOR
