"""Project/template speed floors must survive detection, candidates and caching."""
from __future__ import annotations

import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.models import SceneMatch
from app.services.gap_resolution import GapResolutionService, _NeighborContext


def _gaps(monkeypatch, floor):
    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.65)
    return GapResolutionService.calculate_gaps(
        [SceneMatch(scene_index=0, episode="episode", start_time=10, end_time=10.7,
                    confidence=1, speed_ratio=1)],
        [{"scene_index": 0, "start_time": 0, "end_time": 1}],
        min_speed_factor=floor,
    )


def test_detection_uses_higher_project_floor(monkeypatch):
    gaps = _gaps(monkeypatch, 0.8)
    assert len(gaps) == 1
    assert gaps[0].effective_speed == Fraction(4, 5)
    assert abs(gaps[0].gap_duration - 0.125) < 1e-9


def test_detection_allows_lower_project_floor(monkeypatch):
    assert _gaps(monkeypatch, 0.6) == []


def test_candidates_must_fill_at_project_floor(monkeypatch):
    gap = _gaps(monkeypatch, 0.8)[0]
    rejected = GapResolutionService._build_candidate(
        gap, 10, 10.75, "extend_end", "test", _NeighborContext(),
        is_cut_aligned=True, detector_threshold=27, direction_priority=0,
    )
    assert rejected is None
    candidates = GapResolutionService._generate_fallback_candidates(gap, _NeighborContext())
    assert candidates
    assert all(c.effective_speed >= Fraction(4, 5) for c in candidates)
    assert all(c.duration >= 0.8 - 1e-9 for c in candidates)


def test_candidate_cache_distinguishes_project_floors(monkeypatch):
    gap = _gaps(monkeypatch, 0.8)[0]
    lower_floor = replace(gap, effective_speed=Fraction(13, 20))
    assert GapResolutionService._build_gap_batch_key([gap], [], 6, "anime") != (
        GapResolutionService._build_gap_batch_key([lower_floor], [], 6, "anime")
    )
