"""Regression contract for the manual merge/rematch path (GOAL v4 M5).

`AnimeMatcherService.match_scenes(scene_indices_to_match=..., existing_matches=...)`
is the only remaining production consumer of the legacy matcher pipeline (the
merge-with-previous route re-matches exactly one merged scene). Before the M5
legacy-pass/crop-index deletion, this pins the contract that deletion must
preserve:

  1. non-target scenes keep their existing matches verbatim (episode,
     interval, merged_from);
  2. the target scene is actually re-matched from the faked search evidence;
  3. the output MatchList covers every scene, in order.
"""
import asyncio
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import MatchCandidate, MatchList, Scene, SceneList, SceneMatch
from app.services.anime_matcher import AnimeMatcherService


def _scene(i: int, a: float, b: float) -> Scene:
    return Scene(index=i, start_time=a, end_time=b)


def _match(i: int, episode: str, a: float, b: float, merged=None) -> SceneMatch:
    return SceneMatch(
        scene_index=i,
        episode=episode,
        start_time=a,
        end_time=b,
        confidence=0.9,
        speed_ratio=1.0,
        was_no_match=False,
        merged_from=merged,
    )


def test_partial_rematch_preserves_untargeted_and_rematches_target(
    monkeypatch, tmp_path
) -> None:
    scenes = SceneList(
        scenes=[_scene(0, 0.0, 2.0), _scene(1, 2.0, 5.0), _scene(2, 5.0, 7.0)]
    )
    existing = MatchList(
        matches=[
            _match(0, "EP01", 100.0, 102.0),
            _match(1, "EP01", 200.0, 203.0, merged=[1, 2]),
            _match(2, "EP02", 300.0, 302.0),
        ]
    )

    monkeypatch.setattr(
        AnimeMatcherService,
        "_init_searcher",
        classmethod(lambda cls, library_path, library_type, anime_name: True),
    )

    img = Image.new("RGB", (16, 16), "black")
    monkeypatch.setattr(
        AnimeMatcherService,
        "_extract_scene_probe_frames_with_indices",
        classmethod(
            lambda cls, video_path, items: (
                {i: (img, img, img) for i, _ in items},
                {i: (0, 1, 2) for i, _ in items},
            )
        ),
    )

    # deterministic evidence: the target scene's probes all point at
    # EP01@400 with a real-time slope across the scene span
    def fake_batch(cls, probe_frames, **kwargs):
        out = {}
        for i in probe_frames:
            sc = scenes.scenes[i]
            dur = sc.end_time - sc.start_time
            out[i] = (
                [MatchCandidate(episode="EP01", timestamp=400.0, similarity=0.92, series="S")],
                [MatchCandidate(episode="EP01", timestamp=400.0 + dur / 2, similarity=0.93, series="S")],
                [MatchCandidate(episode="EP01", timestamp=400.0 + dur, similarity=0.91, series="S")],
            )
        return out

    monkeypatch.setattr(
        AnimeMatcherService,
        "_search_scene_probe_candidates_batch",
        classmethod(fake_batch),
    )

    async def fake_cuts(cls, scenes_arg, matches_arg, library_type):
        return {}

    monkeypatch.setattr(
        AnimeMatcherService, "_load_dense_source_cuts", classmethod(fake_cuts)
    )
    # dense sampling / extra evidence paths must not touch the (absent) video
    monkeypatch.setattr(
        AnimeMatcherService,
        "extract_frames",
        classmethod(lambda cls, video_path, ts: [img for _ in ts]),
    )

    async def run() -> MatchList | None:
        final = None
        async for progress in AnimeMatcherService.match_scenes(
            tmp_path / "video.mp4",
            scenes,
            tmp_path / "library",
            "local",
            anime_name="S",
            scene_indices_to_match=[1],
            existing_matches=existing,
        ):
            if progress.status == "error":
                raise AssertionError(progress.error)
            if progress.status == "complete" and progress.matches:
                final = progress.matches
        return final

    final = asyncio.run(run())

    assert final is not None
    assert len(final.matches) == 3
    # 1. untargeted scenes preserved verbatim
    for k in (0, 2):
        assert final.matches[k].episode == existing.matches[k].episode
        assert final.matches[k].start_time == existing.matches[k].start_time
        assert final.matches[k].end_time == existing.matches[k].end_time
        assert final.matches[k].was_no_match is False
    # 2. the target scene was re-matched from the search evidence
    assert final.matches[1].was_no_match is False
    assert final.matches[1].episode == "EP01"
    assert abs(final.matches[1].start_time - 400.0) < 5.0
    # 3. order/coverage intact
    assert [m.scene_index for m in final.matches] == [0, 1, 2]
