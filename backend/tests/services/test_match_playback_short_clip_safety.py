from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))


def _match_playback_symbols():
    module = pytest.importorskip("app.services.match_playback_service")
    return module.MatchPlaybackService, module._ClipPlan


def test_encode_clip_raises_when_ffmpeg_produces_no_temp_file(monkeypatch, tmp_path):
    MatchPlaybackService, _ClipPlan = _match_playback_symbols()
    output_path = tmp_path / "missing-temp.mp4"
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"input")

    plan = _ClipPlan(
        scene_index=3,
        track="tiktok",
        input_path=input_path,
        start_time=1.0,
        end_time=1.02,
        profile="tiktok_fast",
        clip_id="clip-missing-temp",
        source_key=None,
    )

    monkeypatch.setattr(
        MatchPlaybackService,
        "_clip_file",
        classmethod(lambda cls, project_id, clip_id: output_path),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_is_nvenc_available_sync",
        classmethod(lambda cls: False),
    )

    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(*args, **kwargs):
        return _FakeCompleted()

    monkeypatch.setattr("app.services.match_playback_service.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="did not produce output clip"):
        MatchPlaybackService._encode_clip_sync(project_id="p", plan=plan)


def test_encode_clip_clamps_duration_to_minimum_frame(monkeypatch, tmp_path):
    MatchPlaybackService, _ClipPlan = _match_playback_symbols()
    output_path = tmp_path / "out.mp4"
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"input")

    plan = _ClipPlan(
        scene_index=1,
        track="tiktok",
        input_path=input_path,
        start_time=0.0,
        end_time=0.001,
        profile="tiktok_fast",
        clip_id="clip-min-duration",
        source_key=None,
    )

    monkeypatch.setattr(
        MatchPlaybackService,
        "_clip_file",
        classmethod(lambda cls, project_id, clip_id: output_path),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_is_nvenc_available_sync",
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_validate_clip_sync",
        classmethod(lambda cls, path: 0.2),
    )
    monkeypatch.setattr(
        MatchPlaybackService,
        "_write_clip_meta_sync",
        classmethod(lambda cls, project_id, clip_id, duration, profile: None),
    )

    captured = {"duration": None}

    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, *args, **kwargs):
        duration_arg = float(cmd[cmd.index("-t") + 1])
        captured["duration"] = duration_arg
        Path(cmd[-1]).write_bytes(b"encoded")
        return _FakeCompleted()

    monkeypatch.setattr("app.services.match_playback_service.subprocess.run", fake_run)

    duration = MatchPlaybackService._encode_clip_sync(project_id="p", plan=plan)

    assert duration == 0.2
    assert captured["duration"] is not None
    assert captured["duration"] >= (1.0 / MatchPlaybackService._PROFILE_MAP["tiktok_fast"].fps)
