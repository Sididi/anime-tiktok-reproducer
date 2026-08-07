"""Characterization of the /exports/gdrive SSE frame contract.

The frontend (driveUploadProgress.ts + ProcessingPage.tsx) re-derives all
displayed text from `phase` plus the numeric fields below. The rclone
adapter must keep emitting exactly this shape.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.routes.processing import _gdrive_progress_to_sse_payload
from app.services.export_service import _RcloneDriveProgressAdapter
from app.services.rclone_runner import RcloneStats

EXPECTED_KEYS = {
    "status",
    "step",
    "progress",
    "message",
    "phase",
    "file_count",
    "files_completed",
    "total_bytes",
    "uploaded_bytes",
    "current_file",
    "clear_item_count",
    "clear_items_completed",
    "elapsed_ms",
    "throughput_mb_per_sec",
}


def _stats(
    *, bytes_done: int, total: int, transfers: int = 1, total_transfers: int = 3
) -> RcloneStats:
    return RcloneStats(
        bytes_transferred=bytes_done,
        bytes_total=total,
        speed_bytes_per_sec=2 * 1024 * 1024,
        eta_seconds=12.0,
        transferring_names=["sources/Episode 01.mkv"],
        transfers=transfers,
        total_transfers=total_transfers,
        checks=2,
        total_checks=4,
    )


def _collect_frames() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    adapter = _RcloneDriveProgressAdapter(
        callback=payloads.append, file_count=12, total_bytes=10_000
    )
    adapter.emit_manifest()
    adapter.on_stats(_stats(bytes_done=1_000, total=4_000))
    adapter.on_stats(_stats(bytes_done=4_000, total=4_000, transfers=3))
    adapter.emit_persist()
    return [_gdrive_progress_to_sse_payload(p) for p in payloads]


def test_frames_carry_exact_key_set() -> None:
    for frame in _collect_frames():
        assert set(frame.keys()) == EXPECTED_KEYS
        assert frame["status"] == "processing"
        assert frame["step"] == "gdrive"


def test_first_frame_is_manifest_phase_with_manifest_totals() -> None:
    frames = _collect_frames()
    first = frames[0]
    assert first["phase"] == "manifest"
    assert first["file_count"] == 12
    assert first["total_bytes"] == 10_000
    assert first["progress"] == 0.12


def test_upload_fraction_is_monotone_and_capped() -> None:
    frames = _collect_frames()
    upload_frames = [f for f in frames if f["phase"] == "upload"]
    assert len(upload_frames) == 2
    first, second = upload_frames
    # 0.3 + (uploaded/total) * 0.65
    assert first["progress"] == pytest.approx(0.3 + 0.25 * 0.65)
    assert second["progress"] == pytest.approx(0.95)  # 100% of bytes, capped ≤0.96
    assert second["progress"] >= first["progress"]
    assert first["current_file"] == "sources/Episode 01.mkv"
    assert first["uploaded_bytes"] == 1_000
    assert first["total_bytes"] == 4_000
    assert first["files_completed"] == 1
    assert first["file_count"] == 3


def test_no_clear_frames_and_null_clear_counts_tolerated() -> None:
    frames = _collect_frames()
    assert all(frame["phase"] != "clear" for frame in frames)
    for frame in frames:
        assert frame["clear_item_count"] is None
        assert frame["clear_items_completed"] is None


def test_persist_frame_maps_to_098() -> None:
    frames = _collect_frames()
    last = frames[-1]
    assert last["phase"] == "persist"
    assert last["progress"] == 0.98


def test_checks_only_sync_keeps_frames_flowing() -> None:
    payloads: list[dict[str, Any]] = []
    adapter = _RcloneDriveProgressAdapter(
        callback=payloads.append, file_count=5, total_bytes=1_000
    )
    adapter.on_stats(
        RcloneStats(
            bytes_transferred=0,
            bytes_total=0,
            speed_bytes_per_sec=0.0,
            eta_seconds=None,
            transferring_names=[],
            transfers=0,
            total_transfers=0,
            checks=3,
            total_checks=5,
        )
    )
    frame = _gdrive_progress_to_sse_payload(payloads[0])
    assert frame["phase"] == "upload"
    assert frame["progress"] == 0.35  # documented floor when total_bytes == 0
    assert "Comparing files" in frame["message"]
