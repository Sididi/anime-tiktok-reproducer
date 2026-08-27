"""Instagram prep passthrough (2026-08-27): when nothing has to change, the
VPS is pointed at the Drive final video and no output_instagram.mp4 is
remuxed/uploaded. The dedicated artifact stays mandatory for cut / sped-up /
copyright-swapped sources."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.google_drive_service import GoogleDriveService
from app.services.social_upload_service import LimitedDurationVideoPreparation, SocialUploadService
from app.services.upload_phase import UploadPhaseService


def _box(kind: bytes, payload: bytes = b"", *, large: bool = False) -> bytes:
    if large:
        return struct.pack(">I", 1) + kind + struct.pack(">Q", len(payload) + 16) + payload
    return struct.pack(">I", len(payload) + 8) + kind + payload


# ---------------------------------------------------------------- faststart


def test_is_faststart_true_when_moov_precedes_mdat(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(_box(b"ftyp", b"isom") + _box(b"moov", b"m" * 40) + _box(b"mdat", b"d" * 100))
    assert SocialUploadService._is_faststart_mp4(f) is True


def test_is_faststart_false_when_mdat_precedes_moov(tmp_path):
    f = tmp_path / "b.mp4"
    f.write_bytes(_box(b"ftyp", b"isom") + _box(b"mdat", b"d" * 100) + _box(b"moov", b"m" * 40))
    assert SocialUploadService._is_faststart_mp4(f) is False


def test_is_faststart_handles_free_and_largesize_boxes(tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(
        _box(b"ftyp", b"mp42") + _box(b"free", b"\0" * 8, large=True) + _box(b"moov", b"m" * 10) + _box(b"mdat", b"d")
    )
    assert SocialUploadService._is_faststart_mp4(f) is True


def test_is_faststart_false_on_garbage_or_missing(tmp_path):
    f = tmp_path / "d.mp4"
    f.write_bytes(b"not an mp4 at all")
    assert SocialUploadService._is_faststart_mp4(f) is False
    assert SocialUploadService._is_faststart_mp4(tmp_path / "missing.mp4") is False


# ------------------------------------------- prepare_instagram_video_for_drive


def _untouched(source: Path) -> LimitedDurationVideoPreparation:
    return LimitedDurationVideoPreparation(
        status="ready", video_path=source, transcoded=False, original_duration_seconds=60.0
    )


def _wire(monkeypatch, *, prep, faststart=True, validation=None):
    calls: dict[str, list] = {"remux": [], "validate": []}
    monkeypatch.setattr(
        SocialUploadService, "_prepare_facebook_video_for_upload", classmethod(lambda cls, **kw: prep)
    )
    monkeypatch.setattr(SocialUploadService, "_is_faststart_mp4", staticmethod(lambda p: faststart))

    def remux(cls, *, input_path, output_path):
        calls["remux"].append((input_path, output_path))
        output_path.write_bytes(b"remuxed")
        return None

    monkeypatch.setattr(SocialUploadService, "_remux_video_faststart", classmethod(remux))

    def validate(cls, *, video_path, max_duration_seconds):
        calls["validate"].append(video_path)
        return validation if video_path != video_path.parent / "output_instagram.mp4" else None

    monkeypatch.setattr(SocialUploadService, "_validate_facebook_reel_media", classmethod(validate))
    return calls


def test_prepare_passes_source_through_when_nothing_changes(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    calls = _wire(monkeypatch, prep=_untouched(source))
    prep = SocialUploadService.prepare_instagram_video_for_drive(
        source_video_path=source,
        output_path=tmp_path / "output_instagram.mp4",
        instagram_strategy="auto",
        max_duration_seconds=180,
        allow_source_passthrough=True,
    )
    assert prep.status == "ready" and prep.passthrough is True
    assert prep.video_path == source and prep.transcoded is False
    assert calls["remux"] == []  # no artifact produced
    assert calls["validate"] == [source]  # the source itself was validated
    assert not (tmp_path / "output_instagram.mp4").exists()


def test_prepare_still_remuxes_without_passthrough_permission(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    calls = _wire(monkeypatch, prep=_untouched(source))
    prep = SocialUploadService.prepare_instagram_video_for_drive(
        source_video_path=source, output_path=tmp_path / "output_instagram.mp4", max_duration_seconds=180
    )
    assert prep.passthrough is False and prep.video_path == tmp_path / "output_instagram.mp4"
    assert calls["remux"] == [(source, tmp_path / "output_instagram.mp4")]


def test_prepare_remuxes_when_source_is_not_faststart(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    calls = _wire(monkeypatch, prep=_untouched(source), faststart=False)
    prep = SocialUploadService.prepare_instagram_video_for_drive(
        source_video_path=source,
        output_path=tmp_path / "output_instagram.mp4",
        max_duration_seconds=180,
        allow_source_passthrough=True,
    )
    assert prep.passthrough is False and len(calls["remux"]) == 1


def test_prepare_falls_back_to_remux_when_source_fails_validation(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    calls = _wire(monkeypatch, prep=_untouched(source), validation="Facebook reel media validation failed: x")
    prep = SocialUploadService.prepare_instagram_video_for_drive(
        source_video_path=source,
        output_path=tmp_path / "output_instagram.mp4",
        max_duration_seconds=180,
        allow_source_passthrough=True,
    )
    assert prep.passthrough is False and prep.status == "ready"
    assert len(calls["remux"]) == 1 and calls["validate"][0] == source


def test_prepare_never_passes_through_a_transcoded_or_cut_result(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    sped = tmp_path / "sped.mp4"
    sped.write_bytes(b"sped")
    prep_in = LimitedDurationVideoPreparation(status="ready", video_path=sped, transcoded=True, speed_factor=1.2)
    calls = _wire(monkeypatch, prep=prep_in)
    prep = SocialUploadService.prepare_instagram_video_for_drive(
        source_video_path=source,
        output_path=tmp_path / "output_instagram.mp4",
        instagram_strategy="sped_up",
        max_duration_seconds=180,
        allow_source_passthrough=True,
    )
    assert prep.passthrough is False and prep.transcoded is True
    assert calls["remux"] == [(sped, tmp_path / "output_instagram.mp4")]


# ---------------------------------------------- _prepare_instagram_drive_video


def _drive_stubs(monkeypatch):
    calls: dict[str, object] = {"deleted": [], "upsert": None}
    monkeypatch.setattr(GoogleDriveService, "client", lambda: "drive")
    monkeypatch.setattr(
        GoogleDriveService,
        "list_children_named",
        lambda parent_id, filename, *, drive=None: [{"id": "stale_ig", "name": filename}],
    )
    monkeypatch.setattr(GoogleDriveService, "delete_file", lambda file_id, *, drive=None: calls["deleted"].append(file_id))

    def upsert(**kwargs):
        calls["upsert"] = kwargs
        return {"id": "ig_file", "webViewLink": "https://drive.google.com/file/d/ig_file"}

    monkeypatch.setattr(GoogleDriveService, "upsert_local_file", upsert)
    monkeypatch.setattr(GoogleDriveService, "set_public_read", lambda file_id, *, drive=None: None)
    monkeypatch.setattr(GoogleDriveService, "get_direct_download_url", lambda fid: f"https://dl/{fid}")
    return calls


ORIGINAL = {
    "file_id": "orig_id",
    "direct_url": "https://drive.usercontent.google.com/download?id=orig_id",
    "web_url": "https://drive.google.com/file/d/orig_id/view",
    "filename": "output.mp4",
}


def test_drive_prep_points_vps_at_original_and_uploads_nothing(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    seen = {}

    def prepare(**kwargs):
        seen.update(kwargs)
        return LimitedDurationVideoPreparation(status="ready", video_path=source, passthrough=True)

    monkeypatch.setattr(SocialUploadService, "prepare_instagram_video_for_drive", prepare)
    calls = _drive_stubs(monkeypatch)

    result, metadata = UploadPhaseService._prepare_instagram_drive_video(
        project_id="p1",
        source_video_path=source,
        drive_folder_id="folder_1",
        instagram_strategy="auto",
        max_duration_seconds=180,
        work_dir=tmp_path,
        source_drive_video=ORIGINAL,
    )
    assert result is None
    assert seen["allow_source_passthrough"] is True
    assert calls["upsert"] is None  # no output_instagram.mp4 upload
    assert calls["deleted"] == ["stale_ig"]  # leftover from an earlier run removed
    assert metadata == {
        "instagram_drive_file_id": "orig_id",
        "instagram_drive_video_url": ORIGINAL["direct_url"],
        "instagram_drive_web_url": ORIGINAL["web_url"],
        "instagram_drive_filename": "output.mp4",
        "instagram_drive_source": "original",
        "instagram_speed_factor": "1.0",
        "instagram_prepared_local_path": str(source),
    }


def test_drive_prep_uploads_artifact_when_prep_changed_the_video(monkeypatch, tmp_path):
    source = tmp_path / "output.mp4"
    source.write_bytes(b"src")
    prepared = tmp_path / "output_instagram.mp4"
    prepared.write_bytes(b"cut")
    monkeypatch.setattr(
        SocialUploadService,
        "prepare_instagram_video_for_drive",
        lambda **kw: LimitedDurationVideoPreparation(status="ready", video_path=prepared, transcoded=True),
    )
    calls = _drive_stubs(monkeypatch)
    result, metadata = UploadPhaseService._prepare_instagram_drive_video(
        project_id="p1",
        source_video_path=source,
        drive_folder_id="folder_1",
        instagram_strategy="cut",
        max_duration_seconds=180,
        work_dir=tmp_path,
        source_drive_video=ORIGINAL,
    )
    assert result is None
    assert calls["upsert"]["local_path"] == prepared
    assert calls["deleted"] == []
    assert metadata["instagram_drive_file_id"] == "ig_file"
    assert metadata["instagram_drive_source"] == "prepared"


def test_drive_prep_without_drive_identity_never_asks_for_passthrough(monkeypatch, tmp_path):
    """Copyright-swapped sources (local != Drive original) must keep the artifact."""
    source = tmp_path / "copyright_replaced.mp4"
    source.write_bytes(b"src")
    prepared = tmp_path / "output_instagram.mp4"
    seen = {}

    def prepare(**kwargs):
        seen.update(kwargs)
        prepared.write_bytes(b"remuxed")
        return LimitedDurationVideoPreparation(status="ready", video_path=prepared)

    monkeypatch.setattr(SocialUploadService, "prepare_instagram_video_for_drive", prepare)
    calls = _drive_stubs(monkeypatch)
    UploadPhaseService._prepare_instagram_drive_video(
        project_id="p1",
        source_video_path=source,
        drive_folder_id="folder_1",
        instagram_strategy="auto",
        max_duration_seconds=180,
        work_dir=tmp_path,
        source_drive_video=None,
    )
    assert seen["allow_source_passthrough"] is False
    assert calls["upsert"]["local_path"] == prepared
