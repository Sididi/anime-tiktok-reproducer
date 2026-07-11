from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "meta_reel_duration_probe.py"


def _module():
    spec = importlib.util.spec_from_file_location("meta_reel_duration_probe", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_has_exact_requested_duration(tmp_path: Path) -> None:
    module = _module()
    fixture = tmp_path / "probe.mp4"
    module.generate_fixture(fixture, 3)
    measured = float(
        subprocess.check_output(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(fixture),
            ],
            text=True,
        ).strip()
    )
    assert measured == 3.0


def test_cli_requires_explicit_external_write_confirmation() -> None:
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), "--account-id", "unused",
            "--platform", "instagram", "--candidate", "180",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--confirm-nonpublic-uploads is required" in result.stderr


def test_facebook_status_accepts_both_processing_and_video_status_shapes() -> None:
    module = _module()
    assert module.facebook_processing_result(
        {"status": {"processing_phase": {"status": "complete"}}}
    )[:2] == (True, True)
    assert module.facebook_processing_result(
        {"status": {"video_status": "ready"}}
    )[:2] == (True, True)
    assert module.facebook_processing_result(
        {"status": {"processing_phase": {"status": "failed"}}}
    )[:2] == (True, False)
