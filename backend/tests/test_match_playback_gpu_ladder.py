from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.match_playback_service import MatchPlaybackService, _ClipPlan


@pytest.fixture(autouse=True)
def reset_capability_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class-level probe caches must not leak between tests."""
    monkeypatch.setattr(MatchPlaybackService, "_nvenc_checked", False)
    monkeypatch.setattr(MatchPlaybackService, "_nvenc_available", False)
    monkeypatch.setattr(MatchPlaybackService, "_full_gpu_checked", False)
    monkeypatch.setattr(MatchPlaybackService, "_full_gpu_available", False)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capability_fake_run(*, encoders: str, decoders: str, filters: str, calls: list):
    """Return a fake subprocess.run serving ffmpeg capability listings."""

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "-encoders" in cmd:
            return _FakeCompleted(stdout=encoders)
        if "-decoders" in cmd:
            return _FakeCompleted(stdout=decoders)
        if "-filters" in cmd:
            return _FakeCompleted(stdout=filters)
        raise AssertionError(f"unexpected command: {cmd}")

    return _fake_run


def test_full_gpu_probe_true_when_all_capabilities_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264_cuvid ... hevc_cuvid ...",
            filters="... scale_cuda ...",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is True
    first_call_count = len(calls)

    # Second call must be served from the cache: no new subprocess calls.
    assert MatchPlaybackService._is_full_gpu_available_sync() is True
    assert len(calls) == first_call_count


def test_full_gpu_probe_false_without_scale_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264_cuvid ... hevc_cuvid ...",
            filters="... scale ... (no cuda resizer)",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def test_full_gpu_probe_false_without_cuvid_decoders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _capability_fake_run(
            encoders="... h264_nvenc ...",
            decoders="... h264 ... hevc ...",
            filters="... scale_cuda ...",
            calls=calls,
        ),
    )
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def test_full_gpu_probe_false_when_nvenc_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: False)
    )

    def _explode(cmd, **kwargs):
        raise AssertionError("no capability listing should run when nvenc is absent")

    monkeypatch.setattr("app.services.match_playback_service.subprocess.run", _explode)
    assert MatchPlaybackService._is_full_gpu_available_sync() is False


def _make_plan(tmp_path: Path, track: str = "tiktok", profile: str = "tiktok_fast") -> _ClipPlan:
    src = tmp_path / "input.mp4"
    src.write_bytes(b"fake")
    return _ClipPlan(
        scene_index=0,
        track=track,  # type: ignore[arg-type]
        input_path=src,
        start_time=12.5,
        end_time=15.0,
        profile=profile,
        clip_id="clipid0000000000000000000000000000000000",
        source_key=None,
    )


def test_full_gpu_command_shape(tmp_path: Path) -> None:
    plan = _make_plan(tmp_path)
    profile = MatchPlaybackService._PROFILE_MAP["tiktok_fast"]
    out = tmp_path / "out.mp4"
    cmd = MatchPlaybackService._build_full_gpu_command_sync(
        plan=plan, profile=profile, duration=2.5, output_path=out
    )

    joined = " ".join(cmd)
    # Decode on GPU, frames stay on GPU.
    assert "-hwaccel cuda -hwaccel_output_format cuda" in joined
    # Input seek before -i (fast keyframe seek), exact window.
    assert cmd.index("-ss") < cmd.index("-i")
    assert "12.500000" in cmd and "2.500000" in cmd
    # fps drop BEFORE the CUDA scaler; nv12 handles 10-bit sources.
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == (
        "fps=24,scale_cuda=w=540:h=960:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:format=nv12"
    )
    # Fastest NVENC preset at the profile's constant QP.
    assert "h264_nvenc" in cmd and "p1" in cmd
    assert cmd[cmd.index("-qp") + 1] == "28"
    # No CPU pix_fmt flag: the encoder consumes CUDA frames directly.
    assert "-pix_fmt" not in cmd
    assert "+faststart" in cmd and str(out) in cmd


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


def _classify(cmd: list[str]) -> str:
    if "-hwaccel" in cmd:
        return "full_gpu"
    if "h264_nvenc" in cmd:
        return "nvenc_cpu"
    if "libx264" in cmd:
        return "libx264"
    return "other"


def _ladder_fake_run(outcomes: dict[str, int], attempts: list[str]):
    """Fake subprocess.run: rung name -> returncode; rc 0 writes the output."""

    def _fake_run(cmd, **kwargs):
        rung = _classify(list(cmd))
        attempts.append(rung)
        rc = outcomes[rung]
        if rc == 0:
            Path(cmd[-1]).write_bytes(b"encoded")
        return _FakeCompleted(returncode=rc, stderr=f"{rung} boom" if rc else "")

    return _fake_run


def _patch_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MatchPlaybackService,
        "_validate_clip_sync",
        classmethod(lambda cls, path: 2.5),
    )


def test_ladder_full_gpu_success_stops_ladder(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu"]
    assert (clip_store / f"{plan.clip_id}.mp4").exists()


def test_ladder_falls_through_gpu_then_nvenc_then_libx264(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 1, "nvenc_cpu": 1, "libx264": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu", "nvenc_cpu", "libx264"]


def test_ladder_skips_gpu_rung_when_probe_false(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: False)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"nvenc_cpu": 0}, attempts),
    )

    MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert attempts == ["nvenc_cpu"]


def test_ladder_gpu_output_failing_validation_falls_through(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 but undecodable GPU output must fall through, not poison the store."""
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )

    def _validate(cls, path: Path) -> float:
        # First validation call (the GPU rung's inline check) rejects the clip;
        # later calls (nvenc output + final gate) accept it.
        if attempts == ["full_gpu"]:
            raise RuntimeError("no decodable video stream")
        return 2.5

    monkeypatch.setattr(
        MatchPlaybackService, "_validate_clip_sync", classmethod(_validate)
    )
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 0, "nvenc_cpu": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu", "nvenc_cpu"]


def test_ladder_gpu_success_validates_exactly_once(
    clip_store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GPU rung's inline validation must satisfy the final gate too:
    no redundant re-probe of the same freshly-encoded file."""
    plan = _make_plan(tmp_path)
    attempts: list[str] = []
    validate_calls: list[Path] = []
    monkeypatch.setattr(
        MatchPlaybackService, "_is_full_gpu_available_sync", classmethod(lambda cls: True)
    )
    monkeypatch.setattr(
        MatchPlaybackService, "_is_nvenc_available_sync", classmethod(lambda cls: True)
    )

    def _counting_validate(cls, path: Path) -> float:
        validate_calls.append(path)
        return 2.5

    monkeypatch.setattr(
        MatchPlaybackService, "_validate_clip_sync", classmethod(_counting_validate)
    )
    monkeypatch.setattr(
        "app.services.match_playback_service.subprocess.run",
        _ladder_fake_run({"full_gpu": 0}, attempts),
    )

    duration = MatchPlaybackService._encode_clip_sync(project_id="proj", plan=plan)
    assert duration == 2.5
    assert attempts == ["full_gpu"]
    assert len(validate_calls) == 1
