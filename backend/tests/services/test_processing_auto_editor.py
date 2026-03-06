"""Tests for the auto-editor processing pipeline."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.auto_editor_profiles import PRODUCTION_AUTO_EDITOR_PROFILE
from app.services.processing import ProcessingService
from app.utils.subprocess_runner import CommandResult


def test_production_profile_command_args() -> None:
    """The production profile should match the documented baseline."""
    assert PRODUCTION_AUTO_EDITOR_PROFILE.command_args() == [
        "--edit",
        "audio:threshold=0.080,stream=all",
        "--margin",
        "0.04sec,0.24sec",
        "--silent-speed",
        "99999",
        "--no-open",
    ]


@pytest.mark.asyncio
async def test_run_auto_editor_invokes_single_audio_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """Processing should call auto-editor once and never request Premiere XML export."""
    calls: list[list[str]] = []

    async def fake_run_command(cmd: list[str], **_: object) -> CommandResult:
        calls.append(cmd)
        return CommandResult(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("app.services.processing.run_command", fake_run_command)

    result = await ProcessingService.run_auto_editor(
        Path("/tmp/input.wav"),
        Path("/tmp/output.wav"),
    )

    assert result is True
    assert len(calls) == 1
    assert calls[0] == [
        "pixi",
        "run",
        "--locked",
        "--",
        "auto-editor",
        "/tmp/input.wav",
        "--edit",
        "audio:threshold=0.080,stream=all",
        "--margin",
        "0.04sec,0.24sec",
        "--silent-speed",
        "99999",
        "--no-open",
        "-o",
        "/tmp/output.wav",
    ]
    assert "--export" not in calls[0]
    assert "premiere" not in calls[0]


def test_processing_source_has_no_legacy_xml_references() -> None:
    """The processing service should not keep the removed XML code path around."""
    processing_source = Path("app/services/processing.py").read_text(encoding="utf-8")

    assert "auto_editor_cuts.xml" not in processing_source
    assert "generate_fcp_xml" not in processing_source
    assert not hasattr(ProcessingService, "generate_fcp_xml")
