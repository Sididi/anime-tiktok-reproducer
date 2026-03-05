from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.downloader import DownloaderService
import app.services.downloader as downloader_module


def test_build_download_command_includes_ffmpeg_location(monkeypatch) -> None:
    monkeypatch.setattr(
        downloader_module,
        "get_ytdlp_ffmpeg_location",
        lambda: "/usr/bin/ffmpeg",
    )

    output_path = Path("/tmp/tiktok.mp4")
    cmd = DownloaderService._build_download_command("https://example.com/video", output_path)

    assert "--ffmpeg-location" in cmd
    assert cmd[cmd.index("--ffmpeg-location") + 1] == "/usr/bin/ffmpeg"
    assert cmd[-1] == "https://example.com/video"
