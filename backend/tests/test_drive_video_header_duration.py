from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services.google_drive_service import GoogleDriveService


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + box_type + payload


def _mvhd_v0(timescale: int, duration: int) -> bytes:
    payload = (
        bytes([0])  # version
        + b"\x00\x00\x00"  # flags
        + struct.pack(">I", 0)  # creation time
        + struct.pack(">I", 0)  # modification time
        + struct.pack(">I", timescale)
        + struct.pack(">I", duration)
        + b"\x00" * 80  # rate/volume/matrix/predefined/next track id
    )
    return _box(b"mvhd", payload)


def _mvhd_v1(timescale: int, duration: int) -> bytes:
    payload = (
        bytes([1])
        + b"\x00\x00\x00"
        + struct.pack(">Q", 0)
        + struct.pack(">Q", 0)
        + struct.pack(">I", timescale)
        + struct.pack(">Q", duration)
        + b"\x00" * 80
    )
    return _box(b"mvhd", payload)


class _FakeDrive:
    """Serves byte ranges out of a sparse virtual file whose mdat is a hole:
    only the ranges the probe actually asks for are ever materialised."""

    def __init__(self, head: bytes, moov_offset: int | None, moov: bytes, size: int):
        self.head = head
        self.moov_offset = moov_offset
        self.moov = moov
        self.size = size
        self.requests: list[tuple[int, int]] = []

    def read(self, start: int, length: int) -> bytes:
        self.requests.append((start, length))
        out = bytearray()
        for pos in range(start, min(start + length, self.size)):
            if pos < len(self.head):
                out.append(self.head[pos])
            elif self.moov_offset is not None and self.moov_offset <= pos < self.moov_offset + len(self.moov):
                out.append(self.moov[pos - self.moov_offset])
            else:
                out.append(0)
        return bytes(out)

    @property
    def bytes_served(self) -> int:
        return sum(length for _, length in self.requests)


def _sparse_mp4(mvhd: bytes, mdat_bytes: int = 213_000_000) -> _FakeDrive:
    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 8)
    moov = _box(b"moov", mvhd + _box(b"udta", b"\x00" * 16))
    mdat_header = struct.pack(">I", mdat_bytes) + b"mdat"
    head = ftyp + mdat_header
    moov_offset = len(ftyp) + mdat_bytes
    return _FakeDrive(head, moov_offset, moov, moov_offset + len(moov))


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr(GoogleDriveService, "_video_duration_cache", {})


def _install(monkeypatch, fake: _FakeDrive) -> None:
    monkeypatch.setattr(
        GoogleDriveService,
        "_fetch_file_range",
        classmethod(lambda cls, file_id, start, length: fake.read(start, length)),
    )


def test_reads_duration_from_trailing_moov_without_downloading_media(monkeypatch):
    fake = _sparse_mp4(_mvhd_v0(timescale=60_000, duration=8_535_960))
    _install(monkeypatch, fake)

    duration = GoogleDriveService.probe_video_duration_from_header("f1")

    assert duration == pytest.approx(142.266, abs=1e-3)
    # The whole point: a 213 MB export costs a few KB.
    assert fake.bytes_served < 64 * 1024


def test_reads_duration_from_64bit_mvhd(monkeypatch):
    fake = _sparse_mp4(_mvhd_v1(timescale=90_000, duration=12_803_940))
    _install(monkeypatch, fake)

    assert GoogleDriveService.probe_video_duration_from_header("f1") == pytest.approx(
        142.266, abs=1e-3
    )


def test_probe_caches_so_sibling_platform_checks_reuse_it(monkeypatch):
    fake = _sparse_mp4(_mvhd_v0(timescale=1000, duration=142_266))
    _install(monkeypatch, fake)

    assert GoogleDriveService.probe_video_duration_from_header("f1") == pytest.approx(142.266)
    served = len(fake.requests)
    assert GoogleDriveService.get_video_duration_seconds("f1") == pytest.approx(142.266)
    assert len(fake.requests) == served  # served from cache, no second walk


def test_unknown_duration_sentinel_is_not_reported(monkeypatch):
    fake = _sparse_mp4(_mvhd_v0(timescale=60_000, duration=0xFFFFFFFF))
    _install(monkeypatch, fake)

    assert GoogleDriveService.probe_video_duration_from_header("f1") is None


def test_zero_timescale_is_not_reported(monkeypatch):
    fake = _sparse_mp4(_mvhd_v0(timescale=0, duration=1000))
    _install(monkeypatch, fake)

    assert GoogleDriveService.probe_video_duration_from_header("f1") is None


def test_no_moov_returns_none_without_endless_walking(monkeypatch):
    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 8)
    mdat_header = struct.pack(">I", 4096) + b"mdat"
    fake = _FakeDrive(ftyp + mdat_header, None, b"", len(ftyp) + 4096)
    _install(monkeypatch, fake)

    assert GoogleDriveService.probe_video_duration_from_header("f1") is None


def test_range_fetch_failure_is_swallowed(monkeypatch):
    def boom(cls, file_id, start, length):
        raise RuntimeError("drive unreachable")

    monkeypatch.setattr(
        GoogleDriveService, "_fetch_file_range", classmethod(boom)
    )
    assert GoogleDriveService.probe_video_duration_from_header("f1") is None
