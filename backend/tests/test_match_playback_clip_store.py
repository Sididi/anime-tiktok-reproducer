from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.match_playback_service import MatchPlaybackService, _ClipPlan


@pytest.fixture
def clip_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "clip_store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        MatchPlaybackService,
        "_clip_store_dir",
        classmethod(lambda cls, project_id: store),
    )
    return store


def _write(path: Path, data: bytes = b"data") -> None:
    path.write_bytes(data)


def test_clip_is_reusable_requires_meta_sidecar(clip_store: Path) -> None:
    clip_id = "abc123"
    clip = clip_store / f"{clip_id}.mp4"

    # No clip at all -> not reusable
    assert MatchPlaybackService._clip_is_reusable_sync("proj", clip_id) is False

    # Clip present but no validated meta sidecar -> NOT reusable (this is the
    # exact state a pre-validation-crash left behind and that used to poison
    # manifest building).
    _write(clip)
    assert MatchPlaybackService._clip_is_reusable_sync("proj", clip_id) is False

    # Empty clip with a meta sidecar -> not reusable
    clip.write_bytes(b"")
    MatchPlaybackService._write_clip_meta_sync(
        "proj", clip_id=clip_id, duration=1.0, profile="tiktok"
    )
    assert MatchPlaybackService._clip_is_reusable_sync("proj", clip_id) is False

    # Non-empty clip + validated meta sidecar -> reusable
    _write(clip)
    assert MatchPlaybackService._clip_is_reusable_sync("proj", clip_id) is True


def test_encode_validates_before_persisting(
    clip_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clip whose encode yields no decodable video stream must never be left
    in the store, and no meta sidecar may be written for it."""
    plan = _ClipPlan(
        scene_index=0,
        track="tiktok",
        input_path=Path("/does/not/matter.mp4"),
        start_time=0.0,
        end_time=1.0,
        profile="tiktok_fast",
        clip_id="deadbeef",
        source_key=None,
    )

    # Force the CPU transcode path and make ffmpeg "succeed" by writing the tmp
    # output file, but make validation reject it (simulating an empty/streamless
    # container that ffmpeg produced with a zero exit code).
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: False)
    )

    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, *args, **kwargs):
        Path(cmd[-1]).write_bytes(b"not-a-real-video")
        return _FakeCompleted()

    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run", _fake_run
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_validate_clip_sync",
        classmethod(
            lambda cls, path: (_ for _ in ()).throw(
                RuntimeError(f"No video stream in clip: {path.name}")
            )
        ),
    )

    with pytest.raises(RuntimeError, match="No video stream"):
        MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)

    # Nothing left behind: no final clip, no temp file, no meta sidecar.
    assert not (clip_store / "deadbeef.mp4").exists()
    assert not (clip_store / "deadbeef.tmp.mp4").exists()
    assert not (clip_store / "deadbeef.json").exists()

    # And such a (non-existent) clip is correctly not reusable.
    assert MatchPlaybackService._clip_is_reusable_sync("proj", "deadbeef") is False


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
def test_subframe_window_still_produces_a_decodable_clip(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-frame match window (shorter than a single frame) must still encode
    to a clip that has a real video stream.

    Regression for: the matcher can emit windows shorter than one frame; when the
    source frame rate (here 23.976fps, the common anime rate) does not divide the
    output rate, the ``fps`` filter fails to flush a frame if the encode window is
    only one output-frame period long, so ffmpeg writes a valid container with
    *zero* video streams (exit code 0). That surfaced as "No video stream in clip".

    The conditions below (source 24000/1001 fps, output 20fps via ``source_fast``,
    start=0.589s) produce zero frames at the old ``1/fps`` floor; the corrected
    ``min_duration`` widens the window enough for the filter to emit a frame.
    """
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24000/1001:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True,
    )

    # Force the transcode (fps-filter) path deterministically: skip the
    # stream-copy fast path and the GPU encoder regardless of the host.
    monkeypatch.setattr(
        MatchPlaybackService,
        "_is_source_web_compatible_sync",
        classmethod(lambda cls, path: False),
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: False)
    )

    start = 0.589
    plan = _ClipPlan(
        scene_index=6,
        track="source",
        input_path=source,
        start_time=start,
        end_time=start + 0.02,  # sub-frame: ~half a frame at 23.976fps
        profile="source_fast",  # 20fps output — does not divide 23.976
        clip_id="subframe",
        source_key="ep",
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration > 0

    clip = clip_store / "subframe.mp4"
    assert clip.exists() and clip.stat().st_size > 0

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(clip),
        ],
        capture_output=True, text=True, check=True,
    )
    frames = int((probe.stdout.strip() or "0"))
    assert frames >= 1, "sub-frame window produced a clip with no video frames"
