from pathlib import Path
import sys

import pytest

sys.path.append(str(Path(__file__).resolve().parents[2]))


def _social_upload_symbols():
    module = pytest.importorskip("app.services.social_upload_service")
    return module.SocialUploadService, module.MediaProbe


def _upload_phase_symbols():
    module = pytest.importorskip("app.services.upload_phase")
    return module.UploadPhaseService


def test_check_youtube_duration_uses_safe_upload_target(monkeypatch):
    SocialUploadService, _ = _social_upload_symbols()
    UploadPhaseService = _upload_phase_symbols()
    captured: dict[str, float] = {}

    def fake_check_platform_duration(
        cls,
        project_id,
        account_id,
        *,
        platform_label,
        prep_dir,
        cleanup_stale,
        is_enabled,
        probe_media,
        transcode_to_limit,
        max_duration,
        max_speed,
    ):
        captured["max_duration"] = max_duration
        captured["max_speed"] = max_speed
        return {"needed": False}

    monkeypatch.setattr(
        UploadPhaseService,
        "_check_platform_duration",
        classmethod(fake_check_platform_duration),
    )

    result = UploadPhaseService.check_youtube_duration("project-1")

    assert result == {"needed": False}
    assert captured["max_duration"] == pytest.approx(
        SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS
    )
    assert captured["max_speed"] == pytest.approx(SocialUploadService._YOUTUBE_MAX_SPEED_FACTOR)


def test_prepare_youtube_video_transcodes_when_above_safe_target(monkeypatch, tmp_path):
    SocialUploadService, MediaProbe = _social_upload_symbols()
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source")
    expected_output_path = tmp_path / "source.youtube_180s.mp4"
    captured: dict[str, float | bool | Path] = {}

    def fake_probe_media(cls, *, video_path):
        if video_path == source_path:
            return MediaProbe(duration_seconds=179.95, has_audio=True), None
        if video_path == expected_output_path:
            return MediaProbe(duration_seconds=179.89, has_audio=True), None
        raise AssertionError(f"Unexpected probe path: {video_path}")

    def fake_transcode(
        cls,
        *,
        input_path,
        output_path,
        speed_factor,
        has_audio,
        max_duration_seconds,
    ):
        captured["input_path"] = input_path
        captured["output_path"] = output_path
        captured["speed_factor"] = speed_factor
        captured["has_audio"] = has_audio
        captured["max_duration_seconds"] = max_duration_seconds
        output_path.write_bytes(b"transcoded")
        return None

    monkeypatch.setattr(SocialUploadService, "_probe_media", classmethod(fake_probe_media))
    monkeypatch.setattr(
        SocialUploadService,
        "_transcode_video_to_duration_limit",
        classmethod(fake_transcode),
    )

    prep = SocialUploadService._prepare_youtube_video_for_upload(
        source_video_path=source_path,
        work_dir=tmp_path,
    )

    assert prep.status == "ready"
    assert prep.transcoded is True
    assert prep.video_path == expected_output_path
    assert captured["input_path"] == source_path
    assert captured["output_path"] == expected_output_path
    assert captured["has_audio"] is True
    assert captured["max_duration_seconds"] == pytest.approx(
        SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS
    )
    assert captured["speed_factor"] == pytest.approx(
        179.95 / SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS
    )


def test_cut_youtube_video_retranscodes_when_copy_cut_misses_safe_target(monkeypatch, tmp_path):
    SocialUploadService, MediaProbe = _social_upload_symbols()
    source_path = tmp_path / "source.mp4"
    output_path = tmp_path / "cut.mp4"
    source_path.write_bytes(b"source")
    output_path.write_bytes(b"cut")
    captured: dict[str, float | bool | Path] = {}

    def fake_cut_video_to_duration_limit(cls, *, input_path, output_path, max_duration_seconds):
        captured["cut_input_path"] = input_path
        captured["cut_output_path"] = output_path
        captured["cut_max_duration_seconds"] = max_duration_seconds
        return None

    def fake_probe_youtube_media(cls, *, video_path):
        if video_path == output_path:
            return MediaProbe(duration_seconds=179.95, has_audio=True), None
        if video_path == source_path:
            return MediaProbe(duration_seconds=181.0, has_audio=True), None
        raise AssertionError(f"Unexpected probe path: {video_path}")

    def fake_transcode(
        cls,
        *,
        input_path,
        output_path,
        speed_factor,
        has_audio,
        max_duration_seconds,
    ):
        captured["transcode_input_path"] = input_path
        captured["transcode_output_path"] = output_path
        captured["transcode_speed_factor"] = speed_factor
        captured["transcode_has_audio"] = has_audio
        captured["transcode_max_duration_seconds"] = max_duration_seconds
        return None

    monkeypatch.setattr(
        SocialUploadService,
        "_cut_video_to_duration_limit",
        classmethod(fake_cut_video_to_duration_limit),
    )
    monkeypatch.setattr(
        SocialUploadService,
        "_probe_youtube_media",
        classmethod(fake_probe_youtube_media),
    )
    monkeypatch.setattr(
        SocialUploadService,
        "_transcode_video_to_duration_limit",
        classmethod(fake_transcode),
    )

    error = SocialUploadService._cut_youtube_video(
        input_path=source_path,
        output_path=output_path,
    )

    assert error is None
    assert captured["cut_input_path"] == source_path
    assert captured["cut_output_path"] == output_path
    assert captured["cut_max_duration_seconds"] == pytest.approx(
        SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS
    )
    assert captured["transcode_input_path"] == source_path
    assert captured["transcode_output_path"] == output_path
    assert captured["transcode_speed_factor"] == pytest.approx(1.0)
    assert captured["transcode_has_audio"] is True
    assert captured["transcode_max_duration_seconds"] == pytest.approx(
        SocialUploadService._YOUTUBE_UPLOAD_TARGET_DURATION_SECONDS
    )
