from __future__ import annotations

import json

import pytest

from app.api.routes.gaps import ComputeSpeedRequest, compute_speed, get_gaps, get_gaps_config
from app.config import settings
from app.models import MatchList, Project, SceneMatch
from app.services.project_service import ProjectService


def _make_match(*, start_time: float, end_time: float) -> MatchList:
    return MatchList(
        matches=[
            SceneMatch(
                scene_index=0,
                episode="episode-1",
                start_time=start_time,
                end_time=end_time,
                confidence=1.0,
                speed_ratio=1.0,
                confirmed=True,
            )
        ]
    )


@pytest.mark.asyncio
async def test_get_gaps_config_exposes_min_speed_factor(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "gaps_full_auto_enabled", True)
    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.8)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: Project(id=project_id))

    result = await get_gaps_config("proj123")

    assert result.full_auto_enabled is True
    assert result.min_speed_factor == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_get_gaps_uses_configured_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.8)
    monkeypatch.setattr(ProjectService, "load", lambda project_id: Project(id=project_id))
    monkeypatch.setattr(ProjectService, "get_project_dir", lambda project_id: tmp_path)
    monkeypatch.setattr(ProjectService, "load_matches", lambda project_id: _make_match(start_time=0.0, end_time=3.0))

    transcription_path = tmp_path / "gap_detection_transcription.json"
    transcription_path.write_text(
        json.dumps(
            {
                "scenes": [
                    {
                        "scene_index": 0,
                        "start_time": 0.0,
                        "end_time": 4.0,
                        "words": [],
                    }
                ]
            }
        )
    )

    result = await get_gaps("proj123")

    assert result.min_speed_factor == pytest.approx(0.8)
    assert result.has_gaps is True
    assert len(result.gaps) == 1
    assert result.gaps[0]["required_speed"] == pytest.approx(0.75)
    assert result.gaps[0]["effective_speed"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_compute_speed_uses_configured_floor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "min_playback_speed_factor", 0.8)

    result = await compute_speed(
        "proj123",
        ComputeSpeedRequest(start_time=0.0, end_time=3.0, target_duration=4.0),
    )

    assert result.raw_speed == pytest.approx(0.75)
    assert result.effective_speed == pytest.approx(0.8)
    assert result.has_gap is True
