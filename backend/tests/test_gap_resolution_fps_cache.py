"""The fps cache in GapResolutionService is keyed by resolved episode path and
lives for the process lifetime: unlike its siblings (_scene_cut_cache LRU 256,
_candidate_batch_result_cache LRU 16) it must not grow unbounded."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.gap_resolution import GapResolutionService


@dataclass
class _FakeResult:
    returncode: int = 0
    stdout: bytes = b"24000/1001\n"


@pytest.fixture(autouse=True)
def _isolate_fps_cache():
    saved = dict(GapResolutionService._fps_cache)
    GapResolutionService._fps_cache.clear()
    yield
    GapResolutionService._fps_cache.clear()
    GapResolutionService._fps_cache.update(saved)


@pytest.mark.asyncio
async def test_fps_cache_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run_command(cmd, timeout_seconds=None):
        return _FakeResult()

    monkeypatch.setattr(
        "app.services.gap_resolution.run_command", fake_run_command
    )

    limit = GapResolutionService.FPS_CACHE_MAX_ENTRIES
    for i in range(limit + 10):
        fps = await GapResolutionService.detect_video_fps(tmp_path / f"ep-{i}.mkv")
        assert fps == Fraction(24000, 1001)

    assert len(GapResolutionService._fps_cache) <= limit
    # Newest entries survive, oldest were evicted.
    assert str((tmp_path / f"ep-{limit + 9}.mkv").resolve()) in GapResolutionService._fps_cache
    assert str((tmp_path / "ep-0.mkv").resolve()) not in GapResolutionService._fps_cache


@pytest.mark.asyncio
async def test_fps_cache_hit_skips_ffprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []

    async def fake_run_command(cmd, timeout_seconds=None):
        calls.append(cmd)
        return _FakeResult()

    monkeypatch.setattr(
        "app.services.gap_resolution.run_command", fake_run_command
    )

    path = tmp_path / "episode.mkv"
    first = await GapResolutionService.detect_video_fps(path)
    second = await GapResolutionService.detect_video_fps(path)

    assert first == second == Fraction(24000, 1001)
    assert len(calls) == 1
