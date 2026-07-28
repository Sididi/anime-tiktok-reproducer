# backend/tests/test_video_color.py
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.utils.video_color import ensure_bt709_tags

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _probe_color(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,color_space,color_transfer,color_primaries",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    return (json.loads(out).get("streams") or [{}])[0]


def _make_untagged_h264(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30:duration=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mp4", "-y", str(path),
        ],
        capture_output=True, check=True,
    )


def test_tags_untagged_h264_in_place(tmp_path: Path):
    video = tmp_path / "output.mp4.abc123.part"
    _make_untagged_h264(video)
    assert _probe_color(video).get("color_space") != "bt709"

    assert ensure_bt709_tags(video) is True

    stream = _probe_color(video)
    assert stream["color_space"] == "bt709"
    assert stream["color_transfer"] == "bt709"
    assert stream["color_primaries"] == "bt709"


def test_already_tagged_is_skipped(tmp_path: Path):
    video = tmp_path / "tagged.mp4"
    _make_untagged_h264(video)
    assert ensure_bt709_tags(video) is True
    assert ensure_bt709_tags(video) is False  # idempotent second pass


def test_non_video_file_is_left_alone(tmp_path: Path):
    audio = tmp_path / "output_no_music.wav"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
            "-y", str(audio),
        ],
        capture_output=True, check=True,
    )
    before = audio.read_bytes()
    assert ensure_bt709_tags(audio) is False
    assert audio.read_bytes() == before


def test_missing_file_returns_false(tmp_path: Path):
    assert ensure_bt709_tags(tmp_path / "nope.mp4") is False
